"""Loudness measurement (EBU R128) and a dialogue/music/sfx mix planner.

`measure(paths)` reports integrated LUFS, LRA, and true peak per file using
ffmpeg's loudnorm analysis pass.

`mix_plan(...)` derives dialogue-normalisation gain, a music-bed level relative
to dialogue, and music ducking windows (from silencedetect on the dialogue),
returning a JSON gain plan. With confirm=true it also renders a premix WAV --
mixed sample-accurately in numpy (deterministic, exact ramps) -- and re-measures
the output so the achieved integrated loudness is reported back.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from . import fftools

_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


# ---- measurement -------------------------------------------------------

def _loudnorm_analyze(path: str, target_i: float = -16.0) -> dict[str, Any]:
    cmd = [
        fftools.ffmpeg_path(),
        "-v", "info",
        "-i", path,
        "-af", f"loudnorm=I={target_i}:print_format=json",
        "-f", "null", "-",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    stderr = completed.stderr or ""
    m = _JSON_RE.search(stderr)
    if not m:
        raise RuntimeError(f"Could not parse loudnorm output for {path}")
    data = json.loads(m.group(0))
    return data


def measure_one(path: str) -> dict[str, Any]:
    p = str(Path(path).expanduser())
    if not Path(p).exists():
        return {"ok": False, "path": p, "error": "File does not exist."}
    try:
        d = _loudnorm_analyze(p)
    except Exception as exc:
        return {"ok": False, "path": p, "error": f"{type(exc).__name__}: {exc}"}

    def _f(key: str) -> float | None:
        try:
            return float(d[key])
        except (KeyError, ValueError, TypeError):
            return None

    return {
        "ok": True,
        "path": p,
        "integrated_lufs": _f("input_i"),
        "loudness_range_lu": _f("input_lra"),
        "true_peak_dbtp": _f("input_tp"),
        "threshold_lufs": _f("input_thresh"),
    }


def measure(paths: list[str]) -> dict[str, Any]:
    if not paths:
        return {"ok": False, "error": "No paths supplied."}
    return {"ok": True, "measurements": [measure_one(p) for p in paths]}


# ---- silence / speech windows -----------------------------------------

def detect_speech_windows(
    path: str, *, noise_db: float | None = None, min_silence: float = 0.3
) -> tuple[list[tuple[float, float]], float, float]:
    """Return (speech_windows, duration, noise_floor_db).

    Speech windows are the complement of detected silence. If ``noise_db`` is
    None it is auto-calibrated from the file's measured threshold.
    """
    dur = fftools.ffprobe_json(path).get("format", {}).get("duration")
    duration = float(dur) if dur else 0.0

    if noise_db is None:
        try:
            meas = measure_one(path)
            thr = meas.get("threshold_lufs")
            # silencedetect noise is a dBFS-ish level; derive from threshold.
            noise_db = (thr + 6.0) if thr is not None else -30.0
            noise_db = max(min(noise_db, -18.0), -50.0)
        except Exception:
            noise_db = -30.0

    cmd = [
        fftools.ffmpeg_path(),
        "-v", "info",
        "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    stderr = completed.stderr or ""

    silences: list[tuple[float, float]] = []
    cur_start: float | None = None
    for line in stderr.splitlines():
        ms = _SILENCE_START_RE.search(line)
        if ms:
            cur_start = float(ms.group(1))
            continue
        me = _SILENCE_END_RE.search(line)
        if me and cur_start is not None:
            silences.append((max(0.0, cur_start), float(me.group(1))))
            cur_start = None
    if cur_start is not None:
        silences.append((max(0.0, cur_start), duration))

    # Complement -> speech windows.
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor:
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        speech.append((cursor, duration))
    # Filter zero-length.
    speech = [(a, b) for a, b in speech if b - a > 1e-3]
    return speech, duration, float(noise_db)


# ---- gain / duck helpers ----------------------------------------------

def _db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _build_duck_envelope(
    n: int, sr: int, speech_windows: list[tuple[float, float]], duck_lin: float, ramp: float
) -> np.ndarray:
    """1.0 baseline, dropping to ``duck_lin`` across each speech window with
    linear ramps of ``ramp`` seconds at the leading and trailing edges."""
    env = np.ones(n, dtype=np.float32)
    for s, e in speech_windows:
        si = int(max(0.0, s) * sr)
        ei = int(min(n / sr, e) * sr)
        if ei <= si:
            continue
        rs = max(1, int(ramp * sr))
        # Down ramp entering speech.
        d_start = max(0, si - rs)
        env[d_start:si] = np.minimum(
            env[d_start:si], np.linspace(1.0, duck_lin, si - d_start, dtype=np.float32)
        ) if si > d_start else env[d_start:si]
        # Hold ducked during speech.
        env[si:ei] = np.minimum(env[si:ei], duck_lin)
        # Up ramp leaving speech.
        u_end = min(n, ei + rs)
        env[ei:u_end] = np.minimum(
            env[ei:u_end], np.linspace(duck_lin, 1.0, u_end - ei, dtype=np.float32)
        ) if u_end > ei else env[ei:u_end]
    return env


def _apply_fades(sig: np.ndarray, sr: int, fade_in: float, fade_out: float) -> np.ndarray:
    n = sig.shape[0]
    if fade_in > 0:
        fi = min(n, int(fade_in * sr))
        ramp = np.linspace(0.0, 1.0, fi, dtype=np.float32).reshape(-1, 1)
        sig[:fi] *= ramp
    if fade_out > 0:
        fo = min(n, int(fade_out * sr))
        ramp = np.linspace(1.0, 0.0, fo, dtype=np.float32).reshape(-1, 1)
        sig[n - fo:] *= ramp
    return sig


# ---- mix plan ----------------------------------------------------------

def mix_plan(
    dialogue: list[str] | str,
    *,
    music: list[str] | None = None,
    sfx: list[str] | None = None,
    dialogue_lufs: float = -16.0,
    music_bed_db: float = -18.0,
    duck_db: float = -7.0,
    ramp: float = 0.4,
    music_fade_in: float = 1.0,
    music_fade_out: float = 2.0,
    output_path: str | None = None,
    sr: int = 48000,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    dia_paths = [dialogue] if isinstance(dialogue, str) else list(dialogue)
    dia_paths = [str(Path(p).expanduser()) for p in dia_paths]
    music = [str(Path(p).expanduser()) for p in (music or [])]
    sfx = [str(Path(p).expanduser()) for p in (sfx or [])]

    for p in dia_paths + music + sfx:
        if not Path(p).exists():
            return {"ok": False, "error": f"File does not exist: {p}"}
    if not dia_paths:
        return {"ok": False, "error": "At least one dialogue file is required."}

    # Measure dialogue (use the first as the reference bus level).
    dia_meas = [measure_one(p) for p in dia_paths]
    dia_ref = next((m for m in dia_meas if m.get("integrated_lufs") is not None), None)
    if dia_ref is None:
        return {"ok": False, "error": "Could not measure dialogue loudness.", "dialogue": dia_meas}

    dia_integrated = dia_ref["integrated_lufs"]
    dialogue_gain_db = round(dialogue_lufs - dia_integrated, 3)

    music_target_lufs = dialogue_lufs + music_bed_db
    music_meas = [measure_one(p) for p in music]
    music_gain_db = None
    if music_meas and music_meas[0].get("integrated_lufs") is not None:
        music_gain_db = round(music_target_lufs - music_meas[0]["integrated_lufs"], 3)

    # Speech windows drive ducking.
    speech_windows, dia_duration, noise_db = detect_speech_windows(dia_paths[0])
    duck_windows = [
        {"start": round(s, 3), "end": round(e, 3), "ramp": ramp, "duck_db": duck_db}
        for s, e in speech_windows
    ]

    plan = {
        "ok": True,
        "dry_run": dry_run and not confirm,
        "targets": {
            "dialogue_lufs": dialogue_lufs,
            "music_bed_db": music_bed_db,
            "music_target_lufs": round(music_target_lufs, 2),
            "duck_db": duck_db,
            "ramp_seconds": ramp,
        },
        "dialogue": {
            "files": dia_paths,
            "measured_integrated_lufs": dia_integrated,
            "gain_db": dialogue_gain_db,
            "duration_seconds": round(dia_duration, 3),
            "noise_floor_db": noise_db,
        },
        "music": {
            "files": music,
            "measured": music_meas,
            "gain_db": music_gain_db,
            "fade_in": music_fade_in,
            "fade_out": music_fade_out,
        },
        "sfx": {"files": sfx},
        "duck_windows": duck_windows,
    }

    if dry_run and not confirm:
        plan["note"] = "Set dry_run=false and confirm=true to render a premix WAV."
        return plan

    if not confirm:
        return {"ok": False, "error": "confirm must be true when dry_run is false."}

    # ---- render premix (sample-accurate numpy mix) ----
    try:
        premix, achieved = _render_premix(
            dia_paths, music, sfx,
            dialogue_gain_db=dialogue_gain_db,
            music_gain_db=music_gain_db or 0.0,
            speech_windows=speech_windows,
            duck_db=duck_db, ramp=ramp,
            music_fade_in=music_fade_in, music_fade_out=music_fade_out,
            sr=sr, output_path=output_path,
        )
    except Exception as exc:
        plan["ok"] = False
        plan["error"] = f"Premix render failed: {type(exc).__name__}: {exc}"
        return plan

    plan["premix"] = premix
    plan["premix"]["remeasured"] = achieved
    return plan


def _render_premix(
    dia_paths, music, sfx, *, dialogue_gain_db, music_gain_db,
    speech_windows, duck_db, ramp, music_fade_in, music_fade_out,
    sr, output_path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Decode all stems to stereo float32 at sr.
    dia_stems = [fftools.decode_pcm(p, sr=sr, channels=2) for p in dia_paths]
    dia_len = max((s.shape[0] for s in dia_stems), default=0)
    music_stems = [fftools.decode_pcm(p, sr=sr, channels=2) for p in music]
    sfx_stems = [fftools.decode_pcm(p, sr=sr, channels=2) for p in sfx]

    total_len = max(
        [dia_len]
        + [s.shape[0] for s in music_stems]
        + [s.shape[0] for s in sfx_stems]
        + [1]
    )

    mix = np.zeros((total_len, 2), dtype=np.float32)

    # Dialogue bus.
    dia_gain = _db_to_lin(dialogue_gain_db)
    for s in dia_stems:
        seg = np.zeros((total_len, 2), dtype=np.float32)
        seg[: s.shape[0]] = s
        mix += seg * dia_gain

    # Music bus with ducking + fades.
    if music_stems:
        duck_lin = _db_to_lin(duck_db)
        env = _build_duck_envelope(total_len, sr, speech_windows, duck_lin, ramp)
        env2 = env.reshape(-1, 1)
        music_gain = _db_to_lin(music_gain_db)
        music_bus = np.zeros((total_len, 2), dtype=np.float32)
        for s in music_stems:
            seg = np.zeros((total_len, 2), dtype=np.float32)
            seg[: s.shape[0]] = s
            music_bus += seg
        music_bus *= music_gain
        music_bus *= env2
        music_bus = _apply_fades(music_bus, sr, music_fade_in, music_fade_out)
        mix += music_bus

    # SFX bus (unity; caller can pre-level).
    for s in sfx_stems:
        seg = np.zeros((total_len, 2), dtype=np.float32)
        seg[: s.shape[0]] = s
        mix += seg

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    clipped = peak > 1.0
    # Safety limiter only if it would clip: scale to -0.5 dBTP.
    if clipped:
        mix *= (_db_to_lin(-0.5) / peak)

    if output_path is None:
        output_path = tempfile.mkstemp(prefix="premix_", suffix=".wav")[1]
    else:
        output_path = str(Path(output_path).expanduser())
    fftools.encode_wav(mix, output_path, sr=sr)

    achieved = measure_one(output_path)
    premix = {
        "path": output_path,
        "sample_rate": sr,
        "channels": 2,
        "duration_seconds": round(total_len / sr, 3),
        "peak_linear_before_limit": round(peak, 4),
        "limiter_applied": clipped,
    }
    return premix, achieved


# ---- registration ------------------------------------------------------

def register(add_tool) -> None:
    add_tool(
        "measure_loudness",
        {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        lambda params: measure(list(params["paths"])),
        "both",
        "Measure EBU R128 integrated LUFS, loudness range, and true peak per file.",
    )
    add_tool(
        "mix_plan",
        {
            "type": "object",
            "properties": {
                "dialogue": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Dialogue file(s) / track.",
                },
                "music": {"type": "array", "items": {"type": "string"}},
                "sfx": {"type": "array", "items": {"type": "string"}},
                "dialogue_lufs": {"type": "number", "default": -16.0},
                "music_bed_db": {"type": "number", "default": -18.0,
                                  "description": "Music bed level relative to dialogue LUFS."},
                "duck_db": {"type": "number", "default": -7.0},
                "ramp": {"type": "number", "default": 0.4},
                "music_fade_in": {"type": "number", "default": 1.0},
                "music_fade_out": {"type": "number", "default": 2.0},
                "output_path": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["dialogue"],
            "additionalProperties": False,
        },
        lambda params: mix_plan(
            params["dialogue"],
            music=params.get("music"),
            sfx=params.get("sfx"),
            dialogue_lufs=params.get("dialogue_lufs", -16.0),
            music_bed_db=params.get("music_bed_db", -18.0),
            duck_db=params.get("duck_db", -7.0),
            ramp=params.get("ramp", 0.4),
            music_fade_in=params.get("music_fade_in", 1.0),
            music_fade_out=params.get("music_fade_out", 2.0),
            output_path=params.get("output_path"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Plan a dialogue/music/sfx mix: dialogue normalisation gain, music bed "
        "level, and ducking windows; render a re-measured premix WAV on confirm.",
    )
