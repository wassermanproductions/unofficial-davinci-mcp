"""Deterministic creative engines for Wasserman's Unofficial DaVinci MCP.

Pure stdlib + numpy + the ffmpeg/ffprobe binaries. librosa is an optional
extra (beat tracking) with a graceful energy-envelope fallback. No network,
no LLM calls.

Each engine module exposes ``register(add_tool)``. The package-level
``register`` fans out to all of them so the core MCP server can wire the whole
creative surface in one call:

    from engines import register as register_engines
    register_engines(add_tool)

where ``add_tool(name, schema, handler, tier, description)`` matches the
convention in ``davinci_mcp/registry.py``:
  - name:        agent-facing tool name (str)
  - schema:      JSON Schema dict for the tool parameters
  - handler:     callable(params: dict) -> JSON-serialisable result
  - tier:        "both" | "live" | "interchange"
  - description: one-line human description
"""

from __future__ import annotations

from . import (
    assemble,
    beat_grid,
    color_match,
    dead_air,
    loudness,
    media,
    music_cut,
)

__all__ = [
    "assemble",
    "beat_grid",
    "color_match",
    "dead_air",
    "loudness",
    "media",
    "music_cut",
    "register",
]

# Every engine module that exposes register(add_tool).
_ENGINE_MODULES = (
    media,
    color_match,
    loudness,
    dead_air,
    beat_grid,
    music_cut,
    assemble,
)


def register(add_tool) -> None:
    """Register every engine's tools via ``add_tool``."""
    for module in _ENGINE_MODULES:
        module.register(add_tool)
