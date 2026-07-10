"""Dead-air tightening for talking clips.

`tighten_dialogue` runs silencedetect with an auto-calibrated noise floor,
finds pauses longer than ``max_pause``, and removes the middle of each while
leaving ``tail`` seconds after speech and ``head`` seconds before the next
speech. It emits a cut plan (the source ranges to KEEP) that downstream
assemble/FCPXML tools turn into real NLE edits, plus an optional rendered
preview (WAV or MP4) built by concatenating the kept ranges with ffmpeg.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import fftools, media

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")
_MEAN_VOL_RE = re.compile(r"mean_volume:\s*(-?[0-9.]+)\s*dB")
_MAX_VOL_RE = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")


def _measure_noise_floor(path: str) -> tuple[float, float]:
    """Return (mean_volume_db, max_volume_db) via the volumedetect filter."""
    cmd = [
        fftools.ffmpeg_path(), "-v", "info", "-i", path,
        "-af", "volumedetect", "-f", "null", "-",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    stderr = completed.stderr or ""
    mean_m = _MEAN_VOL_RE.search(stderr)
    max_m = _MAX_VOL_RE.search(stderr)
    mean_db = float(mean_m.group(1)) if mean_m else -30.0
    max_db = float(max_m.group(1)) if max_m else 0.0
    return mean_db, max_db


def _detect_silences(path: str, noise_db: float, min_silence: float) -> tuple[list[tuple[float, float]], float]:
    dur = fftools.ffprobe_json(path).get("format", {}).get("duration")
    duration = float(dur) if dur else 0.0
    cmd = [
        fftools.ffmpeg_path(), "-v", "info", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    stderr = completed.stderr or ""
    silences: list[tuple[float, float]] = []
    cur: float | None = None
    for line in stderr.splitlines():
        ms = _SILENCE_START_RE.search(line)
        if ms:
            cur = max(0.0, float(ms.group(1)))
            continue
        me = _SILENCE_END_RE.search(line)
        if me and cur is not None:
            silences.append((cur, float(me.group(1))))
            cur = None
    if cur is not None:
        silences.append((cur, duration))
    return silences, duration


def tighten_dialogue(
    media_path: str,
    *,
    max_pause: float = 0.6,
    head: float = 0.15,
    tail: float = 0.15,
    min_cut: float = 0.4,
    noise_db: float | None = None,
    min_silence: float = 0.25,
    output_path: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    p = str(Path(media_path).expanduser())
    if not Path(p).exists():
        return {"ok": False, "error": "File does not exist.", "path": p}

    probe = media.probe_one(p)
    kind = probe.get("kind", "audio")
    if kind not in {"audio", "video"}:
        return {"ok": False, "error": f"Unsupported media kind '{kind}'.", "path": p}

    # Auto-calibrate the silence threshold from the noise floor.
    if noise_db is None:
        mean_db, max_db = _measure_noise_floor(p)
        # Put the gate a bit above the mean but well below peaks: split the
        # difference, clamped to a sane band.
        noise_db = mean_db - 6.0
        noise_db = max(min(noise_db, -18.0), -55.0)
        calib = {"mean_volume_db": mean_db, "max_volume_db": max_db, "gate_db": round(noise_db, 2)}
    else:
        calib = {"gate_db": noise_db}

    silences, duration = _detect_silences(p, noise_db, min_silence)

    # Compute removed regions (middle of long pauses, keeping handles).
    removed: list[tuple[float, float]] = []
    for s, e in silences:
        pause = e - s
        if pause <= max_pause:
            continue
        rm_start = s + tail
        rm_end = e - head
        if rm_end - rm_start >= min_cut:
            removed.append((rm_start, rm_end))

    # Keep ranges = complement of removed over [0, duration].
    keep: list[dict[str, float]] = []
    cursor = 0.0
    for rs, re_ in removed:
        if rs > cursor:
            keep.append({"start": round(cursor, 3), "end": round(rs, 3)})
        cursor = max(cursor, re_)
    if cursor < duration:
        keep.append({"start": round(cursor, 3), "end": round(duration, 3)})
    if not keep:  # nothing detected -> keep whole thing
        keep = [{"start": 0.0, "end": round(duration, 3)}]

    total_removed = round(sum(r[1] - r[0] for r in removed), 3)
    new_duration = round(sum(k["end"] - k["start"] for k in keep), 3)

    plan = {
        "ok": True,
        "dry_run": dry_run and not confirm,
        "path": p,
        "kind": kind,
        "source_duration_seconds": round(duration, 3),
        "parameters": {
            "max_pause": max_pause, "head": head, "tail": tail,
            "min_cut": min_cut, "min_silence": min_silence,
        },
        "calibration": calib,
        "keep_ranges": keep,
        "removed_ranges": [{"start": round(a, 3), "end": round(b, 3)} for a, b in removed],
        "cuts": len(removed),
        "removed_seconds": total_removed,
        "tightened_duration_seconds": new_duration,
    }

    if dry_run and not confirm:
        plan["note"] = "Set dry_run=false and confirm=true to render a cut preview."
        return plan
    if not confirm:
        return {"ok": False, "error": "confirm must be true when dry_run is false."}

    # Render preview by concatenating kept ranges.
    if len(keep) == 1 and keep[0]["start"] == 0.0 and abs(keep[0]["end"] - duration) < 1e-3:
        plan["preview"] = {"path": None, "note": "No cuts to apply; preview skipped."}
        return plan

    try:
        out = _render_preview(p, kind, keep, output_path)
        plan["preview"] = {"path": out, "kind": kind}
    except Exception as exc:
        plan["ok"] = False
        plan["error"] = f"Preview render failed: {type(exc).__name__}: {exc}"
    return plan


def _render_preview(path: str, kind: str, keep: list[dict[str, float]], output_path: str | None) -> str:
    n = len(keep)
    parts = []
    if kind == "video":
        for i, k in enumerate(keep):
            parts.append(
                f"[0:v]trim=start={k['start']}:end={k['end']},setpts=PTS-STARTPTS[v{i}];"
                f"[0:a]atrim=start={k['start']}:end={k['end']},asetpts=PTS-STARTPTS[a{i}];"
            )
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
        filtergraph = "".join(parts) + f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]"
        suffix = ".mp4"
        maps = ["-map", "[outv]", "-map", "[outa]"]
    else:
        for i, k in enumerate(keep):
            parts.append(
                f"[0:a]atrim=start={k['start']}:end={k['end']},asetpts=PTS-STARTPTS[a{i}];"
            )
        concat_inputs = "".join(f"[a{i}]" for i in range(n))
        filtergraph = "".join(parts) + f"{concat_inputs}concat=n={n}:v=0:a=1[outa]"
        suffix = ".wav"
        maps = ["-map", "[outa]"]

    if output_path is None:
        output_path = tempfile.mkstemp(prefix="tightened_", suffix=suffix)[1]
    else:
        output_path = str(Path(output_path).expanduser())

    cmd = [fftools.ffmpeg_path(), "-v", "error", "-y", "-i", path,
           "-filter_complex", filtergraph] + maps + [output_path]
    fftools.run(cmd, timeout=300, check=True)
    return output_path


def register(add_tool) -> None:
    add_tool(
        "tighten_dialogue",
        {
            "type": "object",
            "properties": {
                "media": {"type": "string", "description": "Talking clip (audio or video)."},
                "max_pause": {"type": "number", "default": 0.6},
                "head": {"type": "number", "default": 0.15},
                "tail": {"type": "number", "default": 0.15},
                "min_cut": {"type": "number", "default": 0.4},
                "noise_db": {"type": "number", "description": "Override the auto silence gate (dBFS)."},
                "min_silence": {"type": "number", "default": 0.25},
                "output_path": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["media"],
            "additionalProperties": False,
        },
        lambda params: tighten_dialogue(
            params["media"],
            max_pause=params.get("max_pause", 0.6),
            head=params.get("head", 0.15),
            tail=params.get("tail", 0.15),
            min_cut=params.get("min_cut", 0.4),
            noise_db=params.get("noise_db"),
            min_silence=params.get("min_silence", 0.25),
            output_path=params.get("output_path"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Detect and tighten dead air in a talking clip; returns a keep-range "
        "cut plan (for the NLE) plus an optional rendered preview.",
    )
