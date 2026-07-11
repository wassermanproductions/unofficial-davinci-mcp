"""One-command music-driven auto-edit: footage folder + a song -> a cut timeline.

`auto_edit` is pure glue over the existing engines, executing the doctrine in
``skills/beat-cutting.md`` deterministically:

  1. scan/probe the footage (video only) for usable ranges,
  2. `beat_grid` the music for beats + per-onset strengths,
  3. `cut_music` to land the track on a musical boundary at ``target_seconds``,
  4. build a shot plan whose CUT DENSITY tracks the song's energy (onset
     strength per beat) - long holds on low-energy sections, 1-2 beat cuts on
     high-energy ones - with cuts landing ON beats and a shot-variety rule that
     never places two adjacent segments from the same source region,
  5. hand the plan to `assemble_edit` (beat-snapped) and, on confirm, write an
     FCPXML via the interchange generator; the same plan is shaped so the live
     tier can feed ``resolve_create_timeline`` directly.

Determinism: shot selection is seeded purely by file order and a per-clip
source cursor - no RNG anywhere, so the same inputs always produce the same cut.

Every cut carries a human-readable rationale ('chorus: 1-beat cuts', ...).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import assemble as _assemble
from . import beat_grid as _beat_grid
from . import media as _media
from . import music_cut as _music_cut

# Usable-range guard: never cut into the first/last of a clip (roll-in / tail).
_EDGE_GUARD = 0.25
# Minimum source-time separation when the same clip is reused (variety rule).
_MIN_SOURCE_SEP = 0.5

# Style presets. ``density`` maps a beat's normalised energy (0..1) to a shot
# length in BEATS: (energy_threshold, beats_per_shot), highest threshold first.
# ``sting`` is the music-cut ending; ``snap_tolerance_s`` is the beat-snap window.
_STYLES: dict[str, dict[str, Any]] = {
    "music_video": {
        "sting": "button",
        "density": [(0.66, 1), (0.33, 2), (0.0, 3)],
        "label": "music video",
    },
    "montage": {
        "sting": "tail",
        "density": [(0.66, 2), (0.33, 3), (0.0, 4)],
        "label": "montage",
    },
    "trailer": {
        "sting": "button",
        "density": [(0.66, 1), (0.33, 1), (0.0, 2)],
        "label": "trailer",
    },
}

# Energy tier -> human section name used in each shot's rationale.
_TIER_NAME = {"high": "chorus/drop", "mid": "verse", "low": "intro/breakdown"}


def _energy_tier(energy: float) -> str:
    if energy >= 0.66:
        return "high"
    if energy >= 0.33:
        return "mid"
    return "low"


def _beats_per_shot(energy: float, style: dict[str, Any]) -> int:
    for threshold, beats in style["density"]:
        if energy >= threshold:
            return beats
    return style["density"][-1][1]


def _gather_footage(
    media_dir: Optional[str], clips: Optional[list[str]]
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Return (video_probes, error). Video-only; keeps caller/scan order."""
    probes: list[dict[str, Any]] = []
    if clips:
        result = _media.probe_media([str(Path(c).expanduser()) for c in clips])
        probes = result.get("media", [])
    elif media_dir:
        result = _media.scan_media_folder(media_dir)
        if not result.get("ok"):
            return [], result.get("error", "Could not scan media_dir.")
        probes = result.get("media", [])
    else:
        return [], "Supply either media_dir or clips[]."

    videos = [
        p
        for p in probes
        if p.get("ok")
        and p.get("kind") == "video"
        and (p.get("duration_seconds") or 0) > 2 * _EDGE_GUARD
    ]
    if not videos:
        return [], "No usable video footage found (need clips longer than 0.5s)."
    return videos, None


def _clip_states(videos: list[dict[str, Any]], fps: float) -> list[dict[str, Any]]:
    states = []
    for v in videos:
        dur = float(v["duration_seconds"])
        usable_start = _EDGE_GUARD
        usable_end = max(usable_start + 1.0 / fps, dur - _EDGE_GUARD)
        states.append(
            {
                "path": v["path"],
                "fps": v.get("fps") or fps,
                "duration": dur,
                "usable_start": usable_start,
                "usable_end": usable_end,
                "usable_len": usable_end - usable_start,
                "cursor": usable_start,
            }
        )
    return states


