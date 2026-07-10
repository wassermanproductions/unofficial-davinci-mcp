"""Push-to-talk voice bridge for macOS.

Hold a hotkey anywhere -> record the mic -> release -> transcribe locally with
faster-whisper -> paste the transcript into whatever app is focused (your agent
terminal), optionally pressing Return. Everything runs on-device; nothing leaves
the machine.

Heavy dependencies (sounddevice, faster-whisper, pynput, rumps) are imported
lazily inside the functions that need them, so ``import voice`` and the pure
helpers here always work even when the ``[voice]`` extras aren't installed.
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    # Hold this key to talk. Named keys: right_option (default), left_option,
    # right_cmd, left_cmd, right_ctrl, f13...f19.
    "hotkey": "right_option",
    # Press Return automatically after pasting the transcript.
    "auto_enter": False,
    # faster-whisper model size: tiny | base | small | medium | large-v3.
    "model": "base",
    # Input device name or index; null = system default input.
    "input_device": None,
    # Hard cap on a single recording.
    "max_seconds": 90,
    # Force a language (e.g. "en") or null to auto-detect.
    "language": None,
    # Sample rate for capture and transcription (Whisper wants 16 kHz mono).
    "sample_rate": 16000,
    # Restore the user's previous clipboard this many seconds after paste.
    "restore_clipboard_delay": 0.5,
    # Play soft start/stop cue tones.
    "cue_tones": True,
    # Show a menu-bar icon (requires rumps). --no-menubar overrides to False.
    "menubar": True,
}

_CONFIG_FILENAME = "config.json"


def default_config_path() -> Path:
    """Path to the config file that ships next to this module."""
    return Path(__file__).resolve().parent / _CONFIG_FILENAME


def load_config(path: Optional[os.PathLike[str] | str] = None) -> dict[str, Any]:
    """Return DEFAULT_CONFIG merged with the JSON file at ``path`` if it exists.

    Unknown keys in the file are preserved (forward-compatible). Missing file or
    empty file yields the defaults. A malformed file raises ValueError with a
    clear message rather than a bare JSON error.
    """
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path) if path is not None else default_config_path()
    if not p.exists():
        return cfg
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return cfg
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {p}: {exc}") from exc
    if not isinstance(overrides, dict):
        raise ValueError(f"Config in {p} must be a JSON object, got {type(overrides).__name__}")
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# WAV encoding / cue tones  (pure stdlib; numpy used only if a array is passed)
# --------------------------------------------------------------------------- #

def encode_wav(samples: Iterable[float], sample_rate: int = 16000) -> bytes:
    """Encode mono float samples in [-1, 1] to 16-bit PCM WAV bytes.

    Accepts a numpy array (fast path) or any iterable of floats. Values are
    clamped to [-1, 1] before quantizing.
    """
    raw: bytes
    try:
        import numpy as np  # noqa: PLC0415 - optional fast path
    except Exception:
        np = None  # type: ignore[assignment]

    if np is not None and isinstance(samples, np.ndarray):
        arr = np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0)
        raw = (arr * 32767.0).astype("<i2").tobytes()
    else:
        raw = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, float(s))) * 32767.0))
            for s in samples
        )

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(raw)
    return buf.getvalue()


def generate_tone_wav(
    freq: float = 660.0,
    ms: int = 90,
    sample_rate: int = 16000,
    volume: float = 0.25,
) -> bytes:
    """Generate a short sine-tone WAV (for the start/stop cue) as bytes."""
    import math

    n = max(1, int(sample_rate * ms / 1000))
    # Short raised-cosine fade in/out so the tone doesn't click.
    fade = max(1, int(sample_rate * 0.005))
    samples = []
    for i in range(n):
        env = 1.0
        if i < fade:
            env = 0.5 - 0.5 * math.cos(math.pi * i / fade)
        elif i > n - fade:
            env = 0.5 - 0.5 * math.cos(math.pi * (n - i) / fade)
        samples.append(volume * env * math.sin(2 * math.pi * freq * i / sample_rate))
    return encode_wav(samples, sample_rate)


# --------------------------------------------------------------------------- #
# Hotkey state machine  (pure; driven by pynput at runtime, by tests directly)
# --------------------------------------------------------------------------- #

class HotkeyRecorder:
    """Press/release state machine for a single push-to-talk hotkey.

    ``matches(key)`` decides whether an incoming key event is the hotkey. The
    machine guards against key auto-repeat (many press events while held) by
    only firing ``on_start`` on the first press and ``on_stop`` on the matching
    release.
    """

    def __init__(
        self,
        matches: Callable[[Any], bool],
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
    ) -> None:
        self._matches = matches
        self._on_start = on_start
        self._on_stop = on_stop
        self.recording = False

    def press(self, key: Any) -> bool:
        """Handle a key-down. Returns True if it started a recording."""
        if self._matches(key) and not self.recording:
            self.recording = True
            self._on_start()
            return True
        return False

    def release(self, key: Any) -> bool:
        """Handle a key-up. Returns True if it stopped a recording."""
        if self._matches(key) and self.recording:
            self.recording = False
            self._on_stop()
            return True
        return False


# --------------------------------------------------------------------------- #
# Clipboard delivery  (pure logic; subprocess shims injected at runtime)
# --------------------------------------------------------------------------- #

def deliver_text(
    text: str,
    *,
    auto_enter: bool = False,
    restore_delay: float = 0.5,
    copy: Callable[[str], None],
    paste: Callable[[], str],
    keystroke: Callable[..., None],
    sleep: Callable[[float], None] = time.sleep,
    schedule: Optional[Callable[[Callable[[], None]], None]] = None,
) -> str:
    """Copy ``text``, paste it into the frontmost app, then restore the clipboard.

    Returns the prior clipboard contents (what gets restored). The paste is a
    Cmd-V keystroke; ``auto_enter`` appends a Return. Injecting copy/paste/
    keystroke/sleep/schedule makes this fully testable without a real clipboard.
    When ``schedule`` is given the restore runs there (e.g. a background timer);
    otherwise it runs inline after ``restore_delay``.
    """
    prior = ""
    try:
        prior = paste()
    except Exception:
        prior = ""

    copy(text)
    keystroke(auto_enter=auto_enter)

    def _restore() -> None:
        sleep(restore_delay)
        copy(prior)

    if schedule is not None:
        schedule(_restore)
    else:
        _restore()
    return prior


# --------------------------------------------------------------------------- #
# Filmmaker vocabulary bias for Whisper
# --------------------------------------------------------------------------- #

_FILMMAKER_TERMS = [
    "timeline", "LUT", "LUFS", "J-cut", "L-cut", "sting", "sting-out",
    "Resolve", "DaVinci Resolve", "FCPXML", "EDL", "beat grid", "beat snap",
    "teal and orange", "bleach bypass", "day for night", "golden hour",
    "color match", "dead air", "tighten dialogue", "mix", "duck", "ducking",
    "true peak", "dBTP", "crossfade", "handles", "punch-in", "B-roll",
    "downbeat", "chorus", "verse", "frame", "frames", "cube",
]


def build_initial_prompt(extra_terms: Optional[Iterable[str]] = None) -> str:
    """Whisper ``initial_prompt`` that biases decoding toward editing jargon."""
    terms = list(_FILMMAKER_TERMS)
    if extra_terms:
        terms.extend(extra_terms)
    return (
        "Video editing voice command using terms like "
        + ", ".join(terms)
        + "."
    )


# --------------------------------------------------------------------------- #
# Runtime shims (subprocess) — thin, not unit-tested directly
# --------------------------------------------------------------------------- #

def _pbcopy(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)


def _pbpaste() -> str:
    out = subprocess.run(["pbpaste"], capture_output=True, check=False)
    return out.stdout.decode("utf-8", "replace")


def _paste_keystroke(auto_enter: bool = False) -> None:
    """Send Cmd-V (and optionally Return) to the frontmost app via System Events."""
    script = 'tell application "System Events" to keystroke "v" using command down'
    subprocess.run(["osascript", "-e", script], check=False)
    if auto_enter:
        time.sleep(0.05)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 36'],
            check=False,
        )


def _play_wav_bytes(data: bytes) -> None:
    """Play WAV bytes via afplay; fall back to NSBeep, then silence."""
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(data)
            path = tf.name
        subprocess.run(["afplay", path], check=False)
        try:
            os.unlink(path)
        except OSError:
            pass
    except Exception:
        try:
            subprocess.run(
                ["osascript", "-e", "beep"],
                check=False,
            )
        except Exception:
            pass


def _resolve_hotkey(name: str):
    """Map a config hotkey name to a pynput Key. Imported lazily."""
    from pynput import keyboard  # noqa: PLC0415

    table = {
        "right_option": keyboard.Key.alt_r,
        "left_option": keyboard.Key.alt_l,
        "right_alt": keyboard.Key.alt_r,
        "left_alt": keyboard.Key.alt_l,
        "right_cmd": keyboard.Key.cmd_r,
        "left_cmd": keyboard.Key.cmd_l,
        "right_ctrl": keyboard.Key.ctrl_r,
        "left_ctrl": keyboard.Key.ctrl_l,
        "right_shift": keyboard.Key.shift_r,
        "left_shift": keyboard.Key.shift_l,
    }
    for i in range(13, 20):
        fk = getattr(keyboard.Key, f"f{i}", None)
        if fk is not None:
            table[f"f{i}"] = fk
    key = table.get((name or "").strip().lower())
    if key is None:
        raise ValueError(
            f"Unknown hotkey {name!r}. Choose one of: {', '.join(sorted(table))}."
        )
    return key


# --------------------------------------------------------------------------- #
# Recorder + transcriber (lazy heavy deps)
# --------------------------------------------------------------------------- #

class MicRecorder:
    """Record mono float32 audio into memory using sounddevice."""

    def __init__(self, sample_rate: int = 16000, device: Any = None) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self._frames: list[Any] = []
        self._stream = None

    def start(self) -> None:
        import numpy as np  # noqa: PLC0415
        import sounddevice as sd  # noqa: PLC0415

        self._frames = []

        def _callback(indata, _frames, _time, _status):  # pragma: no cover - realtime
            self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=_callback,
        )
        self._stream.start()
        # keep numpy referenced for type clarity
        del np

    def stop(self):
        import numpy as np  # noqa: PLC0415

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return np.zeros(0, dtype="float32")
        return np.concatenate(self._frames, axis=0).reshape(-1).astype("float32")


class Transcriber:
    """faster-whisper wrapper. Model loads on first use (and downloads once)."""

    def __init__(
        self,
        model_size: str = "base",
        language: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> None:
        self.model_size = model_size
        self.language = language
        self.initial_prompt = initial_prompt or build_initial_prompt()
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8"
            )
        return self._model

    def transcribe_wav_file(self, wav_path: str) -> str:
        model = self._ensure_model()
        segments, _info = model.transcribe(
            wav_path,
            language=self.language,
            initial_prompt=self.initial_prompt,
            beam_size=1,
            vad_filter=True,
        )
        return "".join(seg.text for seg in segments).strip()

    def transcribe_samples(self, samples, sample_rate: int = 16000) -> str:
        """Transcribe an in-memory float array by writing a temp WAV first."""
        import tempfile

        data = encode_wav(samples, sample_rate)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(data)
            path = tf.name
        try:
            return self.transcribe_wav_file(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

class VoiceBridge:
    """Glue: hotkey -> record -> transcribe -> paste. Runtime object."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.recorder = MicRecorder(
            sample_rate=config["sample_rate"], device=config["input_device"]
        )
        self.transcriber = Transcriber(
            model_size=config["model"],
            language=config["language"],
            initial_prompt=build_initial_prompt(),
        )
        self._start_tone = (
            generate_tone_wav(660, 90, config["sample_rate"])
            if config["cue_tones"]
            else b""
        )
        self._stop_tone = (
            generate_tone_wav(440, 90, config["sample_rate"])
            if config["cue_tones"]
            else b""
        )
        self._deadline = 0.0

    def on_start(self) -> None:
        if self._start_tone:
            _play_wav_bytes(self._start_tone)
        self._deadline = time.time() + self.config["max_seconds"]
        self.recorder.start()

    def on_stop(self) -> None:
        samples = self.recorder.stop()
        if self._stop_tone:
            _play_wav_bytes(self._stop_tone)
        # Trim to max_seconds worth of samples defensively.
        limit = int(self.config["max_seconds"] * self.config["sample_rate"])
        if hasattr(samples, "__len__") and len(samples) > limit:
            samples = samples[:limit]
        if not hasattr(samples, "__len__") or len(samples) == 0:
            return
        text = self.transcriber.transcribe_samples(
            samples, self.config["sample_rate"]
        )
        if not text:
            return
        deliver_text(
            text,
            auto_enter=self.config["auto_enter"],
            restore_delay=self.config["restore_clipboard_delay"],
            copy=_pbcopy,
            paste=_pbpaste,
            keystroke=_paste_keystroke,
            schedule=lambda fn: _spawn_thread(fn),
        )

    def run_headless(self) -> None:  # pragma: no cover - requires pynput + mic
        from pynput import keyboard  # noqa: PLC0415

        hotkey = _resolve_hotkey(self.config["hotkey"])
        machine = HotkeyRecorder(
            matches=lambda k: k == hotkey,
            on_start=self.on_start,
            on_stop=self.on_stop,
        )
        with keyboard.Listener(
            on_press=machine.press, on_release=machine.release
        ) as listener:
            listener.join()


