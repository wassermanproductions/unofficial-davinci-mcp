"""Compound live-tier tools: one tool, many related actions.

Each function here is a *compound* tool - it takes an ``action`` enum and the
parameters that action needs, dispatching internally. This keeps the exposed
tool surface small while covering the everyday 90% of a project/timeline/edit/
review/media/color workflow.

The contract matches :mod:`davinci_mcp.tools_live`:

- Read/navigation actions connect and return data (friendly error if Resolve is
  unreachable).
- Mutating actions default to ``dry_run=True`` and return the plan; call again
  with ``dry_run=False`` and ``confirm=True`` to apply.

Where DaVinci Resolve's documented scripting API genuinely cannot do something
(in-place trim, clip speed, transitions, setting title text), these tools return
a clear "the API cannot do X - do it in the app" message rather than faking it.
See the module docstring's capability notes and README for the full list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import tools_live
from .tools_live import (
    _connect,
    _error,
    _guard_mutation,
    _ok,
    _pool,
    _project,
    _timeline_video_items,
)


# --- Shared helpers --------------------------------------------------------


def _current_timeline(project: Any) -> tuple[Any | None, dict[str, Any] | None]:
    timeline = project.GetCurrentTimeline()
    if not timeline:
        return None, _error("No current timeline is active.")
    return timeline, None


def _select_timeline(
    project: Any, name: str | None = None, index: int | None = None
) -> tuple[Any | None, dict[str, Any] | None]:
    """Find a timeline by name or 1-based index, defaulting to the current one."""
    if index is not None:
        timeline = project.GetTimelineByIndex(int(index))
        if not timeline:
            return None, _error(f"No timeline at index {index}.")
        return timeline, None
    if name:
        for i in range(1, (project.GetTimelineCount() or 0) + 1):
            timeline = project.GetTimelineByIndex(i)
            if timeline and timeline.GetName() == name:
                return timeline, None
        return None, _error(f"No timeline named '{name}' in this project.")
    return _current_timeline(project)


def _track_item(
    timeline: Any, track_index: int, clip_index: int
) -> tuple[Any | None, dict[str, Any] | None]:
    """Return the 1-based clip on a video track, or a friendly error."""
    items = _timeline_video_items(timeline, track_index)
    if not items:
        return None, _error(f"No clips on video track {track_index}.")
    if clip_index < 1 or clip_index > len(items):
        return None, _error(
            f"clip_index {clip_index} is out of range - track {track_index} "
            f"has {len(items)} clip(s)."
        )
    return items[clip_index - 1], None


def _bad_action(action: str, valid: list[str]) -> dict[str, Any]:
    return _error(
        f"Unknown action '{action}'. Valid actions: {', '.join(valid)}.",
        action=action,
    )


def _walk_folders(folder: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Depth-first bin tree: name, clip count, nested subfolders."""
    node: dict[str, Any] = {
        "name": folder.GetName() if hasattr(folder, "GetName") else "",
        "clip_count": len(folder.GetClipList() or []),
        "subfolders": [],
    }
    for sub in folder.GetSubFolderList() or []:
        node["subfolders"].append(_walk_folders(sub, depth + 1))
    return [node] if depth == 0 else node  # type: ignore[return-value]


def _find_folder(folder: Any, name: str) -> Any | None:
    if hasattr(folder, "GetName") and folder.GetName() == name:
        return folder
    for sub in folder.GetSubFolderList() or []:
        found = _find_folder(sub, name)
        if found:
            return found
    return None


# --- 1. resolve_project ----------------------------------------------------

_PROJECT_ACTIONS = [
    "list_projects", "open", "create", "save", "current",
    "get_settings", "set_settings",
]

# Common project-setting keys, surfaced so an agent does not have to guess.
_COMMON_PROJECT_SETTINGS = [
    "timelineFrameRate",
    "timelineResolutionWidth",
    "timelineResolutionHeight",
    "timelinePlaybackFrameRate",
    "videoMonitorFormat",
    "colorScienceMode",
    "timelineOutputResolutionWidth",
    "timelineOutputResolutionHeight",
]


