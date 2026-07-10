"""Wasserman's Unofficial DaVinci MCP - agent control for DaVinci Resolve.

An MCP stdio server that lets an MCP agent operate DaVinci Resolve. Two tiers,
auto-detected at startup: ``live`` (drive a running Resolve Studio through the
scripting API) and ``interchange`` (write FCPXML/EDL/marker files the user
imports, the fallback for the free edition or when Resolve is not running).

Unofficial and not affiliated with or endorsed by Blackmagic Design. DaVinci
Resolve is a trademark of Blackmagic Design.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