def _beat_energy(
    beats: list[float], onset_times: list[float], onset_strengths: list[float]
) -> list[float]:
    """Per-beat-interval energy in [0,1] from onset strengths within each gap."""
    energy: list[float] = []
    for i in range(len(beats) - 1):
        lo, hi = beats[i], beats[i + 1]
        strengths = [
            s for t, s in zip(onset_times, onset_strengths) if lo <= t < hi
        ]
        energy.append(max(strengths) if strengths else 0.0)
    peak = max(energy) if energy else 0.0
    if peak > 1e-9:
        energy = [e / peak for e in energy]
    else:
        energy = [0.5 for _ in energy]  # no onset info -> neutral mid density
    return energy


def _pick_clip(
    states: list[dict[str, Any]],
    rotation: int,
    span: float,
    last_path: Optional[str],
) -> tuple[int, dict[str, Any]]:
    """Round-robin from ``rotation`` for a clip that can supply ``span`` seconds
    and differs from the previous shot's clip when possible (variety)."""
    n = len(states)
    # First pass: enough room AND a different source than the last shot.
    for step in range(n):
        idx = (rotation + step) % n
        st = states[idx]
        if st["usable_len"] + 1e-6 >= span and st["path"] != last_path:
            return idx, st
    # Second pass: enough room, allow repeating the same source (new region).
    for step in range(n):
        idx = (rotation + step) % n
        st = states[idx]
        if st["usable_len"] + 1e-6 >= span:
            return idx, st
    # Fallback: the largest clip (span was capped to maxspan, so this fits).
    idx = max(range(n), key=lambda i: states[i]["usable_len"])
    return idx, states[idx]


def _source_range(st: dict[str, Any], span: float) -> tuple[float, float]:
    """Advance the clip's cursor and return an (in, out) source window of length
    ``span`` (clamped to the usable range), wrapping with a min separation so a
    reused clip never repeats the same region back-to-back."""
    us, ue = st["usable_start"], st["usable_end"]
    length = min(span, st["usable_len"])
    start = st["cursor"]
    if start + length > ue + 1e-6:
        start = us  # wrap to the top of the usable range
    out = start + length
    advance = max(length, _MIN_SOURCE_SEP)
    st["cursor"] = start + advance
    if st["cursor"] >= ue - 1e-6:
        st["cursor"] = us
    return round(start, 4), round(out, 4)


def _plan_shots(
    beats: list[float],
    energy: list[float],
    style: dict[str, Any],
    states: list[dict[str, Any]],
    fps: float,
) -> list[dict[str, Any]]:
    """Group beats into shots by energy, assign a source per the variety rule.

    Cut points land on beat times; timeline placement tiles sequentially so the
    cumulative cut positions land on beats too (each span is an integer number
    of beat gaps). Shots that fall on a moving beat get a 1-frame lead per the
    'motion arrives on the beat' doctrine - expressed as a note, applied by the
    live/interchange snap, never large enough to leave the +/-2 frame window.
    """
    maxspan = max(st["usable_len"] for st in states)
    frame = 1.0 / fps
    shots: list[dict[str, Any]] = []
    rotation = 0
    last_path: Optional[str] = None
    timeline_pos = 0.0
    i = 0
    n_beats = len(beats)
    while i < n_beats - 1:
        k = _beats_per_shot(energy[i], style)
        # Cap the span so at least one clip can supply it (keeps beats aligned).
        while k >= 1 and i + k < n_beats and (beats[i + k] - beats[i]) > maxspan + 1e-6:
            k -= 1
        k = max(1, k)
        j = min(i + k, n_beats - 1)
        span = beats[j] - beats[i]
        if span <= 1e-6:
            i += 1
            continue

        idx, st = _pick_clip(states, rotation, span, last_path)
        rotation = idx + 1
        src_in, src_out = _source_range(st, span)
        tier = _energy_tier(energy[i])

        shots.append(
            {
                "index": len(shots),
                "source": st["path"],
                "in": src_in,
                "out": src_out,
                "timeline_in": round(timeline_pos, 4),
                "timeline_out": round(timeline_pos + span, 4),
                "beats": k,
                "span_seconds": round(span, 4),
                "energy": round(energy[i], 3),
                "section": _TIER_NAME[tier],
                "rationale": f"{_TIER_NAME[tier]}: {k}-beat "
                + ("cut" if k == 1 else "hold"),
                "motion_lead_frames": 1,  # nudge picture 1 frame early on the beat
            }
        )
        timeline_pos += span
        last_path = st["path"]
        i = j
    return shots


