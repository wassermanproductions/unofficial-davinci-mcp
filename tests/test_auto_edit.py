"""Tests for the music-driven auto-edit engine (engines/auto_edit.py).

Real ffmpeg fixtures: a handful of distinct short videos plus click-track music
(steady and energy-ramped). Assertions cover cut count, beat-aligned cut times
(+/-2 frames), the no-adjacent-same-source variety rule, energy->density
mapping, and the footage-shorter-than-target safety note.
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import auto_edit, beat_grid, fftools  # noqa: E402

_SR = 48000


def _clips(n: int = 3) -> list[str]:
    """n distinct 2s videos (different grades so they are genuinely different)."""
    eqs = [
        "eq=gamma_r=1.4:saturation=1.2",
        "eq=gamma_b=1.4:saturation=0.9",
        "eq=gamma_g=1.3:contrast=1.1",
        "hue=h=90:s=1.1",
    ]
    return [fx._make_graded_video(f"ae_clip{i}.mp4", eqs[i % len(eqs)]) for i in range(n)]


def _ramp_click_wav() -> str:
    """120 BPM clicks; softer (0.5) for the first 4s, loud (1.0) for the last 4s.

    All beats stay loud enough to track cleanly across the whole track, while
    beat_grid's normalised onset strengths give a clean low->high energy split
    for the density-mapping test.
    """
    dur, bpm = 8.0, 120.0
    n = int(dur * _SR)
    sig = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm
    click_len = int(0.03 * _SR)
    ct = np.arange(click_len) / _SR
    click = (np.sin(2 * np.pi * 1000.0 * ct) * np.exp(-60.0 * ct)).astype(np.float32)
    beat = 0.0
    while beat < dur:
        amp = 0.5 if beat < 4.0 else 1.0
        start = int(beat * _SR)
        end = min(n, start + click_len)
        sig[start:end] += amp * click[: end - start]
        beat += period
    stereo = np.stack([sig, sig], axis=-1)
    p = os.path.join(os.path.dirname(fx.click_track_wav()), "ramp_click.wav")
    fftools.encode_wav(stereo, p, sr=_SR)
    return p


def test_auto_edit_plan_shape():
    clips = _clips(3)
    music = fx.click_track_wav(120.0)
    r = auto_edit.auto_edit(music, target_seconds=4.0, clips=clips, output="plan")
    assert r["ok"], r
    assert r["shot_count"] >= 2
    assert len(r["cut_list"]) == r["shot_count"]
    # Every shot carries a rationale and a section label.
    for s in r["cut_list"]:
        assert s["rationale"]
        assert s["section"] in {"chorus/drop", "verse", "intro/breakdown"}
    # The normalized plan's video track has exactly one clip per shot.
    assert len(r["normalized_plan"]["video"]) == r["shot_count"]
    # Live-tier clips are shaped for resolve_create_timeline.
    lc = r["live_plan"]["clips"][0]
    assert set(lc) >= {"path", "in_seconds", "out_seconds", "fps"}


def test_auto_edit_cuts_land_on_beats():
    clips = _clips(3)
    music = fx.click_track_wav(120.0)
    fps = 24
    r = auto_edit.auto_edit(music, target_seconds=4.0, clips=clips, fps=fps, output="plan")
    assert r["ok"], r
    beats = beat_grid.beat_grid(music)["beat_times"]
    b0 = beats[0]
    tol = 2.0 / fps
    # Each shot's timeline cut boundary lands within 2 frames of a beat.
    for s in r["cut_list"]:
        cut_time = s["timeline_out"] + b0
        nearest = min(beats, key=lambda b: abs(b - cut_time))
        assert abs(nearest - cut_time) <= tol + 1e-6, (s, nearest)


def test_auto_edit_variety_no_adjacent_same_source():
    clips = _clips(3)
    music = fx.click_track_wav(120.0)
    r = auto_edit.auto_edit(music, target_seconds=6.0, clips=clips, output="plan")
    assert r["ok"], r
    cut = r["cut_list"]
    for a, b in zip(cut, cut[1:]):
        if a["source"] == b["source"]:
            # Same clip only if the source regions are well separated.
            assert abs(b["in"] - a["in"]) >= auto_edit._MIN_SOURCE_SEP - 1e-6, (a, b)
        else:
            assert a["source"] != b["source"]


def test_auto_edit_energy_maps_to_density():
    clips = _clips(3)
    music = _ramp_click_wav()
    r = auto_edit.auto_edit(music, target_seconds=8.0, clips=clips, output="plan")
    assert r["ok"], r
    # Split by each shot's measured energy, not timeline position (beats start
    # at the first downbeat, so timeline time is offset from song time).
    low = [s["beats"] for s in r["cut_list"] if s["energy"] < 0.5]
    high = [s["beats"] for s in r["cut_list"] if s["energy"] >= 0.5]
    assert low and high, r["cut_list"]
    # Low-energy sections -> longer holds; high-energy -> shorter cuts.
    assert np.mean(low) > np.mean(high), (low, high)


def test_auto_edit_writes_fcpxml(tmp_path):
    clips = _clips(3)
    music = fx.click_track_wav(120.0)
    out = str(tmp_path / "auto.fcpxml")
    dry = auto_edit.auto_edit(music, target_seconds=4.0, clips=clips, output="fcpxml",
                              output_path=out, dry_run=True)
    assert dry["ok"] and dry["dry_run"] is True
    assert dry["fcpxml"]["ok"]
    assert not os.path.exists(out)

    done = auto_edit.auto_edit(music, target_seconds=4.0, clips=clips, output="fcpxml",
                               output_path=out, dry_run=False, confirm=True)
    assert done["ok"], done
    # clip_count = one per video shot + the music audio clip.
    assert done["fcpxml"]["clip_count"] == done["shot_count"] + 1
    assert os.path.exists(out)


def test_auto_edit_footage_shorter_than_target_warns():
    clips = _clips(2)  # ~2x 1.5s usable = 3s usable
    music = fx.click_track_wav(120.0)
    r = auto_edit.auto_edit(music, target_seconds=8.0, clips=clips, output="plan")
    assert r["ok"], r
    assert "warning" in r and "shorter" in r["warning"]


def test_auto_edit_bad_inputs(tmp_path):
    music = fx.click_track_wav(120.0)
    assert auto_edit.auto_edit(music, 4.0, clips=[], output="plan")["ok"] is False
    assert auto_edit.auto_edit("/no/song.wav", 4.0, clips=["x"])["ok"] is False
    clips = _clips(1)
    assert auto_edit.auto_edit(music, 4.0, clips=clips, style="weird")["ok"] is False
    assert auto_edit.auto_edit(music, -1.0, clips=clips)["ok"] is False
