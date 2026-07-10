import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import loudness  # noqa: E402


def test_measure_reports_lufs():
    d = loudness.measure_one(fx.dialogue_wav())
    assert d["ok"], d
    assert d["integrated_lufs"] is not None
    assert d["true_peak_dbtp"] is not None
    assert -60 < d["integrated_lufs"] < 0


def test_speech_windows_detected():
    windows, dur, noise = loudness.detect_speech_windows(fx.dialogue_wav())
    assert dur > 7.0
    # 0.8 s on / 0.8 s off across 8 s -> several speech windows.
    assert len(windows) >= 4
    for s, e in windows:
        assert e > s


def test_mix_plan_dry_run():
    plan = loudness.mix_plan(
        fx.dialogue_wav(), music=[fx.music_wav()], dry_run=True,
    )
    assert plan["ok"] and plan["dry_run"] is True
    assert plan["dialogue"]["gain_db"] is not None
    assert plan["music"]["gain_db"] is not None
    assert len(plan["duck_windows"]) >= 1


def test_premix_hits_target_within_1LU():
    out = os.path.join(os.path.dirname(fx.dialogue_wav()), "premix_out.wav")
    plan = loudness.mix_plan(
        fx.dialogue_wav(), music=[fx.music_wav()],
        dialogue_lufs=-16.0, music_bed_db=-18.0, duck_db=-7.0,
        output_path=out, dry_run=False, confirm=True,
    )
    assert plan["ok"], plan
    assert os.path.exists(plan["premix"]["path"])
    remeasured = plan["premix"]["remeasured"]
    assert remeasured["ok"], remeasured
    achieved = remeasured["integrated_lufs"]
    assert abs(achieved - (-16.0)) <= 1.0, f"premix integrated {achieved} LUFS not within 1 LU of -16"
    # No hard clipping (limiter only nudges if needed).
    assert remeasured["true_peak_dbtp"] <= 0.5


def test_confirm_required():
    plan = loudness.mix_plan(fx.dialogue_wav(), dry_run=False, confirm=False)
    assert plan["ok"] is False
