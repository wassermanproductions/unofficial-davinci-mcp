import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import assemble  # noqa: E402


def test_validate_good_plan():
    v = fx.graded_target_mp4()
    a = fx.dialogue_wav()
    plan = {
        "timeline": {"name": "Test", "fps": 25, "resolution": "320x180"},
        "video": [{"path": v, "in": 0.0, "out": 1.0}],
        "audio": [{"path": a, "in": 0.0, "out": 2.0, "gain_db": -3.0, "fade_out": 0.5}],
        "markers": [{"time": 0.5, "name": "hit"}],
    }
    result = assemble.assemble_edit(plan)
    assert result["ok"], result
    assert result["errors"] == []
    norm = result["normalized_plan"]
    assert norm["video"][0]["out"] == 1.0
    assert norm["audio"][0]["gain_db"] == -3.0
    assert result["counts"] == {"video": 1, "audio": 1, "markers": 1}


def test_invalid_ranges_flagged():
    v = fx.graded_target_mp4()
    plan = {
        "video": [{"path": v, "in": 1.0, "out": 0.5}],  # out < in
    }
    result = assemble.assemble_edit(plan)
    assert result["ok"] is False
    assert any("out" in e for e in result["errors"])


def test_missing_media_flagged():
    plan = {"video": [{"path": "/nope/missing.mov", "in": 0, "out": 1}]}
    result = assemble.assemble_edit(plan)
    assert result["ok"] is False
    assert any("unreadable" in e or "does not exist" in e.lower() for e in result["errors"])


def test_beat_snap_from_click_track():
    click = fx.click_track_wav(120.0)  # beats at 0.5s intervals
    v = fx.graded_target_mp4()
    # Put a cut point close to a beat (2.53 -> should snap to 2.5).
    plan = {
        "video": [{"path": v, "in": 0.0, "out": 1.53}],
        "audio": [{"path": click, "in": 0.0, "out": 2.53}],
        "beat_snap": {"grid": click, "tolerance": 0.08},
    }
    result = assemble.assemble_edit(plan)
    assert result["ok"], result
    assert result["snapped"], "expected at least one snap"
    # Find the audio out snap.
    snapped_targets = {round(s["to"], 2) for s in result["snapped"]}
    assert any(abs(t - 2.5) < 0.06 or abs(t - 1.5) < 0.06 for t in snapped_targets), result["snapped"]


def test_beat_snap_inline_grid():
    v = fx.graded_target_mp4()
    plan = {
        "video": [{"path": v, "in": 0.0, "out": 0.98}],
        "beat_snap": {"grid": [0.0, 0.5, 1.0, 1.5], "tolerance": 0.05},
    }
    result = assemble.assemble_edit(plan)
    assert result["ok"], result
    assert result["normalized_plan"]["video"][0]["out"] == 1.0


def test_to_fcpxml_integration_point():
    v = fx.graded_target_mp4()
    plan = {
        "timeline": {"name": "IT", "fps": 25, "resolution": "320x180"},
        "video": [{"path": v, "in": 0.0, "out": 1.0}],
        "markers": [{"time": 0.5, "name": "m"}],
    }
    result = assemble.to_fcpxml(plan, dry_run=True)
    assert isinstance(result, dict) and "ok" in result
    # If the sibling's generator is wired, a dry-run plan should succeed;
    # otherwise we must get the clearly-marked integration point.
    if not result["ok"]:
        assert "integration_point" in result or "tools_interchange" in result.get("error", "")
    else:
        assert result.get("ok") is True


def test_to_edl_integration_point():
    v = fx.graded_target_mp4()
    plan = {
        "timeline": {"name": "IT", "fps": 25, "resolution": "320x180"},
        "video": [{"path": v, "in": 0.0, "out": 1.0}],
    }
    result = assemble.to_edl(plan, dry_run=True)
    assert isinstance(result, dict) and "ok" in result
    if not result["ok"]:
        assert "integration_point" in result or "tools_interchange" in result.get("error", "")
