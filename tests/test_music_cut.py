"""Tests for the splice-based music editor (engines/music_cut.py).

These exercise the real librosa + ffmpeg path against a STRUCTURED synthetic
song (not a click track): distinct sections with real chords, timbre and
percussion, plus a composed ending. The fixtures are built deterministically in
numpy and encoded with the engine's own WAV writer -- no binary assets.

Fixture layout note: the song is A A' B + ending, i.e. the two SIMILAR sections
are ADJACENT (A' is an identical repeat of A) and the CONTRASTING section B is
last, right before the composed cadence + ring-out. This is deliberate: the
seamless algorithm scores a splice by how well the material AROUND endpoint a
matches the material AROUND endpoint b (the jukebox criterion the spec mandates
-- "4 beats before a vs 4 beats before b, plus 4 after a vs 4 after b"). An
8-bar (16 s) removal therefore only has matching context on both sides when the
song contains an 8-bar-shiftable self-similar region -- which the adjacent
A->A' repeat provides. Removing 8 bars from inside the A->A' repeat joins A to
A' seamlessly while preserving B and the real ending.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines import fftools, music_cut  # noqa: E402

SR = 44100
BPM = 120.0
BEAT = 60.0 / BPM        # 0.5 s
BAR = 4 * BEAT           # 2.0 s
_TMP = None


# --------------------------------------------------------------------------
# structured synthetic song fixture
# --------------------------------------------------------------------------
def _tmpdir():
    global _TMP
    if _TMP is None:
        import tempfile
        _TMP = tempfile.mkdtemp(prefix="music_cut_fx_")
    return _TMP


def _adsr(n, a=0.05, r=0.3):
    env = np.ones(n, dtype=np.float32)
    na, nr = int(a * SR), int(r * SR)
    if na:
        env[:na] = np.linspace(0, 1, na)
    if nr:
        env[-nr:] *= np.linspace(1, 0, nr)
    return env


def _chord(freqs, dur, amp=0.22):
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = np.zeros(n, dtype=np.float32)
    for f in freqs:
        sig += np.sin(2 * np.pi * f * t)
    return (sig * amp / len(freqs) * _adsr(n)).astype(np.float32)


def _perc(dur, kind, seed):
    """'kick' = broadband click + low thump (a clear onset marker on every beat
    so the tracker locks to the true 120 bpm); 'snare' = bright noise burst."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    if kind == "kick":
        thump = np.sin(2 * np.pi * (90.0 * np.exp(-25 * t)) * t) * np.exp(-30 * t)
        click = np.sin(2 * np.pi * 2000.0 * t) * np.exp(-200 * t)
        sig = 0.7 * thump + 0.6 * click
    else:
        sig = np.random.default_rng(seed).standard_normal(n) * np.exp(-40 * t)
    return (0.25 * sig).astype(np.float32)


def _section(chords, timbre, seed0, bars=8):
    beat_n = int(BEAT * SR)
    out = []
    for bar in range(bars):
        ch = [f * timbre for f in chords[bar % len(chords)]]
        bar_sig = _chord(ch, BAR)
        for beat in range(4):
            start = beat * beat_n
            gain = 1.6 if beat == 0 else 1.0     # accent the downbeat
            pk = gain * _perc(BEAT, "kick", seed0 + bar * 10 + beat)
            m = min(len(pk), len(bar_sig) - start)
            bar_sig[start:start + m] += pk[:m]
            if beat in (1, 3):
                sn = 0.8 * _perc(BEAT, "snare", seed0 + bar * 10 + beat + 100)
                m2 = min(len(sn), len(bar_sig) - start)
                bar_sig[start:start + m2] += sn[:m2]
        out.append(bar_sig)
    return np.concatenate(out)


@pytest.fixture(scope="module")
def song():
    """A A' B + composed ending. dur ~= 54 s at 120 bpm 4/4.

    A  [0,16)  I-IV, timbre 1.0
    A' [16,32) identical repeat of A
    B  [32,48) V-IV, timbre 1.6 (different chords + timbre)
    cadence [48,52) V->I, then a 2 s decaying ring-out.
    """
    I = [261.63, 329.63, 392.00]
    IV = [349.23, 440.00, 523.25]
    V = [392.00, 493.88, 587.33]
    A = _section([I, IV], 1.0, seed0=100)
    Ap = _section([I, IV], 1.0, seed0=100)         # identical to A
    B = _section([V, IV], 1.6, seed0=500)          # contrasting
    cad = _section([V, I], 1.0, seed0=900, bars=2)
    rn = int(2.0 * SR)
    t = np.arange(rn) / SR
    ring = np.zeros(rn, dtype=np.float32)
    for f in I:
        ring += np.sin(2 * np.pi * f * t)
    ring = (ring * 0.22 / len(I) * np.exp(-2.5 * t)).astype(np.float32)
    mono = np.concatenate([A, Ap, B, cad, ring])
    stereo = np.stack([mono, mono], axis=-1)
    p = os.path.join(_tmpdir(), "structured_song.wav")
    fftools.encode_wav(stereo, p, sr=SR)
    return p


