"""Interchange generators: FCPXML, EDL, and marker CSV.

These write files the user imports into DaVinci Resolve in a single action, so
they work with the free edition and whenever Resolve is not running. Output is
deterministic: stable ordering and no wall-clock timestamps, so the same input
always produces byte-identical files (golden-testable).

The FCPXML is version 1.9 and is written to import cleanly into free DaVinci
Resolve 19/20 via File > Import > Timeline.
"""

from __future__ import annotations

import csv
import io
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


FCPXML_VERSION = "1.9"


VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".avi", ".braw", ".cin", ".crm", ".dv", ".flv", ".m2ts",
    ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mxf", ".r3d", ".ts",
    ".vob", ".webm",
}
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".bwf", ".flac", ".m4a", ".mp3", ".ogg", ".wav",
    ".wma",
}
IMAGE_EXTENSIONS = {
    ".ari", ".arw", ".bmp", ".cr2", ".cr3", ".dng", ".dpx", ".exr", ".gif",
    ".heic", ".jpeg", ".jpg", ".nef", ".png", ".psd", ".raf", ".tif", ".tiff",
}


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _error(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **payload}


def _media_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return "unknown"


def _expand(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _path_report(paths: list[str]) -> dict[str, list[str]]:
    existing = [p for p in paths if Path(p).exists()]
    missing = [p for p in paths if not Path(p).exists()]
    return {"existing_paths": existing, "missing_paths": missing}


def _safe_stem(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return safe.strip("_") or "davinci_mcp"


def _default_output_dir() -> Path:
    candidates = [
        Path.home() / "Documents" / "DaVinci MCP Exports",
        Path(tempfile.gettempdir()) / "davinci-mcp-exports",
    ]
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    raise OSError("Could not create a writable interchange output directory.")


def _output_path(output_path: str | None, name: str, suffix: str) -> Path:
    if output_path:
        path = Path(output_path).expanduser().resolve()
    else:
        path = _default_output_dir() / f"{_safe_stem(name)}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _file_uri(path: str) -> str:
    return Path(path).expanduser().resolve().as_uri()


def _seconds_to_frames(seconds: float, frame_rate: int) -> int:
    return max(1, int(round(seconds * frame_rate)))


def _fcpx_time(frames: int, frame_rate: int) -> str:
    """Rational FCPXML time value in seconds."""
    return f"{int(frames)}/{int(frame_rate)}s"


def _timecode(frames: int, frame_rate: int) -> str:
    """Non-drop HH:MM:SS:FF timecode from an absolute frame count."""
    frames = max(0, int(frames))
    fps = int(round(frame_rate))
    ff = frames % fps
    total_seconds = frames // fps
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = (total_seconds // 3600) % 24
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


# --- Clip normalization ----------------------------------------------------


def _normalize_clips(
    clips: list[dict[str, Any]], frame_rate: int, default_duration_seconds: float
) -> tuple[list[dict[str, Any]], str | None]:
    """Turn loose clip dicts into a stable, fully-resolved clip list.

    Each clip may specify:
      path                (required)
      name                (defaults to file stem)
      kind                "video" | "audio" (defaults from the file extension)
      in_seconds          source in point (default 0.0)
      out_seconds         source out point (optional)
      duration_seconds    clip length (optional; derived from in/out if absent)
      record_seconds      explicit timeline placement (optional; otherwise the
                          clip tiles after the previous clip of the same kind)
    """
    normalized: list[dict[str, Any]] = []
    video_cursor = 0
    audio_cursor = 0
    for index, clip in enumerate(clips):
        raw_path = str(clip.get("path", "")).strip()
        if not raw_path:
            return [], f"clip {index} is missing a path."
        path = _expand(raw_path)

        kind = clip.get("kind") or _media_kind(path)
        if kind not in {"video", "audio", "image"}:
            kind = "video"
        # Images behave like video on the timeline.
        track_kind = "audio" if kind == "audio" else "video"

        in_seconds = float(clip.get("in_seconds", 0.0) or 0.0)
        if in_seconds < 0:
            return [], f"clip {index} in_seconds cannot be negative."

        out_seconds = clip.get("out_seconds")
        duration_seconds = clip.get("duration_seconds")
        if duration_seconds is not None:
            duration_seconds = float(duration_seconds)
        elif out_seconds is not None:
            duration_seconds = float(out_seconds) - in_seconds
        else:
            duration_seconds = float(default_duration_seconds)
        if duration_seconds <= 0:
            return [], f"clip {index} resolves to a non-positive duration."

        src_in_frames = _seconds_to_frames(in_seconds, frame_rate) if in_seconds else 0
        duration_frames = _seconds_to_frames(duration_seconds, frame_rate)

        record_seconds = clip.get("record_seconds")
        if record_seconds is not None:
            offset_frames = _seconds_to_frames(float(record_seconds), frame_rate)
            if float(record_seconds) == 0:
                offset_frames = 0
        elif track_kind == "audio":
            offset_frames = audio_cursor
        else:
            offset_frames = video_cursor

        if track_kind == "audio":
            audio_cursor = offset_frames + duration_frames
        else:
            video_cursor = offset_frames + duration_frames

        normalized.append(
            {
                "index": index,
                "path": path,
                "name": clip.get("name") or Path(path).stem,
                "kind": track_kind,
                "src_in_frames": src_in_frames,
                "duration_frames": duration_frames,
                "offset_frames": offset_frames,
                "note": str(clip.get("note", "")),
            }
        )
    return normalized, None


def _total_frames(clips: list[dict[str, Any]]) -> int:
    if not clips:
        return 1
    return max(c["offset_frames"] + c["duration_frames"] for c in clips)


# --- FCPXML ----------------------------------------------------------------


def _build_fcpxml(
    name: str,
    clips: list[dict[str, Any]],
    frame_rate: int,
    width: int,
    height: int,
    markers: list[dict[str, Any]],
) -> bytes:
    """Return indented, declaration-prefixed FCPXML bytes for the clip list."""
    fcpxml = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": f"FFVideoFormat{height}p{frame_rate}",
            "frameDuration": _fcpx_time(1, frame_rate),
            "width": str(width),
            "height": str(height),
            "colorSpace": "1-1-1 (Rec. 709)",
        },
    )

    # One asset resource per unique media path, in first-seen order.
    asset_id_by_path: dict[str, str] = {}
    next_asset = 2
    for clip in clips:
        if clip["path"] in asset_id_by_path:
            continue
        asset_id = f"r{next_asset}"
        next_asset += 1
        asset_id_by_path[clip["path"]] = asset_id
        media_file = Path(clip["path"])
        has_video = "1" if clip["kind"] == "video" else "0"
        attrs = {
            "id": asset_id,
            "name": media_file.stem,
            "src": _file_uri(clip["path"]),
            "start": "0s",
            "hasVideo": has_video,
            "format": "r1" if has_video == "1" else "",
            "hasAudio": "1",
            "audioSources": "1",
            "audioChannels": "2",
            "audioRate": "48000",
        }
        # Drop empty format attribute for audio-only assets.
        if not attrs["format"]:
            del attrs["format"]
        ET.SubElement(resources, "asset", attrs)

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", {"name": name})
    project = ET.SubElement(event, "project", {"name": name})
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": "r1",
            "duration": _fcpx_time(_total_frames(clips), frame_rate),
            "tcStart": "0s",
            "tcFormat": "NDF",
        },
    )
    spine = ET.SubElement(sequence, "spine")

    video_clips = [c for c in clips if c["kind"] == "video"]
    audio_clips = [c for c in clips if c["kind"] == "audio"]

    def _emit_markers(parent: ET.Element, clip: dict[str, Any]) -> None:
        start = clip["offset_frames"]
        end = start + clip["duration_frames"]
        for marker in markers:
            frame = int(marker.get("frame", 0))
            if start <= frame < end:
                ET.SubElement(
                    parent,
                    "marker",
                    {
                        "start": _fcpx_time(
                            clip["src_in_frames"] + (frame - start), frame_rate
                        ),
                        "duration": _fcpx_time(1, frame_rate),
                        "value": str(marker.get("name", "Marker")),
                        "note": str(marker.get("note", "")),
                    },
                )

    # Video clips tile the spine; audio clips attach as connected (lane -1)
    # clips so they layer under the video rather than sharing its track.
    spine_clip_elements: list[ET.Element] = []
    for clip in video_clips:
        element = ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": clip["name"],
                "ref": asset_id_by_path[clip["path"]],
                "offset": _fcpx_time(clip["offset_frames"], frame_rate),
                "start": _fcpx_time(clip["src_in_frames"], frame_rate),
                "duration": _fcpx_time(clip["duration_frames"], frame_rate),
            },
        )
        _emit_markers(element, clip)
        spine_clip_elements.append(element)

    if audio_clips:
        # Anchor audio to the first spine clip when one exists; otherwise the
        # audio clips tile the spine themselves.
        anchor = spine_clip_elements[0] if spine_clip_elements else spine
        anchor_offset = video_clips[0]["offset_frames"] if spine_clip_elements else 0
        for clip in audio_clips:
            attrs = {
                "name": clip["name"],
                "ref": asset_id_by_path[clip["path"]],
                "offset": _fcpx_time(clip["offset_frames"], frame_rate),
                "start": _fcpx_time(clip["src_in_frames"], frame_rate),
                "duration": _fcpx_time(clip["duration_frames"], frame_rate),
                "audioRole": "dialogue" if anchor is not spine else "music",
            }
            if anchor is not spine:
                attrs["lane"] = "-1"
                # A connected clip's offset is relative to the parent's start.
                attrs["offset"] = _fcpx_time(
                    max(0, clip["offset_frames"] - anchor_offset), frame_rate
                )
            element = ET.SubElement(anchor, "asset-clip", attrs)
            _emit_markers(element, clip)

    # Serialize deterministically with a stable declaration and DOCTYPE.
    tree = ET.ElementTree(fcpxml)
    ET.indent(tree, space="  ")
    buffer = io.BytesIO()
    tree.write(buffer, encoding="utf-8", xml_declaration=True)
    text = buffer.getvalue().decode("utf-8")
    text = text.replace("?>\n", "?>\n<!DOCTYPE fcpxml>\n\n", 1)
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def generate_fcpxml(
    name: str,
    clips: list[dict[str, Any]],
    output_path: str | None = None,
    frame_rate: int = 24,
    width: int = 1920,
    height: int = 1080,
    clip_duration_seconds: float = 5.0,
    markers: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Generate an FCPXML 1.9 timeline for import into DaVinci Resolve."""
    if not clips:
        return _error("clips must contain at least one clip.")
    if frame_rate <= 0 or frame_rate > 240:
        return _error("frame_rate must be between 1 and 240.", frame_rate=frame_rate)
    if width <= 0 or height <= 0:
        return _error("width and height must be positive.", width=width, height=height)
    if clip_duration_seconds <= 0:
        return _error(
            "clip_duration_seconds must be greater than 0.",
            clip_duration_seconds=clip_duration_seconds,
        )

    normalized, err = _normalize_clips(clips, frame_rate, clip_duration_seconds)
    if err:
        return _error(err)

    all_markers = markers or []
    destination = _output_path(output_path, name, ".fcpxml")
    report = _path_report([c["path"] for c in normalized])
    plan = {
        "action": "generate_fcpxml",
        "mode": "interchange",
        "timeline_name": name,
        "output_path": str(destination),
        "frame_rate": frame_rate,
        "width": width,
        "height": height,
        "clip_count": len(normalized),
        "total_frames": _total_frames(normalized),
        "markers": all_markers,
        **report,
    }
    if dry_run:
        return _ok(dry_run=True, plan=plan)
    if report["missing_paths"]:
        return _error("Cannot generate FCPXML with missing media files.", **plan)

    data = _build_fcpxml(name, normalized, frame_rate, width, height, all_markers)
    destination.write_bytes(data)
    return _ok(
        dry_run=False,
        artifact_type="fcpxml",
        import_instructions=[
            "Open DaVinci Resolve.",
            "File > Import > Timeline > Import AAF, EDL, XML...",
            f"Select {destination}.",
        ],
        **plan,
    )


# --- EDL (CM3600) ----------------------------------------------------------


def _build_edl(name: str, clips: list[dict[str, Any]], frame_rate: int) -> str:
    """Return a CM3600 EDL string for the (video) clips."""
    lines = [f"TITLE: {name}", "FCM: NON-DROP FRAME", ""]
    event = 0
    for clip in clips:
        if clip["kind"] != "video":
            continue
        event += 1
        src_in = clip["src_in_frames"]
        src_out = src_in + clip["duration_frames"]
        rec_in = clip["offset_frames"]
        rec_out = rec_in + clip["duration_frames"]
        lines.append(
            f"{event:03d}  AX       V     C        "
            f"{_timecode(src_in, frame_rate)} {_timecode(src_out, frame_rate)} "
            f"{_timecode(rec_in, frame_rate)} {_timecode(rec_out, frame_rate)}"
        )
        lines.append(f"* FROM CLIP NAME: {clip['name']}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def generate_edl(
    name: str,
    clips: list[dict[str, Any]],
    output_path: str | None = None,
    frame_rate: int = 24,
    clip_duration_seconds: float = 5.0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Generate a CM3600 EDL for the video clips in the plan."""
    if not clips:
        return _error("clips must contain at least one clip.")
    if frame_rate <= 0 or frame_rate > 240:
        return _error("frame_rate must be between 1 and 240.", frame_rate=frame_rate)

    normalized, err = _normalize_clips(clips, frame_rate, clip_duration_seconds)
    if err:
        return _error(err)
    video_clips = [c for c in normalized if c["kind"] == "video"]
    if not video_clips:
        return _error("EDL export needs at least one video clip.")

    destination = _output_path(output_path, name, ".edl")
    report = _path_report([c["path"] for c in video_clips])
    plan = {
        "action": "generate_edl",
        "mode": "interchange",
        "timeline_name": name,
        "output_path": str(destination),
        "frame_rate": frame_rate,
        "clip_count": len(video_clips),
        **report,
    }
    if dry_run:
        return _ok(dry_run=True, plan=plan)

    destination.write_text(_build_edl(name, normalized, frame_rate), encoding="utf-8")
    return _ok(
        dry_run=False,
        artifact_type="edl",
        import_instructions=[
            "Open DaVinci Resolve.",
            "File > Import > Timeline > Import AAF, EDL, XML...",
            f"Select {destination}. Relink the source media when prompted.",
        ],
        **plan,
    )


# --- Marker CSV ------------------------------------------------------------


_MARKER_FIELDS = ["frame", "timecode", "seconds", "name", "color", "note", "duration"]


def generate_marker_csv(
    markers: list[dict[str, Any]],
    output_path: str | None = None,
    name: str = "Markers",
    frame_rate: int = 24,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Generate a deterministic marker manifest CSV."""
    if not markers:
        return _error("markers must contain at least one marker.")
    if frame_rate <= 0 or frame_rate > 240:
        return _error("frame_rate must be between 1 and 240.", frame_rate=frame_rate)

    normalized = []
    for marker in sorted(markers, key=lambda m: int(m.get("frame", 0))):
        frame = max(0, int(marker.get("frame", 0)))
        normalized.append(
            {
                "frame": frame,
                "timecode": _timecode(frame, frame_rate),
                "seconds": f"{frame / frame_rate:.3f}",
                "name": str(marker.get("name", "Marker")),
                "color": str(marker.get("color", "Blue")),
                "note": str(marker.get("note", "")),
                "duration": int(marker.get("duration", 1)),
            }
        )

    destination = _output_path(output_path, name, ".markers.csv")
    plan = {
        "action": "generate_marker_csv",
        "mode": "interchange",
        "output_path": str(destination),
        "name": name,
        "frame_rate": frame_rate,
        "marker_count": len(normalized),
        "markers": normalized,
    }
    if dry_run:
        return _ok(dry_run=True, plan=plan)

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_MARKER_FIELDS)
        writer.writeheader()
        writer.writerows(normalized)

    return _ok(
        dry_run=False,
        artifact_type="marker_csv",
        import_instructions=[
            "Use this CSV as a marker manifest.",
            "For live marker insertion, use DaVinci Resolve Studio (live tier).",
        ],
        **plan,
    )
