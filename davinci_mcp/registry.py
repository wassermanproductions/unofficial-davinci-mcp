"""The single tool table for Wasserman's Unofficial DaVinci MCP.

Every tool the server exposes is registered here with its name, JSON schema,
handler, tier, and an agent-facing description. The server reads this table for
``tools/list`` and dispatches ``tools/call`` through it.

Tiers
-----
- ``live``  - requires a reachable DaVinci Resolve Studio (the handler returns a
              friendly "not reachable" message otherwise).
- ``both``  - works in either tier (interchange file generators, capabilities).

Sibling plug-in hook
--------------------
The creative engines (``engines/``) and editorial knowledge (``skills/``) are
built concurrently in sibling packages. When present, each exposes a module
level ``register(add_tool)`` function and this registry calls it so their tools
join the same table. The contract is::

    def register(add_tool):
        add_tool(
            name="probe_media",
            schema={...},          # JSON Schema for the tool's arguments
            handler=my_handler,    # callable(args: dict) -> dict (JSON-safe)
            tier="both",           # "live" or "both"
            description="...",     # written for an agent: when + how to use it
        )

Handlers registered by siblings must accept a single ``dict`` of arguments and
return a JSON-serializable ``dict``. Both imports are guarded, so the core
server runs whether or not the sibling packages exist yet.
"""

from __future__ import annotations

from typing import Any, Callable

from . import mode, tools_interchange, tools_live


Handler = Callable[[dict[str, Any]], dict[str, Any]]


