"""Edit-plan executor (interchange side).

Validates an edit plan against probed media, optionally snaps cut points to a
beat grid, and emits a normalised plan. File output (FCPXML/EDL) is produced by
the core package's interchange generators; `to_fcpxml` / `to_edl` are lazy
integration points that call them when present.

Plan schema (all times in seconds):
  {
    "timeline": {"name": str, "fps": float, "resolution": "WxH"},
    "video": [{"path": str, "in": float, "out": float, "speed"?: float}, ...],
    "audio": [{"path": str, "in": float, "out": float,
               "gain_db"?: float, "fade_in"?: float, "fade_out"?: float}, ...],
    "markers": [{"time": float, "name"?: str, "color"?: str, "note"?: str}, ...],
    "beat_snap": {"grid": [float,...] | str, "tolerance": float}?
  }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import beat_grid as bg
from . import media


def _resolve_grid(beat_snap: dict[str, Any]) -> tuple[list[float], float, dict[str, Any]]:
    """Return (beat_times, tolerance, info). ``grid`` may be a list of times or
    a path to an audio file analysed with beat_grid."""
    tolerance = float(beat_snap.get("tolerance", 0.05))
    grid = beat_snap.get("grid")
    info: dict[str, Any] = {"tolerance": tolerance}
    if isinstance(grid, str):
        result = bg.beat_grid(str(Path(grid).expanduser()))
        if not result.get("ok"):
            info["error"] = f"Grid analysis failed: {result.get('error')}"
            return [], tolerance, info
        info["source"] = grid
        info["bpm"] = result.get("bpm")
        info["method"] = result.get("method")
        return list(result["beat_times"]), tolerance, info
    if isinstance(grid, list):
        info["source"] = "inline"
        return [float(x) for x in grid], tolerance, info
    return [], tolerance, info


def _snap(value: float, beats: list[float], tolerance: float) -> tuple[float, bool]:
    if not beats:
        return value, False
    nearest = min(beats, key=lambda b: abs(b - value))
    if abs(nearest - value) <= tolerance:
        return float(nearest), True
    return value, False


def _validate_clip(clip: dict[str, Any], *, kind: str, probe_cache: dict[str, dict]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    path = clip.get("path")
    if not path:
        errors.append("clip missing 'path'")
        return {"errors": errors, "warnings": warnings}
    ap = str(Path(path).expanduser())
    if ap not in probe_cache:
        probe_cache[ap] = media.probe_one(ap)
    probe = probe_cache[ap]

    if not probe.get("ok"):
        errors.append(f"unreadable media: {probe.get('error')}")
        return {"errors": errors, "warnings": warnings, "probe": probe}

    if kind == "video" and probe.get("kind") not in {"video", "image"}:
        warnings.append(f"expected video, media kind is '{probe.get('kind')}'")
    if kind == "audio" and probe.get("kind") not in {"audio", "video"}:
        warnings.append(f"expected audio, media kind is '{probe.get('kind')}'")

    in_t = float(clip.get("in", 0.0))
    out_t = clip.get("out")
    dur = probe.get("duration_seconds")
    if out_t is None:
        out_t = dur if dur else in_t
    out_t = float(out_t)
    if in_t < 0:
        errors.append("in < 0")
    if out_t <= in_t and probe.get("kind") != "image":
        errors.append(f"out ({out_t}) <= in ({in_t})")
    if dur is not None and out_t - dur > 0.05:
        warnings.append(f"out ({out_t}) beyond media duration ({round(dur,3)})")

    return {"errors": errors, "warnings": warnings, "in": in_t, "out": out_t, "probe": probe}


def assemble_edit(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise an edit plan. Never raises on bad input."""
    if not isinstance(plan, dict):
        return {"ok": False, "error": "plan must be an object."}

    timeline = plan.get("timeline") or {}
    tl = {
        "name": timeline.get("name", "Timeline"),
        "fps": float(timeline.get("fps", 24.0)),
        "resolution": timeline.get("resolution", "1920x1080"),
    }

    beat_snap = plan.get("beat_snap")
    beats: list[float] = []
    tolerance = 0.0
    snap_info: dict[str, Any] = {}
    if beat_snap:
        beats, tolerance, snap_info = _resolve_grid(beat_snap)

    probe_cache: dict[str, dict] = {}
    all_errors: list[str] = []
    all_warnings: list[str] = []
    snaps: list[dict[str, Any]] = []

    def _process(clips: list[dict], kind: str) -> list[dict]:
        out = []
        for idx, clip in enumerate(clips or []):
            v = _validate_clip(clip, kind=kind, probe_cache=probe_cache)
            for e in v["errors"]:
                all_errors.append(f"{kind}[{idx}]: {e}")
            for w in v["warnings"]:
                all_warnings.append(f"{kind}[{idx}]: {w}")
            if v["errors"]:
                continue
            in_t, out_t = v["in"], v["out"]
            norm = {
                "path": str(Path(clip["path"]).expanduser()),
                "in": in_t,
                "out": out_t,
            }
            if kind == "video" and clip.get("speed") is not None:
                norm["speed"] = float(clip["speed"])
            if kind == "audio":
                for opt in ("gain_db", "fade_in", "fade_out"):
                    if clip.get(opt) is not None:
                        norm[opt] = float(clip[opt])
            # Beat snapping.
            if beats:
                for field in ("in", "out"):
                    snapped, did = _snap(norm[field], beats, tolerance)
                    if did and abs(snapped - norm[field]) > 1e-9:
                        snaps.append({
                            "clip": f"{kind}[{idx}]", "field": field,
                            "from": round(norm[field], 4), "to": round(snapped, 4),
                        })
                        norm[field] = round(snapped, 4)
            out.append(norm)
        return out

    video = _process(plan.get("video", []), "video")
    audio = _process(plan.get("audio", []), "audio")

    markers = []
    for idx, m in enumerate(plan.get("markers", []) or []):
        if "time" not in m:
            all_warnings.append(f"marker[{idx}]: missing 'time'")
            continue
        t = float(m["time"])
        if beats:
            snapped, did = _snap(t, beats, tolerance)
            if did:
                t = round(snapped, 4)
        markers.append({
            "time": t,
            "name": m.get("name", f"Marker {idx+1}"),
            "color": m.get("color", "Blue"),
            "note": m.get("note", ""),
        })

    # Timeline duration = sum of video out-in (assumes sequential lay-down).
    video_dur = round(sum(c["out"] - c["in"] for c in video), 4)
    audio_dur = round(sum(c["out"] - c["in"] for c in audio), 4)

    normalized = {
        "timeline": tl,
        "video": video,
        "audio": audio,
        "markers": markers,
        "video_duration_seconds": video_dur,
        "audio_duration_seconds": audio_dur,
    }

    return {
        "ok": len(all_errors) == 0,
        "normalized_plan": normalized,
        "errors": all_errors,
        "warnings": all_warnings,
        "beat_snap": snap_info if beat_snap else None,
        "snapped": snaps,
        "counts": {"video": len(video), "audio": len(audio), "markers": len(markers)},
    }


