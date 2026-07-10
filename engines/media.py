"""Media probing: ffprobe JSON -> structured, stable metadata.

`probe_media(paths)` probes an explicit list of files.
`scan_media_folder(path)` walks a folder and probes the media within.

Both return plain dicts (JSON-serialisable), with stable ordering so callers
and golden tests get deterministic output.
"""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import fftools

VIDEO_EXTS = {
    ".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".webm", ".mpg", ".mpeg",
    ".mts", ".m2ts", ".r3d", ".braw", ".dv", ".wmv", ".flv",
}
AUDIO_EXTS = {
    ".wav", ".aif", ".aiff", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga",
    ".wma", ".caf", ".opus",
}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".dpx", ".bmp", ".gif",
    ".webp", ".heic", ".heif", ".tga",
}

DEFAULT_MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS


def classify_extension(ext: str) -> str:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


def _parse_fraction(value: str | None) -> float | None:
    if not value:
        return None
    try:
        f = Fraction(value)
        if f.denominator == 0:
            return None
        return float(f)
    except (ValueError, ZeroDivisionError):
        try:
            return float(value)
        except ValueError:
            return None


def _classify_streams(streams: list[dict[str, Any]]) -> str:
    """Refine kind using stream contents (an .mkv could hold only audio)."""
    has_video = False
    has_audio = False
    for s in streams:
        codec_type = s.get("codec_type")
        if codec_type == "video":
            # An attached cover-art / mjpeg still in an audio file has
            # disposition.attached_pic == 1; do not treat as video.
            if s.get("disposition", {}).get("attached_pic") == 1:
                continue
            has_video = True
        elif codec_type == "audio":
            has_audio = True
    if has_video:
        return "video"
    if has_audio:
        return "audio"
    return "other"


def _timecode(fmt: dict[str, Any], streams: list[dict[str, Any]]) -> str | None:
    tc = (fmt.get("tags") or {}).get("timecode")
    if tc:
        return tc
    for s in streams:
        tags = s.get("tags") or {}
        if tags.get("timecode"):
            return tags["timecode"]
        # Some cameras carry a dedicated timecode data stream.
        if s.get("codec_tag_string") == "tmcd" and tags.get("timecode"):
            return tags["timecode"]
    return None


def probe_one(path: str) -> dict[str, Any]:
    """Probe a single file. Never raises for missing/unreadable files;
    returns a dict with ``ok`` False and an ``error`` field instead."""
    p = Path(path).expanduser()
    abspath = str(p.resolve()) if p.exists() else str(p)
    ext = p.suffix.lower()
    base: dict[str, Any] = {
        "path": abspath,
        "name": p.name,
        "stem": p.stem,
        "extension": ext,
    }

    if not p.exists():
        base.update(ok=False, error="File does not exist.", kind=classify_extension(ext))
        return base
    if not p.is_file():
        base.update(ok=False, error="Path is not a regular file.", kind="other")
        return base

    base["size_bytes"] = p.stat().st_size
    ext_kind = classify_extension(ext)

    # Images: ffprobe still gives resolution but no duration.
    try:
        info = fftools.ffprobe_json(abspath)
    except Exception as exc:  # ffprobe missing or file undecodable
        base.update(ok=False, error=f"{type(exc).__name__}: {exc}", kind=ext_kind)
        return base

    fmt = info.get("format") or {}
    streams = info.get("streams") or []

    kind = _classify_streams(streams)
    if kind == "other":
        # Fall back to extension classification (e.g. image formats ffprobe
        # reports as a single video stream we still want called "image").
        kind = ext_kind if ext_kind != "other" else "other"
    # An image container (png/jpg) shows as a 1-frame "video" stream; prefer ext.
    if ext_kind == "image":
        kind = "image"

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    duration = _parse_fraction(fmt.get("duration"))
    if duration is None:
        for s in streams:
            d = _parse_fraction(s.get("duration"))
            if d is not None:
                duration = d
                break

    result: dict[str, Any] = dict(base)
    result.update(
        ok=True,
        kind=kind,
        duration_seconds=duration,
        container=fmt.get("format_name"),
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate", "").isdigit() else None,
    )

    if video_streams and kind in {"video", "image"}:
        v = video_streams[0]
        width = v.get("width")
        height = v.get("height")
        fps = _parse_fraction(v.get("avg_frame_rate")) or _parse_fraction(
            v.get("r_frame_rate")
        )
        # A 0/0 avg_frame_rate (stills) yields None; keep it None.
        result.update(
            width=width,
            height=height,
            resolution=f"{width}x{height}" if width and height else None,
            fps=round(fps, 6) if fps else None,
            video_codec=v.get("codec_name"),
            pix_fmt=v.get("pix_fmt"),
        )

    if audio_streams:
        a = audio_streams[0]
        result.update(
            audio_codec=a.get("codec_name"),
            audio_channels=a.get("channels"),
            audio_sample_rate=int(a["sample_rate"]) if a.get("sample_rate") else None,
            audio_streams=len(audio_streams),
        )

    tc = _timecode(fmt, streams)
    if tc:
        result["timecode"] = tc

    return result