@pytest.fixture(scope="module")
def through_composed():
    """Through-composed 'no self-similar material' fixture: a chromatic march --
    each bar holds a single tone one semitone above the last, so any two bars a
    removal-distance apart land on DISJOINT pitch classes (orthogonal chroma).

    (A literal continuous glissando is a poor degrade fixture here: chroma is
    octave-folded, so points an integer octave apart in a smooth sweep read as
    IDENTICAL pitch class and score ~1.0. A chromatic march with disjoint pitch
    classes is the honest 'nothing repeats' signal for chroma-based similarity.)
    Seamless must find no splice above the floor and DEGRADE to a button."""
    bars = 24
    beat_n = int(BEAT * SR)
    out = []
    for bar in range(bars):
        root = 196.0 * 2 ** ((bar % 12) / 12.0)   # step a semitone each bar
        n = int(BAR * SR)
        t = np.arange(n) / SR
        sig = (np.sin(2 * np.pi * root * t)
               + 0.15 * np.sin(2 * np.pi * 2 * root * t)).astype(np.float32)
        sig *= 0.2 * _adsr(n)
        for beat in range(4):
            s = beat * beat_n
            gain = 1.6 if beat == 0 else 1.0
            pk = gain * _perc(BEAT, "kick", bar * 10 + beat)
            m = min(len(pk), len(sig) - s)
            sig[s:s + m] += pk[:m]
            if beat in (1, 3):
                sn = 0.8 * _perc(BEAT, "snare", bar * 10 + beat + 100)
                m2 = min(len(sn), len(sig) - s)
                sig[s:s + m2] += sn[:m2]
        out.append(sig)
    mono = np.concatenate(out)
    stereo = np.stack([mono, mono], axis=-1).astype(np.float32)
    p = os.path.join(_tmpdir(), "through_composed.wav")
    fftools.encode_wav(stereo, p, sr=SR)
    return p


def _mono(path):
    return fftools.decode_pcm(path, sr=SR, channels=1).reshape(-1)


def _env(x, k=441):
    a = np.abs(x)
    return np.array([a[i:i + k].mean() for i in range(0, len(a) - k, k)])


# --------------------------------------------------------------------------
# seamless: the headline behaviour
# --------------------------------------------------------------------------
def test_seamless_preserves_ending_and_lands_on_bar(song):
    A = music_cut._analyze(song)
    dur = A["duration"]
    target = dur - 16.0                       # remove ~8 bars
    out = os.path.join(_tmpdir(), "seamless.wav")
    plan = music_cut.cut_music(song, target_seconds=target, sting="seamless",
                               output_path=out, dry_run=False, confirm=True)
    assert plan["ok"], plan
    rep = plan["report"]

    # exactly one phrase-aligned splice removing 8 bars
    assert len(rep["splices"]) == 1, rep
    assert rep["bars_removed"] == 8, rep
    assert rep["phrase_aligned"] is True
    assert rep["ending_strategy"] == "seamless"

    # output duration within +/- 1 bar of target
    assert abs(plan["end_time"] - target) <= BAR + 0.1, plan["end_time"]

    # splice lands on a bar boundary (a downbeat in the analysis grid)
    bt = A["beat_times"]
    bars = [float(bt[i]) for i in music_cut._bar_beat_indices(A["n_beats"], A["phase"])]
    at = rep["splices"][0]["at_seconds"]
    assert min(abs(at - b) for b in bars) < 0.12, (at, bars)

    # seam is below the audible threshold and there is a real crossfade
    sp = rep["splices"][0]
    assert sp["seam_ratio"] is not None and sp["seam_ratio"] < music_cut._SEAM_AUDIBLE_RATIO, sp
    assert rep["seam_quality"]["acceptable"] is True
    assert sp["crossfade_ms"] >= 30.0

    # the REAL ending survived: last 1.5 s of output correlates with the source
    src = _mono(song)
    o = _mono(out)
    w = int(1.5 * SR)
    c = np.corrcoef(_env(src[-w:]), _env(o[-w:]))[0, 1]
    assert c > 0.9, c

    # no raw discontinuity: the seam introduces no jump worse than the song's
    # own transients (equal-power crossfade of matching material)
    si = int(at * SR)
    seam_jump = float(np.abs(np.diff(o[max(0, si - 2000):si + 2000])).max())
    global_jump = float(np.abs(np.diff(o)).max())
    assert seam_jump <= global_jump + 1e-4, (seam_jump, global_jump)


