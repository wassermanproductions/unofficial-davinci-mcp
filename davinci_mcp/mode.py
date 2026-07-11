"""Tier detection: is this a live (Studio) session or an interchange session?

The server auto-detects at startup and reports through ``resolve_capabilities``.
Live tier drives a running DaVinci Resolve Studio through the scripting API.
Interchange tier writes files (FCPXML/EDL/marker CSV) the user imports by hand -
the fallback for the free edition, or when Resolve is not running.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from . import resolve_api


LIVE = "live"
INTERCHANGE = "interchange"


# ffmpeg/ffprobe locations to probe beyond PATH.
_FFMPEG_FALLBACK_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]


def _which(binary: str) -> str | None:
    """Locate a binary on PATH, then in the documented fallback directories."""
    found = shutil.which(binary)
    if found:
        return found
    for directory in _FFMPEG_FALLBACK_DIRS:
        candidate = os.path.join(directory, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ffmpeg_path() -> str | None:
    return _which("ffmpeg")


def ffprobe_path() -> str | None:
    return _which("ffprobe")


def _optional_dep(module_name: str) -> bool:
    """Report whether an optional dependency is importable, without importing it."""
    import importlib.util

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def optional_deps() -> dict[str, bool]:
    """Availability of optional accelerators the engines can use when present."""
    return {
        "numpy": _optional_dep("numpy"),
        "librosa": _optional_dep("librosa"),
        "faster_whisper": _optional_dep("faster_whisper"),
    }


def detect(status: resolve_api.ResolveStatus | None = None) -> dict[str, Any]:
    """Detect the operating tier.

    Passing an existing ``ResolveStatus`` avoids a second connection attempt;
    otherwise one is made here.
    """
    if status is None:
        status = resolve_api.connect()

    if status.reachable:
        tier = LIVE
        if status.details.get("via_bridge"):
            why = (
                "free edition via in-app bridge - live scripting through "
                f"{status.product or 'DaVinci Resolve'}"
                f"{' ' + status.version if status.version else ''} "
                "(Workspace > Scripts)."
            )
        else:
            why = (
                f"Connected to {status.product or 'DaVinci Resolve'}"
                f"{' ' + status.version if status.version else ''}; "
                "live scripting is available."
            )
    else:
        tier = INTERCHANGE
        why = status.message

    return {
        "tier": tier,
        "why": why,
        "resolve_state": status.state,
        "resolve_product": status.product,
        "resolve_version": status.version,
        "ffmpeg": ffmpeg_path(),
        "ffprobe": ffprobe_path(),
        "optional_deps": optional_deps(),
    }


def capabilities() -> dict[str, Any]:
    """Full capability report returned by the ``resolve_capabilities`` tool."""
    status = resolve_api.connect()
    detected = detect(status)
    return {
        "ok": True,
        "product_name": "Wasserman's Unofficial DaVinci MCP",
        "tier": detected["tier"],
        "why": detected["why"],
        "resolve": {
            "state": status.state,
            "message": status.message,
            "product": status.product,
            "version": status.version,
            "edition": status.details.get("edition"),
            "via_bridge": bool(status.details.get("via_bridge")),
            "installed_app_paths": status.details.get("installed_app_paths", []),
        },
        "ffmpeg_available": detected["ffmpeg"] is not None,
        "ffmpeg_path": detected["ffmpeg"],
        "ffprobe_available": detected["ffprobe"] is not None,
        "ffprobe_path": detected["ffprobe"],
        "optional_deps": detected["optional_deps"],
        "tiers": {
            "live": (
                "A running DaVinci Resolve - Studio via external scripting, or "
                "the FREE edition via the bundled in-app bridge script "
                "(python -m davinci_mcp.install_bridge, then Workspace > Scripts "
                "> resolve_bridge once per session). Import media, build and "
                "edit timelines, markers, grades/LUTs, and renders happen "
                "directly in the app."
            ),
            "interchange": (
                "Resolve not running (or no bridge started). The tools write "
                "FCPXML/EDL timelines and marker CSVs that import in one action."
            ),
        },
        "safety": (
            "Every mutating tool defaults to dry_run=true and returns a plan. "
            "Re-run with dry_run=false and confirm=true to apply it."
        ),
        "editorial_knowledge": {
            "note": (
                "This server ships editor field guides with concrete numbers "
                "(LUFS targets, fade lengths, frame offsets, look recipes). "
                "Before any creative task - cutting to music, tightening "
                "dialogue, cutting a song, mixing, or matching color - call "
                "get_editing_knowledge on the matching topic and follow it. "
                "It is the difference between operating the tools and editing "
                "well."
            ),
            "topics": _knowledge_topics(),
        },
        "suggested_workflow": [
            "1. probe_media / scan_media_folder to learn the footage",
            "2. get_editing_knowledge for the creative task at hand",
            "3. beat_grid / cut_music / tighten_dialogue / color_match / mix_plan as needed (dry_run first)",
            "4. assemble_edit to normalize the cut",
            "5. live tier: resolve_* tools build it in the app; interchange tier: generate_fcpxml for a one-import timeline",
            "6. live tier follow-through: resolve_project / resolve_timelines / resolve_edit / resolve_review / resolve_media / resolve_color are compound action-dispatched tools covering project settings, timeline management, per-clip edits, marker+still review, media-pool housekeeping, and grades/versions",
        ],
    }


def _knowledge_topics() -> list[str]:
    """Topic list from the skills package, when it ships alongside."""
    try:
        import skills

        if hasattr(skills, "list_topics"):
            # list of {topic, summary} dicts - pass through so agents see
            # what each guide covers before deciding to read it.
            return sorted(skills.list_topics(), key=lambda t: t["topic"])
    except Exception:
        pass
    return []