# ---- interchange integration points -----------------------------------

def _parse_resolution(res: str) -> tuple[int, int]:
    try:
        w, h = str(res).lower().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _plan_to_interchange_clips(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate my normalized video/audio clips into the interchange clip
    dict shape (path, in_seconds, out_seconds, kind)."""
    clips: list[dict[str, Any]] = []
    for c in normalized.get("video", []):
        clips.append({
            "path": c["path"], "in_seconds": c["in"], "out_seconds": c["out"],
            "kind": "video",
        })
    for c in normalized.get("audio", []):
        clips.append({
            "path": c["path"], "in_seconds": c["in"], "out_seconds": c["out"],
            "kind": "audio",
        })
    return clips


def _markers_to_interchange(normalized: dict[str, Any], fps: float) -> list[dict[str, Any]]:
    out = []
    for m in normalized.get("markers", []):
        out.append({
            "frame": int(round(float(m["time"]) * fps)),
            "name": m.get("name", "Marker"),
            "note": m.get("note", ""),
            "color": m.get("color", "Blue"),
        })
    return out


def to_fcpxml(plan: dict[str, Any], output_path: str | None = None, *, dry_run: bool = True) -> dict[str, Any]:
    """Generate FCPXML via the core package's interchange generator.

    Integration point: the core agent's ``davinci_mcp.tools_interchange`` owns
    the FCPXML 1.9 generator. We validate/normalise here and delegate there,
    adapting to its ``generate_fcpxml(name, clips, ...)`` signature.
    """
    validated = assemble_edit(plan)
    if not validated["ok"]:
        return {"ok": False, "error": "Plan failed validation.", "detail": validated}
    norm = validated["normalized_plan"]
    try:
        from davinci_mcp import tools_interchange  # type: ignore
    except Exception:
        return {
            "ok": False,
            "error": "FCPXML generator not yet available (davinci_mcp.tools_interchange).",
            "normalized_plan": norm,
            "integration_point": "davinci_mcp.tools_interchange.generate_fcpxml(name, clips, ...)",
        }
    fn = getattr(tools_interchange, "generate_fcpxml", None)
    if not callable(fn):
        return {"ok": False, "error": "tools_interchange present but no generate_fcpxml found.",
                "normalized_plan": norm}
    fps = int(round(norm["timeline"]["fps"]))
    width, height = _parse_resolution(norm["timeline"]["resolution"])
    return fn(  # type: ignore
        norm["timeline"]["name"],
        _plan_to_interchange_clips(norm),
        output_path,
        frame_rate=fps,
        width=width,
        height=height,
        markers=_markers_to_interchange(norm, fps),
        dry_run=dry_run,
    )


def to_edl(plan: dict[str, Any], output_path: str | None = None, *, dry_run: bool = True) -> dict[str, Any]:
    validated = assemble_edit(plan)
    if not validated["ok"]:
        return {"ok": False, "error": "Plan failed validation.", "detail": validated}
    norm = validated["normalized_plan"]
    try:
        from davinci_mcp import tools_interchange  # type: ignore
    except Exception:
        return {
            "ok": False,
            "error": "EDL generator not yet available (davinci_mcp.tools_interchange).",
            "normalized_plan": norm,
            "integration_point": "davinci_mcp.tools_interchange.generate_edl(name, clips, ...)",
        }
    fn = getattr(tools_interchange, "generate_edl", None)
    if not callable(fn):
        return {"ok": False, "error": "tools_interchange present but no generate_edl found.",
                "normalized_plan": norm}
    fps = int(round(norm["timeline"]["fps"]))
    return fn(  # type: ignore
        norm["timeline"]["name"],
        _plan_to_interchange_clips(norm),
        output_path,
        frame_rate=fps,
        dry_run=dry_run,
    )


def register(add_tool) -> None:
    _plan_schema = {
        "type": "object",
        "properties": {
            "timeline": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "fps": {"type": "number"},
                    "resolution": {"type": "string"},
                },
            },
            "video": {"type": "array", "items": {"type": "object"}},
            "audio": {"type": "array", "items": {"type": "object"}},
            "markers": {"type": "array", "items": {"type": "object"}},
            "beat_snap": {
                "type": "object",
                "properties": {
                    "grid": {"oneOf": [
                        {"type": "array", "items": {"type": "number"}},
                        {"type": "string"},
                    ]},
                    "tolerance": {"type": "number", "default": 0.05},
                },
            },
        },
        "additionalProperties": True,
    }
    add_tool(
        "assemble_edit",
        {
            "type": "object",
            "properties": {"plan": _plan_schema},
            "required": ["plan"],
            "additionalProperties": False,
        },
        lambda params: assemble_edit(params["plan"]),
        "both",
        "Validate an edit plan against probed media and optionally snap cut "
        "points to a beat grid; returns a normalised plan for interchange/live.",
    )
