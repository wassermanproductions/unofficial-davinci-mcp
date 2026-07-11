"""Grade a whole timeline to a reference - one auto-tuned LUT per clip.

`grade_timeline` runs a windowed ``color_match`` per clip (grading the SHOT, not
the whole file) and then closes the loop the ``color-looks`` doctrine demands:
it READS the quality report and, if the grade tripped a flag (flat, washed_out,
milky, dim, noisy, clipped, banding...), retries at a lower strength
(``strength * 0.8`` each try, up to ``max_tries``, never below ``strength_floor``
= 0.5). The first attempt that passes every gate wins.

A clip that still fails after exhausting its retries is returned flagged
``needs_human`` with its BEST attempt - never silently shipped.

The result carries an application manifest for both tiers:
  - live: a clip_index -> LUT mapping to drive ``resolve_apply_lut``,
  - interchange: the ordered LUT file list with drop-on-the-node instructions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from . import color_match as _color_match

_STRENGTH_FLOOR = 0.5
_STRENGTH_DECAY = 0.8
_MAX_TRIES = 3


def _resolve_clips(
    clips: Optional[list[dict[str, Any]]], timeline_source: Optional[str]
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Return a normalised [{path, in, out}] list (in/out optional)."""
    out: list[dict[str, Any]] = []
    if clips:
        for i, c in enumerate(clips):
            if not isinstance(c, dict) or not c.get("path"):
                return [], f"clip {i} must be an object with a 'path'."
            entry: dict[str, Any] = {"path": str(Path(c["path"]).expanduser())}
            if c.get("in") is not None:
                entry["in"] = float(c["in"])
            if c.get("out") is not None:
                entry["out"] = float(c["out"])
            out.append(entry)
        return out, None
    if timeline_source:
        return [{"path": str(Path(timeline_source).expanduser())}], None
    return [], "Supply clips=[{path,in,out}] or timeline_source."


def _strength_schedule(strength: float, max_tries: int, floor: float) -> list[float]:
    schedule: list[float] = []
    s = float(strength)
    for _ in range(max_tries):
        cs = round(max(s, floor), 4)
        if schedule and schedule[-1] == cs:
            break  # clamped to the floor already; another try is identical
        schedule.append(cs)
        s = s * _STRENGTH_DECAY
    return schedule


def _target_from_clip(clip: dict[str, Any]) -> Any:
    if "in" in clip or "out" in clip:
        t: dict[str, Any] = {"path": clip["path"]}
        if "in" in clip:
            t["in_seconds"] = clip["in"]
        if "out" in clip:
            t["out_seconds"] = clip["out"]
        return t
    return clip["path"]


