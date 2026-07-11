"""Tests for spoken-content footage search (engines/footage_search.py).

The exact-timing and selects tests seed a transcript cache next to real media
(so they run everywhere without a model). A final end-to-end test uses macOS
`say` + faster-whisper when both are present.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines import fftools, footage_search, transcribe  # noqa: E402

_HAS_FASTER_WHISPER = importlib.util.find_spec("faster_whisper") is not None
_HAS_SAY = shutil.which("say") is not None


def _seed_transcript(media_path: str, words: list[dict]) -> None:
    """Write a transcript cache next to the media, newer than the media file."""
    cache = transcribe._cache_path(media_path, None)
    payload = {
        "ok": True,
        "words": words,
        "segments": [],
        "duration": max((w["end"] for w in words), default=1.0),
        "detected_language": "en",
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(cache, None)


def _words(*items) -> list[dict]:
    return [{"start": s, "end": e, "word": w} for (s, e, w) in items]


# --------------------------------------------------------------------------- #
# Soft-error paths
# --------------------------------------------------------------------------- #

def test_bad_inputs(make_media):
    audio = make_media("s_bad", kind="audio", seconds=1.0)
    assert footage_search.find_in_footage("hi", audio, mode="nope")["ok"] is False
    assert footage_search.find_in_footage("", audio)["ok"] is False
    assert footage_search.find_in_footage("hi", [])["ok"] is False


# --------------------------------------------------------------------------- #
# Exact-timing search over a seeded transcript
# --------------------------------------------------------------------------- #

def test_phrase_hit_has_correct_timing(make_media):
    audio = make_media("selects_src", kind="audio", seconds=2.0)
    _seed_transcript(audio, _words(
        (0.0, 0.4, " the"), (1.0, 1.5, " helicopter"), (1.6, 2.0, " landed"),
    ))
    r = footage_search.find_in_footage("helicopter", audio, mode="phrase")
    assert r["ok"], r
    assert r["hit_count"] == 1
    hit = r["hits"][0]
    assert abs(hit["start"] - 1.0) <= 0.5
    assert abs(hit["end"] - 1.5) <= 0.5
    assert "helicopter" in hit["text"].lower()
    assert hit["context"]  # padded context present


def test_phrase_multiword_and_case_insensitive(make_media):
    audio = make_media("phrase2", kind="audio", seconds=2.0)
    _seed_transcript(audio, _words(
        (0.0, 0.4, "The"), (0.5, 0.9, "Red"), (1.0, 1.4, "Balloon"), (1.5, 1.9, "flew"),
    ))
    r = footage_search.find_in_footage("red balloon", audio, mode="phrase")
    assert r["ok"] and r["hit_count"] == 1, r
    assert abs(r["hits"][0]["start"] - 0.5) <= 0.5


def test_all_words_requires_all_present(make_media):
    audio = make_media("allw", kind="audio", seconds=2.0)
    _seed_transcript(audio, _words(
        (0.0, 0.4, "alpha"), (0.5, 0.9, "bravo"), (1.0, 1.4, "alpha"),
    ))
    # Both words present -> occurrences of each returned.
    r = footage_search.find_in_footage("alpha bravo", audio, mode="all_words")
    assert r["ok"] and r["hit_count"] == 3, r
    # A missing word -> file gated out, no hits.
    r2 = footage_search.find_in_footage("alpha charlie", audio, mode="all_words")
    assert r2["ok"] and r2["hit_count"] == 0, r2


def test_regex_mode(make_media):
    audio = make_media("rx", kind="audio", seconds=2.0)
    _seed_transcript(audio, _words(
        (0.0, 0.4, "helicopter"), (0.5, 0.9, "landing"),
    ))
    r = footage_search.find_in_footage(r"heli\w+", audio, mode="regex")
    assert r["ok"] and r["hit_count"] == 1, r
    assert "helicopter" in r["hits"][0]["text"].lower()


def test_files_without_speech_reported(make_media):
    spoken = make_media("has_speech", kind="audio", seconds=2.0)
    silent = make_media("no_speech", kind="audio", seconds=2.0)
    _seed_transcript(spoken, _words((1.0, 1.5, "helicopter")))
    _seed_transcript(silent, [])  # empty transcript
    r = footage_search.find_in_footage("helicopter", [spoken, silent], mode="phrase")
    assert r["ok"], r
    assert r["hit_count"] == 1
    assert silent in [f["file"] for f in r["files_without_speech"]]


def test_build_selects_writes_fcpxml(make_media, tmp_path):
    audio = make_media("sel_src", kind="video", seconds=3.0)
    _seed_transcript(audio, _words(
        (0.5, 0.9, "one"), (1.5, 1.9, "helicopter"), (2.4, 2.8, "three"),
    ))
    out = str(tmp_path / "selects.fcpxml")
    dry = footage_search.find_in_footage(
        "helicopter", audio, mode="phrase", build_selects=True,
        output_path=out, dry_run=True,
    )
    assert dry["ok"] and dry["selects"]["ok"], dry
    # A 0.5s handle either side of the 1.5-1.9 hit.
    clip = dry["selects_plan"]["video"][0]
    assert abs(clip["in"] - 1.0) <= 1e-6 and abs(clip["out"] - 2.4) <= 1e-6
    assert not os.path.exists(out)

    done = footage_search.find_in_footage(
        "helicopter", audio, mode="phrase", build_selects=True,
        output_path=out, dry_run=False, confirm=True,
    )
    assert done["ok"] and done["selects"]["ok"], done
    assert os.path.exists(out)


# --------------------------------------------------------------------------- #
# End-to-end: real speech -> transcribe -> search
# --------------------------------------------------------------------------- #

def _synthesize_speech(phrase: str, out_wav: Path) -> bool:
    aiff = out_wav.with_suffix(".aiff")
    try:
        subprocess.run(["say", "-o", str(aiff), phrase], check=True, timeout=60)
    except Exception:
        return False
    if shutil.which("afconvert"):
        rc = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             str(aiff), str(out_wav)], capture_output=True,
        ).returncode
        if rc == 0 and out_wav.exists():
            return True
    if fftools.have_ffmpeg():
        rc = subprocess.run(
            [fftools.ffmpeg_path(), "-y", "-i", str(aiff), "-ar", "16000",
             "-ac", "1", str(out_wav)], capture_output=True,
        ).returncode
        if rc == 0 and out_wav.exists():
            return True
    return False


@pytest.mark.skipif(not _HAS_FASTER_WHISPER, reason="faster-whisper not installed")
@pytest.mark.skipif(not _HAS_SAY, reason="macOS `say` not available")
def test_real_search_finds_distinctive_word(tmp_path):
    phrase = "The helicopter landed on the rooftop at dawn"
    wav = tmp_path / "spoken_search.wav"
    if not _synthesize_speech(phrase, wav):
        pytest.skip("could not synthesize/convert speech on this machine")

    r = footage_search.find_in_footage(
        "helicopter", str(wav), mode="phrase", language="en", model="base",
    )
    assert r["ok"], r
    assert r["hit_count"] >= 1, r
    hit = r["hits"][0]
    # "helicopter" is the 2nd word: it lands early in the clip.
    assert 0.0 <= hit["start"] <= 3.0, hit
    assert hit["end"] > hit["start"]
