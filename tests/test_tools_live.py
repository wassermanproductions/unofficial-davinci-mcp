"""Live tools against a mocked Resolve: dry-run plans vs confirmed execution."""

from __future__ import annotations

from davinci_mcp import resolve_api, tools_live


# --- dry_run / confirm contract -------------------------------------------


def test_import_media_dry_run_makes_no_calls(mock_resolve, make_media):
    path = make_media("a", "video")
    result = tools_live.import_media([path], dry_run=True)
    assert result["dry_run"] is True
    assert mock_resolve.calls == []  # nothing touched Resolve


def test_confirm_required_when_not_dry_run(mock_resolve, make_media):
    path = make_media("a", "video")
    result = tools_live.import_media([path], dry_run=False, confirm=False)
    assert result["ok"] is False
    assert "confirm=true" in result["error"]
    assert mock_resolve.calls == []


def test_import_media_confirmed_calls_import(mock_resolve, make_media):
    path = make_media("a", "video")
    result = tools_live.import_media([path], bin_name="Footage", dry_run=False, confirm=True)
    assert result["ok"] is True
    assert result["imported_count"] == 1
    names = mock_resolve.names()
    assert "AddSubFolder" in names
    assert "ImportMedia" in names


def test_import_media_missing_file_errors(mock_resolve, tmp_path):
    result = tools_live.import_media([str(tmp_path / "ghost.mov")], dry_run=False, confirm=True)
    assert result["ok"] is False
    assert result["missing_paths"]


# --- create / append / markers --------------------------------------------


def test_create_timeline_with_clip_ranges(mock_resolve, make_media):
    clips = [
        {"path": make_media("a", "video"), "start_frame": 0, "end_frame": 48},
        {"path": make_media("b", "video"), "start_frame": 12},
    ]
    result = tools_live.create_timeline("Cut", clips=clips, dry_run=False, confirm=True)
    assert result["ok"] is True
    assert result["timeline_name"] == "Cut"
    assert result["appended_count"] == 2
    assert "CreateEmptyTimeline" in mock_resolve.names()
    # The append payload carried the source range.
    append_calls = [c for c in mock_resolve.calls if c[0] == "AppendToTimeline"]
    payloads = append_calls[0][1][0]
    assert payloads[0]["startFrame"] == 0
    assert payloads[0]["endFrame"] == 48


def test_create_timeline_rejects_bad_range(mock_resolve, make_media):
    clips = [{"path": make_media("a", "video"), "start_frame": 50, "end_frame": 10}]
    result = tools_live.create_timeline("Cut", clips=clips, dry_run=True)
    assert result["ok"] is False
    assert "end_frame" in result["error"]


def test_add_markers_confirmed(mock_resolve):
    markers = [{"frame": 0, "name": "start"}, {"frame": 48, "name": "mid", "color": "Green"}]
    result = tools_live.add_markers(markers, dry_run=False, confirm=True)
    assert result["ok"] is True
    assert len(result["added_markers"]) == 2
    assert mock_resolve.names().count("AddMarker") == 2


# --- grades and LUTs -------------------------------------------------------


def test_apply_lut_to_all_clips(mock_resolve, tmp_path):
    lut = tmp_path / "look.cube"
    lut.write_text("# LUT\n", encoding="utf-8")
    result = tools_live.apply_lut(str(lut), track_index=1, dry_run=False, confirm=True)
    assert result["ok"] is True
    assert result["applied_count"] == 2  # two clips on the fake track
    assert mock_resolve.names().count("SetLUT") == 2


def test_apply_lut_missing_file_errors(mock_resolve, tmp_path):
    result = tools_live.apply_lut(str(tmp_path / "none.cube"), dry_run=False, confirm=True)
    assert result["ok"] is False


def test_set_grade_builds_cdl(mock_resolve):
    result = tools_live.set_grade(
        slope=[1.1, 1.0, 0.9], offset=[0.0, 0.0, 0.05], saturation=0.8,
        clip_indexes=[1], dry_run=False, confirm=True,
    )
    assert result["ok"] is True
    assert result["applied_count"] == 1
    cdl_calls = [c for c in mock_resolve.calls if c[0] == "SetCDL"]
    assert cdl_calls[0][1][0]["Slope"] == "1.1 1.0 0.9"
    assert cdl_calls[0][1][0]["Saturation"] == "0.8"


# --- render + status -------------------------------------------------------


def test_render_dry_run_plan(mock_resolve, tmp_path):
    result = tools_live.render(str(tmp_path), "out", dry_run=True)
    assert result["dry_run"] is True
    assert result["plan"]["render_settings"]["CustomName"] == "out"
    assert mock_resolve.calls == []


def test_render_confirmed_creates_job(mock_resolve, tmp_path):
    result = tools_live.render(str(tmp_path), "out", dry_run=False, confirm=True)
    assert result["ok"] is True
    assert result["job_id"] == "job-1"
    assert result["started"] is True
    assert "AddRenderJob" in mock_resolve.names()


def test_project_summary_reads_state(mock_resolve):
    result = tools_live.project_summary()
    assert result["ok"] is True
    assert result["project_name"] == "FakeProject"


def test_live_tool_reports_friendly_error_when_unreachable(monkeypatch):
    status = resolve_api.ResolveStatus(
        resolve_api.ResolveStatus.FREE_EDITION, "Studio required."
    )
    monkeypatch.setattr(resolve_api, "connect", lambda: status)
    result = tools_live.project_summary()
    assert result["ok"] is False
    assert result["resolve_state"] == resolve_api.ResolveStatus.FREE_EDITION
    assert "Studio required." in result["error"]


def test_create_timeline_seconds_ranges_convert_to_frames(mock_resolve, make_media):
    """in_seconds/out_seconds must become real frame ranges - never silently drop."""
    from davinci_mcp import tools_live

    clip = make_media("ranged", "video", seconds=2.0)
    result = tools_live.create_timeline(
        "SecCut",
        clips=[{"path": clip, "in_seconds": 0.5, "out_seconds": 1.5}],
        dry_run=True,
    )
    plan_clip = result["plan"]["clips"][0]
    assert plan_clip["start_frame"] == 12  # 0.5s * 24fps
    assert plan_clip["end_frame"] == 36


def test_create_timeline_rejects_unknown_clip_keys(mock_resolve, make_media):
    from davinci_mcp import tools_live

    clip = make_media("badkeys", "video")
    result = tools_live.create_timeline(
        "BadCut", clips=[{"path": clip, "in_secs": 1}], dry_run=True,
    )
    assert result["ok"] is False
    assert "unknown keys" in result["error"]


def test_create_timeline_music_drops_clip_audio(mock_resolve, make_media):
    """Supplying music implies picture-only video clips by default."""
    from davinci_mcp import tools_live

    clip = make_media("vid", "video")
    music = make_media("song", "audio")
    result = tools_live.create_timeline(
        "MusicCut",
        clips=[{"path": clip, "start_frame": 0, "end_frame": 24}],
        music_paths=[music],
        dry_run=False,
        confirm=True,
    )
    assert result["ok"] is True
    appends = [c for c in mock_resolve.calls if c[0] == "AppendToTimeline"]
    video_payloads = appends[0][1][0]
    assert all(p.get("mediaType") == 1 for p in video_payloads)
