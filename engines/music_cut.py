"""Cut a song to a target duration ending on a musical boundary with a
smooth "sting out".

Two sting styles:
  - 'tail'   : cut at the nearest musical boundary (downbeat/phrase) at or
               before target, then let the following beat ring out under an
               exponential fade (default 1.8 s).
  - 'button' : end on a hit -- find a strong onset near target, hold ~50 ms,
               then a 250 ms fade so the piece lands on the accent.

Outputs a cut WAV, JSON metadata (exit time, method, fade), and an
assemble-compatible audio clip spec.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from . import beat_grid as bg
from . import fftools, media

_DEFAULT_TAIL_FADE = 1.8
_BUTTON_HOLD = 0.05
_BUTTON_FADE = 0.25


def _exp_fade_env(n: int) -> np.ndarray:
    """Monotonic exponential fade from 1.0 -> ~1e-3 (-60 dB) over n samples."""
    if n <= 0:
        return np.ones(0, dtype=np.float32)
    x = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return (10.0 ** (-3.0 * x)).astype(np.float32)


def _downbeats(beats: list[float], bar: int) -> list[float]:
    if not beats:
        return []
    return [b for i, b in enumerate(beats) if i % bar == 0]


def cut_music(
    song: str,
    target_seconds: float,
    *,
    sting: str = "tail",
    bar_hint: int | None = None,
    tail_fade: float = _DEFAULT_TAIL_FADE,
    output_path: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    if sting not in {"tail", "button"}:
        return {"ok": False, "error": f"Unknown sting '{sting}'."}
    p = str(Path(song).expanduser())
    if not Path(p).exists():
        return {"ok": False, "error": "Song does not exist.", "path": p}
    if target_seconds <= 0:
        return {"ok": False, "error": "target_seconds must be positive."}

    grid = bg.beat_grid(p)
    if not grid.get("ok"):
        return {"ok": False, "error": f"Beat analysis failed: {grid.get('error')}"}

    beats = grid["beat_times"]
    onsets = grid["onset_times"]
    onset_str = grid["onset_strengths"]
    duration = grid["duration_seconds"]
    bar = bar_hint or 4

    target = min(target_seconds, duration)

    if sting == "tail":
        downbeats = _downbeats(beats, bar)
        candidates = [d for d in downbeats if d <= target]
        boundary_kind = "downbeat"
        if not candidates:
            candidates = [b for b in beats if b <= target]
            boundary_kind = "beat"
        exit_time = max(candidates) if candidates else target
        if not candidates:
            boundary_kind = "target"
        fade = float(tail_fade)
        end_time = min(duration, exit_time + fade)
        method = {
            "sting": "tail",
            "boundary_kind": boundary_kind,
            "exit_time": round(exit_time, 4),
            "fade_seconds": round(end_time - exit_time, 4),
            "fade_shape": "exponential",
        }
    else:  # button
        # Strong onset near target, preferring at/just before it.
        win_lo, win_hi = target - 2.0, target + 0.5
        best_i = None
        best_score = -1.0
        for i, t in enumerate(onsets):
            if t < win_lo or t > win_hi:
                continue
            strength = onset_str[i] if i < len(onset_str) else 0.5
            # Prefer strong + close to target.
            score = strength - 0.15 * abs(t - target)
            if score > best_score:
                best_score = score
                best_i = i
        if best_i is not None:
            hit = onsets[best_i]
        elif onsets:
            hit = min(onsets, key=lambda t: abs(t - target))
        else:
            hit = target
        exit_time = hit
        fade = _BUTTON_FADE
        end_time = min(duration, hit + _BUTTON_HOLD + _BUTTON_FADE)
        method = {
            "sting": "button",
            "boundary_kind": "onset_hit",
            "hit_time": round(hit, 4),
            "exit_time": round(exit_time, 4),
            "hold_seconds": _BUTTON_HOLD,
            "fade_seconds": _BUTTON_FADE,
            "fade_shape": "exponential",
        }

    out_name = f"{Path(p).stem}_cut_{int(round(target))}s.wav"
    if output_path is None:
        planned_out = str(Path(tempfile.gettempdir()) / out_name)
    else:
        planned_out = str(Path(output_path).expanduser())

    clip_spec = {
        "path": planned_out,
        "in": 0.0,
        "out": round(end_time, 4),
        "fade_out": round(method["fade_seconds"], 4),
    }

    plan = {
        "ok": True,
        "dry_run": dry_run and not confirm,
        "song": p,
        "target_seconds": round(target_seconds, 4),
        "source_duration_seconds": round(duration, 4),
        "bpm": grid["bpm"],
        "bar": bar,
        "beat_method": grid["method"],
        "librosa": grid["librosa"],
        "cut": method,
        "end_time": round(end_time, 4),
        "output_path": planned_out,
        "clip_spec": clip_spec,
    }

    if dry_run and not confirm:
        plan["note"] = "Set dry_run=false and confirm=true to render the cut WAV."
        return plan
    if not confirm:
        return {"ok": False, "error": "confirm must be true when dry_run is false."}

    # ---- render the cut WAV ----
    try:
        probe = media.probe_one(p)
        sr = probe.get("audio_sample_rate") or 44100
        audio = fftools.decode_pcm(p, sr=sr, channels=2)
        n_total = audio.shape[0]
        end_sample = min(n_total, int(round(end_time * sr)))
        exit_sample = min(end_sample, int(round(exit_time * sr)))
        cut = audio[:end_sample].copy()

        # Apply the fade over [exit_sample or fade-window, end_sample].
        fade_seconds = method["fade_seconds"]
        fade_samples = int(round(fade_seconds * sr))
        if fade_samples > 0 and end_sample > 0:
            fs = max(0, end_sample - fade_samples)
            env = _exp_fade_env(end_sample - fs).reshape(-1, 1)
            cut[fs:end_sample] *= env

        fftools.encode_wav(cut, planned_out, sr=sr)
        plan["rendered"] = {
            "path": planned_out,
            "sample_rate": sr,
            "duration_seconds": round(cut.shape[0] / sr, 4),
            "fade_start_seconds": round((end_sample - fade_samples) / sr, 4),
        }
    except Exception as exc:
        plan["ok"] = False
        plan["error"] = f"Render failed: {type(exc).__name__}: {exc}"
    return plan


def register(add_tool) -> None:
    add_tool(
        "cut_music",
        {
            "type": "object",
            "properties": {
                "song": {"type": "string"},
                "target_seconds": {"type": "number", "minimum": 0.1},
                "sting": {"type": "string", "enum": ["tail", "button"], "default": "tail"},
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
            sting=params.get("sting", "tail"),
            bar_hint=params.get("bar_hint"),
            tail_fade=params.get("tail_fade", _DEFAULT_TAIL_FADE),
            output_path=params.get("output_path"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Cut a song to a target length ending on a musical boundary with a "
        "smooth sting-out (tail ring-out or button hit).",
    )
