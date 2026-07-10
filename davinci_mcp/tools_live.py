"""Live-tier tools: drive a running DaVinci Resolve Studio through its API.

Every mutating tool follows one contract: it defaults to ``dry_run=True`` and
returns the plan it would execute. To apply the plan, call again with
``dry_run=False`` and ``confirm=True``. This mirrors the reviewed bridge and
keeps an agent from changing a project without an explicit second step.

The functions here are thin, defensive wrappers over Resolve's scripting
objects. When Resolve is not reachable they return the friendly connection
message from :mod:`davinci_mcp.resolve_api` instead of raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import resolve_api


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _error(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **payload}


def _guard_mutation(dry_run: bool, confirm: bool, action: str) -> dict[str, Any] | None:
    """Enforce the dry_run/confirm contract. Returns an error dict or None."""
    if dry_run:
        return None
    if not confirm:
        return _error(
            f"{action} requires confirm=true when dry_run=false.",
            dry_run=dry_run,
            confirm=confirm,
        )
    return None


def _connect() -> tuple[Any | None, dict[str, Any] | None]:
    status = resolve_api.connect()
    if not status.reachable:
        return None, _error(
            status.message, resolve_state=status.state, **status.details
        )
    return status.resolve, None


def _project(resolve: Any) -> tuple[Any | None, dict[str, Any] | None]:
    project, err = resolve_api.current_project(resolve)
    if err:
        return None, _error(err)
    return project, None


def _pool(project: Any) -> tuple[Any | None, dict[str, Any] | None]:
    pool, err = resolve_api.media_pool(project)
    if err:
        return None, _error(err)
    return pool, None


def _expand(paths: list[str]) -> list[str]:
    return [str(Path(p).expanduser().resolve()) for p in paths]


def _path_report(paths: list[str]) -> dict[str, list[str]]:
    existing = [p for p in paths if Path(p).exists()]
    missing = [p for p in paths if not Path(p).exists()]
    return {"existing_paths": existing, "missing_paths": missing}


def _media_type_value(media_type: str | int | None) -> int | None:
    if media_type is None:
        return None
    if isinstance(media_type, int):
        return media_type
    normalized = str(media_type).lower().strip()
    if normalized == "video":
        return 1
    if normalized == "audio":
        return 2
    return None


def _normalize_clip_plan(clips: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    normalized: list[dict[str, Any]] = []
    for index, clip in enumerate(clips):
        raw = str(clip.get("path", "")).strip()
        if not raw:
            return [], f"clip {index} is missing a path."
        path = str(Path(raw).expanduser().resolve())
        entry = {
            "index": index,
            "path": path,
            "name": clip.get("name") or Path(path).stem,
            "start_frame": int(clip.get("start_frame", 0)),
            "end_frame": int(clip["end_frame"]) if clip.get("end_frame") is not None else None,
            "record_frame": int(clip["record_frame"]) if clip.get("record_frame") is not None else None,
            "media_type": clip.get("media_type"),
            "track_index": int(clip["track_index"]) if clip.get("track_index") is not None else None,
            "note": str(clip.get("note", "")),
        }
        if entry["start_frame"] < 0:
            return [], f"clip {index} start_frame cannot be negative."
        if entry["end_frame"] is not None and entry["end_frame"] <= entry["start_frame"]:
            return [], f"clip {index} end_frame must be greater than start_frame."
        normalized.append(entry)
    return normalized, None


def _append_payload(item: Any, clip: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"mediaPoolItem": item, "startFrame": clip["start_frame"]}
    if clip["end_frame"] is not None:
        payload["endFrame"] = clip["end_frame"]
    media_type = _media_type_value(clip.get("media_type"))
    if media_type is not None:
        payload["mediaType"] = media_type
    if clip["track_index"] is not None:
        payload["trackIndex"] = clip["track_index"]
    if clip["record_frame"] is not None:
        payload["recordFrame"] = clip["record_frame"]
    return payload


def _add_markers(timeline: Any, markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    for marker in markers:
        frame = int(marker.get("frame", 0))
        color = str(marker.get("color", "Blue"))
        name = str(marker.get("name", "Marker"))
        note = str(marker.get("note", ""))
        duration = int(marker.get("duration", 1))
        ok = timeline.AddMarker(frame, color, name, note, duration, str(marker.get("custom_data", "")))
        added.append({"frame": frame, "name": name, "added": bool(ok)})
    return added


# --- Read-only -------------------------------------------------------------


def project_summary() -> dict[str, Any]:
    """Inspect the current project and active timeline."""
    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err

    timeline = project.GetCurrentTimeline()
    count = project.GetTimelineCount()
    names = []
    for index in range(1, count + 1):
        item = project.GetTimelineByIndex(index)
        if item:
            names.append(item.GetName())

    current = None
    if timeline:
        markers = timeline.GetMarkers() or {}
        current = {
            "name": timeline.GetName(),
            "start_frame": timeline.GetStartFrame(),
            "end_frame": timeline.GetEndFrame(),
            "frame_rate": timeline.GetSetting("timelineFrameRate"),
            "marker_count": len(markers),
        }

    return _ok(
        project_name=project.GetName(),
        timeline_count=count,
        timelines=names,
        current_timeline=current,
    )


def render_status(job_id: str | None = None) -> dict[str, Any]:
    """Read render progress and the render queue for the current project."""
    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    status = project.GetRenderJobStatus(job_id) if job_id else None
    jobs = project.GetRenderJobList() or []
    return _ok(
        job_id=job_id,
        job_status=status,
        is_rendering=bool(project.IsRenderingInProgress()),
        render_jobs=jobs,
    )


# --- Mutators --------------------------------------------------------------


def import_media(
    paths: list[str],
    bin_name: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Import media files into the media pool."""
    guard = _guard_mutation(dry_run, confirm, "Importing media")
    if guard:
        return guard

    expanded = _expand(paths)
    report = _path_report(expanded)
    plan = {"action": "import_media", "paths": expanded, "bin_name": bin_name, **report}
    if dry_run:
        return _ok(dry_run=True, plan=plan)
    if report["missing_paths"]:
        return _error("Cannot import missing media files.", **plan)

    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    pool, err = _pool(project)
    if err:
        return err

    if bin_name:
        root = pool.GetRootFolder()
        target = pool.AddSubFolder(root, bin_name)
        if target:
            pool.SetCurrentFolder(target)

    imported = pool.ImportMedia(expanded) or []
    return _ok(
        dry_run=False,
        imported_count=len(imported),
        imported_names=[item.GetName() for item in imported if item],
        **plan,
    )