class Tool:
    def __init__(
        self,
        name: str,
        schema: dict[str, Any],
        handler: Handler,
        tier: str,
        description: str,
    ) -> None:
        self.name = name
        self.schema = schema
        self.handler = handler
        self.tier = tier
        self.description = description

    def as_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add_tool(
        self,
        name: str,
        schema: dict[str, Any],
        handler: Handler,
        tier: str = "both",
        description: str = "",
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Duplicate tool registration: {name}")
        self._tools[name] = Tool(name, schema, handler, tier, description)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_mcp(self) -> list[dict[str, Any]]:
        return [self._tools[name].as_mcp() for name in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)


# --- Shared schema fragments ----------------------------------------------

_DRY_RUN = {
    "type": "boolean",
    "default": True,
    "description": "When true (default), return the plan without changing anything.",
}
_CONFIRM = {
    "type": "boolean",
    "default": False,
    "description": "Must be true when dry_run is false, to apply the plan.",
}
_PATHS = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": "Absolute or user-relative media file paths.",
}
_MARKERS = {
    "type": "array",
    "description": "Timeline markers.",
    "items": {
        "type": "object",
        "properties": {
            "frame": {"type": "integer", "minimum": 0, "description": "Timeline frame."},
            "name": {"type": "string", "description": "Marker name."},
            "color": {"type": "string", "default": "Blue", "description": "Marker color."},
            "note": {"type": "string", "default": "", "description": "Optional note."},
            "duration": {"type": "integer", "minimum": 1, "default": 1, "description": "Frames."},
        },
        "required": ["frame", "name"],
        "additionalProperties": False,
    },
}
_CLIP_PLAN = {
    "type": "array",
    "minItems": 1,
    "description": "Edit decision list. Each item points at a media file with optional source/record ranges.",
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Media file path."},
            "name": {"type": "string", "description": "Optional shot/beat name."},
            "start_frame": {"type": "integer", "minimum": 0, "default": 0, "description": "Source in frame."},
            "end_frame": {"type": "integer", "minimum": 1, "description": "Optional source out frame."},
            "record_frame": {"type": "integer", "minimum": 0, "description": "Optional timeline placement frame."},
            "media_type": {"type": "string", "enum": ["video", "audio"], "description": "Append as video-only or audio-only."},
            "track_index": {"type": "integer", "minimum": 1, "description": "Optional target track."},
            "note": {"type": "string", "description": "Why this clip was chosen."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}
_INTERCHANGE_CLIPS = {
    "type": "array",
    "minItems": 1,
    "description": "Timeline clips. Each has a path and optional in/out or duration and placement (seconds).",
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Media file path."},
            "name": {"type": "string", "description": "Optional clip name."},
            "kind": {"type": "string", "enum": ["video", "audio"], "description": "Track kind; inferred from the file otherwise."},
            "in_seconds": {"type": "number", "minimum": 0, "default": 0, "description": "Source in point."},
            "out_seconds": {"type": "number", "description": "Source out point."},
            "duration_seconds": {"type": "number", "description": "Clip length; derived from in/out if omitted."},
            "record_seconds": {"type": "number", "description": "Timeline placement; tiles after the previous same-kind clip if omitted."},
            "note": {"type": "string", "description": "Optional note."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _wrap(fn: Callable[..., dict[str, Any]]) -> Handler:
    """Adapt a keyword-argument tool function to a single-dict handler."""

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"ok": False, "error": f"Invalid arguments: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return handler


def build_registry() -> Registry:
    registry = Registry()
    add = registry.add_tool

    # --- Capabilities (both) ---
    add(
        name="resolve_capabilities",
        schema=_obj({}),
        handler=_wrap(mode.capabilities),
        tier="both",
        description=(
            "Call FIRST. Reports the active tier (live vs interchange), why, the "
            "DaVinci Resolve version if reachable, ffmpeg availability, and optional "
            "dependency status. Live tools need tier=live; interchange tools work in "
            "either tier. Every mutating tool defaults to dry_run=true - run it once "
            "to preview the plan, then again with dry_run=false and confirm=true."
        ),
    )

    # --- Live: read-only ---
    add(
        name="resolve_project_summary",
        schema=_obj({}),
        handler=_wrap(tools_live.project_summary),
        tier="live",
        description=(
            "Read the current project name, its timelines, and the active timeline's "
            "frame rate and marker count. Use to orient before editing. Live tier only."
        ),
    )
    add(
        name="resolve_render_status",
        schema=_obj({"job_id": {"type": "string", "description": "Render job id from resolve_render."}}),
        handler=_wrap(tools_live.render_status),
        tier="live",
        description="Check render progress and the render queue. Live tier only.",
    )

    # --- Live: mutators ---
    add(
        name="resolve_import_media",
        schema=_obj(
            {
                "paths": _PATHS,
                "bin_name": {"type": "string", "description": "Optional media pool bin to create/use."},
                "dry_run": _DRY_RUN,
                "confirm": _CONFIRM,
            },
            required=["paths"],
        ),
        handler=_wrap(tools_live.import_media),
        tier="live",
        description=(
            "Import media into the project media pool. Workflow: dry_run first, then "
            "confirm. Run before resolve_create_timeline / resolve_append_to_timeline "
            "when the media is not already in the pool. Live tier only."
        ),
    )
    add(
        name="resolve_create_timeline",
        schema=_obj(
            {
                "name": {"type": "string", "description": "Timeline name."},
                "clips": _CLIP_PLAN,
                "music_paths": {"type": "array", "items": {"type": "string"}, "description": "Optional audio to append."},
                "markers": _MARKERS,
                "bin_name": {"type": "string", "default": "DaVinci MCP Edit", "description": "Media pool bin for imported assets."},
                "dry_run": _DRY_RUN,
                "confirm": _CONFIRM,
            },
            required=["name"],
        ),
        handler=_wrap(tools_live.create_timeline),
        tier="live",
        description=(
            "Create a timeline, optionally seeded from a clip plan (with source/record "
            "ranges), music, and markers. dry_run first, then confirm. Live tier only; "
            "for free Resolve use generate_fcpxml instead."
        ),
    )
    add(
        name="resolve_append_to_timeline",
        schema=_obj({"clips": _CLIP_PLAN, "dry_run": _DRY_RUN, "confirm": _CONFIRM}, required=["clips"]),
        handler=_wrap(tools_live.append_to_timeline),
        tier="live",
        description=(
            "Append clips (with ranges) to the active timeline, importing them first if "
            "needed. dry_run first, then confirm. Live tier only."
        ),
    )
    add(
        name="resolve_add_markers",
        schema=_obj({"markers": _MARKERS, "dry_run": _DRY_RUN, "confirm": _CONFIRM}, required=["markers"]),
        handler=_wrap(tools_live.add_markers),
        tier="live",
        description=(
            "Add one or more markers to the active timeline. dry_run first, then "
            "confirm. Live tier only; for free Resolve use generate_marker_csv."
        ),
    )
    add(
        name="resolve_apply_lut",
        schema=_obj(
            {
                "lut_path": {"type": "string", "description": "Path to a .cube LUT."},
                "track_index": {"type": "integer", "minimum": 1, "default": 1, "description": "Video track."},
                "clip_indexes": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "1-based clip positions; omit for all clips on the track."},
                "node_index": {"type": "integer", "minimum": 1, "default": 1, "description": "Grade node to set the LUT on."},
                "dry_run": _DRY_RUN,
                "confirm": _CONFIRM,
            },
            required=["lut_path"],
        ),
        handler=_wrap(tools_live.apply_lut),
        tier="live",
        description=(
            "Apply a .cube LUT to clips on a timeline video track. dry_run first, then "
            "confirm. Live tier only. To produce the LUT file itself, use the color_match "
            "engine (both tiers)."
        ),
    )
    add(
        name="resolve_set_grade",
        schema=_obj(
            {
                "slope": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "ASC CDL slope RGB (default 1 1 1)."},
                "offset": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "ASC CDL offset RGB (default 0 0 0)."},
                "power": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "ASC CDL power RGB (default 1 1 1)."},
                "saturation": {"type": "number", "description": "Saturation (default 1)."},
                "track_index": {"type": "integer", "minimum": 1, "default": 1, "description": "Video track."},
                "clip_indexes": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "1-based clip positions; omit for all clips on the track."},
                "node_index": {"type": "integer", "minimum": 1, "default": 1, "description": "Grade node."},
                "dry_run": _DRY_RUN,
                "confirm": _CONFIRM,
            }
        ),
        handler=_wrap(tools_live.set_grade),
        tier="live",
        description=(
            "Set ASC CDL slope/offset/power/saturation on timeline clips. dry_run first, "
            "then confirm. Live tier only."
        ),
    )
    add(
        name="resolve_render",
        schema=_obj(
            {
                "target_dir": {"type": "string", "description": "Output folder."},
                "custom_name": {"type": "string", "description": "Output base filename (no extension)."},
                "preset_name": {"type": "string", "description": "Optional render preset to load first."},
                "render_format": {"type": "string", "default": "mov", "description": "Render format key."},
                "render_codec": {"type": "string", "default": "H264", "description": "Render codec key."},
                "render_settings": {"type": "object", "additionalProperties": True, "description": "Optional SetRenderSettings dict."},
                "start_render": {"type": "boolean", "default": True, "description": "Start the render after creating the job."},
                "dry_run": _DRY_RUN,
                "confirm": _CONFIRM,
            },
            required=["target_dir", "custom_name"],
        ),
        handler=_wrap(tools_live.render),
        tier="live",
        description=(
            "Configure a render job for the current timeline and optionally start it. "
            "dry_run first, then confirm. Poll with resolve_render_status. Live tier only."
        ),
    )

    # --- Interchange (both tiers) ---
    add(
        name="generate_fcpxml",
        schema=_obj(
            {
                "name": {"type": "string", "description": "Timeline/project name."},
                "clips": _INTERCHANGE_CLIPS,
                "output_path": {"type": "string", "description": "Optional .fcpxml destination."},
                "frame_rate": {"type": "integer", "minimum": 1, "maximum": 240, "default": 24, "description": "Timeline frame rate."},
                "width": {"type": "integer", "minimum": 1, "default": 1920, "description": "Timeline width."},
                "height": {"type": "integer", "minimum": 1, "default": 1080, "description": "Timeline height."},
                "clip_duration_seconds": {"type": "number", "exclusiveMinimum": 0, "default": 5.0, "description": "Default per-clip length when a clip omits in/out and duration."},
                "markers": _MARKERS,
                "dry_run": _DRY_RUN,
            },
            required=["name", "clips"],
        ),
        handler=_wrap(tools_interchange.generate_fcpxml),
        tier="both",
        description=(
            "Generate an FCPXML 1.9 timeline (video + audio clips, in/out ranges, "
            "markers) that imports cleanly into DaVinci Resolve via File > Import > "
            "Timeline. The primary way to deliver an edit in interchange (free Resolve) "
            "mode. dry_run returns the plan; dry_run=false writes the file."
        ),
    )
    add(
        name="generate_edl",
        schema=_obj(
            {
                "name": {"type": "string", "description": "Timeline name."},
                "clips": _INTERCHANGE_CLIPS,
                "output_path": {"type": "string", "description": "Optional .edl destination."},
                "frame_rate": {"type": "integer", "minimum": 1, "maximum": 240, "default": 24, "description": "Timeline frame rate."},
                "clip_duration_seconds": {"type": "number", "exclusiveMinimum": 0, "default": 5.0, "description": "Default per-clip length."},
                "dry_run": _DRY_RUN,
            },
            required=["name", "clips"],
        ),
        handler=_wrap(tools_interchange.generate_edl),
        tier="both",
        description=(
            "Generate a CM3600 EDL for the video clips in the plan. Simpler than FCPXML "
            "(cuts only, no audio/effects); use for round-tripping a cut list. dry_run "
            "returns the plan; dry_run=false writes the file."
        ),
    )
    add(
        name="generate_marker_csv",
        schema=_obj(
            {
                "markers": _MARKERS,
                "output_path": {"type": "string", "description": "Optional .csv destination."},
                "name": {"type": "string", "default": "Markers", "description": "Base filename."},
                "frame_rate": {"type": "integer", "minimum": 1, "maximum": 240, "default": 24, "description": "Frame rate for timecode."},
                "dry_run": _DRY_RUN,
            },
            required=["markers"],
        ),
        handler=_wrap(tools_interchange.generate_marker_csv),
        tier="both",
        description=(
            "Write a deterministic marker manifest CSV (frame, timecode, seconds, name, "
            "color, note, duration). Use in interchange mode where live marker insertion "
            "is unavailable. dry_run returns the plan; dry_run=false writes the file."
        ),
    )

    _register_siblings(registry)
    return registry


def _register_siblings(registry: Registry) -> None:
    """Let the concurrently-built engines/ and skills/ packages plug in.

    Guarded so the core server runs before those packages exist. Each package,
    when importable, must expose ``register(add_tool)`` where ``add_tool`` has
    this registry's signature (name, schema, handler, tier, description).
    """
    for module_name in ("engines", "skills"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        register = getattr(module, "register", None)
        if callable(register):
            register(registry.add_tool)