def test_seamless_joins_similar_sections_not_contrasting(song):
    A = music_cut._analyze(song)
    dur = A["duration"]
    plan = music_cut.cut_music(song, target_seconds=dur - 16.0, sting="seamless",
                               output_path=os.path.join(_tmpdir(), "sim.wav"),
                               dry_run=False, confirm=True)
    rep = plan["report"]
    sp = rep["splices"][0]

    # A-vs-B baseline: an A position (t=8) against a B position (t=40)
    bt = A["beat_times"]
    bi = lambda tt: int(np.argmin(np.abs(bt - tt)))
    a_vs_b = music_cut._pair_score(A["feats"], bi(8.0), bi(40.0))

    # the chosen splice must match FAR better than joining A to B
    assert sp["similarity"] > a_vs_b + 0.1, (sp["similarity"], a_vs_b)
    # and the removed span sits inside the A/A' repeat (<= B start, ~32 s)
    assert sp["removed_to"] <= 33.0, sp
    assert sp["removed_from"] >= 0.0, sp


def test_seamless_degrades_to_button_without_self_similarity(through_composed):
    A = music_cut._analyze(through_composed)
    plan = music_cut.cut_music(through_composed, target_seconds=A["duration"] - 16.0,
                               sting="seamless",
                               output_path=os.path.join(_tmpdir(), "degrade.wav"),
                               dry_run=False, confirm=True)
    assert plan["ok"], plan
    rep = plan["report"]
    assert rep["ending_strategy"] == "button", rep
    assert rep["splices"] == []
    assert "note" in rep and "degrad" in rep["note"].lower()


# --------------------------------------------------------------------------
# seam quality control: retry + flagging
# --------------------------------------------------------------------------
def test_audible_seam_triggers_retry(song, monkeypatch):
    A = music_cut._analyze(song)
    calls = {"n": 0}

    def stub(out, sr, seam_samples, beat_period):
        calls["n"] += 1
        return [5.0] if calls["n"] == 1 else [0.5]   # first pair audible, retry ok

    monkeypatch.setattr(music_cut, "_seam_ratios", stub)
    plan = music_cut.cut_music(song, target_seconds=A["duration"] - 16.0,
                               sting="seamless",
                               output_path=os.path.join(_tmpdir(), "retry.wav"),
                               dry_run=False, confirm=True)
    q = plan["report"]["seam_quality"]
    assert q["attempts"] == 2, q          # it retried once
    assert q["acceptable"] is True
    assert q["worst_ratio"] == pytest.approx(0.5)


def test_all_candidates_audible_flags_unacceptable(song, monkeypatch):
    A = music_cut._analyze(song)
    monkeypatch.setattr(music_cut, "_seam_ratios",
                        lambda *a, **k: [5.0])         # everything audible
    plan = music_cut.cut_music(song, target_seconds=A["duration"] - 16.0,
                               sting="seamless",
                               output_path=os.path.join(_tmpdir(), "bad.wav"),
                               dry_run=False, confirm=True)
    q = plan["report"]["seam_quality"]
    assert q["attempts"] >= 2
    assert q["acceptable"] is False
    assert q.get("flag") == "audible_seam"


# --------------------------------------------------------------------------
# button ending
# --------------------------------------------------------------------------
def test_button_ends_on_hit_and_decays_monotonically(song):
    A = music_cut._analyze(song)
    out = os.path.join(_tmpdir(), "button.wav")
    plan = music_cut.cut_music(song, target_seconds=30.0, sting="button",
                               output_path=out, dry_run=False, confirm=True)
    assert plan["ok"], plan
    m = plan["cut"]
    assert m["sting"] == "button"
    hit = m["hit_time"]

    # the hit sits on a detected onset (snapped within +/-30 ms -> <100 ms)
    onsets = A["onset_times"]
    assert float(np.min(np.abs(onsets - hit))) < 0.1, hit
    # content ends shortly after the hit, then a ring-out tail
    assert plan["end_time"] > hit
    assert plan["end_time"] - hit < 2.5

    # tail decays monotonically (coarse 0.2 s RMS envelope)
    au = _mono(out)
    hs = int((hit + 0.05) * SR)
    tail = np.abs(au[hs:])
    w = int(0.2 * SR)
    rms = np.array([np.sqrt(np.mean(tail[i:i + w] ** 2) + 1e-12)
                    for i in range(0, max(0, len(tail) - w), w)])
    assert len(rms) >= 3, rms
    assert rms[-1] <= 0.3 * rms[0], rms
    assert int(np.sum(np.diff(rms) > 1e-4)) <= 1, rms


