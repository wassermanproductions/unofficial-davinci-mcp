"""Tests for the auto-tuned timeline grade (engines/grade_timeline.py).

Real ffmpeg fixtures: a clean self-match that passes on the first try, and a
dark noisy clip against a bright contrasty reference that trips a gate at every
strength and must come back flagged needs_human with its best attempt.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import grade_timeline  # noqa: E402


def test_strength_schedule_respects_floor():
    # From 1.0, three tries never reach below the floor.
    assert grade_timeline._strength_schedule(1.0, 3, 0.5) == [1.0, 0.8, 0.64]
    # From 0.6, it clamps at the floor and stops repeating.
    sched = grade_timeline._strength_schedule(0.6, 3, 0.5)
    assert sched[0] == 0.6 and min(sched) >= 0.5 and sched == [0.6, 0.5]


def test_dry_run_plans_without_baking(tmp_path):
    ref = fx.plain_image_png()
    target = fx.graded_target_mp4()
    r = grade_timeline.grade_timeline(ref, [{"path": target}], output_dir=str(tmp_path))
    assert r["ok"] and r["dry_run"] is True
    assert r["plan"]["clip_count"] == 1
    assert r["plan"]["strength_schedule"][0] == 1.0
    assert not any(f.endswith(".cube") for f in os.listdir(tmp_path))


def test_clean_match_passes_first_try(tmp_path):
    """Matching a clip to itself converges with no quality flags -> one attempt."""
    clip = fx.graded_reference_mp4()
    r = grade_timeline.grade_timeline(
        clip, [{"path": clip}], method="reinhard", chroma="preserve",
        output_dir=str(tmp_path), dry_run=False, confirm=True,
    )
    assert r["ok"], r
    res = r["results"][0]
    assert res["needs_human"] is False, res
    assert res["status"] == "ok"
    assert res["attempts"] == 1  # passed on the first strength
    assert os.path.exists(res["lut_path"])
    assert r["needs_human_count"] == 0


def test_auto_tune_flags_needs_human_on_bad_clip(tmp_path, ffmpeg_bin):
    """A dark, noisy clip forced toward a bright contrasty reference cannot pass
    the gates; the loop retries and returns needs_human with the best attempt."""
    noisy = tmp_path / "dark_noisy.mp4"
    subprocess.run([
        ffmpeg_bin, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=24",
        "-vf", "eq=brightness=-0.35:saturation=0.6,noise=alls=18:allf=t",
        "-pix_fmt", "yuv420p", str(noisy),
    ], check=True, capture_output=True)
    bright_ref = tmp_path / "bright.png"
    subprocess.run([
        ffmpeg_bin, "-y", "-f", "lavfi",
        "-i", "testsrc=duration=1:size=320x240:rate=1",
        "-vf", "eq=brightness=0.28:contrast=1.5", "-frames:v", "1", str(bright_ref),
    ], check=True, capture_output=True)

    r = grade_timeline.grade_timeline(
        str(bright_ref), [{"path": str(noisy), "in": 0.0, "out": 1.0}],
        method="lab_histogram", chroma="match", strength=1.0,
        output_dir=str(tmp_path), dry_run=False, confirm=True,
    )
    assert r["ok"], r
    res = r["results"][0]
    assert res["needs_human"] is True, res
    assert res["status"] == "needs_human"
    # It exhausted the retry schedule (more than one attempt).
    assert res["attempts"] >= 2, res
    assert len(res["quality"]["flags"]) >= 1  # best attempt still flagged
    # Best attempt still produced a LUT to hand a colorist as a starting point.
    assert res["lut_path"] and os.path.exists(res["lut_path"])
    assert r["needs_human_count"] == 1


def test_apply_manifest_maps_clips(tmp_path):
    clip = fx.graded_reference_mp4()
    r = grade_timeline.grade_timeline(
        clip, [{"path": clip}, {"path": clip}], method="reinhard",
        output_dir=str(tmp_path), dry_run=False, confirm=True,
    )
    assert r["ok"], r
    live = r["apply_manifest"]["live"]
    assert live["tool"] == "resolve_apply_lut"
    assert [c["clip_index"] for c in live["clips"]] == [1, 2]
    inter = r["apply_manifest"]["interchange"]
    assert len(inter["luts"]) == 2
    assert any("node" in s.lower() or "lut" in s.lower() for s in inter["instructions"])


def test_bad_inputs(tmp_path):
    ref = fx.plain_image_png()
    assert grade_timeline.grade_timeline("/no/ref.png", [{"path": "x"}])["ok"] is False
    assert grade_timeline.grade_timeline(ref)["ok"] is False  # no clips
    assert grade_timeline.grade_timeline(ref, [{"path": "x"}], method="nope")["ok"] is False