def auto_edit(
    music: str,
    target_seconds: float,
    *,
    media_dir: Optional[str] = None,
    clips: Optional[list[str]] = None,
    style: str = "music_video",
    output: str = "fcpxml",
    fps: int = 24,
    resolution: str = "1920x1080",
    timeline_name: Optional[str] = None,
    output_path: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Assemble a beat-synced edit from a footage folder (or clip list) + a song.

    See the module docstring for the pipeline. Returns the normalised assemble
    plan, an FCPXML plan/file, live-tier clips for ``resolve_create_timeline``,
    and a per-shot cut list with rationale.
    """
    if style not in _STYLES:
        return {
            "ok": False,
            "error": f"Unknown style '{style}'. Choose one of "
            f"{sorted(_STYLES)}.",
        }
    if output not in {"fcpxml", "plan"}:
        return {"ok": False, "error": "output must be 'fcpxml' or 'plan'."}
    if target_seconds <= 0:
        return {"ok": False, "error": "target_seconds must be positive."}
    music_path = str(Path(music).expanduser())
    if not Path(music_path).exists():
        return {"ok": False, "error": "Music file does not exist.", "path": music_path}

    style_cfg = _STYLES[style]

    videos, err = _gather_footage(media_dir, clips)
    if err:
        return {"ok": False, "error": err}

    grid = _beat_grid.beat_grid(music_path)
    if not grid.get("ok"):
        return {"ok": False, "error": f"Beat analysis failed: {grid.get('error')}"}

    # Cut the music to a musical boundary at/around the target (plan only here;
    # the caller renders the stung WAV via cut_music when they want the audio).
    mc = _music_cut.cut_music(
        music_path, target_seconds, sting=style_cfg["sting"], dry_run=True
    )
    if not mc.get("ok"):
        return {"ok": False, "error": f"Music cut failed: {mc.get('error')}"}
    end_time = float(mc["end_time"])

    beats = [b for b in grid["beat_times"] if b <= end_time + 1e-6]
    if len(beats) < 2:
        return {
            "ok": False,
            "error": "Not enough beats within the target length to build a cut.",
            "beat_count": len(beats),
        }

    energy = _beat_energy(beats, grid["onset_times"], grid["onset_strengths"])
    states = _clip_states(videos, float(fps))
    shots = _plan_shots(beats, energy, style_cfg, states, float(fps))
    if not shots:
        return {"ok": False, "error": "Could not build any shots from the footage."}

    picture_seconds = round(sum(s["span_seconds"] for s in shots), 4)
    total_usable = round(sum(st["usable_len"] for st in states), 4)

    # Safety: footage shorter than the target -> we already reuse clips, but the
    # picture may still run short of the music if even reuse can't fill it.
    footage_short = total_usable < end_time - 1e-6

    name = timeline_name or f"Auto edit ({style_cfg['label']})"

    # Build the assemble plan: video shots tile the spine, the music sits under.
    video_clips = [
        {"path": s["source"], "in": s["in"], "out": s["out"]} for s in shots
    ]
    audio_clips = [
        {"path": music_path, "in": 0.0, "out": round(end_time, 4), "fade_out": round(
            mc["cut"].get("fade_seconds", 0.0), 4
        )}
    ]
    section_markers = []
    prev_tier = None
    for s in shots:
        tier = s["section"]
        if tier != prev_tier:
            section_markers.append(
                {"time": s["timeline_in"], "name": tier, "note": s["rationale"]}
            )
            prev_tier = tier

    plan = {
        "timeline": {"name": name, "fps": float(fps), "resolution": resolution},
        "video": video_clips,
        "audio": audio_clips,
        "markers": section_markers,
        "beat_snap": {"grid": [round(b, 4) for b in beats], "tolerance": 2.0 / fps},
    }

    validated = _assemble.assemble_edit(plan)
    if not validated.get("ok"):
        return {
            "ok": False,
            "error": "Internal assemble validation failed.",
            "detail": validated,
        }

    # Live-tier clips: directly feedable to resolve_create_timeline.
    live_clips = [
        {
            "path": s["source"],
            "in_seconds": s["in"],
            "out_seconds": s["out"],
            "fps": next(
                (st["fps"] for st in states if st["path"] == s["source"]), float(fps)
            ),
        }
        for s in shots
    ]

    result: dict[str, Any] = {
        "ok": True,
        "style": style,
        "music": music_path,
        "target_seconds": round(target_seconds, 4),
        "music_end_seconds": round(end_time, 4),
        "bpm": grid["bpm"],
        "fps": fps,
        "shot_count": len(shots),
        "picture_seconds": picture_seconds,
        "footage_usable_seconds": total_usable,
        "cut_list": shots,
        "assemble_plan": plan,
        "normalized_plan": validated["normalized_plan"],
        "live_plan": {
            "timeline_name": name,
            "fps": fps,
            "clips": live_clips,
            "music": {"path": music_path, "in_seconds": 0.0, "out_seconds": round(end_time, 4)},
            "markers": section_markers,
        },
        "music_cut": mc["cut"],
    }
    if footage_short:
        result["warning"] = (
            f"Footage usable length ({total_usable:.2f}s) is shorter than the "
            f"music target ({end_time:.2f}s); clips are reused and the picture "
            f"runs {picture_seconds:.2f}s. Add footage or lower target_seconds."
        )

    if output == "plan":
        result["dry_run"] = True
        result["note"] = "output='plan': no file written. Use output='fcpxml' to export."
        return result

    # output == 'fcpxml': gated file write via the interchange generator.
    result["dry_run"] = dry_run and not confirm
    fcpxml = _assemble.to_fcpxml(plan, output_path, dry_run=dry_run and not confirm)
    result["fcpxml"] = fcpxml
    if not fcpxml.get("ok"):
        result["ok"] = False
        result["error"] = "FCPXML generation failed."
    elif dry_run and not confirm:
        result["note"] = "Dry run. Set dry_run=false and confirm=true to write the FCPXML."
    return result


def register(add_tool) -> None:
    add_tool(
        "auto_edit",
        {
            "type": "object",
            "properties": {
                "music": {"type": "string", "description": "Song to cut to (audio/video)."},
                "target_seconds": {"type": "number", "minimum": 0.1},
                "media_dir": {"type": "string", "description": "Folder of footage to draw shots from."},
                "clips": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit clip list instead of media_dir.",
                },
                "style": {
                    "type": "string",
                    "enum": ["music_video", "montage", "trailer"],
                    "default": "music_video",
                },
                "output": {"type": "string", "enum": ["fcpxml", "plan"], "default": "fcpxml"},
                "fps": {"type": "integer", "default": 24, "minimum": 1, "maximum": 240},
                "resolution": {"type": "string", "default": "1920x1080"},
                "timeline_name": {"type": "string"},
                "output_path": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["music", "target_seconds"],
            "additionalProperties": False,
        },
        lambda params: auto_edit(
            params["music"],
            params["target_seconds"],
            media_dir=params.get("media_dir"),
            clips=params.get("clips"),
            style=params.get("style", "music_video"),
            output=params.get("output", "fcpxml"),
            fps=params.get("fps", 24),
            resolution=params.get("resolution", "1920x1080"),
            timeline_name=params.get("timeline_name"),
            output_path=params.get("output_path"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "One-command music-driven auto-edit: scan footage, beat-grid the song, "
        "cut it to length, and build a beat-synced shot plan whose cut density "
        "tracks the song's energy (long holds on low-energy sections, 1-2 beat "
        "cuts on high) with a no-adjacent-same-source variety rule. Returns an "
        "assemble plan + FCPXML and live-tier clips, each cut carrying a "
        "rationale. See get_editing_knowledge('beat-cutting').",
    )