def resolve_project(
    action: str,
    name: str | None = None,
    settings: dict[str, Any] | None = None,
    keys: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Project lifecycle and settings: list/open/create/save/current + settings."""
    if action not in _PROJECT_ACTIONS:
        return _bad_action(action, _PROJECT_ACTIONS)

    # set_settings is the only mutator that changes project data; create/open/
    # save alter which project is loaded, so gate all four.
    mutating = action in ("open", "create", "save", "set_settings")
    if mutating:
        label = {"open": "Opening a project", "create": "Creating a project",
                 "save": "Saving the project", "set_settings": "Changing project settings"}[action]
        guard = _guard_mutation(dry_run, confirm, label)
        if guard:
            return guard
        plan = {"action": action, "mode": "live", "name": name, "settings": settings}
        if action == "set_settings" and not settings:
            return _error("set_settings requires a 'settings' object of key/value pairs.")
        if action in ("open", "create") and not name:
            return _error(f"{action} requires a 'name'.")
        if dry_run:
            return _ok(dry_run=True, plan=plan, common_setting_keys=_COMMON_PROJECT_SETTINGS)

    resolve, err = _connect()
    if err:
        return err
    manager = resolve.GetProjectManager()
    if not manager:
        return _error("DaVinci Resolve project manager is unavailable.")

    if action == "list_projects":
        return _ok(projects=list(manager.GetProjectListInCurrentFolder() or []))

    if action == "current":
        project, perr = _project(resolve)
        if perr:
            return perr
        return _ok(
            project_name=project.GetName(),
            timeline_count=project.GetTimelineCount(),
        )

    if action == "open":
        project = manager.LoadProject(name)
        if not project:
            return _error(f"Could not open project '{name}'. Check the name with list_projects.")
        return _ok(dry_run=False, opened=project.GetName())

    if action == "create":
        project = manager.CreateProject(name)
        if not project:
            return _error(
                f"Could not create project '{name}'. A project with that name may "
                "already exist in the current folder."
            )
        return _ok(dry_run=False, created=project.GetName())

    if action == "save":
        return _ok(dry_run=False, saved=bool(manager.SaveProject()))

    # get_settings / set_settings need the open project.
    project, perr = _project(resolve)
    if perr:
        return perr

    if action == "get_settings":
        if keys:
            values = {k: project.GetSetting(k) for k in keys}
        else:
            snapshot = project.GetSetting("")
            values = snapshot if isinstance(snapshot, dict) else {"": snapshot}
        return _ok(settings=values, common_setting_keys=_COMMON_PROJECT_SETTINGS)

    # set_settings
    results = {k: bool(project.SetSetting(k, str(v))) for k, v in (settings or {}).items()}
    applied = sum(1 for v in results.values() if v)
    if results and applied == 0:
        return _error(
            "Resolve rejected every setting. Check the key spelling against the "
            "Project Settings UI (e.g. timelineFrameRate, timelineResolutionWidth).",
            results=results,
        )
    return _ok(dry_run=False, results=results, applied_count=applied)


# --- 2. resolve_timelines --------------------------------------------------

_TIMELINE_ACTIONS = ["list", "switch", "duplicate", "delete", "export", "info"]

# Friendly export format -> (exportType constant name, exportSubtype constant name).
# Names are resolved against the live resolve object (resolve.EXPORT_*).
_EXPORT_FORMATS: dict[str, tuple[str, str | None]] = {
    "fcpxml": ("EXPORT_FCPXML_1_9", "EXPORT_NONE"),
    "fcpxml_1_8": ("EXPORT_FCPXML_1_8", "EXPORT_NONE"),
    "fcpxml_1_9": ("EXPORT_FCPXML_1_9", "EXPORT_NONE"),
    "fcpxml_1_10": ("EXPORT_FCPXML_1_10", "EXPORT_NONE"),
    "fcp7xml": ("EXPORT_FCP_7_XML", "EXPORT_NONE"),
    "xml": ("EXPORT_FCP_7_XML", "EXPORT_NONE"),
    "edl": ("EXPORT_EDL", "EXPORT_NONE"),
    "aaf": ("EXPORT_AAF", "EXPORT_AAF_NEW"),
    "drt": ("EXPORT_DRT", "EXPORT_NONE"),
    "otio": ("EXPORT_OTIO", "EXPORT_NONE"),
    "csv": ("EXPORT_TEXT_CSV", "EXPORT_NONE"),
    "tab": ("EXPORT_TEXT_TAB", "EXPORT_NONE"),
    "ale": ("EXPORT_ALE", "EXPORT_NONE"),
}


def _timeline_info(timeline: Any) -> dict[str, Any]:
    markers = timeline.GetMarkers() or {}
    tracks = {}
    for track_type in ("video", "audio", "subtitle"):
        try:
            tracks[track_type] = int(timeline.GetTrackCount(track_type) or 0)
        except Exception:  # noqa: BLE001
            tracks[track_type] = 0
    return {
        "name": timeline.GetName(),
        "start_frame": timeline.GetStartFrame(),
        "end_frame": timeline.GetEndFrame(),
        "frame_count": max(0, (timeline.GetEndFrame() or 0) - (timeline.GetStartFrame() or 0)),
        "frame_rate": timeline.GetSetting("timelineFrameRate"),
        "marker_count": len(markers),
        "track_counts": tracks,
    }


def resolve_timelines(
    action: str,
    name: str | None = None,
    index: int | None = None,
    new_name: str | None = None,
    format: str | None = None,
    output_path: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Timeline management: list/switch/duplicate/delete/export/info.

    ``switch``/``duplicate``/``delete``/``export``/``info`` target a timeline by
    ``name`` or 1-based ``index``; most default to the current timeline.
    """
    if action not in _TIMELINE_ACTIONS:
        return _bad_action(action, _TIMELINE_ACTIONS)

    mutating = action in ("switch", "duplicate", "delete", "export")
    if mutating:
        label = {
            "switch": "Switching the current timeline",
            "duplicate": "Duplicating a timeline",
            "delete": "Deleting a timeline",
            "export": "Exporting a timeline",
        }[action]
        guard = _guard_mutation(dry_run, confirm, label)
        if guard:
            return guard
        if action == "export":
            if not format:
                return _error(
                    "export requires 'format'. Supported: "
                    + ", ".join(sorted(_EXPORT_FORMATS))
                )
            if format.lower() not in _EXPORT_FORMATS:
                return _error(
                    f"Unsupported export format '{format}'. Supported: "
                    + ", ".join(sorted(_EXPORT_FORMATS))
                )
            if not output_path:
                return _error("export requires 'output_path' (the destination file).")
        plan = {
            "action": action, "mode": "live", "name": name, "index": index,
            "new_name": new_name, "format": format,
            "output_path": str(Path(output_path).expanduser()) if output_path else None,
        }
        if dry_run:
            return _ok(dry_run=True, plan=plan)

    resolve, err = _connect()
    if err:
        return err
    project, perr = _project(resolve)
    if perr:
        return perr

    if action == "list":
        count = project.GetTimelineCount() or 0
        current = project.GetCurrentTimeline()
        current_name = current.GetName() if current else None
        timelines = []
        for i in range(1, count + 1):
            timeline = project.GetTimelineByIndex(i)
            if timeline:
                timelines.append({
                    "index": i,
                    "name": timeline.GetName(),
                    "is_current": timeline.GetName() == current_name,
                })
        return _ok(timeline_count=count, timelines=timelines)

    if action == "info":
        timeline, terr = _select_timeline(project, name, index)
        if terr:
            return terr
        return _ok(info=_timeline_info(timeline))

    if action == "switch":
        timeline, terr = _select_timeline(project, name, index)
        if terr:
            return terr
        ok = bool(project.SetCurrentTimeline(timeline))
        return _ok(dry_run=False, switched=ok, current=timeline.GetName())

    if action == "duplicate":
        timeline, terr = _select_timeline(project, name, index)
        if terr:
            return terr
        dupe = timeline.DuplicateTimeline(new_name) if new_name else timeline.DuplicateTimeline()
        if not dupe:
            return _error("DaVinci Resolve did not duplicate the timeline.")
        return _ok(dry_run=False, duplicated=dupe.GetName(), source=timeline.GetName())

    if action == "delete":
        timeline, terr = _select_timeline(project, name, index)
        if terr:
            return terr
        pool, poolerr = _pool(project)
        if poolerr:
            return poolerr
        target_name = timeline.GetName()
        ok = bool(pool.DeleteTimelines([timeline]))
        return _ok(dry_run=False, deleted=ok, timeline=target_name)

    # export
    timeline, terr = _select_timeline(project, name, index)
    if terr:
        return terr
    export_type_name, export_subtype_name = _EXPORT_FORMATS[format.lower()]
    export_type = getattr(resolve, export_type_name, None)
    if export_type is None:
        return _error(
            f"This DaVinci Resolve build does not expose {export_type_name}; "
            "the format is unavailable in the scripting API here."
        )
    resolved_out = str(Path(output_path).expanduser())
    args = [resolved_out, export_type]
    if export_subtype_name is not None:
        subtype = getattr(resolve, export_subtype_name, None)
        if subtype is not None:
            args.append(subtype)
    ok = bool(timeline.Export(*args))
    if not ok:
        return _error(
            "DaVinci Resolve reported the export failed. Check the output folder "
            "exists and is writable.",
            output_path=resolved_out, format=format,
        )
    return _ok(dry_run=False, exported=ok, output_path=resolved_out, format=format)


# --- 3. resolve_edit -------------------------------------------------------

_EDIT_ACTIONS = [
    "move_clip", "trim", "ripple_delete", "insert_clip", "set_speed",
    "add_transition", "add_title", "delete_clip",
]


def resolve_edit(
    action: str,
    track_index: int = 1,
    clip_index: int | None = None,
    record_frame: int | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    path: str | None = None,
    media_type: str | None = None,
    title_name: str | None = None,
    speed: float | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Edit the CURRENT timeline: move/trim/ripple_delete/insert/title/delete.

    Operates on the active timeline only (switch first with resolve_timelines).
    ``clip_index`` is 1-based within the video track. After editing, verify
    visually with resolve_review goto + grab_still.

    Honest limits: Resolve's scripting API has no in-place trim, clip-speed, or
    add-transition call, and cannot set a title's text. ``move_clip`` and
    ``trim`` are therefore implemented as delete + re-append of the clip's source
    media (grades/effects on the original clip do NOT carry over); ``set_speed``
    and ``add_transition`` return a capability error with the manual step.
    """
    if action not in _EDIT_ACTIONS:
        return _bad_action(action, _EDIT_ACTIONS)

    # Capability gaps: the documented API cannot do these. Fail honestly.
    if action == "set_speed":
        return _error(
            "The DaVinci Resolve scripting API cannot change clip speed - there is "
            "no SetClipSpeed/retime method on TimelineItem. Do it in the app: "
            "right-click the clip > Change Clip Speed (or Retime Controls, R).",
            requested_speed=speed, action=action,
        )
    if action == "add_transition":
        return _error(
            "The DaVinci Resolve scripting API cannot add transitions - there is no "
            "AddTransition method. Do it in the app: select the cut point and press "
            "Ctrl/Cmd+T for the standard transition, or drag one from the Effects "
            "library.",
            action=action,
        )

    if action == "insert_clip":
        # Append at a record position reuses the tested append path.
        if not path:
            return _error("insert_clip requires 'path' (the media file to insert).")
        clip: dict[str, Any] = {"path": path}
        if start_frame is not None:
            clip["start_frame"] = start_frame
        if end_frame is not None:
            clip["end_frame"] = end_frame
        if record_frame is not None:
            clip["record_frame"] = record_frame
        if media_type is not None:
            clip["media_type"] = media_type
        if track_index is not None:
            clip["track_index"] = track_index
        return tools_live.append_to_timeline([clip], dry_run=dry_run, confirm=confirm)

    label = {
        "move_clip": "Moving a clip",
        "trim": "Trimming a clip",
        "ripple_delete": "Ripple-deleting a clip",
        "add_title": "Adding a title",
        "delete_clip": "Deleting a clip",
    }[action]
    guard = _guard_mutation(dry_run, confirm, label)
    if guard:
        return guard

    if action in ("move_clip", "trim", "ripple_delete", "delete_clip") and clip_index is None:
        return _error(f"{action} requires 'clip_index' (1-based clip on the track).")
    if action == "move_clip" and record_frame is None:
        return _error("move_clip requires 'record_frame' (the new timeline position).")
    if action == "add_title" and not title_name:
        return _error(
            "add_title requires 'title_name' (a title template, e.g. 'Text', "
            "'Text+', or the name of a Title generator installed in Resolve)."
        )

    reappend_note = (
        "Resolve's API has no in-place edit; this clip is deleted and its source "
        "media re-appended. Grades, Fusion comps, and effects on the original clip "
        "will NOT carry over."
    )
    plan: dict[str, Any] = {
        "action": action, "mode": "live", "track_index": track_index,
        "clip_index": clip_index, "record_frame": record_frame,
        "start_frame": start_frame, "end_frame": end_frame,
        "title_name": title_name,
    }
    if action in ("move_clip", "trim"):
        plan["note"] = reappend_note
    if dry_run:
        return _ok(dry_run=True, plan=plan)

    resolve, err = _connect()
    if err:
        return err
    project, perr = _project(resolve)
    if perr:
        return perr
    timeline, terr = _current_timeline(project)
    if terr:
        return terr

    if action == "add_title":
        item = timeline.InsertTitleIntoTimeline(title_name)
        if not item:
            return _error(
                f"Resolve did not insert a title named '{title_name}'. The name "
                "must match a Title generator available in this project.",
                **plan,
            )
        return _ok(
            dry_run=False,
            inserted_title=item.GetName() if hasattr(item, "GetName") else title_name,
            text_set=False,
            note=(
                "Title inserted. The scripting API cannot set the title's text - "
                "open the Inspector on the Edit page and type the caption there."
            ),
            **plan,
        )

    item, ierr = _track_item(timeline, track_index, clip_index)
    if ierr:
        return ierr

    if action in ("ripple_delete", "delete_clip"):
        ripple = action == "ripple_delete"
        target_name = item.GetName()
        ok = bool(timeline.DeleteClips([item], ripple))
        return _ok(dry_run=False, deleted=ok, clip=target_name, ripple=ripple, **plan)

    # move_clip / trim: capture the source, delete, re-append.
    pool, poolerr = _pool(project)
    if poolerr:
        return poolerr
    media_item = item.GetMediaPoolItem()
    if not media_item:
        return _error(
            "This timeline clip has no backing media-pool item (it may be a "
            "generator, title, or compound clip), so it cannot be re-appended.",
            **plan,
        )
    src_start = item.GetSourceStartFrame()
    src_end = item.GetSourceEndFrame()
    cur_record = item.GetStart()
    clip_name = item.GetName()

    if action == "move_clip":
        new_record = int(record_frame)
        new_start, new_end = src_start, src_end
    else:  # trim - adjust source in/out, keep the record position
        new_record = int(cur_record)
        new_start = int(start_frame) if start_frame is not None else src_start
        new_end = int(end_frame) if end_frame is not None else src_end
        if new_end is not None and new_start is not None and new_end <= new_start:
            return _error("trim end_frame must be greater than start_frame.", **plan)

    if not timeline.DeleteClips([item], False):
        return _error("Could not remove the original clip; nothing was changed.", **plan)
    payload: dict[str, Any] = {
        "mediaPoolItem": media_item,
        "startFrame": int(new_start) if new_start is not None else 0,
        "recordFrame": new_record,
        "trackIndex": track_index,
    }
    if new_end is not None:
        payload["endFrame"] = int(new_end)
    appended = pool.AppendToTimeline([payload]) or []
    if not appended:
        return _error(
            "Removed the original clip but Resolve refused the re-append. The clip "
            "may need to be restored manually (Undo in the app).",
            **plan,
        )
    return _ok(
        dry_run=False, clip=clip_name, moved_to=new_record,
        source_range=[payload["startFrame"], payload.get("endFrame")], **plan,
    )


# --- 4. resolve_review -----------------------------------------------------

_REVIEW_ACTIONS = [
    "markers_list", "marker_update", "marker_delete", "goto",
    "current_timecode", "page", "grab_still", "export_still",
]

_VALID_PAGES = ["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"]


def resolve_review(
    action: str,
    frame: int | None = None,
    color: str | None = None,
    name: str | None = None,
    note: str | None = None,
    duration: int | None = None,
    timecode: str | None = None,
    page: str | None = None,
    output_path: str | None = None,
    still_format: str = "jpg",
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Review the CURRENT timeline: markers, playhead, page, and stills.

    ``goto``/``page``/``current_timecode``/``markers_list`` are immediate reads/
    navigation. ``marker_update``/``marker_delete``/``grab_still``/``export_still``
    mutate or write and follow the dry_run/confirm contract. Pair goto with
    grab_still to visually confirm an edit made by resolve_edit.
    """
    if action not in _REVIEW_ACTIONS:
        return _bad_action(action, _REVIEW_ACTIONS)

    mutating = action in ("marker_update", "marker_delete", "grab_still", "export_still")
    if mutating:
        label = {
            "marker_update": "Updating a marker",
            "marker_delete": "Deleting a marker",
            "grab_still": "Grabbing a still",
            "export_still": "Exporting a still",
        }[action]
        guard = _guard_mutation(dry_run, confirm, label)
        if guard:
            return guard
        if action in ("marker_update", "marker_delete") and frame is None:
            return _error(f"{action} requires 'frame' (the marker's timeline frame).")
        if action == "export_still" and not output_path:
            return _error("export_still requires 'output_path' (destination folder).")
        plan = {
            "action": action, "mode": "live", "frame": frame, "color": color,
            "name": name, "note": note, "duration": duration,
            "output_path": str(Path(output_path).expanduser()) if output_path else None,
            "still_format": still_format,
        }
        if dry_run:
            return _ok(dry_run=True, plan=plan)

    resolve, err = _connect()
    if err:
        return err

    if action == "page":
        if not page or page.lower() not in _VALID_PAGES:
            return _error(
                f"page requires one of: {', '.join(_VALID_PAGES)}.",
            )
        ok = bool(resolve.OpenPage(page.lower()))
        return _ok(opened_page=page.lower(), switched=ok)

    project, perr = _project(resolve)
    if perr:
        return perr
    timeline, terr = _current_timeline(project)
    if terr:
        return terr

    if action == "markers_list":
        markers = timeline.GetMarkers() or {}
        listed = [
            {"frame": float(f), **info} for f, info in sorted(markers.items())
        ]
        return _ok(marker_count=len(listed), markers=listed)

    if action == "current_timecode":
        return _ok(timecode=timeline.GetCurrentTimecode())

    if action == "goto":
        if not timecode:
            return _error("goto requires 'timecode' (e.g. '01:00:05:12').")
        ok = bool(timeline.SetCurrentTimecode(timecode))
        return _ok(moved=ok, timecode=timecode)

    if action == "marker_delete":
        ok = bool(timeline.DeleteMarkerAtFrame(int(frame)))
        return _ok(dry_run=False, deleted=ok, frame=frame)

    if action == "marker_update":
        # No update-in-place API: read the existing marker, merge, delete, re-add.
        markers = timeline.GetMarkers() or {}
        existing = markers.get(float(frame)) or markers.get(int(frame)) or {}
        if not existing:
            return _error(f"No marker at frame {frame} to update.", **plan)
        merged = {
            "color": color if color is not None else existing.get("color", "Blue"),
            "name": name if name is not None else existing.get("name", "Marker"),
            "note": note if note is not None else existing.get("note", ""),
            "duration": int(duration) if duration is not None else int(existing.get("duration", 1)),
            "custom": existing.get("customData", ""),
        }
        timeline.DeleteMarkerAtFrame(int(frame))
        ok = bool(timeline.AddMarker(
            int(frame), merged["color"], merged["name"], merged["note"],
            merged["duration"], merged["custom"],
        ))
        return _ok(dry_run=False, updated=ok, marker=merged, **plan)

    # grab_still / export_still both grab from the current clip on the Color page.
    still = timeline.GrabStill()
    if not still:
        return _error(
            "Resolve returned no still. GrabStill works on the current video clip - "
            "open the Color page (page action) and park the playhead on a clip first.",
            **plan,
        )
    if action == "grab_still":
        return _ok(dry_run=False, grabbed=True, **plan)

    # export_still
    gallery = project.GetGallery()
    if not gallery:
        return _error("The project gallery is unavailable.", **plan)
    album = gallery.GetCurrentStillAlbum()
    if not album:
        return _error("No current gallery still album to export from.", **plan)
    folder = str(Path(output_path).expanduser())
    Path(folder).mkdir(parents=True, exist_ok=True)
    ok = bool(album.ExportStills([still], folder, name or "still", still_format))
    return _ok(dry_run=False, exported=ok, **plan)


# --- 5. resolve_media ------------------------------------------------------

_MEDIA_ACTIONS = [
    "list_bins", "create_bin", "move_clips_to_bin", "list_clips",
    "set_metadata", "get_metadata", "relink", "import_files",
]

# Clip properties worth surfacing in list_clips without dumping everything.
_CLIP_SUMMARY_KEYS = ["File Path", "Resolution", "FPS", "Duration", "Format", "Type"]


def _clips_by_name(folder: Any, wanted: list[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for item in tools_live._walk_pool_clips(folder):
        n = item.GetName()
        if n in wanted and n not in found:
            found[n] = item
    return found


def resolve_media(
    action: str,
    name: str | None = None,
    parent: str | None = None,
    bin_name: str | None = None,
    clip_names: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    keys: list[str] | None = None,
    folder_path: str | None = None,
    paths: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Media pool: bins, clip listing, metadata, relink, and import.

    Clips are addressed by ``clip_names`` (media-pool item names); bins by name.
    ``import_files`` reuses resolve_import_media internally.
    """
    if action not in _MEDIA_ACTIONS:
        return _bad_action(action, _MEDIA_ACTIONS)

    if action == "import_files":
        if not paths:
            return _error("import_files requires 'paths' (media files to import).")
        return tools_live.import_media(paths, bin_name=bin_name, dry_run=dry_run, confirm=confirm)

    mutating = action in ("create_bin", "move_clips_to_bin", "set_metadata", "relink")
    if mutating:
        label = {
            "create_bin": "Creating a bin",
            "move_clips_to_bin": "Moving clips to a bin",
            "set_metadata": "Setting clip metadata",
            "relink": "Relinking clips",
        }[action]
        guard = _guard_mutation(dry_run, confirm, label)
        if guard:
            return guard
        if action == "create_bin" and not name:
            return _error("create_bin requires 'name' (the new bin's name).")
        if action == "move_clips_to_bin" and (not clip_names or not bin_name):
            return _error("move_clips_to_bin requires 'clip_names' and a target 'bin_name'.")
        if action == "set_metadata" and (not clip_names or not metadata):
            return _error("set_metadata requires 'clip_names' and a 'metadata' object.")
        if action == "relink" and (not clip_names or not folder_path):
            return _error("relink requires 'clip_names' and a 'folder_path' to search.")
        plan = {
            "action": action, "mode": "live", "name": name, "parent": parent,
            "bin_name": bin_name, "clip_names": clip_names, "metadata": metadata,
            "folder_path": folder_path,
        }
        if dry_run:
            return _ok(dry_run=True, plan=plan)

    resolve, err = _connect()
    if err:
        return err
    project, perr = _project(resolve)
    if perr:
        return perr
    pool, poolerr = _pool(project)
    if poolerr:
        return poolerr
    root = pool.GetRootFolder()

    if action == "list_bins":
        return _ok(bins=_walk_folders(root))

    if action == "list_clips":
        folder = _find_folder(root, bin_name) if bin_name else root
        if not folder:
            return _error(f"No bin named '{bin_name}'.")
        clips = []
        for item in folder.GetClipList() or []:
            props = {k: item.GetClipProperty(k) for k in _CLIP_SUMMARY_KEYS}
            clips.append({"name": item.GetName(), "properties": props})
        return _ok(bin=folder.GetName(), clip_count=len(clips), clips=clips)

    if action == "get_metadata":
        if not clip_names:
            return _error("get_metadata requires 'clip_names'.")
        found = _clips_by_name(root, clip_names)
        result = {}
        for n in clip_names:
            item = found.get(n)
            if not item:
                result[n] = None
                continue
            result[n] = item.GetMetadata(keys[0]) if keys and len(keys) == 1 else item.GetMetadata()
        return _ok(metadata=result)

    if action == "create_bin":
        target_parent = _find_folder(root, parent) if parent else root
        if not target_parent:
            return _error(f"No parent bin named '{parent}'.", **plan)
        folder = pool.AddSubFolder(target_parent, name)
        if not folder:
            return _error(f"Resolve did not create bin '{name}'.", **plan)
        return _ok(dry_run=False, created_bin=name, **plan)

    if action == "move_clips_to_bin":
        target = _find_folder(root, bin_name)
        if not target:
            return _error(f"No target bin named '{bin_name}'.", **plan)
        found = _clips_by_name(root, clip_names)
        items = [found[n] for n in clip_names if n in found]
        missing = [n for n in clip_names if n not in found]
        if not items:
            return _error("None of the named clips were found in the media pool.", missing=missing, **plan)
        ok = bool(pool.MoveClips(items, target))
        return _ok(dry_run=False, moved=ok, moved_count=len(items), missing=missing, **plan)

    if action == "set_metadata":
        found = _clips_by_name(root, clip_names)
        results = {}
        for n in clip_names:
            item = found.get(n)
            if not item:
                results[n] = None
                continue
            ok = all(bool(item.SetMetadata(k, str(v))) for k, v in metadata.items())
            results[n] = ok
        return _ok(dry_run=False, results=results, **plan)

    # relink
    found = _clips_by_name(root, clip_names)
    items = [found[n] for n in clip_names if n in found]
    missing = [n for n in clip_names if n not in found]
    if not items:
        return _error("None of the named clips were found to relink.", missing=missing, **plan)
    resolved_folder = str(Path(folder_path).expanduser())
    ok = bool(pool.RelinkClips(items, resolved_folder))
    return _ok(dry_run=False, relinked=ok, relinked_count=len(items), missing=missing, **plan)


# --- 6. resolve_color ------------------------------------------------------

_COLOR_ACTIONS = [
    "copy_grade", "save_still", "apply_still", "list_versions",
    "add_version", "load_version", "set_lut",
]


def resolve_color(
    action: str,
    track_index: int = 1,
    clip_index: int | None = None,
    target_clip_indexes: list[int] | None = None,
    version_name: str | None = None,
    version_type: int = 0,
    lut_path: str | None = None,
    still_path: str | None = None,
    node_index: int = 1,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Color: copy grades, gallery stills, color versions, and LUTs.

    Operates on the CURRENT timeline's video track. ``set_lut`` delegates to the
    same engine as resolve_apply_lut. ``version_type`` is 0 (local) or 1 (remote).
    """
    if action not in _COLOR_ACTIONS:
        return _bad_action(action, _COLOR_ACTIONS)

    if action == "set_lut":
        if not lut_path:
            return _error("set_lut requires 'lut_path' (a .cube file).")
        clip_indexes = [clip_index] if clip_index else None
        return tools_live.apply_lut(
            lut_path, track_index=track_index, clip_indexes=clip_indexes,
            node_index=node_index, dry_run=dry_run, confirm=confirm,
        )

    mutating = action in ("copy_grade", "save_still", "apply_still", "add_version", "load_version")
    if mutating:
        label = {
            "copy_grade": "Copying a grade",
            "save_still": "Saving a still",
            "apply_still": "Applying a still/PowerGrade",
            "add_version": "Adding a color version",
            "load_version": "Loading a color version",
        }[action]
        guard = _guard_mutation(dry_run, confirm, label)
        if guard:
            return guard
        if action == "copy_grade" and (clip_index is None or not target_clip_indexes):
            return _error("copy_grade requires 'clip_index' (source) and 'target_clip_indexes'.")
        if action in ("add_version", "load_version") and not version_name:
            return _error(f"{action} requires 'version_name'.")
        if action == "apply_still" and not still_path:
            return _error(
                "apply_still requires 'still_path' - a .drx still file. The scripting "
                "API applies a grade from a saved DRX still (Graph.ApplyGradeFromDRX), "
                "not directly from a gallery album entry."
            )
        if action in ("copy_grade", "add_version", "load_version", "apply_still") and clip_index is None:
            return _error(f"{action} requires 'clip_index' (1-based clip on the track).")
        plan = {
            "action": action, "mode": "live", "track_index": track_index,
            "clip_index": clip_index, "target_clip_indexes": target_clip_indexes,
            "version_name": version_name, "version_type": version_type,
            "still_path": str(Path(still_path).expanduser()) if still_path else None,
            "node_index": node_index,
        }
        if dry_run:
            return _ok(dry_run=True, plan=plan)

    resolve, err = _connect()
    if err:
        return err
    project, perr = _project(resolve)
    if perr:
        return perr
    timeline, terr = _current_timeline(project)
    if terr:
        return terr

    if action == "save_still":
        still = timeline.GrabStill()
        if not still:
            return _error(
                "Resolve returned no still. Open the Color page and park on a clip first."
            )
        return _ok(dry_run=False, saved=True)

    # The remaining actions target a specific clip.
    if clip_index is None:
        return _error(f"{action} requires 'clip_index'.")
    item, ierr = _track_item(timeline, track_index, clip_index)
    if ierr:
        return ierr

    if action == "list_versions":
        names = item.GetVersionNameList(int(version_type)) or []
        current = item.GetCurrentVersion() if hasattr(item, "GetCurrentVersion") else None
        return _ok(versions=list(names), current_version=current, version_type=version_type)

    if action == "copy_grade":
        items = _timeline_video_items(timeline, track_index)
        targets = []
        for idx in target_clip_indexes:
            if 1 <= idx <= len(items):
                targets.append(items[idx - 1])
        if not targets:
            return _error("No valid target_clip_indexes on the track.", **plan)
        ok = bool(item.CopyGrades(targets))
        return _ok(dry_run=False, copied=ok, source_clip=clip_index,
                   target_count=len(targets), **plan)

    if action == "add_version":
        ok = bool(item.AddVersion(version_name, int(version_type)))
        return _ok(dry_run=False, added=ok, **plan)

    if action == "load_version":
        ok = bool(item.LoadVersionByName(version_name, int(version_type)))
        return _ok(dry_run=False, loaded=ok, **plan)

    # apply_still - via the clip's node graph and a DRX still file.
    resolved_still = str(Path(still_path).expanduser())
    if not Path(resolved_still).exists():
        return _error("The DRX still file does not exist.", **plan)
    graph = item.GetNodeGraph() if hasattr(item, "GetNodeGraph") else None
    if not graph:
        return _error("Could not access the clip's node graph.", **plan)
    ok = bool(graph.ApplyGradeFromDRX(resolved_still, 0))
    if not ok:
        return _error(
            "Resolve rejected the DRX still. Export the still as .drx from the "
            "Gallery first, then pass its path.",
            **plan,
        )
    return _ok(dry_run=False, applied=ok, **plan)
