import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import fftools, music_cut  # noqa: E402


def test_cut_music_tail_dry_run():
    click = fx.click_track_wav(120.0)
    plan = music_cut.cut_music(click, target_seconds=4.0, sting="tail", dry_run=True)
    assert plan["ok"], plan
    assert plan["dry_run"] is True
    cut = plan["cut"]
    assert cut["sting"] == "tail"
    # Exit should be at or before target, and land on a beat boundary (0.5s grid).
    assert cut["exit_time"] <= 4.0 + 1e-6
    nearest_beat = round(cut["exit_time"] / 0.5) * 0.5
    assert abs(cut["exit_time"] - nearest_beat) < 0.08, cut


def test_cut_music_tail_render_and_monotonic_fade():
    # Use the continuous music bed (a click track is silent between clicks, so
    # its "tail" has no sustain to ring out). The exponential fade must make the
    # bed's envelope decrease monotonically to near-silence.
    song = fx.music_wav()
    out = os.path.join(os.path.dirname(song), "cut_tail.wav")
    plan = music_cut.cut_music(
        song, target_seconds=4.0, sting="tail", tail_fade=1.8,
        output_path=out, dry_run=False, confirm=True,
    )
    assert plan["ok"], plan
    rendered = plan["rendered"]["path"]
    assert os.path.exists(rendered)

    audio = fftools.decode_pcm(rendered, sr=48000, channels=1).reshape(-1)
    sr = 48000
    fs = int(plan["rendered"]["fade_start_seconds"] * sr)
    tail = np.abs(audio[fs:])
    win = int(0.05 * sr)
    rms = np.array([np.sqrt(np.mean(tail[i:i + win] ** 2) + 1e-12)
                    for i in range(0, max(0, len(tail) - win), win)])
    assert len(rms) >= 3, "fade tail too short to assess"
    # Strongly decreasing overall.
    assert rms[-1] <= rms[0] * 0.2, f"tail did not ring out: {rms}"
    # And broadly monotonic: few upward steps allowed (noise wobble).
    ups = int(np.sum(np.diff(rms) > 1e-4))
    assert ups <= 2, f"fade not monotonic enough: {rms}"


def test_cut_music_button_ends_on_hit():
    click = fx.click_track_wav(120.0)
    plan = music_cut.cut_music(click, target_seconds=4.0, sting="button", dry_run=True)
    assert plan["ok"], plan
    cut = plan["cut"]
    assert cut["sting"] == "button"
    # Hit should be near a click (0.5s grid) and near the target.
    hit = cut["hit_time"]
    nearest_beat = round(hit / 0.5) * 0.5
    assert abs(hit - nearest_beat) < 0.1, cut
    assert abs(hit - 4.0) < 2.0


def test_clip_spec_shape():
    click = fx.click_track_wav(120.0)
    plan = music_cut.cut_music(click, target_seconds=3.0, dry_run=True)
    spec = plan["clip_spec"]
    assert set(spec) >= {"path", "in", "out", "fade_out"}
    assert spec["out"] > spec["in"]
    assert spec["fade_out"] > 0


def test_bad_inputs():
    assert music_cut.cut_music("/no/file.wav", 4.0)["ok"] is False
    click = fx.click_track_wav(120.0)
    assert music_cut.cut_music(click, -1.0)["ok"] is False
    assert music_cut.cut_music(click, 4.0, sting="weird")["ok"] is False
