"""Shared ffmpeg/ffprobe discovery and invocation helpers.

Deterministic, no network. Locates the ffmpeg/ffprobe binaries the same way
across every engine: PATH first, then the usual Homebrew / system fallbacks.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from typing import Any

# Search order for the binaries: PATH is consulted first (via shutil.which),
# then these explicit fallbacks in order.
_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


class FFmpegNotFound(RuntimeError):
    """Raised when a required ffmpeg/ffprobe binary cannot be located."""


@functools.lru_cache(maxsize=8)
def _find_binary(name: str) -> str | None:
    """Return an absolute path to ``name`` or None if not found."""
    # Allow explicit override via environment (e.g. FFMPEG_BIN / FFPROBE_BIN).
    env_key = f"{name.upper()}_BIN"
    override = os.environ.get(env_key)
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    found = shutil.which(name)
    if found:
        return found

    for d in _FALLBACK_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def ffmpeg_path() -> str:
    p = _find_binary("ffmpeg")
    if not p:
        raise FFmpegNotFound(
            "ffmpeg binary not found on PATH or in "
            f"{', '.join(_FALLBACK_DIRS)}. Install ffmpeg (e.g. `brew install ffmpeg`)."
        )
    return p


def ffprobe_path() -> str:
    p = _find_binary("ffprobe")
    if not p:
        raise FFmpegNotFound(
            "ffprobe binary not found on PATH or in "
            f"{', '.join(_FALLBACK_DIRS)}. Install ffmpeg (e.g. `brew install ffmpeg`)."
        )
    return p


def have_ffmpeg() -> bool:
    return _find_binary("ffmpeg") is not None and _find_binary("ffprobe") is not None


def run(cmd: list[str], *, timeout: float = 120.0, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess capturing stdout/stderr as text."""
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(cmd[:4])} ...\n"
            f"{completed.stderr.strip()[-2000:]}"
        )
    return completed


def decode_pcm_mono(path: str, *, sr: int = 22050, timeout: float = 120.0):
    """Decode ``path`` to a mono float32 numpy array at ``sr`` Hz in [-1, 1].

    Uses ffmpeg -> raw f32le on stdout. numpy is imported lazily so fftools has
    no hard numpy dependency for the discovery helpers.
    """
    import numpy as np  # local import

    cmd = [
        ffmpeg_path(),
        "-v", "error",
        "-i", path,
        "-ac", "1",
        "-ar", str(sr),
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "pipe:1",
    ]
    import subprocess

    completed = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg PCM decode failed for {path}: "
            f"{completed.stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return np.frombuffer(completed.stdout, dtype="<f4").astype(np.float32)


def decode_pcm(path: str, *, sr: int = 48000, channels: int = 2, timeout: float = 300.0):
    """Decode ``path`` to an (n_samples, channels) float32 array in [-1, 1]."""
    import numpy as np  # local import

    cmd = [
        ffmpeg_path(),
        "-v", "error",
        "-i", path,
        "-ac", str(channels),
        "-ar", str(sr),
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "pipe:1",
    ]
    import subprocess

    completed = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg PCM decode failed for {path}: "
            f"{completed.stderr.decode('utf-8', 'replace')[-500:]}"
        )
    flat = np.frombuffer(completed.stdout, dtype="<f4").astype(np.float32)
    if channels > 1:
        n = len(flat) // channels
        return flat[: n * channels].reshape(n, channels)
    return flat.reshape(-1, 1)


def encode_wav(samples, path: str, *, sr: int = 48000, timeout: float = 120.0) -> str:
    """Encode an (n, channels) float32 array to a 24-bit WAV via ffmpeg stdin."""
    import numpy as np  # local import

    arr = np.asarray(samples, dtype="<f4")
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    channels = arr.shape[1]
    raw = np.ascontiguousarray(arr).tobytes()
    cmd = [
        ffmpeg_path(),
        "-v", "error", "-y",
        "-f", "f32le",
        "-ar", str(sr),
        "-ac", str(channels),
        "-i", "pipe:0",
        "-c:a", "pcm_s24le",
        path,
    ]
    import subprocess

    completed = subprocess.run(cmd, input=raw, capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg WAV encode failed: {completed.stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return path


def ffprobe_json(path: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Return the full ffprobe JSON (format + streams) for ``path``."""
    cmd = [
        ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    completed = run(cmd, timeout=timeout, check=True)
    return json.loads(completed.stdout or "{}")
