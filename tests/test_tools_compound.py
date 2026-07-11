"""Compound live tools against a mocked Resolve.

Each action is exercised for its dry-run plan, its confirmed execution, and -
where the documented API cannot do the work - its capability-error path.
"""

from __future__ import annotations

from davinci_mcp import resolve_api, tools_compound


# --- 1. resolve_project ----------------------------------------------------


def test_project_unknown_action_lists_valid(mock_resolve):
    result = tools_compound.resolve_project("frobnicate")
    assert result["ok"] is False
    assert "list_projects" in result["error"]


def test_project_list_projects(mock_resolve):
    result = tools_compound.resolve_project("list_projects")
    assert result["ok"] is True
    assert "FakeProject" in result["projects"]


def test_project_current(mock_resolve):
    result = tools_compound.resolve_project("current")
    assert result["ok"] is True
    assert result["project_name"] == "FakeProject"


def test_project_get_settings_snapshot_and_keys(mock_resolve):
    snap = tools_compound.resolve_project("get_settings")
    assert snap["ok"] is True
    assert snap["settings"]["timelineFrameRate"] == "24"
    keyed = tools_compound.resolve_project("get_settings", keys=["timelineResolutionWidth"])
    assert keyed["settings"] == {"timelineResolutionWidth": "1920"}


def test_project_set_settings_dry_run_then_confirm(mock_resolve):
    plan = tools_compound.resolve_project(
        "set_settings", settings={"timelineFrameRate": "30"}, dry_run=True
    )
    assert plan["dry_run"] is True
    assert mock_resolve.calls == []
    done = tools_compound.resolve_project(
        "set_settings", settings={"timelineFrameRate": "30"}, dry_run=False, confirm=True
    )
    assert done["ok"] is True
    assert done["applied_count"] == 1
    assert "SetSetting" in mock_resolve.names()


def test_project_set_settings_requires_settings(mock_resolve):
    result = tools_compound.resolve_project("set_settings", dry_run=True)
    assert result["ok"] is False


def test_project_open_and_create(mock_resolve):
    opened = tools_compound.resolve_project("open", name="FakeProject", dry_run=False, confirm=True)
    assert opened["ok"] is True and opened["opened"] == "FakeProject"
    created = tools_compound.resolve_project("create", name="New Cut", dry_run=False, confirm=True)
    assert created["ok"] is True
    # A name the fake refuses returns a friendly error.
    dup = tools_compound.resolve_project("create", name="Duplicate Name", dry_run=False, confirm=True)
    assert dup["ok"] is False


def test_project_save(mock_resolve):
    result = tools_compound.resolve_project("save", dry_run=False, confirm=True)
    assert result["ok"] is True and result["saved"] is True


# --- 2. resolve_timelines --------------------------------------------------


def test_timelines_list(mock_resolve):
    result = tools_compound.resolve_timelines("list")
    assert result["ok"] is True
    assert result["timeline_count"] == 2
    assert result["timelines"][0]["is_current"] is True


def test_timelines_info(mock_resolve):
    result = tools_compound.resolve_timelines("info", index=1)
    assert result["ok"] is True
    info = result["info"]
    assert info["frame_count"] == 240
    assert info["track_counts"]["video"] == 1


