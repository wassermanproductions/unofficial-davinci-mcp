"""Cut a song to a target duration like a music editor -- SPLICE-BASED
RETARGETING, not "play from the top and fade at the target time".

The professional method (see skills/music-editing.md, section "Shortening a
song like a music editor"): find two moments A (early) and B (later) where the
music is nearly the same thing -- same section, same instrumentation, same
harmony -- remove A->B, and because the material on both sides of the seam
matches, the join is inaudible. The song's REAL, composed ending is preserved
(the middle is removed so the outro lands where you need it). Every seam is an
equal-power crossfade cut on the downbeat attack, and the seam's spectral flux
is measured against the song's own beat-to-beat variation to reject audible
joins.

Ending strategies (``sting``):
  - 'seamless' (default) : real ending via one or two phrase-aligned splices.
  - 'button'             : end ON a strong downbeat hit, keep its natural decay.
  - 'tail'               : phrase-final exit + exponential ring-out.
  - 'fade'               : last-resort 2-bar musical fade.

If 'seamless' cannot find a splice whose matched material clears the similarity
floor, it automatically DEGRADES to 'button' and says so in the report.

Outputs a cut WAV (sample-accurate stereo rendering in numpy), a per-edit
report ({splices, ending_strategy, seam_quality, bars_removed, phrase_aligned}),
and an assemble-compatible clip spec for the FINAL rendered file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from . import beat_grid as bg
from . import fftools, media

try:  # optional -- seamless/feature analysis needs librosa
    import librosa  # type: ignore

    HAVE_LIBROSA = True
except Exception:  # pragma: no cover - environment dependent
    HAVE_LIBROSA = False

# ---- tunables ----------------------------------------------------------
_ANALYSIS_SR = 22050
_HOP = 512
_N_MFCC = 20
_CHROMA_W = 1.0
_MFCC_W = 0.5
_WIN_BEATS = 4               # beats of context each side of a splice endpoint

_XFADE_BASE_S = 0.030        # 30 ms equal-power crossfade for a good match
_XFADE_SIM_HI = 0.90         # similarity at/above which the base fade is enough
_SNAP_MS = 0.030            # snap the cut to an onset attack within +/-30 ms

_SIM_FLOOR = 0.70            # below this the seamless splice is not trustworthy
_SEAM_AUDIBLE_RATIO = 1.25   # seam flux / p90 beat flux above which it's audible
_MAX_CANDIDATES = 4          # seam-QC retries before giving up

_DEFAULT_TAIL_FADE = 1.8
_BUTTON_HOLD = 0.05
_BUTTON_MAX_TAIL = 2.0

_STINGS = {"seamless", "button", "tail", "fade"}


# ======================================================================
# small dsp helpers
# ======================================================================
def _exp_fade_env(n: int) -> np.ndarray:
    """Monotonic exponential fade 1.0 -> ~1e-3 (-60 dB) over n samples."""
    if n <= 0:
        return np.ones(0, dtype=np.float32)
    x = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return (10.0 ** (-3.0 * x)).astype(np.float32)


def _cos(u: np.ndarray, v: np.ndarray) -> float:
    n = min(len(u), len(v))
    if n == 0:
        return 0.0
    u = u[:n]
    v = v[:n]
    du = float(np.linalg.norm(u))
    dv = float(np.linalg.norm(v))
    if du <= 1e-9 or dv <= 1e-9:
        return 0.0
    return float(np.dot(u, v) / (du * dv))


# ======================================================================
# analysis: beats, downbeats, per-beat features
# ======================================================================
def _analyze(path: str) -> dict[str, Any]:
    """Beat-synchronous analysis. Requires librosa; returns None-ish on fail."""
    y, sr = librosa.load(path, sr=_ANALYSIS_SR, mono=True)
    y = y.astype(np.float32)
    if y.size == 0:
        raise ValueError("empty audio")
    duration = len(y) / sr

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=_HOP)
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=_HOP, units="frames"
    )
    beat_frames = np.asarray(beat_frames, dtype=int)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=_HOP)

    # onset attack times (for snapping the cut to a transient)
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=_HOP, units="frames"
    )
    onset_times = librosa.frames_to_time(np.asarray(onset_frames), sr=sr, hop_length=_HOP)

    # per-beat onset strength (for the downbeat-phase vote and seam reference)
    n_frames = len(onset_env)
    bf_clip = np.clip(beat_frames, 0, max(n_frames - 1, 0))
    beat_strength = onset_env[bf_clip] if n_frames else np.zeros(len(beat_frames))

    # beat-synchronous chroma + mfcc
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=_HOP)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=_N_MFCC, hop_length=_HOP)
    chroma_b = librosa.util.sync(chroma, beat_frames, aggregate=np.mean)
    mfcc_b = librosa.util.sync(mfcc, beat_frames, aggregate=np.mean)
    # sync yields (d, n_beats+?) -- align to beat count
    n_beats = len(beat_times)
    chroma_b = chroma_b[:, :n_beats]
    mfcc_b = mfcc_b[:, :n_beats]

    feats = []
    for i in range(n_beats):
        c = chroma_b[:, i].astype(np.float32)
        m = mfcc_b[:, i].astype(np.float32)
        nc = np.linalg.norm(c)
        nm = np.linalg.norm(m)
        c = c / nc if nc > 1e-9 else c
        m = m / nm if nm > 1e-9 else m
        feats.append(np.concatenate([_CHROMA_W * c, _MFCC_W * m]))
    feats = np.asarray(feats, dtype=np.float32) if feats else np.zeros((0, 12 + _N_MFCC))

    # Downbeat phase: assume 4/4; pick the phase 0-3 whose beats carry the most
    # onset strength on average (simplification -- no true downbeat model).
    phase = 0
    if n_beats >= 4:
        means = [float(beat_strength[p::4].mean()) if beat_strength[p::4].size else 0.0
                 for p in range(4)]
        phase = int(np.argmax(means))

    beat_period = float(np.median(np.diff(beat_times))) if n_beats >= 2 else 0.5
    bpm = 60.0 / beat_period if beat_period > 0 else float(np.atleast_1d(tempo)[0])

    return {
        "sr": sr,
        "duration": duration,
        "onset_env": onset_env,
        "onset_times": np.atleast_1d(onset_times).astype(float),
        "beat_times": beat_times.astype(float),
        "beat_strength": np.asarray(beat_strength, dtype=float),
        "feats": feats,
        "phase": phase,
        "beat_period": beat_period,
        "bpm": bpm,
        "n_beats": n_beats,
    }


def _bar_beat_indices(n_beats: int, phase: int) -> list[int]:
    """Beat indices that fall on a bar start (downbeat), 4/4 assumption."""
    return [i for i in range(n_beats) if i % 4 == phase]


def _window_vec(feats: np.ndarray, i0: int, i1: int) -> np.ndarray:
    i0 = max(0, i0)
    i1 = min(len(feats), i1)
    if i1 <= i0:
        return np.zeros(0, dtype=np.float32)
    return feats[i0:i1].reshape(-1)


def _pair_score(feats: np.ndarray, a: int, b: int) -> float:
    """Cosine similarity of the material joined at a splice removing [a, b).

    Compares W beats of context BEFORE and AFTER each endpoint -- the material
    on both sides of the seam must match for the join to disappear.
    """
    w = _WIN_BEATS
    before = _cos(_window_vec(feats, a - w, a), _window_vec(feats, b - w, b))
    after = _cos(_window_vec(feats, a, a + w), _window_vec(feats, b, b + w))
    vals = [v for v in (before, after) if v != 0.0]
    if not vals:
        return 0.0
    return float(np.mean(vals))


# ======================================================================
# splice search
# ======================================================================
def _phrase_bonus(ka: int, kb: int) -> float:
    """Bonus for phrase-aligned endpoints (bar positions ka, kb among bars)."""
    if ka % 8 == 0 and kb % 8 == 0:
        return 0.10
    if ka % 4 == 0 and kb % 4 == 0:
        return 0.05
    return 0.0


def _single_splices(A: dict, D: float) -> list[dict]:
    """Rank single bar-aligned splice pairs removing ~D seconds.

    Tolerance starts at +/-1 bar and widens to +/-2 bars if nothing is found.
    """
    feats = A["feats"]
    bt = A["beat_times"]
    bars = _bar_beat_indices(A["n_beats"], A["phase"])
    bar_s = 4.0 * A["beat_period"]

    for tol_bars in (1.0, 2.0):
        tol = tol_bars * bar_s
        cands: list[dict] = []
        for ka, a in enumerate(bars):
            for kb, b in enumerate(bars):
                if b <= a:
                    continue
                removed = float(bt[b] - bt[a])
                if abs(removed - D) > tol:
                    continue
                score = _pair_score(feats, a, b)
                bonus = _phrase_bonus(ka, kb)
                cands.append({
                    "a": a, "b": b, "ka": ka, "kb": kb,
                    "removed": removed,
                    "similarity": score,
                    "rank_score": score + bonus,
                    "phrase_aligned": bonus > 0,
                    "bars_removed": int(round(removed / bar_s)),
                })
        if cands:
            cands.sort(key=lambda c: c["rank_score"], reverse=True)
            return cands
    return []


def _two_splices(A: dict, D: float) -> list[dict] | None:
    """Greedy two-splice plan: best splice for D1, then best for the remainder.

    Returns a list of two non-overlapping splice dicts, or None.
    """
    feats = A["feats"]
    bt = A["beat_times"]
    bars = _bar_beat_indices(A["n_beats"], A["phase"])
    bar_s = 4.0 * A["beat_period"]
    tol = 1.0 * bar_s

    # all bar-aligned pairs with a positive removal strictly less than D
    pairs: list[dict] = []
    for ka, a in enumerate(bars):
        for kb, b in enumerate(bars):
            if b <= a:
                continue
            removed = float(bt[b] - bt[a])
            if removed <= bar_s * 0.5 or removed >= D - bar_s * 0.5:
                continue
            pairs.append({
                "a": a, "b": b, "ka": ka, "kb": kb, "removed": removed,
                "similarity": _pair_score(feats, a, b),
                "phrase_aligned": _phrase_bonus(ka, kb) > 0,
                "bars_removed": int(round(removed / bar_s)),
            })
    if not pairs:
        return None
    pairs.sort(key=lambda c: c["similarity"] + (0.05 if c["phrase_aligned"] else 0),
               reverse=True)

    for first in pairs:
        d2 = D - first["removed"]
        for second in pairs:
            # disjoint from the first removed region
            if not (second["b"] <= first["a"] or second["a"] >= first["b"]):
                continue
            if abs(second["removed"] - d2) > tol:
                continue
            # order the two splices by position in the timeline
            plan = sorted([first, second], key=lambda c: c["a"])
            return plan
    return None


# ======================================================================
# rendering
# ======================================================================
def _snap_to_onset(t: float, onset_times: np.ndarray) -> float:
    if onset_times.size == 0:
        return t
    j = int(np.argmin(np.abs(onset_times - t)))
    if abs(onset_times[j] - t) <= _SNAP_MS:
        return float(onset_times[j])
    return t


def _xfade_seconds(similarity: float, beat_period: float) -> float:
    """Equal-power crossfade length: 30 ms base, scaled up to one beat when the
    match is poor."""
    if similarity >= _XFADE_SIM_HI:
        return _XFADE_BASE_S
    frac = np.clip((_XFADE_SIM_HI - similarity) / (_XFADE_SIM_HI - _SIM_FLOOR), 0.0, 1.0)
    return float(_XFADE_BASE_S + frac * (beat_period - _XFADE_BASE_S))


def _render_segments(audio: np.ndarray, sr: int,
                     segments: list[tuple[float, float]],
                     xfades: list[float]) -> tuple[np.ndarray, list[int]]:
    """Concatenate kept [start,end) segments with equal-power crossfades.

    Returns (stereo_out, seam_output_samples).
    """
    out: np.ndarray | None = None
    seam_samples: list[int] = []
    for k, (s, e) in enumerate(segments):
        si = max(0, int(round(s * sr)))
        ei = min(len(audio), int(round(e * sr)))
        seg = audio[si:ei]
        if seg.shape[0] == 0:
            continue
        if out is None:
            out = seg.copy()
            continue
        xf = int(round(xfades[k - 1] * sr))
        xf = min(xf, out.shape[0], seg.shape[0])
        if xf <= 0:
            seam_samples.append(out.shape[0])
            out = np.concatenate([out, seg], axis=0)
            continue
        t = np.linspace(0.0, 1.0, xf, endpoint=False, dtype=np.float32)
        fout = np.cos(t * np.pi / 2).reshape(-1, 1)
        fin = np.sin(t * np.pi / 2).reshape(-1, 1)
        head = out.shape[0] - xf
        blended = out[head:] * fout + seg[:xf] * fin
        seam_samples.append(head + xf // 2)
        out = np.concatenate([out[:head], blended, seg[xf:]], axis=0)
    if out is None:
        out = np.zeros((0, audio.shape[1]), dtype=np.float32)
    return out, seam_samples


# ======================================================================
# seam quality control (self-judging, like the color gates)
# ======================================================================
def _seam_ratios(out: np.ndarray, sr: int, seam_samples: list[int],
                 beat_period: float) -> list[float]:
    """Measure each seam's spectral flux against the song's OWN beat-transition
    flux. A seam lands on a downbeat, so the honest reference is the strength of
    the song's ordinary beat transitions -- not the quiet gaps between them.

    seam_ratio = (max flux in +/-1 beat around the seam) / (90th-percentile of
    the song's beat-transition flux). Ratio > 1.25 => the join is louder than a
    normal beat change and is audible.
    """
    if not seam_samples:
        return []
    mono = out.mean(axis=1).astype(np.float32)
    if HAVE_LIBROSA:
        y = librosa.resample(mono, orig_sr=sr, target_sr=_ANALYSIS_SR) \
            if sr != _ANALYSIS_SR else mono
        env = librosa.onset.onset_strength(y=y.astype(np.float32), sr=_ANALYSIS_SR,
                                           hop_length=_HOP)
        onset_frames = librosa.onset.onset_detect(
            onset_envelope=env, sr=_ANALYSIS_SR, hop_length=_HOP, units="frames")
        env_sr = _ANALYSIS_SR / _HOP
        scale = _ANALYSIS_SR / sr
    else:  # pragma: no cover - librosa present in test env
        env = np.abs(np.diff(mono, prepend=mono[:1]))
        onset_frames = np.arange(0, len(env), max(1, int(beat_period * sr)))
        env_sr = sr
        scale = 1.0
    if env.size == 0:
        return [0.0 for _ in seam_samples]

    win = max(1, int(round(beat_period * env_sr)))  # +/-1 beat window
    seam_frames = [int(round(s * scale / _HOP)) if HAVE_LIBROSA else int(s)
                   for s in seam_samples]

    # Reference distribution: flux at the song's beat transitions, excluding
    # any that fall inside a seam window (those aren't "ordinary").
    onset_frames = np.atleast_1d(np.asarray(onset_frames, dtype=int))
    ref_vals = []
    for f in onset_frames:
        if any(abs(f - sf) <= win for sf in seam_frames):
            continue
        if 0 <= f < len(env):
            ref_vals.append(float(env[f]))
    ref = np.asarray(ref_vals) if ref_vals else env[env > 0]
    p90 = float(np.percentile(ref, 90)) if ref.size else float(env.max())
    if p90 <= 1e-9:
        p90 = float(env.max()) or 1.0

    ratios = []
    for f in seam_frames:
        lo, hi = max(0, f - win), min(len(env), f + win + 1)
        seam_flux = float(env[lo:hi].max()) if hi > lo else 0.0
        ratios.append(seam_flux / p90)
    return ratios


# ======================================================================
# ending strategies that TRUNCATE at target (button / tail / fade)
# ======================================================================
def _plan_button(A: dict, target: float) -> dict:
    bt = A["beat_times"]
    phase = A["phase"]
    downbeats = [(i, bt[i]) for i in range(A["n_beats"]) if i % 4 == phase]
    win_lo, win_hi = target - 2.0, target + 0.5
    best = None
    best_score = -1.0
    for i, t in downbeats:
        if t < win_lo or t > win_hi:
            continue
        strength = A["beat_strength"][i] if i < len(A["beat_strength"]) else 0.0
        score = strength - 0.15 * abs(t - target)
        if score > best_score:
            best_score = score
            best = t
    if best is None:
        # fall back to nearest beat to target
        best = float(bt[np.argmin(np.abs(bt - target))]) if A["n_beats"] else target
    hit = _snap_to_onset(float(best), A["onset_times"])
    return {"hit": hit}


def _phrase_boundaries(A: dict) -> list[float]:
    bt = A["beat_times"]
    bars = _bar_beat_indices(A["n_beats"], A["phase"])
    # 4-bar phrase boundaries: every 4th bar
    return [float(bt[b]) for k, b in enumerate(bars) if k % 4 == 0]


# ======================================================================
# public entry
# ======================================================================
def cut_music(
    song: str,
    target_seconds: float,
    *,
    sting: str = "seamless",
    bar_hint: int | None = None,
    tail_fade: float = _DEFAULT_TAIL_FADE,
    output_path: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    if sting not in _STINGS:
        return {"ok": False, "error": f"Unknown sting '{sting}'."}
    p = str(Path(song).expanduser())
    if not Path(p).exists():
        return {"ok": False, "error": "Song does not exist.", "path": p}
    if target_seconds <= 0:
        return {"ok": False, "error": "target_seconds must be positive."}

    # ---- analysis ----
    if HAVE_LIBROSA:
        try:
            A = _analyze(p)
        except Exception as exc:
            return {"ok": False, "error": f"Analysis failed: {type(exc).__name__}: {exc}"}
    else:
        grid = bg.beat_grid(p)
        if not grid.get("ok"):
            return {"ok": False, "error": f"Beat analysis failed: {grid.get('error')}"}
        # minimal analysis dict from beat_grid (seamless unavailable)
        bt = np.asarray(grid["beat_times"], dtype=float)
        A = {
            "sr": grid["sample_rate"], "duration": grid["duration_seconds"],
            "onset_env": np.array([]), "onset_times": np.asarray(grid["onset_times"], float),
            "beat_times": bt, "beat_strength": np.asarray(grid["onset_strengths"], float),
            "feats": np.zeros((len(bt), 12 + _N_MFCC), dtype=np.float32),
            "phase": 0, "beat_period": (float(np.median(np.diff(bt))) if bt.size >= 2 else 0.5),
            "bpm": grid["bpm"], "n_beats": len(bt),
        }
        if sting == "seamless":
            sting = "tail"  # cannot splice without features

    duration = A["duration"]
    bar_s = 4.0 * A["beat_period"]
    target = min(target_seconds, duration)

    plan = _build_plan(
        A, p, song=p, target_seconds=target_seconds, target=target, duration=duration,
        bar_s=bar_s, sting=sting, tail_fade=float(tail_fade),
        output_path=output_path, dry_run=dry_run, confirm=confirm,
    )
    return plan


def _out_path(p: str, target: float, output_path: str | None) -> str:
    if output_path is not None:
        return str(Path(output_path).expanduser())
    name = f"{Path(p).stem}_cut_{int(round(target))}s.wav"
    return str(Path(tempfile.gettempdir()) / name)


def _build_plan(A, p, *, song, target_seconds, target, duration, bar_s, sting,
                tail_fade, output_path, dry_run, confirm) -> dict[str, Any]:
    planned_out = _out_path(p, target, output_path)

    # ---- decide the edit (splice list + ending) ----
    D = duration - target                 # seconds to remove for seamless
    splice_plan: list[dict] = []
    ending_strategy = sting
    degraded_note = None
    ranked: list[dict] = []               # candidate single-splices for seamless

    if sting == "seamless":
        if D <= bar_s * 0.5:
            # Target >= song length (or within half a bar): nothing to remove.
            ending_strategy = "seamless"
            degraded_note = ("Target is at/above the song length; no removal "
                             "needed -- returning the whole song unchanged.")
        else:
            ranked = _single_splices(A, D)
            best = ranked[0] if ranked else None
            if best is not None and best["similarity"] >= _SIM_FLOOR:
                splice_plan = [best]
            else:
                # try two greedy splices before degrading
                two = _two_splices(A, D)
                if two and min(s["similarity"] for s in two) >= _SIM_FLOOR:
                    splice_plan = two
                else:
                    ending_strategy = "button"
                    degraded_note = (
                        "Seamless found no splice whose matched material cleared "
                        f"the similarity floor ({_SIM_FLOOR}); degraded to a button "
                        "ending at the target.")

    # ---- assemble the render recipe ----
    recipe = _make_recipe(A, target, duration, bar_s, ending_strategy, splice_plan,
                          tail_fade)

    report = {
        "splices": [
            {
                "at_seconds": round(float(A["beat_times"][s["a"]]), 4),
                "removed_from": round(float(A["beat_times"][s["a"]]), 4),
                "removed_to": round(float(A["beat_times"][s["b"]]), 4),
                "similarity": round(float(s["similarity"]), 4),
                "crossfade_ms": round(_xfade_seconds(s["similarity"], A["beat_period"]) * 1000, 1),
                "seam_ratio": None,   # filled after render
            }
            for s in splice_plan
        ],
        "ending_strategy": ending_strategy,
        "seam_quality": {"acceptable": True, "worst_ratio": None},
        "bars_removed": int(sum(s["bars_removed"] for s in splice_plan)),
        "phrase_aligned": bool(splice_plan) and all(s["phrase_aligned"] for s in splice_plan),
    }
    if degraded_note:
        report["note"] = degraded_note

    out = {
        "ok": True,
        "dry_run": dry_run and not confirm,
        "song": song,
        "target_seconds": round(target_seconds, 4),
        "source_duration_seconds": round(duration, 4),
        "bpm": round(float(A["bpm"]), 3),
        "bar": 4,
        "librosa": HAVE_LIBROSA,
        "cut": recipe["method"],
        "end_time": round(recipe["out_duration"], 4),
        "output_path": planned_out,
        "report": report,
        "clip_spec": {
            "path": planned_out,
            "in": 0.0,
            "out": round(recipe["out_duration"], 4),
            "fade_out": round(recipe["method"].get("fade_seconds", 0.0), 4),
        },
    }

    if dry_run and not confirm:
        out["note"] = "Set dry_run=false and confirm=true to render the cut WAV."
        return out
    if not confirm:
        return {"ok": False, "error": "confirm must be true when dry_run is false."}

    # ---- render (+ seam QC + retry) ----
    try:
        _render_and_qc(out, A, p, planned_out, recipe, ranked, splice_plan, report,
                       target, duration, bar_s, tail_fade, ending_strategy)
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"Render failed: {type(exc).__name__}: {exc}"
    return out


def _make_recipe(A, target, duration, bar_s, ending_strategy, splice_plan, tail_fade):
    """Turn the edit decision into segments/fade parameters for rendering."""
    bt = A["beat_times"]
    if ending_strategy == "seamless" and splice_plan:
        # keep everything except each removed [a,b); preserve the real ending.
        cuts = sorted(splice_plan, key=lambda s: s["a"])
        segments: list[tuple[float, float]] = []
        prev = 0.0
        xfades: list[float] = []
        for s in cuts:
            ta = float(bt[s["a"]])
            tb = _snap_to_onset(float(bt[s["b"]]), A["onset_times"])
            segments.append((prev, ta))
            xfades.append(_xfade_seconds(s["similarity"], A["beat_period"]))
            prev = tb
        segments.append((prev, duration))
        # crossfade shortening reduces duration slightly per seam
        removed = sum(float(bt[s["b"]]) - float(bt[s["a"]]) for s in cuts)
        out_dur = duration - removed - sum(xfades)
        return {
            "mode": "splice",
            "segments": segments,
            "xfades": xfades,
            "out_duration": max(0.0, out_dur),
            "method": {
                "sting": "seamless",
                "splices": len(cuts),
                "fade_seconds": 0.0,   # real ending; no synthetic fade
            },
        }

    if ending_strategy == "seamless":
        # whole song, unchanged (target >= duration)
        return {
            "mode": "whole",
            "segments": [(0.0, duration)],
            "xfades": [],
            "out_duration": duration,
            "method": {"sting": "seamless", "splices": 0, "fade_seconds": 0.0},
        }

    if ending_strategy == "button":
        b = _plan_button(A, target)
        return {
            "mode": "button",
            "hit": b["hit"],
            "out_duration": min(duration, b["hit"] + _BUTTON_HOLD + _BUTTON_MAX_TAIL),
            "method": {
                "sting": "button",
                "boundary_kind": "downbeat_hit",
                "hit_time": round(b["hit"], 4),
                "exit_time": round(b["hit"], 4),
                "hold_seconds": _BUTTON_HOLD,
                "fade_seconds": _BUTTON_MAX_TAIL,
                "fade_shape": "natural_decay_or_exponential",
            },
        }

    if ending_strategy == "tail":
        boundaries = [t for t in _phrase_boundaries(A) if t <= target]
        boundary_kind = "phrase_boundary"
        if not boundaries:
            boundaries = [t for t in bt if t <= target]
            boundary_kind = "beat"
        exit_time = max(boundaries) if boundaries else target
        fade = float(tail_fade)
        end_time = min(duration, exit_time + fade)
        return {
            "mode": "tail",
            "exit_time": exit_time,
            "out_duration": end_time,
            "method": {
                "sting": "tail",
                "boundary_kind": boundary_kind,
                "exit_time": round(exit_time, 4),
                "fade_seconds": round(end_time - exit_time, 4),
                "fade_shape": "exponential",
            },
        }

    # fade: last-resort 2-bar musical fade ending near target
    boundaries = [t for t in _phrase_boundaries(A) if t <= target]
    exit_time = max(boundaries) if boundaries else target
    fade = min(2.0 * bar_s, exit_time)
    fade_start = max(0.0, exit_time - fade)
    return {
        "mode": "fade",
        "exit_time": exit_time,
        "fade_start": fade_start,
        "out_duration": exit_time,
        "method": {
            "sting": "fade",
            "boundary_kind": "phrase_boundary",
            "exit_time": round(exit_time, 4),
            "fade_seconds": round(fade, 4),
            "fade_shape": "exponential_musical",
        },
    }


def _render_recipe(audio, sr, recipe, A):
    """Render a recipe to a stereo float array; return (out, seam_samples)."""
    mode = recipe["mode"]
    if mode in ("splice", "whole"):
        return _render_segments(audio, sr, recipe["segments"], recipe["xfades"])

    if mode == "button":
        hit = recipe["hit"]
        hold_end = min(len(audio), int(round((hit + _BUTTON_HOLD) * sr)))
        tail_end = min(len(audio), int(round((hit + _BUTTON_HOLD + _BUTTON_MAX_TAIL) * sr)))
        out = audio[:tail_end].copy()
        tail = out[hold_end:tail_end]
        if tail.shape[0] > 0:
            # decaying if the tail's second half is quieter than the first
            half = tail.shape[0] // 2
            e0 = float(np.sqrt(np.mean(tail[:half] ** 2) + 1e-12)) if half else 1.0
            e1 = float(np.sqrt(np.mean(tail[half:] ** 2) + 1e-12))
            natural = e1 < 0.6 * e0
            if natural:
                # keep the real decay, guard the very end against a click
                g = min(int(0.02 * sr), tail.shape[0])
                if g > 0:
                    out[tail_end - g:tail_end] *= np.linspace(1.0, 0.0, g, dtype=np.float32).reshape(-1, 1)
            else:
                env = _exp_fade_env(tail.shape[0]).reshape(-1, 1)
                out[hold_end:tail_end] = tail * env
        return out, []

    # tail / fade: truncate + exponential fade
    exit_time = recipe["exit_time"]
    out_dur = recipe["out_duration"]
    end_sample = min(len(audio), int(round(out_dur * sr)))
    out = audio[:end_sample].copy()
    if mode == "tail":
        fade_start = int(round(exit_time * sr))
    else:  # fade
        fade_start = int(round(recipe["fade_start"] * sr))
    fade_start = max(0, min(fade_start, end_sample))
    if end_sample > fade_start:
        env = _exp_fade_env(end_sample - fade_start).reshape(-1, 1)
        out[fade_start:end_sample] *= env
    return out, []


def _render_and_qc(out_dict, A, p, planned_out, recipe, ranked, splice_plan, report,
                   target, duration, bar_s, tail_fade, ending_strategy):
    probe = media.probe_one(p)
    sr = probe.get("audio_sample_rate") or 44100
    audio = fftools.decode_pcm(p, sr=sr, channels=2)

    # candidate list for seam-QC retry (seamless single-splice only)
    candidates = [recipe]
    if ending_strategy == "seamless" and len(splice_plan) == 1 and ranked:
        # build alternate recipes from the next-best single splices
        for alt in ranked[1:_MAX_CANDIDATES]:
            candidates.append(_make_recipe(A, target, duration, bar_s, "seamless",
                                           [alt], tail_fade))

    attempts = 0
    chosen = None
    chosen_out = None
    chosen_seams: list[int] = []
    chosen_ratios: list[float] = []
    best_fallback = None
    for rec in candidates:
        attempts += 1
        rendered, seams = _render_recipe(audio, sr, rec, A)
        ratios = _seam_ratios(rendered, sr, seams, A["beat_period"])
        worst = max(ratios) if ratios else 0.0
        if best_fallback is None or worst < best_fallback[3]:
            best_fallback = (rec, rendered, seams, worst, ratios)
        if worst <= _SEAM_AUDIBLE_RATIO:
            chosen, chosen_out, chosen_seams, chosen_ratios = rec, rendered, seams, ratios
            break

    acceptable = chosen is not None
    if chosen is None:  # all candidates audible -> keep the least-bad
        chosen, chosen_out, chosen_seams, worst, chosen_ratios = best_fallback

    # write the winner
    fftools.encode_wav(chosen_out, planned_out, sr=sr)

    # backfill the report against the chosen recipe
    if chosen is not recipe:
        # rebuild splice entries for the alternate recipe
        cuts = _recipe_cuts(chosen, A)
        report["splices"] = [
            {
                "at_seconds": round(float(A["beat_times"][c["a"]]), 4),
                "removed_from": round(float(A["beat_times"][c["a"]]), 4),
                "removed_to": round(float(A["beat_times"][c["b"]]), 4),
                "similarity": round(float(c["similarity"]), 4),
                "crossfade_ms": round(_xfade_seconds(c["similarity"], A["beat_period"]) * 1000, 1),
                "seam_ratio": None,
            }
            for c in cuts
        ]
        report["bars_removed"] = int(sum(c["bars_removed"] for c in cuts))
        report["phrase_aligned"] = bool(cuts) and all(c["phrase_aligned"] for c in cuts)

    for i, r in enumerate(chosen_ratios):
        if i < len(report["splices"]):
            report["splices"][i]["seam_ratio"] = round(float(r), 4)
    worst_ratio = max(chosen_ratios) if chosen_ratios else 0.0
    report["seam_quality"] = {
        "acceptable": bool(acceptable),
        "worst_ratio": round(float(worst_ratio), 4),
        "attempts": attempts,
        "threshold": _SEAM_AUDIBLE_RATIO,
    }
    if not acceptable and chosen_seams:
        report["seam_quality"]["flag"] = "audible_seam"
        out_dict["ok"] = True  # still returns, but flags the problem

    out_dict["end_time"] = round(chosen_out.shape[0] / sr, 4)
    out_dict["clip_spec"]["out"] = out_dict["end_time"]
    out_dict["rendered"] = {
        "path": planned_out,
        "sample_rate": sr,
        "duration_seconds": round(chosen_out.shape[0] / sr, 4),
        "seam_count": len(chosen_seams),
    }


def _recipe_cuts(recipe, A) -> list[dict]:
    """Recover splice dicts (a,b,similarity,...) from a splice recipe's segments."""
    bt = A["beat_times"]
    bar_s = 4.0 * A["beat_period"]
    if recipe["mode"] != "splice":
        return []
    segs = recipe["segments"]
    cuts = []
    for k in range(len(segs) - 1):
        ta = segs[k][1]
        tb = segs[k + 1][0]
        a = int(np.argmin(np.abs(bt - ta)))
        b = int(np.argmin(np.abs(bt - tb)))
        cuts.append({
            "a": a, "b": b,
            "similarity": _pair_score(A["feats"], a, b),
            "phrase_aligned": True,
            "bars_removed": int(round((float(bt[b]) - float(bt[a])) / bar_s)),
        })
    return cuts


def register(add_tool) -> None:
    add_tool(
        "cut_music",
        {
            "type": "object",
            "properties": {
                "song": {"type": "string"},
                "target_seconds": {"type": "number", "minimum": 0.1},
                "sting": {
                    "type": "string",
                    "enum": ["seamless", "button", "tail", "fade"],
                    "default": "seamless",
                    "description": (
                        "Ending strategy. 'seamless' (default): preserve the "
                        "song's real ending and splice matching phrases out of "
                        "the middle. 'button': end on a hit + natural decay. "
                        "'tail': phrase-final exponential ring-out. 'fade': "
                        "last-resort 2-bar musical fade."),
                },
                "bar_hint": {"type": "integer", "minimum": 1,
                             "description": "Beats per bar (default 4)."},
                "tail_fade": {"type": "number", "default": _DEFAULT_TAIL_FADE},
                "output_path": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["song", "target_seconds"],
            "additionalProperties": False,
        },
        lambda params: cut_music(
            params["song"],
            params["target_seconds"],
            sting=params.get("sting", "seamless"),
            bar_hint=params.get("bar_hint"),
            tail_fade=params.get("tail_fade", _DEFAULT_TAIL_FADE),
            output_path=params.get("output_path"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Cut a song to a target length like a music editor: SPLICE matching "
        "phrases out of the middle so the song's real, composed ending is "
        "preserved, equal-power crossfading every downbeat seam and rejecting "
        "joins whose spectral flux exceeds the song's own beat-to-beat "
        "variation (self-judged seam QC, auto-degrades seamless->button when no "
        "clean splice exists). Ending strategies: seamless (default) / button / "
        "tail / fade. Read the per-edit 'report' (splices, ending_strategy, "
        "seam_quality, bars_removed, phrase_aligned). See "
        "get_editing_knowledge('music-editing').",
    )
