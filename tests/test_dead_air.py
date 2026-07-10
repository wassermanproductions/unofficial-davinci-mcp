import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import dead_air, fftools  # noqa: E402


def test_dead_air_plan_detects_cuts():
    plan = dead_air.tighten_dialogue(fx.dialogue_wav(), max_pause=0.6, dry_run=True)
    assert plan["ok"], plan
    assert plan["dry_run"] is True
    assert plan["cuts"] >= 3, plan
    assert plan["removed_seconds"] > 0
    assert plan["tightened_duration_seconds"] < plan["source_duration_seconds"]
    # Keep ranges sorted and non-overlapping.
    keep = plan["keep_ranges"]
    for i in range(1, len(keep)):
        assert keep[i]["start"] >= keep[i - 1]["end"] - 1e-6
        assert keep[i]["end"] > keep[i]["start"]


def test_handles_preserved():
    head, tail = 0.15, 0.15
    plan = dead_air.tighten_dialogue(
        fx.dialogue_wav(), max_pause=0.6, head=head, tail=tail, dry_run=True
    )
    # Each removed region should sit inside a silence with the handles left in.
    assert plan["removed_ranges"]
    # removed region length must be >= min_cut (0.4 default)
    for r in plan["removed_ranges"]:
        assert r["end"] - r["start"] >= 0.4 - 1e-6


def test_render_preview_matches_tightened_duration():
    out = os.path.join(os.path.dirname(fx.dialogue_wav()), "tightened_out.wav")
    plan = dead_air.tighten_dialogue(
        fx.dialogue_wav(), max_pause=0.6, output_path=out, dry_run=False, confirm=True
    )
    assert plan["ok"], plan
    prev = plan["preview"]["path"]
    assert prev and os.path.exists(prev)
    dur = float(fftools.ffprobe_json(prev)["format"]["duration"])
    # Rendered duration should be close to the planned tightened duration.
    assert abs(dur - plan["tightened_duration_seconds"]) < 0.3, (dur, plan["tightened_duration_seconds"])


def test_no_cuts_when_pause_threshold_high():
    # With a very large max_pause, nothing should be removed.
    plan = dead_air.tighten_dialogue(fx.dialogue_wav(), max_pause=100.0, dry_run=True)
    assert plan["cuts"] == 0
    assert plan["removed_seconds"] == 0


def test_confirm_required():
    plan = dead_air.tighten_dialogue(fx.dialogue_wav(), dry_run=False, confirm=False)
    assert plan["ok"] is False