# --------------------------------------------------------------------------
# tail ending: exit ONLY on phrase boundaries
# --------------------------------------------------------------------------
def test_tail_exits_on_phrase_boundary_with_ringout(song):
    A = music_cut._analyze(song)
    plan = music_cut.cut_music(song, target_seconds=20.0, sting="tail",
                               tail_fade=1.8, dry_run=True)
    assert plan["ok"], plan
    m = plan["cut"]
    assert m["sting"] == "tail"
    assert m["boundary_kind"] == "phrase_boundary"
    assert m["fade_seconds"] == pytest.approx(1.8, abs=0.01)
    # exit must be a 4-bar phrase boundary at/before target
    phrases = [t for t in music_cut._phrase_boundaries(A) if t <= 20.0]
    assert min(abs(m["exit_time"] - t) for t in phrases) < 1e-3, m


def test_tail_render_monotonic_fade(song):
    out = os.path.join(_tmpdir(), "tail.wav")
    plan = music_cut.cut_music(song, target_seconds=20.0, sting="tail",
                               tail_fade=1.8, output_path=out,
                               dry_run=False, confirm=True)
    assert plan["ok"], plan
    au = _mono(out)
    fs = int(plan["cut"]["exit_time"] * SR)
    tail = np.abs(au[fs:])
    w = int(0.2 * SR)
    rms = np.array([np.sqrt(np.mean(tail[i:i + w] ** 2) + 1e-12)
                    for i in range(0, max(0, len(tail) - w), w)])
    assert len(rms) >= 3, rms
    assert rms[-1] <= 0.25 * rms[0], rms
    assert int(np.sum(np.diff(rms) > 1e-4)) <= 2, rms


# --------------------------------------------------------------------------
# fade ending: last-resort 2-bar musical fade
# --------------------------------------------------------------------------
def test_fade_is_two_bars(song):
    plan = music_cut.cut_music(song, target_seconds=20.0, sting="fade", dry_run=True)
    assert plan["ok"], plan
    m = plan["cut"]
    assert m["sting"] == "fade"
    assert m["fade_seconds"] == pytest.approx(2 * BAR, abs=0.25)


# --------------------------------------------------------------------------
# target longer than the song: nothing to remove
# --------------------------------------------------------------------------
def test_target_at_or_above_length_returns_whole_song(song):
    A = music_cut._analyze(song)
    over = A["duration"] + 10.0
    out = os.path.join(_tmpdir(), "whole.wav")
    plan = music_cut.cut_music(song, target_seconds=over, sting="seamless",
                               output_path=out, dry_run=False, confirm=True)
    assert plan["ok"], plan
    assert plan["report"]["splices"] == []
    assert plan["report"]["bars_removed"] == 0
    # whole song preserved
    assert plan["end_time"] == pytest.approx(A["duration"], abs=0.1)


# --------------------------------------------------------------------------
# report + assemble-compatible clip spec
# --------------------------------------------------------------------------
def test_report_schema(song):
    A = music_cut._analyze(song)
    plan = music_cut.cut_music(song, target_seconds=A["duration"] - 16.0,
                               sting="seamless", dry_run=True)
    rep = plan["report"]
    assert set(rep) >= {"splices", "ending_strategy", "seam_quality",
                        "bars_removed", "phrase_aligned"}
    assert set(rep["seam_quality"]) >= {"acceptable", "worst_ratio"}
    for s in rep["splices"]:
        assert set(s) >= {"at_seconds", "removed_from", "removed_to",
                          "similarity", "crossfade_ms", "seam_ratio"}


def test_clip_spec_shape(song):
    # a tail cut carries a real fade_out; every mode exposes in/out
    plan = music_cut.cut_music(song, target_seconds=20.0, sting="tail", dry_run=True)
    spec = plan["clip_spec"]
    assert set(spec) >= {"path", "in", "out", "fade_out"}
    assert spec["out"] > spec["in"]
    assert spec["fade_out"] > 0

    seamless = music_cut.cut_music(song, target_seconds=38.0, sting="seamless",
                                   dry_run=True)["clip_spec"]
    assert seamless["out"] > seamless["in"]
    assert seamless["fade_out"] >= 0.0


def test_dry_run_does_not_render(song):
    plan = music_cut.cut_music(song, target_seconds=38.0, dry_run=True)
    assert plan["ok"], plan
    assert plan["dry_run"] is True
    assert "rendered" not in plan
    assert "end_time" in plan
    # default sting is seamless
    assert plan["cut"]["sting"] == "seamless"


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------
def test_bad_inputs(song):
    assert music_cut.cut_music("/no/file.wav", 4.0)["ok"] is False
    assert music_cut.cut_music(song, -1.0)["ok"] is False
    assert music_cut.cut_music(song, 4.0, sting="weird")["ok"] is False
