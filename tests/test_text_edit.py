"""Deterministic tests for transcript-driven editing (engines/text_edit.py).

The heuristics are exercised with hand-built transcript JSON (no model, fully
deterministic). One end-to-end test constructs real audio with ffmpeg (tone /
silence / tone) and renders a pause-collapsed preview.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines import fftools, text_edit  # noqa: E402


# --------------------------------------------------------------------------- #
# Hand-built transcript helpers
# --------------------------------------------------------------------------- #

def _words(pairs):
    """pairs: list of (word, start, end) -> transcript word dicts."""
    return [{"word": w, "start": s, "end": e} for (w, s, e) in pairs]


def _transcript(pairs, duration):
    return {"words": _words(pairs), "duration": duration, "segments": []}


def _plan(pairs, duration, **kw):
    kw.setdefault("keep_ratio_floor", 0.0)  # don't trip the safety floor in unit tests
    kw.setdefault("dry_run", True)
    return text_edit.cut_by_transcript("/tmp/nonexistent_clip.wav", _transcript(pairs, duration), **kw)


def _reason_types(plan):
    return {r["type"] for c in plan["cuts"] for r in c["reasons"]}


# --------------------------------------------------------------------------- #
# Filler words
# --------------------------------------------------------------------------- #

def test_removes_basic_single_word_fillers():
    plan = _plan(
        [(" So", 0.0, 0.4), (" um", 0.45, 0.65), (" yeah", 0.7, 1.1),
         (" uh", 1.15, 1.35), (" okay", 1.4, 1.8)],
        duration=2.0, max_pause=99,
    )
    assert plan["ok"]
    texts = [r["text"] for c in plan["cuts"] for r in c["reasons"]]
    assert "um" in texts and "uh" in texts
    assert _reason_types(plan) == {"filler"}


def test_custom_fillers_are_removed():
    # "basically" is mid-sentence (not a sentence-opening beat), so it is cut.
    plan = _plan(
        [(" And", 0.0, 0.3), (" basically", 0.4, 0.9), (" the", 1.0, 1.3),
         (" plan", 1.4, 1.8)],
        duration=2.2, max_pause=99, custom_fillers=["basically"],
    )
    texts = [r["text"] for c in plan["cuts"] for r in c["reasons"]]
    assert "basically" in texts


def test_leading_filler_that_opens_a_sentence_is_kept():
    # "Um" at index 0 opens the sentence -> kept (deliberate beat).
    plan = _plan([(" Um", 0.0, 0.3), (" hello", 0.5, 1.0)], duration=1.5, max_pause=99)
    assert plan["cut_count"] == 0


def test_repeated_leading_filler_is_cut():
    # "Um um" at the start reads as a stumble -> the repetition is cut.
    plan = _plan(
        [(" Um", 0.0, 0.3), (" um", 0.35, 0.6), (" hello", 0.8, 1.2)],
        duration=1.6, max_pause=99,
    )
    assert plan["cut_count"] >= 1
    assert "filler" in _reason_types(plan)


def test_phrase_filler_you_know_removed_as_unit():
    plan = _plan(
        [(" it", 0.0, 0.3), (" was", 0.35, 0.6), (" you", 0.65, 0.85),
         (" know", 0.9, 1.1), (" fine", 1.15, 1.5)],
        duration=2.0, max_pause=99,
    )
    texts = [r["text"] for c in plan["cuts"] for r in c["reasons"]]
    assert "you know" in texts


# --------------------------------------------------------------------------- #
# Guarded "like"
# --------------------------------------------------------------------------- #

def test_like_inside_a_phrase_is_kept():
    plan = _plan(
        [(" I", 0.0, 0.3), (" like", 0.31, 0.6), (" this", 0.61, 0.9)],
        duration=1.2, max_pause=99,
    )
    assert plan["cut_count"] == 0  # "I like this" is meaningful


def test_isolated_like_is_removed():
    plan = _plan(
        [(" well", 0.0, 0.3), (" like", 1.0, 1.3), (" yeah", 2.0, 2.3)],
        duration=3.0, max_pause=99,
    )
    texts = [r["text"] for c in plan["cuts"] for r in c["reasons"]]
    assert "like" in texts


# --------------------------------------------------------------------------- #
# False starts (restarts)
# --------------------------------------------------------------------------- #

def test_detects_two_word_restart():
    plan = _plan(
        [(" what", 0.0, 0.25), (" I", 0.3, 0.5), (" what", 0.55, 0.8),
         (" I", 0.85, 1.05), (" meant", 1.1, 1.5)],
        duration=2.0, max_pause=99,
    )
    assert "restart" in _reason_types(plan)
    # The first copy is cut, up to the clean restart.
    restart_cut = next(c for c in plan["cuts"] if any(r["type"] == "restart" for r in c["reasons"]))
    assert restart_cut["start"] < 0.55 <= restart_cut["end"] + 0.2


def test_remove_restarts_false_keeps_repetition():
    plan = _plan(
        [(" what", 0.0, 0.25), (" I", 0.3, 0.5), (" what", 0.55, 0.8),
         (" I", 0.85, 1.05), (" meant", 1.1, 1.5)],
        duration=2.0, max_pause=99, remove_restarts=False,
    )
    assert "restart" not in _reason_types(plan)


# --------------------------------------------------------------------------- #
# Pause collapse + handles + min_cut
# --------------------------------------------------------------------------- #

def test_collapses_long_pause_leaving_handles():
    handle = 0.1
    plan = _plan(
        [(" one", 0.0, 0.5), (" two", 3.0, 3.5)],
        duration=4.0, max_pause=0.6, handle=handle, remove_fillers=False,
    )
    assert "pause" in _reason_types(plan)
    cut = next(c for c in plan["cuts"] if any(r["type"] == "pause" for r in c["reasons"]))
    # Handle preserved against both neighbouring words.
    assert cut["start"] >= 0.5 + handle - 1e-6
    assert cut["end"] <= 3.0 - handle + 1e-6


def test_short_pause_below_max_pause_is_kept():
    plan = _plan(
        [(" one", 0.0, 0.5), (" two", 0.9, 1.4)],  # 0.4 s gap < 0.6
        duration=1.6, max_pause=0.6, remove_fillers=False,  # 0.2 s tail < 0.6
    )
    assert plan["cut_count"] == 0


def test_min_cut_suppresses_tiny_pause_removals():
    # Gap is 0.8 s; with 0.1 handles the removable core is 0.6 s. min_cut above
    # that suppresses the cut.
    plan = _plan(
        [(" one", 0.0, 0.5), (" two", 1.3, 1.8)],
        duration=2.5, max_pause=0.6, handle=0.1, min_cut=0.9, remove_fillers=False,
    )
    assert plan["cut_count"] == 0


def test_tighten_only_skips_fillers_and_restarts():
    plan = _plan(
        [(" um", 0.0, 0.3), (" one", 0.4, 0.9), (" two", 3.0, 3.5)],
        duration=4.0, max_pause=0.6, tighten_only=True,
    )
    assert _reason_types(plan) == {"pause"}


# --------------------------------------------------------------------------- #
# Merge, keep ranges, assemble plan
# --------------------------------------------------------------------------- #

def test_adjacent_cuts_merge_and_combine_reasons():
    # A pause immediately followed by a filler should merge into one cut whose
    # reasons list carries both.
    plan = _plan(
        [(" one", 0.0, 0.5), (" um", 2.6, 2.9), (" two", 2.95, 3.4)],
        duration=4.0, max_pause=0.6, handle=0.05,
    )
    multi = [c for c in plan["cuts"] if len(c["reasons"]) > 1]
    assert multi, plan["cuts"]
    assert {r["type"] for r in multi[0]["reasons"]} == {"pause", "filler"}


def test_keep_ranges_are_sorted_non_overlapping_and_cover_cuts():
    plan = _plan(
        [(" a", 0.0, 0.4), (" um", 0.45, 0.7), (" b", 2.0, 2.4)],
        duration=3.0, max_pause=0.6,
    )
    keep = plan["keep_ranges"]
    for i in range(1, len(keep)):
        assert keep[i]["start"] >= keep[i - 1]["end"] - 1e-6
        assert keep[i]["end"] > keep[i]["start"]
    total = sum(k["end"] - k["start"] for k in keep)
    assert abs(total - plan["cut_duration_seconds"]) < 1e-6


def test_assemble_plan_is_audio_only_for_audio_kind():
    plan = _plan([(" a", 0.0, 0.4), (" b", 2.0, 2.4)], duration=3.0, max_pause=0.6)
    ap = plan["assemble_plan"]
    assert "audio" in ap and ap["audio"]
    assert "video" not in ap  # nonexistent file -> best-effort probe -> audio
    assert set(ap["audio"][0]) == {"path", "in", "out"}
    assert "timeline" in ap and ap["timeline"]["name"]


def test_every_cut_carries_reasons_and_handles():
    plan = _plan([(" a", 0.0, 0.4), (" um", 0.45, 0.7), (" b", 0.75, 1.2)],
                 duration=1.5, max_pause=99)
    assert plan["cuts"]
    for c in plan["cuts"]:
        assert c["reasons"] and all("type" in r and "text" in r for r in c["reasons"])
        assert c["end"] > c["start"]


# --------------------------------------------------------------------------- #
# Safety floor + dry-run/confirm gating
# --------------------------------------------------------------------------- #

def test_safety_floor_flags_and_does_not_execute():
    # A single huge pause collapses almost the whole clip -> keep_ratio below floor.
    plan = text_edit.cut_by_transcript(
        "/tmp/nonexistent_clip.wav",
        _transcript([(" a", 0.0, 0.3), (" b", 4.7, 5.0)], 5.0),
        max_pause=0.6, keep_ratio_floor=0.9, dry_run=True,
    )
    assert plan["ok"]
    assert plan.get("executed") is False
    assert "warning" in plan
    assert "preview" not in plan


def test_confirm_required_when_not_dry_run():
    plan = text_edit.cut_by_transcript(
        "/tmp/nonexistent_clip.wav",
        _transcript([(" a", 0.0, 0.4), (" um", 0.45, 0.7), (" b", 0.75, 1.2)], 1.5),
        keep_ratio_floor=0.0, dry_run=False, confirm=False,
    )
    assert plan["ok"] is False
    assert "confirm" in plan["error"]


def test_dry_run_note_present():
    plan = _plan([(" a", 0.0, 0.4), (" um", 0.45, 0.7), (" b", 0.75, 1.2)],
                 duration=1.5, max_pause=99)
    assert plan["dry_run"] is True
    assert "note" in plan


# --------------------------------------------------------------------------- #
# Bad input
# --------------------------------------------------------------------------- #

def test_missing_transcript_file_is_soft_error():
    plan = text_edit.cut_by_transcript("/tmp/x.wav", "/tmp/does_not_exist.json")
    assert plan["ok"] is False
    assert "not found" in plan["error"].lower()


def test_transcript_without_words_is_soft_error():
    plan = text_edit.cut_by_transcript("/tmp/x.wav", {"words": [], "duration": 5.0})
    assert plan["ok"] is False


# --------------------------------------------------------------------------- #
# End-to-end pause collapse on real constructed audio (ffmpeg concat)
# --------------------------------------------------------------------------- #

def _tone_silence_tone_wav(tmp_path, sr=48000):
    """1 s tone + 2 s silence + 1 s tone = 4 s of real audio."""
    def tone(dur, freq=220.0):
        t = np.arange(int(dur * sr)) / sr
        return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    sig = np.concatenate([tone(1.0), np.zeros(int(2.0 * sr), np.float32), tone(1.0)])
    stereo = np.stack([sig, sig], axis=-1)
    p = str(tmp_path / "tst.wav")
    fftools.encode_wav(stereo, p, sr=sr)
    return p


@pytest.mark.skipif(not fftools.have_ffmpeg(), reason="ffmpeg not available")
def test_pause_collapse_renders_shorter_preview(tmp_path):
    wav = _tone_silence_tone_wav(tmp_path)
    # Hand transcript matching the constructed audio: words either side of the gap.
    transcript = _transcript([(" first", 0.0, 1.0), (" second", 3.0, 4.0)], 4.0)
    out = str(tmp_path / "cut.wav")
    plan = text_edit.cut_by_transcript(
        wav, transcript, max_pause=0.6, handle=0.1, remove_fillers=False,
        keep_ratio_floor=0.0, output_path=out, dry_run=False, confirm=True,
    )
    assert plan["ok"], plan
    assert "pause" in _reason_types(plan)
    prev = plan["preview"]["path"]
    assert prev and os.path.exists(prev)
    dur = float(fftools.ffprobe_json(prev)["format"]["duration"])
    assert dur < 4.0  # the 2 s gap was collapsed
    assert abs(dur - plan["cut_duration_seconds"]) < 0.3