def probe_media(paths: list[str]) -> dict[str, Any]:
    """Probe an explicit list of paths. Preserves caller order."""
    if not paths:
        return {"ok": False, "error": "No paths supplied.", "media": []}
    media = [probe_one(p) for p in paths]
    counts: dict[str, int] = {}
    for m in media:
        counts[m.get("kind", "other")] = counts.get(m.get("kind", "other"), 0) + 1
    return {
        "ok": True,
        "count": len(media),
        "counts_by_kind": counts,
        "media": media,
    }


def scan_media_folder(
    folder_path: str,
    *,
    recursive: bool = True,
    include_extensions: list[str] | None = None,
    probe: bool = True,
    max_files: int = 1000,
) -> dict[str, Any]:
    """Walk ``folder_path`` and return media entries in stable (sorted) order."""
    folder = Path(folder_path).expanduser()
    if not folder.exists():
        return {"ok": False, "error": "Folder does not exist.", "folder_path": str(folder)}
    if not folder.is_dir():
        return {"ok": False, "error": "folder_path is not a directory.", "folder_path": str(folder)}
    if max_files < 1:
        return {"ok": False, "error": "max_files must be >= 1.", "max_files": max_files}

    if include_extensions:
        exts = {
            (e if e.startswith(".") else f".{e}").lower() for e in include_extensions
        }
    else:
        exts = DEFAULT_MEDIA_EXTS

    iterator = folder.rglob("*") if recursive else folder.iterdir()
    candidates = sorted(
        (p for p in iterator if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: str(p).lower(),
    )

    skipped = max(0, len(candidates) - max_files)
    candidates = candidates[:max_files]

    media: list[dict[str, Any]] = []
    for p in candidates:
        if probe:
            media.append(probe_one(str(p)))
        else:
            ext = p.suffix.lower()
            media.append(
                {
                    "ok": True,
                    "path": str(p.resolve()),
                    "name": p.name,
                    "stem": p.stem,
                    "extension": ext,
                    "kind": classify_extension(ext),
                    "size_bytes": p.stat().st_size,
                }
            )

    counts: dict[str, int] = {}
    for m in media:
        counts[m.get("kind", "other")] = counts.get(m.get("kind", "other"), 0) + 1

    return {
        "ok": True,
        "folder_path": str(folder.resolve()),
        "recursive": recursive,
        "returned_count": len(media),
        "skipped_count": skipped,
        "counts_by_kind": counts,
        "media": media,
    }


# ---- Tool registration -------------------------------------------------

_PATHS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": "Absolute or ~-relative paths to media files.",
}


def register(add_tool) -> None:
    add_tool(
        "probe_media",
        {
            "type": "object",
            "properties": {"paths": _PATHS_SCHEMA},
            "required": ["paths"],
            "additionalProperties": False,
        },
        lambda params: probe_media(list(params.get("paths", []))),
        "both",
        "Probe media files with ffprobe: duration, fps, resolution, codecs, "
        "audio channels/rate, timecode, and video/audio/image/other kind.",
    )
    add_tool(
        "scan_media_folder",
        {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string", "description": "Directory to scan."},
                "recursive": {"type": "boolean", "default": True},
                "include_extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict to these extensions (with or without dot).",
                },
                "probe": {
                    "type": "boolean",
                    "default": True,
                    "description": "Run ffprobe on each file (slower, richer).",
                },
                "max_files": {"type": "integer", "default": 1000, "minimum": 1},
            },
            "required": ["folder_path"],
            "additionalProperties": False,
        },
        lambda params: scan_media_folder(
            params["folder_path"],
            recursive=params.get("recursive", True),
            include_extensions=params.get("include_extensions"),
            probe=params.get("probe", True),
            max_files=params.get("max_files", 1000),
        ),
        "both",
        "Scan a folder for media and probe each file. Stable sorted order.",
    )