def test_timelines_switch_by_name(mock_resolve):
    result = tools_compound.resolve_timelines(
        "switch", name="Timeline 2", dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert result["current"] == "Timeline 2"


def test_timelines_switch_missing_name_errors(mock_resolve):
    result = tools_compound.resolve_timelines(
        "switch", name="Nope", dry_run=False, confirm=True
    )
    assert result["ok"] is False


def test_timelines_duplicate(mock_resolve):
    result = tools_compound.resolve_timelines(
        "duplicate", name="Timeline 1", new_name="Timeline 1 v2", dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert result["duplicated"] == "Timeline 1 v2"
    assert "DuplicateTimeline" in mock_resolve.names()


def test_timelines_delete(mock_resolve):
    result = tools_compound.resolve_timelines(
        "delete", index=2, dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert "DeleteTimelines" in mock_resolve.names()


def test_timelines_export_maps_format(mock_resolve, tmp_path):
    out = tmp_path / "cut.fcpxml"
    result = tools_compound.resolve_timelines(
        "export", format="fcpxml", output_path=str(out), dry_run=False, confirm=True
    )
    assert result["ok"] is True
    export_calls = [c for c in mock_resolve.calls if c[0] == "Export"]
    assert export_calls
    # FCPXML 1.9 constant value (5) and EXPORT_NONE subtype (100) from the fake.
    assert export_calls[0][1][1] == 5
    assert export_calls[0][1][2] == 100


def test_timelines_export_unknown_format_errors(mock_resolve, tmp_path):
    result = tools_compound.resolve_timelines(
        "export", format="quicktime", output_path=str(tmp_path / "x"),
        dry_run=False, confirm=True,
    )
    assert result["ok"] is False
    assert "Unsupported export format" in result["error"]


def test_timelines_export_dry_run_no_calls(mock_resolve, tmp_path):
    result = tools_compound.resolve_timelines(
        "export", format="edl", output_path=str(tmp_path / "c.edl"), dry_run=True
    )
    assert result["dry_run"] is True
    assert mock_resolve.calls == []


# --- 3. resolve_edit -------------------------------------------------------


def test_edit_set_speed_is_capability_error(mock_resolve):
    result = tools_compound.resolve_edit("set_speed", clip_index=1, speed=2.0)
    assert result["ok"] is False
    assert "cannot change clip speed" in result["error"]
    assert mock_resolve.calls == []


def test_edit_add_transition_is_capability_error(mock_resolve):
    result = tools_compound.resolve_edit("add_transition", clip_index=1)
    assert result["ok"] is False
    assert "cannot add transitions" in result["error"]


def test_edit_move_clip_dry_run_discloses_reappend(mock_resolve):
    result = tools_compound.resolve_edit(
        "move_clip", track_index=1, clip_index=1, record_frame=120, dry_run=True
    )
    assert result["dry_run"] is True
    assert "re-append" in result["plan"]["note"]
    assert mock_resolve.calls == []


def test_edit_move_clip_confirmed_deletes_and_reappends(mock_resolve):
    result = tools_compound.resolve_edit(
        "move_clip", track_index=1, clip_index=1, record_frame=120,
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    names = mock_resolve.names()
    assert "DeleteClips" in names
    assert "AppendToTimeline" in names
    append = [c for c in mock_resolve.calls if c[0] == "AppendToTimeline"][0]
    assert append[1][0][0]["recordFrame"] == 120


def test_edit_trim_adjusts_source_range(mock_resolve):
    result = tools_compound.resolve_edit(
        "trim", track_index=1, clip_index=1, start_frame=10, end_frame=60,
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    append = [c for c in mock_resolve.calls if c[0] == "AppendToTimeline"][0]
    payload = append[1][0][0]
    assert payload["startFrame"] == 10
    assert payload["endFrame"] == 60


def test_edit_trim_rejects_bad_range(mock_resolve):
    result = tools_compound.resolve_edit(
        "trim", clip_index=1, start_frame=60, end_frame=10, dry_run=False, confirm=True
    )
    assert result["ok"] is False
    assert "greater than" in result["error"]


def test_edit_ripple_delete(mock_resolve):
    result = tools_compound.resolve_edit(
        "ripple_delete", track_index=1, clip_index=2, dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert result["ripple"] is True
    delete = [c for c in mock_resolve.calls if c[0] == "DeleteClips"][0]
    assert delete[1][1] is True  # ripple flag


def test_edit_delete_clip(mock_resolve):
    result = tools_compound.resolve_edit(
        "delete_clip", track_index=1, clip_index=1, dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert result["ripple"] is False


def test_edit_insert_clip_reuses_append(mock_resolve, make_media):
    clip = make_media("insert", "video")
    result = tools_compound.resolve_edit(
        "insert_clip", path=clip, start_frame=0, end_frame=24, record_frame=48,
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    assert result["appended_count"] == 1


def test_edit_add_title_inserts_but_flags_text_limit(mock_resolve):
    result = tools_compound.resolve_edit(
        "add_title", title_name="Text", dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert result["text_set"] is False
    assert "InsertTitleIntoTimeline" in mock_resolve.names()


def test_edit_move_requires_record_frame(mock_resolve):
    result = tools_compound.resolve_edit(
        "move_clip", clip_index=1, dry_run=False, confirm=True
    )
    assert result["ok"] is False
    assert "record_frame" in result["error"]


# --- 4. resolve_review -----------------------------------------------------


def test_review_page_opens(mock_resolve):
    result = tools_compound.resolve_review("page", page="color")
    assert result["ok"] is True
    assert result["opened_page"] == "color"
    assert "OpenPage" in mock_resolve.names()


def test_review_page_invalid(mock_resolve):
    result = tools_compound.resolve_review("page", page="nonsense")
    assert result["ok"] is False


def test_review_goto_and_current_timecode(mock_resolve):
    goto = tools_compound.resolve_review("goto", timecode="01:00:05:00")
    assert goto["ok"] is True and goto["moved"] is True
    now = tools_compound.resolve_review("current_timecode")
    assert now["timecode"] == "01:00:05:00"


def test_review_markers_list_and_update(mock_resolve):
    # Seed a marker through the timeline, then update it via the tool.
    from davinci_mcp import tools_live

    tools_live.add_markers([{"frame": 96, "name": "beat", "color": "Blue"}], dry_run=False, confirm=True)
    listed = tools_compound.resolve_review("markers_list")
    assert listed["marker_count"] == 1
    updated = tools_compound.resolve_review(
        "marker_update", frame=96, color="Red", name="hit", dry_run=False, confirm=True
    )
    assert updated["ok"] is True
    assert updated["marker"]["color"] == "Red"
    # delete-then-re-add is how an update is done.
    assert mock_resolve.names().count("DeleteMarkerAtFrame") == 1


def test_review_marker_update_missing_frame_errors(mock_resolve):
    result = tools_compound.resolve_review(
        "marker_update", frame=999, color="Red", dry_run=False, confirm=True
    )
    assert result["ok"] is False


def test_review_marker_delete(mock_resolve):
    from davinci_mcp import tools_live

    tools_live.add_markers([{"frame": 48, "name": "x"}], dry_run=False, confirm=True)
    result = tools_compound.resolve_review(
        "marker_delete", frame=48, dry_run=False, confirm=True
    )
    assert result["ok"] is True and result["deleted"] is True


def test_review_grab_still(mock_resolve):
    result = tools_compound.resolve_review("grab_still", dry_run=False, confirm=True)
    assert result["ok"] is True
    assert "GrabStill" in mock_resolve.names()


def test_review_export_still(mock_resolve, tmp_path):
    result = tools_compound.resolve_review(
        "export_still", output_path=str(tmp_path), name="frame", still_format="png",
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    assert "ExportStills" in mock_resolve.names()


def test_review_export_still_requires_output(mock_resolve):
    result = tools_compound.resolve_review("export_still", dry_run=False, confirm=True)
    assert result["ok"] is False


# --- 5. resolve_media ------------------------------------------------------


def test_media_list_bins(mock_resolve):
    result = tools_compound.resolve_media("list_bins")
    assert result["ok"] is True
    assert result["bins"][0]["name"] == "Master"


def test_media_import_files_reuses_engine(mock_resolve, make_media):
    clip = make_media("m", "video")
    result = tools_compound.resolve_media(
        "import_files", paths=[clip], bin_name="Footage", dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert result["imported_count"] == 1


def test_media_create_bin(mock_resolve):
    result = tools_compound.resolve_media(
        "create_bin", name="Selects", dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert result["created_bin"] == "Selects"
    assert "AddSubFolder" in mock_resolve.names()


def test_media_list_clips_and_metadata_round_trip(mock_resolve, make_media):
    clip = make_media("meta", "video")
    tools_compound.resolve_media("import_files", paths=[clip], dry_run=False, confirm=True)
    listed = tools_compound.resolve_media("list_clips")
    assert listed["clip_count"] == 1
    name = listed["clips"][0]["name"]
    setres = tools_compound.resolve_media(
        "set_metadata", clip_names=[name], metadata={"Scene": "12"},
        dry_run=False, confirm=True,
    )
    assert setres["ok"] is True and setres["results"][name] is True
    getres = tools_compound.resolve_media("get_metadata", clip_names=[name], keys=["Scene"])
    assert getres["metadata"][name] == "12"


def test_media_move_clips_to_bin(mock_resolve, make_media):
    clip = make_media("mv", "video")
    tools_compound.resolve_media("import_files", paths=[clip], dry_run=False, confirm=True)
    tools_compound.resolve_media("create_bin", name="B-Roll", dry_run=False, confirm=True)
    name = tools_compound.resolve_media("list_clips")["clips"][0]["name"]
    result = tools_compound.resolve_media(
        "move_clips_to_bin", clip_names=[name], bin_name="B-Roll",
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    assert result["moved_count"] == 1
    assert "MoveClips" in mock_resolve.names()


def test_media_relink(mock_resolve, make_media, tmp_path):
    clip = make_media("rl", "video")
    tools_compound.resolve_media("import_files", paths=[clip], dry_run=False, confirm=True)
    name = tools_compound.resolve_media("list_clips")["clips"][0]["name"]
    result = tools_compound.resolve_media(
        "relink", clip_names=[name], folder_path=str(tmp_path),
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    assert "RelinkClips" in mock_resolve.names()


def test_media_relink_requires_args(mock_resolve):
    result = tools_compound.resolve_media("relink", clip_names=["x"], dry_run=True)
    assert result["ok"] is False


# --- 6. resolve_color ------------------------------------------------------


def test_color_list_versions(mock_resolve):
    result = tools_compound.resolve_color("list_versions", clip_index=1)
    assert result["ok"] is True
    assert "Version 1" in result["versions"]


def test_color_add_and_load_version(mock_resolve):
    add = tools_compound.resolve_color(
        "add_version", clip_index=1, version_name="Warm", dry_run=False, confirm=True
    )
    assert add["ok"] is True and "AddVersion" in mock_resolve.names()
    load = tools_compound.resolve_color(
        "load_version", clip_index=1, version_name="Warm", dry_run=False, confirm=True
    )
    assert load["ok"] is True and "LoadVersionByName" in mock_resolve.names()


def test_color_copy_grade(mock_resolve):
    result = tools_compound.resolve_color(
        "copy_grade", clip_index=1, target_clip_indexes=[2],
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    assert result["target_count"] == 1
    assert "CopyGrades" in mock_resolve.names()


def test_color_save_still(mock_resolve):
    result = tools_compound.resolve_color("save_still", dry_run=False, confirm=True)
    assert result["ok"] is True
    assert "GrabStill" in mock_resolve.names()


def test_color_apply_still_from_drx(mock_resolve, tmp_path):
    drx = tmp_path / "look.drx"
    drx.write_text("<drx/>", encoding="utf-8")
    result = tools_compound.resolve_color(
        "apply_still", clip_index=1, still_path=str(drx), dry_run=False, confirm=True
    )
    assert result["ok"] is True
    assert "ApplyGradeFromDRX" in mock_resolve.names()


def test_color_apply_still_missing_file(mock_resolve, tmp_path):
    result = tools_compound.resolve_color(
        "apply_still", clip_index=1, still_path=str(tmp_path / "ghost.drx"),
        dry_run=False, confirm=True,
    )
    assert result["ok"] is False


def test_color_apply_still_requires_drx(mock_resolve):
    result = tools_compound.resolve_color("apply_still", clip_index=1, dry_run=True)
    assert result["ok"] is False
    assert ".drx" in result["error"]


def test_color_set_lut_delegates(mock_resolve, tmp_path):
    lut = tmp_path / "look.cube"
    lut.write_text("# LUT\n", encoding="utf-8")
    result = tools_compound.resolve_color(
        "set_lut", lut_path=str(lut), track_index=1, clip_index=1,
        dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    assert "SetLUT" in mock_resolve.names()


# --- connection + confirm contract (shared across tools) -------------------


def test_compound_reports_friendly_error_when_unreachable(monkeypatch):
    status = resolve_api.ResolveStatus(
        resolve_api.ResolveStatus.FREE_EDITION, "Studio required."
    )
    monkeypatch.setattr(resolve_api, "connect", lambda: status)
    result = tools_compound.resolve_timelines("list")
    assert result["ok"] is False
    assert "Studio required." in result["error"]


def test_compound_confirm_required(mock_resolve):
    result = tools_compound.resolve_project(
        "set_settings", settings={"timelineFrameRate": "25"}, dry_run=False, confirm=False
    )
    assert result["ok"] is False
    assert "confirm=true" in result["error"]
    assert mock_resolve.calls == []


def test_resolve_edit_accepts_seconds(mock_resolve, make_media):
    """in_seconds/out_seconds convert with the media frame rate, like create_timeline."""
    from davinci_mcp import tools_compound

    clip = make_media("sec", "video", seconds=2.0)
    result = tools_compound.resolve_edit(
        "insert_clip", path=clip, in_seconds=0.5, out_seconds=1.5, dry_run=True,
    )
    assert result["ok"] is True
    plan_clip = result["plan"]["clips"][0]
    assert plan_clip["start_frame"] == 12
    assert plan_clip["end_frame"] == 36
