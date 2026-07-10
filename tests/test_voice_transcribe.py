"""Tests for the voice push-to-talk bridge.

The pure parts (config load/merge, WAV encoding, clipboard-restore logic, the
hotkey state machine, the vocabulary prompt) are tested with no extras installed.
The real faster-whisper transcription is skipped unless the extra is present; when
it is, and macOS `say` can synthesize speech, it transcribes a known phrase and
asserts the words come back.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from voice import ptt


# --------------------------------------------------------------------------- #
# Config load / merge
# --------------------------------------------------------------------------- #

def test_load_config_defaults_when_missing(tmp_path):
    cfg = ptt.load_config(tmp_path / "nope.json")
    assert cfg == ptt.DEFAULT_CONFIG
    assert cfg is not ptt.DEFAULT_CONFIG  # a copy, not the shared dict


def test_load_config_merges_overrides(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"model": "small", "auto_enter": True, "extra": 1}))
    cfg = ptt.load_config(p)
    assert cfg["model"] == "small"
    assert cfg["auto_enter"] is True
    assert cfg["hotkey"] == "right_option"  # untouched default
    assert cfg["extra"] == 1  # forward-compatible: unknown keys preserved


def test_load_config_empty_file_is_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("   \n")
    assert ptt.load_config(p) == ptt.DEFAULT_CONFIG


def test_load_config_rejects_malformed_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not json ")
    with pytest.raises(ValueError):
        ptt.load_config(p)


def test_load_config_rejects_non_object(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(ValueError):
        ptt.load_config(p)


def test_shipped_config_matches_defaults():
    """The config.json that ships must parse and agree with the defaults."""
    cfg = ptt.load_config(ptt.default_config_path())
    for key, value in ptt.DEFAULT_CONFIG.items():
        assert cfg[key] == value


# --------------------------------------------------------------------------- #
# WAV encoding
# --------------------------------------------------------------------------- #

def _read_wav(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        return {
            "channels": w.getnchannels(),
            "sampwidth": w.getsampwidth(),
            "framerate": w.getframerate(),
            "nframes": w.getnframes(),
            "raw": w.readframes(w.getnframes()),
        }


def test_encode_wav_from_list_is_valid_pcm():
    samples = [0.0, 0.5, -0.5, 1.0, -1.0]
    meta = _read_wav(ptt.encode_wav(samples, sample_rate=16000))
    assert meta["channels"] == 1
    assert meta["sampwidth"] == 2
    assert meta["framerate"] == 16000
    assert meta["nframes"] == len(samples)


def test_encode_wav_clamps_out_of_range():
    meta = _read_wav(ptt.encode_wav([5.0, -5.0], sample_rate=8000))
    import struct

    vals = struct.unpack("<2h", meta["raw"])
    assert vals[0] == 32767  # clamped up
    assert vals[1] == -32767  # clamped down


def test_generate_tone_wav_has_expected_length():
    ms, sr = 90, 16000
    meta = _read_wav(ptt.generate_tone_wav(freq=660, ms=ms, sample_rate=sr))
    assert meta["framerate"] == sr
    assert meta["channels"] == 1
    assert meta["nframes"] == int(sr * ms / 1000)


# --------------------------------------------------------------------------- #
# Hotkey state machine
# --------------------------------------------------------------------------- #

def test_hotkey_press_release_fires_once_each():
    events = []
    hotkey = object()  # stand-in for a pynput Key
    m = ptt.HotkeyRecorder(
        matches=lambda k: k is hotkey,
        on_start=lambda: events.append("start"),
        on_stop=lambda: events.append("stop"),
    )
    assert m.press(hotkey) is True
    assert m.recording is True
    assert m.release(hotkey) is True
    assert m.recording is False
    assert events == ["start", "stop"]


def test_hotkey_ignores_autorepeat_presses():
    starts = []
    hotkey = object()
    m = ptt.HotkeyRecorder(
        matches=lambda k: k is hotkey,
        on_start=lambda: starts.append(1),
        on_stop=lambda: None,
    )
    m.press(hotkey)
    m.press(hotkey)  # auto-repeat while held
    m.press(hotkey)
    assert starts == [1]  # only the first press starts recording


def test_hotkey_ignores_other_keys():
    events = []
    hotkey = object()
    other = object()
    m = ptt.HotkeyRecorder(
        matches=lambda k: k is hotkey,
        on_start=lambda: events.append("start"),
        on_stop=lambda: events.append("stop"),
    )
    assert m.press(other) is False
    assert m.release(other) is False
    assert events == []
    # release without a prior matching press does nothing
    assert m.release(hotkey) is False
    assert events == []


# --------------------------------------------------------------------------- #
# Clipboard delivery / restore
# --------------------------------------------------------------------------- #

def test_deliver_text_pastes_and_restores_inline():
    clipboard = {"value": "PRIOR"}
    calls = []

    def fake_copy(text):
        clipboard["value"] = text
        calls.append(("copy", text))

    def fake_paste():
        return clipboard["value"]

    def fake_keystroke(auto_enter=False):
        calls.append(("keystroke", auto_enter))

    prior = ptt.deliver_text(
        "hello world",
        auto_enter=True,
        restore_delay=0.0,
        copy=fake_copy,
        paste=fake_paste,
        keystroke=fake_keystroke,
        sleep=lambda _s: None,  # no real waiting
    )

    assert prior == "PRIOR"
    # Order: copy transcript -> keystroke(paste) -> copy prior back.
    assert calls == [
        ("copy", "hello world"),
        ("keystroke", True),
        ("copy", "PRIOR"),
    ]
    assert clipboard["value"] == "PRIOR"  # original clipboard restored


def test_deliver_text_defers_restore_to_scheduler():
    clipboard = {"value": "ORIG"}
    deferred = []

    def fake_copy(text):
        clipboard["value"] = text

    ptt.deliver_text(
        "typed text",
        copy=fake_copy,
        paste=lambda: clipboard["value"],
        keystroke=lambda auto_enter=False: None,
        sleep=lambda _s: None,
        schedule=deferred.append,  # capture the restore instead of running it
    )
    # Before the scheduled restore runs, the transcript is on the clipboard.
    assert clipboard["value"] == "typed text"
    assert len(deferred) == 1
    deferred[0]()  # run the restore
    assert clipboard["value"] == "ORIG"


def test_deliver_text_survives_paste_failure():
    clipboard = {"value": "X"}

    def boom():
        raise RuntimeError("no clipboard")

    prior = ptt.deliver_text(
        "t",
        copy=lambda text: clipboard.__setitem__("value", text),
        paste=boom,
        keystroke=lambda auto_enter=False: None,
        sleep=lambda _s: None,
    )
    assert prior == ""  # falls back to empty prior, doesn't crash


# --------------------------------------------------------------------------- #
# Vocabulary prompt
# --------------------------------------------------------------------------- #

def test_initial_prompt_includes_editing_terms():
    prompt = ptt.build_initial_prompt()
    for term in ("LUFS", "J-cut", "Resolve", "FCPXML", "timeline"):
        assert term in prompt


def test_initial_prompt_appends_extra_terms():
    prompt = ptt.build_initial_prompt(extra_terms=["Steadicam", "anamorphic"])
    assert "Steadicam" in prompt and "anamorphic" in prompt


# --------------------------------------------------------------------------- #
# Real transcription (only when faster-whisper is installed AND `say` exists)
# --------------------------------------------------------------------------- #

_HAS_FASTER_WHISPER = importlib.util.find_spec("faster_whisper") is not None
_HAS_SAY = shutil.which("say") is not None


def _synthesize_speech_wav(phrase: str, out_wav: Path) -> bool:
    """Use macOS `say` to make speech, convert to 16 kHz mono WAV. Returns ok."""
    aiff = out_wav.with_suffix(".aiff")
    try:
        subprocess.run(["say", "-o", str(aiff), phrase], check=True, timeout=60)
    except Exception:
        return False
    # Prefer afconvert (ships with macOS); fall back to ffmpeg.
    if shutil.which("afconvert"):
        rc = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             str(aiff), str(out_wav)],
            capture_output=True,
        ).returncode
        if rc == 0 and out_wav.exists():
            return True
    if shutil.which("ffmpeg"):
        rc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1",
             str(out_wav)],
            capture_output=True,
        ).returncode
        if rc == 0 and out_wav.exists():
            return True
    return False


@pytest.mark.skipif(
    not _HAS_FASTER_WHISPER, reason="faster-whisper not installed"
)
@pytest.mark.skipif(
    not _HAS_SAY, reason="macOS `say` not available to synthesize speech"
)
def test_real_transcription_of_spoken_phrase(tmp_path):
    phrase = "cut the music at thirty seconds"
    wav = tmp_path / "spoken.wav"
    if not _synthesize_speech_wav(phrase, wav):
        pytest.skip("could not synthesize/convert speech on this machine")

    transcriber = ptt.Transcriber(model_size="base", language="en")
    text = transcriber.transcribe_wav_file(str(wav)).lower()

    # Don't demand an exact string; assert the key words survived.
    assert "cut" in text
    assert "music" in text
    assert ("thirty" in text or "30" in text)
