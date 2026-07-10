"""Local push-to-talk voice bridge (macOS).

Importing this package never requires the ``[voice]`` extras — the pure helpers
(config loading, WAV encoding, the hotkey state machine, clipboard delivery
logic, the Whisper vocabulary prompt) are always available. Heavy dependencies
(sounddevice, faster-whisper, pynput, rumps) are imported lazily only when you
actually start recording.
"""

from __future__ import annotations

from .ptt import (
    DEFAULT_CONFIG,
    HotkeyRecorder,
    VoiceBridge,
    build_initial_prompt,
    default_config_path,
    deliver_text,
    encode_wav,
    generate_tone_wav,
    load_config,
    main,
)

__all__ = [
    "DEFAULT_CONFIG",
    "HotkeyRecorder",
    "VoiceBridge",
    "build_initial_prompt",
    "default_config_path",
    "deliver_text",
    "encode_wav",
    "generate_tone_wav",
    "load_config",
    "main",
]