def _spawn_thread(fn: Callable[[], None]) -> None:
    import threading

    threading.Thread(target=fn, daemon=True).start()


def run_menubar(config: dict[str, Any]) -> None:  # pragma: no cover - needs rumps
    """Menu-bar app via rumps; falls back to headless if rumps is unavailable."""
    try:
        import rumps  # noqa: PLC0415
    except Exception:
        VoiceBridge(config).run_headless()
        return

    bridge = VoiceBridge(config)

    class _App(rumps.App):
        def __init__(self) -> None:
            super().__init__("PTT", icon=None, quit_button="Quit")
            self.menu = [
                rumps.MenuItem(f"Hold {config['hotkey']} to talk"),
                rumps.MenuItem(
                    f"Auto-Enter: {'on' if config['auto_enter'] else 'off'}"
                ),
                rumps.MenuItem(f"Model: {config['model']}"),
            ]

    import threading

    threading.Thread(target=bridge.run_headless, daemon=True).start()
    _App().run()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="voice",
        description="Local push-to-talk voice bridge for agent terminals (macOS).",
    )
    parser.add_argument("--config", default=None, help="Path to config.json.")
    parser.add_argument(
        "--no-menubar",
        action="store_true",
        help="Run as a plain background listener with no menu-bar icon.",
    )
    parser.add_argument("--model", default=None, help="Override model size.")
    parser.add_argument("--hotkey", default=None, help="Override hotkey name.")
    parser.add_argument(
        "--auto-enter",
        action="store_true",
        help="Press Return after pasting the transcript.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.model:
        config["model"] = args.model
    if args.hotkey:
        config["hotkey"] = args.hotkey
    if args.auto_enter:
        config["auto_enter"] = True
    if args.no_menubar:
        config["menubar"] = False

    print(
        f"Voice bridge ready. Hold {config['hotkey']} to talk. "
        f"Model={config['model']}, auto_enter={config['auto_enter']}. Ctrl-C to quit."
    )
    try:
        if config["menubar"]:
            run_menubar(config)
        else:
            VoiceBridge(config).run_headless()
    except KeyboardInterrupt:
        print("\nBye.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