def create_timeline(
    name: str,
    clips: list[dict[str, Any]] | None = None,
    music_paths: list[str] | None = None,
    markers: list[dict[str, Any]] | None = None,
    bin_name: str | None = "DaVinci MCP Edit",
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a timeline, optionally seeded from a clip plan with ranges."""
    guard = _guard_mutation(dry_run, confirm, "Creating a timeline")
    if guard:
        return guard

    normalized, clip_err = _normalize_clip_plan(clips or [])
    if clip_err:
        return _error(clip_err)
    expanded_music = _expand(music_paths or [])
    all_paths = [c["path"] for c in normalized] + expanded_music
    report = _path_report(all_paths)
    music_plan = [
        {"path": p, "name": Path(p).stem, "media_type": "audio", "track_index": 1}
        for p in expanded_music
    ]
    plan = {
        "action": "create_timeline",
        "mode": "live",
        "name": name,
        "bin_name": bin_name,
        "clips": normalized,
        "music": music_plan,
        "markers": markers or [],
        **report,
    }
    if dry_run:
        return _ok(dry_run=True, plan=plan)
    if report["missing_paths"]:
        return _error("Cannot create timeline with missing media files.", **plan)

    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    pool, err = _pool(project)
    if err:
        return err

    if bin_name:
        root = pool.GetRootFolder()
        target = pool.AddSubFolder(root, bin_name)
        if target:
            pool.SetCurrentFolder(target)

    imported = pool.ImportMedia(all_paths) if all_paths else []
    item_by_path: dict[str, Any] = {}
    for path, item in zip(all_paths, imported):
        if item:
            item_by_path[path] = item

    timeline = pool.CreateEmptyTimeline(name)
    if not timeline:
        return _error("DaVinci Resolve did not create the timeline.", **plan)
    project.SetCurrentTimeline(timeline)

    failed: list[dict[str, Any]] = []
    payloads = []
    for clip in normalized:
        item = item_by_path.get(clip["path"])
        if not item:
            failed.append({"path": clip["path"], "reason": "media item unavailable after import"})
            continue
        payloads.append(_append_payload(item, clip))
    appended_count = 0
    if payloads:
        appended = pool.AppendToTimeline(payloads) or []
        appended_count = len(appended)

    music_appended = 0
    for path in expanded_music:
        item = item_by_path.get(path)
        if not item:
            failed.append({"path": path, "reason": "music item unavailable after import"})
            continue
        appended = pool.AppendToTimeline([{"mediaPoolItem": item, "mediaType": 2, "trackIndex": 1}]) or []
        music_appended += len(appended)

    added_markers = _add_markers(timeline, markers or [])

    return _ok(
        dry_run=False,
        timeline_name=timeline.GetName(),
        imported_count=len(item_by_path),
        appended_count=appended_count,
        music_appended_count=music_appended,
        failed_clips=failed,
        added_markers=added_markers,
        **plan,
    )


def append_to_timeline(
    clips: list[dict[str, Any]],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Import (if needed) and append clips with ranges to the active timeline."""
    guard = _guard_mutation(dry_run, confirm, "Appending to the current timeline")
    if guard:
        return guard
    if not clips:
        return _error("clips must contain at least one clip.")

    normalized, clip_err = _normalize_clip_plan(clips)
    if clip_err:
        return _error(clip_err)
    paths = [c["path"] for c in normalized]
    report = _path_report(paths)
    plan = {"action": "append_to_timeline", "mode": "live", "clips": normalized, **report}
    if dry_run:
        return _ok(dry_run=True, plan=plan)
    if report["missing_paths"]:
        return _error("Cannot append missing media files.", **plan)

    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    if not project.GetCurrentTimeline():
        return _error("No current timeline is active.", **plan)
    pool, err = _pool(project)
    if err:
        return err

    imported = pool.ImportMedia(paths) or []
    item_by_path: dict[str, Any] = {}
    for path, item in zip(paths, imported):
        if item:
            item_by_path[path] = item

    failed: list[dict[str, Any]] = []
    payloads = []
    for clip in normalized:
        item = item_by_path.get(clip["path"])
        if not item:
            failed.append({"path": clip["path"], "reason": "media item unavailable after import"})
            continue
        payloads.append(_append_payload(item, clip))
    appended_count = 0
    if payloads:
        appended = pool.AppendToTimeline(payloads) or []
        appended_count = len(appended)

    return _ok(
        dry_run=False,
        imported_count=len(item_by_path),
        appended_count=appended_count,
        failed_clips=failed,
        **plan,
    )


def add_markers(
    markers: list[dict[str, Any]],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Add one or more markers to the current timeline."""
    guard = _guard_mutation(dry_run, confirm, "Adding timeline markers")
    if guard:
        return guard
    if not markers:
        return _error("markers must contain at least one marker.")

    plan = {"action": "add_markers", "mode": "live", "markers": markers}
    if dry_run:
        return _ok(dry_run=True, plan=plan)

    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    timeline = project.GetCurrentTimeline()
    if not timeline:
        return _error("No current timeline is active.", **plan)

    added = _add_markers(timeline, markers)
    return _ok(dry_run=False, added_markers=added, **plan)


def _timeline_video_items(timeline: Any, track_index: int) -> list[Any]:
    return list(timeline.GetItemListInTrack("video", track_index) or [])


def apply_lut(
    lut_path: str,
    track_index: int = 1,
    clip_indexes: list[int] | None = None,
    node_index: int = 1,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Apply a .cube LUT to timeline clips on a video track.

    ``clip_indexes`` are 1-based positions within the track; omit to apply to
    every clip on the track. ``node_index`` is the grade node to set the LUT on.
    """
    guard = _guard_mutation(dry_run, confirm, "Applying a LUT")
    if guard:
        return guard

    resolved_lut = str(Path(lut_path).expanduser().resolve())
    plan = {
        "action": "apply_lut",
        "mode": "live",
        "lut_path": resolved_lut,
        "track_index": track_index,
        "clip_indexes": clip_indexes,
        "node_index": node_index,
        "lut_exists": Path(resolved_lut).exists(),
    }
    if dry_run:
        return _ok(dry_run=True, plan=plan)
    if not Path(resolved_lut).exists():
        return _error("LUT file does not exist.", **plan)

    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    timeline = project.GetCurrentTimeline()
    if not timeline:
        return _error("No current timeline is active.", **plan)

    items = _timeline_video_items(timeline, track_index)
    if not items:
        return _error("No clips found on the target video track.", **plan)

    results = []
    for one_based, item in enumerate(items, start=1):
        if clip_indexes and one_based not in clip_indexes:
            continue
        ok = bool(item.SetLUT(node_index, resolved_lut))
        results.append({"clip_index": one_based, "name": item.GetName(), "applied": ok})

    return _ok(dry_run=False, results=results, applied_count=sum(r["applied"] for r in results), **plan)


def set_grade(
    slope: list[float] | None = None,
    offset: list[float] | None = None,
    power: list[float] | None = None,
    saturation: float | None = None,
    track_index: int = 1,
    clip_indexes: list[int] | None = None,
    node_index: int = 1,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Set ASC CDL slope/offset/power/saturation on timeline clips.

    Each of slope/offset/power is a 3-value RGB list. Omitted channels default
    to the identity grade (slope 1 1 1, offset 0 0 0, power 1 1 1, sat 1).
    """
    guard = _guard_mutation(dry_run, confirm, "Setting a grade")
    if guard:
        return guard

    def _triple(value: list[float] | None, default: float) -> list[float]:
        if value is None:
            return [default, default, default]
        if len(value) != 3:
            raise ValueError("slope/offset/power must each have exactly 3 values.")
        return [float(v) for v in value]

    try:
        s = _triple(slope, 1.0)
        o = _triple(offset, 0.0)
        p = _triple(power, 1.0)
    except ValueError as exc:
        return _error(str(exc))
    sat = 1.0 if saturation is None else float(saturation)

    cdl = {
        "NodeIndex": str(node_index),
        "Slope": f"{s[0]} {s[1]} {s[2]}",
        "Offset": f"{o[0]} {o[1]} {o[2]}",
        "Power": f"{p[0]} {p[1]} {p[2]}",
        "Saturation": str(sat),
    }
    plan = {
        "action": "set_grade",
        "mode": "live",
        "cdl": cdl,
        "track_index": track_index,
        "clip_indexes": clip_indexes,
    }
    if dry_run:
        return _ok(dry_run=True, plan=plan)

    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    timeline = project.GetCurrentTimeline()
    if not timeline:
        return _error("No current timeline is active.", **plan)

    items = _timeline_video_items(timeline, track_index)
    if not items:
        return _error("No clips found on the target video track.", **plan)

    results = []
    for one_based, item in enumerate(items, start=1):
        if clip_indexes and one_based not in clip_indexes:
            continue
        ok = bool(item.SetCDL(cdl))
        results.append({"clip_index": one_based, "name": item.GetName(), "applied": ok})

    return _ok(dry_run=False, results=results, applied_count=sum(r["applied"] for r in results), **plan)


def render(
    target_dir: str,
    custom_name: str,
    preset_name: str | None = None,
    render_format: str = "mov",
    render_codec: str = "H264",
    render_settings: dict[str, Any] | None = None,
    start_render: bool = True,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Configure a render job for the current timeline and optionally start it."""
    guard = _guard_mutation(dry_run, confirm, "Rendering the current timeline")
    if guard:
        return guard

    target = str(Path(target_dir).expanduser().resolve())
    settings = dict(render_settings or {})
    settings.update({"TargetDir": target, "CustomName": custom_name})
    plan = {
        "action": "render",
        "mode": "live",
        "target_dir": target,
        "custom_name": custom_name,
        "preset_name": preset_name,
        "render_format": render_format,
        "render_codec": render_codec,
        "render_settings": settings,
        "start_render": start_render,
    }
    if dry_run:
        return _ok(dry_run=True, plan=plan)

    Path(target).mkdir(parents=True, exist_ok=True)
    resolve, err = _connect()
    if err:
        return err
    project, err = _project(resolve)
    if err:
        return err
    if not project.GetCurrentTimeline():
        return _error("No current timeline is active.", **plan)

    preset_loaded = None
    if preset_name:
        preset_loaded = bool(project.LoadRenderPreset(preset_name))
    format_codec_set = None
    if render_format and render_codec:
        format_codec_set = bool(project.SetCurrentRenderFormatAndCodec(render_format, render_codec))
    settings_set = bool(project.SetRenderSettings(settings))
    job_id = project.AddRenderJob()
    if not job_id:
        return _error(
            "DaVinci Resolve did not create a render job.",
            preset_loaded=preset_loaded,
            format_codec_set=format_codec_set,
            render_settings_set=settings_set,
            **plan,
        )

    started = False
    if start_render:
        started = bool(project.StartRendering(job_id))

    return _ok(
        dry_run=False,
        job_id=job_id,
        started=started,
        preset_loaded=preset_loaded,
        format_codec_set=format_codec_set,
        render_settings_set=settings_set,
        **plan,
    )