def grade_timeline(
    reference_image: str,
    clips: Optional[list[dict[str, Any]]] = None,
    *,
    timeline_source: Optional[str] = None,
    method: str = "lab_histogram",
    chroma: str = "preserve",
    strength: float = 1.0,
    output_dir: Optional[str] = None,
    max_tries: int = _MAX_TRIES,
    strength_floor: float = _STRENGTH_FLOOR,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Auto-tuned per-clip grade to a reference. See the module docstring."""
    if method not in {"reinhard", "lab_histogram"}:
        return {"ok": False, "error": f"Unknown method '{method}'."}
    if chroma not in {"match", "preserve"}:
        return {"ok": False, "error": f"Unknown chroma '{chroma}' (match|preserve)."}
    ref_path = str(Path(reference_image).expanduser())
    if not os.path.exists(ref_path):
        return {"ok": False, "error": "reference_image does not exist.", "path": ref_path}

    resolved, err = _resolve_clips(clips, timeline_source)
    if err:
        return {"ok": False, "error": err}
    if not resolved:
        return {"ok": False, "error": "No clips to grade."}

    strength = float(min(1.0, max(0.0, strength)))
    strength_floor = float(min(strength, max(0.0, strength_floor)))
    schedule = _strength_schedule(strength, max_tries, strength_floor)

    if output_dir:
        base_dir = str(Path(output_dir).expanduser())
    else:
        import tempfile
        base_dir = tempfile.mkdtemp(prefix="grade_timeline_")

    plan = {
        "reference_image": ref_path,
        "method": method,
        "chroma": chroma,
        "clip_count": len(resolved),
        "strength_schedule": schedule,
        "strength_floor": strength_floor,
        "output_dir": base_dir,
        "clips": resolved,
    }
    if dry_run and not confirm:
        return {
            "ok": True,
            "dry_run": True,
            "plan": plan,
            "note": "Dry run. Set dry_run=false and confirm=true to bake and auto-tune LUTs.",
        }
    if not confirm:
        return {"ok": False, "error": "confirm must be true when dry_run is false."}

    clip_results: list[dict[str, Any]] = []
    for idx, clip in enumerate(resolved, start=1):
        target = _target_from_clip(clip)
        attempts: list[dict[str, Any]] = []
        best: Optional[dict[str, Any]] = None

        for s in schedule:
            attempt_dir = os.path.join(base_dir, f"clip{idx:02d}_s{int(round(s * 100))}")
            cm = _color_match.color_match(
                ref_path, [target], method=method, chroma=chroma, strength=s,
                output_dir=attempt_dir, preview=False, dry_run=False, confirm=True,
            )
            if not cm.get("ok") or not cm.get("results"):
                attempts.append({"strength": s, "ok": False, "error": cm.get("error", "color_match failed")})
                continue
            res = cm["results"][0]
            if not res.get("ok"):
                attempts.append({"strength": s, "ok": False, "error": res.get("error", "grade failed")})
                continue
            q = res["quality"]
            attempt = {
                "strength": s,
                "ok": True,
                "acceptable": bool(q.get("acceptable")),
                "flags": list(q.get("flags", [])),
                "lut_path": res["lut_path"],
                "convergence": res.get("convergence"),
                "quality": q,
            }
            attempts.append(attempt)
            if best is None or len(attempt["flags"]) < len(best["flags"]):
                best = attempt
            if attempt["acceptable"]:
                best = attempt
                break

        if best is None:
            clip_results.append({
                "clip_index": idx,
                "path": clip["path"],
                "window": {k: clip[k] for k in ("in", "out") if k in clip} or None,
                "status": "error",
                "needs_human": True,
                "attempts": len(attempts),
                "attempt_log": attempts,
                "lut_path": None,
                "quality": None,
            })
            continue

        needs_human = not best["acceptable"]
        clip_results.append({
            "clip_index": idx,
            "path": clip["path"],
            "window": {k: clip[k] for k in ("in", "out") if k in clip} or None,
            "status": "needs_human" if needs_human else "ok",
            "needs_human": needs_human,
            "lut_path": best["lut_path"],
            "strength_used": best["strength"],
            "convergence": best["convergence"],
            "quality": best["quality"],
            "attempts": len(attempts),
            "attempt_log": attempts,
        })

    ok_clips = [c for c in clip_results if c.get("lut_path")]
    flagged = [c for c in clip_results if c.get("needs_human")]

    live_manifest = {
        "tool": "resolve_apply_lut",
        "clips": [
            {
                "clip_index": c["clip_index"],
                "lut_path": c.get("lut_path"),
                "node_index": 1,
                "needs_human": c.get("needs_human", False),
            }
            for c in ok_clips
        ],
        "note": (
            "Apply each LUT to its clip via resolve_apply_lut(lut_path, "
            "clip_indexes=[clip_index]). Review any needs_human clip before applying."
        ),
    }
    interchange_manifest = {
        "luts": [c["lut_path"] for c in ok_clips],
        "instructions": [
            "Import each .cube into Resolve's LUT folder (or point the node at it).",
            "On the Color page, add the LUT to a node on the matching clip, in "
            "clip order; exposure/balance node first, look LUT after (color-looks doctrine).",
            "Clips marked needs_human tripped a quality gate at the floor strength "
            "— eyes on them before you ship the grade.",
        ],
    }

    return {
        "ok": True,
        "dry_run": False,
        "reference_image": ref_path,
        "method": method,
        "chroma": chroma,
        "output_dir": base_dir,
        "clip_count": len(resolved),
        "graded_count": len(ok_clips),
        "needs_human_count": len(flagged),
        "strength_schedule": schedule,
        "results": clip_results,
        "apply_manifest": {"live": live_manifest, "interchange": interchange_manifest},
    }


def register(add_tool) -> None:
    add_tool(
        "grade_timeline",
        {
            "type": "object",
            "properties": {
                "reference_image": {"type": "string", "description": "Reference still/clip to grade toward."},
                "clips": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "in": {"type": "number", "minimum": 0},
                            "out": {"type": "number", "minimum": 0},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    "description": "Timeline clips to grade, each an optional source window of a file.",
                },
                "timeline_source": {"type": "string", "description": "A single media file to grade whole (instead of clips)."},
                "method": {"type": "string", "enum": ["reinhard", "lab_histogram"], "default": "lab_histogram"},
                "chroma": {"type": "string", "enum": ["match", "preserve"], "default": "preserve"},
                "strength": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 1.0},
                "output_dir": {"type": "string"},
                "max_tries": {"type": "integer", "default": _MAX_TRIES, "minimum": 1, "maximum": 6},
                "strength_floor": {"type": "number", "default": _STRENGTH_FLOOR, "minimum": 0.0, "maximum": 1.0},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["reference_image"],
            "additionalProperties": False,
        },
        lambda params: grade_timeline(
            params["reference_image"],
            params.get("clips"),
            timeline_source=params.get("timeline_source"),
            method=params.get("method", "lab_histogram"),
            chroma=params.get("chroma", "preserve"),
            strength=params.get("strength", 1.0),
            output_dir=params.get("output_dir"),
            max_tries=params.get("max_tries", _MAX_TRIES),
            strength_floor=params.get("strength_floor", _STRENGTH_FLOOR),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Grade a timeline to a reference with one auto-tuned LUT per clip: "
        "windowed color_match, then a gate-driven retry loop (strength*0.8 down "
        "to a 0.5 floor) that reads the quality report and lowers strength until "
        "the grade passes — flagging any clip that still fails as needs_human. "
        "Returns per-clip LUTs + a live/interchange apply manifest. See "
        "get_editing_knowledge('color-looks').",
    )
