import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import beat_grid  # noqa: E402


def _check_grid(result, expected_bpm=120.0, tol=2.0):
    assert result["ok"], result
    assert result["duration_seconds"] > 7.0
    # BPM within tolerance (allow octave error handled by caller if needed).
    bpm = result["bpm"]
    assert bpm > 0
    assert abs(bpm - expected_bpm) <= tol or abs(bpm - expected_bpm / 2) <= tol or abs(bpm - expected_bpm * 2) <= tol, bpm
    assert result["beat_count"] > 0
    beats = result["beat_times"]
    assert beats == sorted(beats)
    assert all(0 <= b <= result["duration_seconds"] + 0.1 for b in beats)


def test_beat_grid_librosa_or_default():
    click = fx.click_track_wav(120.0)
    result = beat_grid.beat_grid(click)
    _check_grid(result, 120.0, tol=2.0)
    # Detected BPM should be exactly near 120 for a clean click track.
    assert abs(result["bpm"] - 120.0) <= 2.0, result["bpm"]


def test_beat_grid_fallback_path(monkeypatch):
    """Force the energy-envelope fallback and verify it still lands near 120."""
    monkeypatch.setattr(beat_grid, "HAVE_LIBROSA", False)
    click = fx.click_track_wav(120.0)
    result = beat_grid.beat_grid(click)
    assert result["method"] == "energy_envelope_fallback"
    assert result["approximate"] is True
    # Fallback is approximate; allow a wider band but still meaningful.
    assert abs(result["bpm"] - 120.0) <= 6.0, result["bpm"]
    assert result["onset_times"], "fallback should detect onsets"


def test_onsets_present_and_sorted():
    click = fx.click_track_wav(120.0)
    result = beat_grid.beat_grid(click)
    onsets = result["onset_times"]
    assert onsets == sorted(onsets)
    # Roughly one onset per click (16 clicks in 8s); allow slack.
    assert len(onsets) >= 8


def test_bad_input():
    result = beat_grid.beat_grid("/nonexistent/file.wav")
    assert result["ok"] is False
