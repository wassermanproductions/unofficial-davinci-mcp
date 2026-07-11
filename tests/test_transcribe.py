"""Tests for local word-level transcription (engines/transcribe.py).

Pure/soft-error paths run everywhere. The real transcription path is skipped
unless faster-whisper is installed AND macOS `say` can synthesize speech; when
both are present it transcribes a known phrase (with deliberate fillers) and
asserts the words and word-level timings come back, then feeds the transcript
into cut_by_transcript to prove the end-to-end voice -> edit path.
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

from engines import fftools, text_edit, transcribe  # noqa: E402

_HAS_FASTER_WHISPER = importlib.util.find_spec("faster_whisper") is not None
_HAS_SAY = shutil.which("say") is not None


# --------------------------------------------------------------------------- #
# Soft-error paths (no model needed)
# --------------------------------------------------------------------------- #

def test_missing_file_is_soft_error():
    result = transcribe.transcribe_media("/tmp/does_not_exist_xyz.wav")
    assert result["ok"] is False
    assert "exist" in result["error"].lower()


def test_initial_prompt_biases_to_filmmaker_vocab():
    prompt = transcribe._build_initial_prompt()
    assert isinstance(prompt, str) and prompt
    # Some editing jargon must be present regardless of which source supplied it.
    assert any(term in prompt for term in ("timeline", "LUT", "Resolve", "FCPXML"))


def test_cache_path_defaults_next_to_media_and_honours_override(tmp_path):
    media = tmp_path / "clip.wav"
    default = transcribe._cache_path(str(media), None)
    assert default.name == "clip.wav.transcript.json"
    assert default.parent == tmp_path
    override = transcribe._cache_path(str(media), str(tmp_path / "custom.json"))
    assert override.name == "custom.json"


def test_reads_valid_cache_without_running_model(tmp_path, make_media):
    """A fresh cache sitting next to the media is returned as-is (cached=True)
    without importing the model."""
    audio = make_media("cached", kind="audio", seconds=1.0)
    cache = Path(audio).with_suffix(Path(audio).suffix + ".transcript.json")
    payload = {
        "ok": True, "words": [{"start": 0.0, "end": 0.5, "word": " hi"}],
        "segments": [], "duration": 1.0, "detected_language": "en",
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")
    # Ensure the cache is newer than the media.
    os.utime(cache, None)

    result = transcribe.transcribe_media(audio)
    assert result["ok"] is True
    assert result["cached"] is True
    assert result["words"] == payload["words"]


# --------------------------------------------------------------------------- #
# Real transcription (faster-whisper + macOS `say`)
# --------------------------------------------------------------------------- #

def _synthesize_speech(phrase: str, out_wav: Path) -> bool:
    """macOS `say` -> AIFF -> 16 kHz mono WAV. Returns ok."""
    aiff = out_wav.with_suffix(".aiff")
    try:
        subprocess.run(["say", "-o", str(aiff), phrase], check=True, timeout=60)
    except Exception:
        return False
    if shutil.which("afconvert"):
        rc = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             str(aiff), str(out_wav)],
            capture_output=True,
        ).returncode
        if rc == 0 and out_wav.exists():
            return True
    if fftools.have_ffmpeg():
        rc = subprocess.run(
            [fftools.ffmpeg_path(), "-y", "-i", str(aiff), "-ar", "16000",
             "-ac", "1", str(out_wav)],
            capture_output=True,
        ).returncode
        if rc == 0 and out_wav.exists():
            return True
    return False


@pytest.mark.skipif(not _HAS_FASTER_WHISPER, reason="faster-whisper not installed")
@pytest.mark.skipif(not _HAS_SAY, reason="macOS `say` not available")
def test_real_transcription_returns_word_timings(tmp_path):
    phrase = "So um I think we should cut this shot"
    wav = tmp_path / "spoken.wav"
    if not _synthesize_speech(phrase, wav):
        pytest.skip("could not synthesize/convert speech on this machine")

    result = transcribe.transcribe_media(str(wav), language="en", model="base")
    assert result["ok"], result
    assert result["cached"] is False
    assert result["detected_language"] == "en"
    assert result["duration"] and result["duration"] > 0

    # Word-level timings are present and monotonic.
    words = result["words"]
    assert len(words) >= 4
    for w in words:
        assert w["end"] >= w["start"]
        assert "word" in w
    joined = " ".join(text_edit._norm(w["word"]) for w in words)
    assert "cut" in joined and "shot" in joined

    # The cache was written next to the media and round-trips.
    cache = Path(str(wav) + ".transcript.json")
    assert cache.exists()
    reloaded = json.loads(cache.read_text(encoding="utf-8"))
    assert reloaded["word_count"] == result["word_count"]


@pytest.mark.skipif(not _HAS_FASTER_WHISPER, reason="faster-whisper not installed")
@pytest.mark.skipif(not _HAS_SAY, reason="macOS `say` not available")
def test_real_transcription_feeds_filler_removal(tmp_path):
    """End-to-end: `say` a phrase, auto-transcribe inside cut_by_transcript, and
    prove a filler word from the real transcript is flagged for removal.

    Whisper is unreliable at spelling out "um"/"uh" from synthesized speech, so we
    target a distinctive mid-sentence word registered as a custom filler — this
    still exercises the full transcribe -> word-level cut pipeline on real audio.
    """
    phrase = "The plan is basically to cut this shot"
    wav = tmp_path / "fillers.wav"
    if not _synthesize_speech(phrase, wav):
        pytest.skip("could not synthesize/convert speech on this machine")

    plan = text_edit.cut_by_transcript(
        str(wav), transcript_json="auto", language="en", model="base",
        custom_fillers=["basically"], max_pause=99, keep_ratio_floor=0.0,
        dry_run=True,
    )
    assert plan["ok"], plan
    assert plan["transcript_source"] == "auto"
    reason_types = {r["type"] for c in plan["cuts"] for r in c["reasons"]}
    texts = {r["text"] for c in plan["cuts"] for r in c["reasons"]}
    assert "filler" in reason_types
    assert "basically" in texts
