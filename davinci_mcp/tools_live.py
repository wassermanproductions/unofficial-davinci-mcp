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

import os
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


def _ffprobe_bin() -> str:
    import shutil

    found = shutil.which("ffprobe")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe"):
        if os.path.exists(candidate):
            return candidate
    return "ffprobe"


def _probe_fps(path: str) -> float | None:
    import subprocess

    try:
        out = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        if "/" in out:
            num, den = out.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(out) if out else None
    except Exception:  # noqa: BLE001
        return None


_KNOWN_CLIP_KEYS = {
    "path", "name", "start_frame", "end_frame", "record_frame", "media_type",
    "track_index", "note", "in_seconds", "out_seconds", "fps",
}


def _normalize_clip_plan(clips: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    normalized: list[dict[str, Any]] = []
    for index, clip in enumerate(clips):
        raw = str(clip.get("path", "")).strip()
        if not raw:
            return [], f"clip {index} is missing a path."
        unknown = set(clip) - _KNOWN_CLIP_KEYS
        if unknown:
            return [], (
                f"clip {index} has unknown keys {sorted(unknown)}. Use "
                "start_frame/end_frame (frames) or in_seconds/out_seconds "
                "(seconds; converted with the clip's own frame rate)."
            )
        path = str(Path(raw).expanduser().resolve())
        # Seconds-based ranges: convert with the clip's actual frame rate so
        # a range is never silently dropped.
        start_frame = clip.get("start_frame")
        end_frame = clip.get("end_frame")
        if clip.get("in_seconds") is not None or clip.get("out_seconds") is not None:
            fps = clip.get("fps")
            if fps is None:
                fps = _probe_fps(path)
            if not fps or fps <= 0:
                return [], (
                    f"clip {index} uses in_seconds/out_seconds but the frame "
                    "rate could not be determined - pass 'fps' explicitly."
                )
            if clip.get("in_seconds") is not None:
                start_frame = int(round(float(clip["in_seconds"]) * fps))
            if clip.get("out_seconds") is not None:
                end_frame = int(round(float(clip["out_seconds"]) * fps))
        entry = {
            "index": index,
            "path": path,
            "name": clip.get("name") or Path(path).stem,
            "start_frame": int(start_frame or 0),
            "end_frame": int(end_frame) if end_frame is not None else None,
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


def _clip_file_path(item: Any) -> str | None:
    try:
        value = item.GetClipProperty("File Path")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, str) and value:
        return os.path.realpath(value)
    return None


def _walk_pool_clips(folder: Any):
    try:
        for item in folder.GetClipList() or []:
            yield item
        for sub in folder.GetSubFolderList() or []:
            yield from _walk_pool_clips(sub)
    except Exception:  # noqa: BLE001
        return


def _pool_items_by_path(pool: Any, paths: list[str]) -> dict[str, Any]:
    """Map file paths to media-pool items, robust to ImportMedia dedup.

    ``ImportMedia`` only returns items for media that was newly imported -
    files already in the pool (or listed twice) come back short, so a
    positional zip against the request misaligns. Import first, then resolve
    every requested path by matching the pool items' actual "File Path"
    property.
    """
    wanted = {os.path.realpath(p): p for p in paths}
    missing = list(dict.fromkeys(wanted))  # unique, order-preserving realpaths
    try:
        pool.ImportMedia(list(wanted.values()))
    except Exception:  # noqa: BLE001
        pass
    mapping: dict[str, Any] = {}
    root = pool.GetRootFolder()
    for item in _walk_pool_clips(root):
        real = _clip_file_path(item)
        if real in wanted and wanted[real] not in mapping:
            mapping[wanted[real]] = item
        if len(mapping) == len(set(wanted.values())):
            break
    return mapping


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
    include_clip_audio: bool | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a timeline, optionally seeded from a clip plan with ranges.

    ``include_clip_audio`` defaults to True without music and False when
    ``music_paths`` are given - a supplied music track almost always means
    the clips' embedded audio should stay out of the cut.
    """
    if include_clip_audio is None:
        include_clip_audio = not music_paths
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
        "include_clip_audio": include_clip_audio,
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

    item_by_path = _pool_items_by_path(pool, all_paths) if all_paths else {}

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
        payload = _append_payload(item, clip)
        if not include_clip_audio and "mediaType" not in payload:
            payload["mediaType"] = 1  # video only - keep embedded audio out
        payloads.append(payload)
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

    item_by_path = _pool_items_by_path(pool, paths)

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


# Resolve LUT folders, preferred order. SetLUT frequently refuses paths that
# are not inside one of these; installing a copy and using the name relative
# to the LUT root is the reliable route.
_LUT_DIRS = [
    Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT"),
    Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
]


def _install_lut(project: Any, lut_path: str) -> str | None:
    """Copy the LUT into a Resolve LUT folder and return the relative name."""
    import shutil

    source = Path(lut_path)
    for root in _LUT_DIRS:
        if not root.is_dir() or not os.access(root, os.W_OK):
            continue
        target_dir = root / "DaVinci MCP"
        try:
            target_dir.mkdir(exist_ok=True)
            target = target_dir / source.name
            shutil.copyfile(source, target)
        except OSError:
            continue
        try:
            project.RefreshLUTList()
        except Exception:  # noqa: BLE001
            pass
        return f"DaVinci MCP/{source.name}"
    return None


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

    def _apply(path_arg: str) -> list[dict[str, Any]]:
        applied = []
        for one_based, item in enumerate(items, start=1):
            if clip_indexes and one_based not in clip_indexes:
                continue
            ok = bool(item.SetLUT(node_index, path_arg))
            applied.append({"clip_index": one_based, "name": item.GetName(), "applied": ok})
        return applied

    results = _apply(resolved_lut)
    installed_as = None
    if results and not any(r["applied"] for r in results):
        # Resolve often only accepts LUTs that live inside its LUT folder.
        # Install a copy there, refresh, and retry with the relative name.
        installed_as = _install_lut(project, resolved_lut)
        if installed_as:
            results = _apply(installed_as)

    applied_count = sum(r["applied"] for r in results)
    if results and applied_count == 0:
        return _error(
            "Resolve rejected the LUT on every clip. The file was "
            + ("copied into the Resolve LUT folder and retried, still refused - "
               "check the .cube file itself." if installed_as else
               "offered by absolute path and no Resolve LUT folder was writable - "
               "add it via Project Settings > Color Management > Open LUT Folder.")
            ,
            results=results,
            installed_as=installed_as,
            **plan,
        )
    return _ok(
        dry_run=False,
        results=results,
        applied_count=applied_count,
        installed_as=installed_as,
        **plan,
    )


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

    applied_count = sum(r["applied"] for r in results)
    if results and applied_count == 0:
        return _error(
            "Resolve rejected the CDL on every clip. If node_index is above 1, "
            "note the scripting API cannot create grade nodes - use node_index 1 "
            "or add nodes in the app first.",
            results=results,
            **plan,
        )
    return _ok(dry_run=False, results=results, applied_count=applied_count, **plan)


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
