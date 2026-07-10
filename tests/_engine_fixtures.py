"""Deterministic media fixtures for the engine tests.

Audio fixtures are synthesised in numpy and written with the engine's own WAV
encoder (exact, reproducible). Video fixtures use ffmpeg's testsrc2 with two
distinct color grades. Everything is built once, lazily, into a module-level
temp dir and cached -- no binary assets are committed.

This is a plain helper module (not a pytest conftest) so it cannot collide with
sibling-owned test scaffolding in the same directory.
"""

from __future__ import annotations

import functools
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines import fftools  # noqa: E402

SR = 48000
_DIR = tempfile.mkdtemp(prefix="engine_fixtures_")


def _path(name: str) -> str:
    return os.path.join(_DIR, name)


# ---- audio fixtures ----------------------------------------------------

@functools.lru_cache(maxsize=1)
def dialogue_wav() -> str:
    """8 s of 220 Hz tone bursts: 0.8 s on / 0.8 s off (0.8 s pauses > 0.6 s
    max_pause). Speech-window / dead-air / ducking fixture."""
    dur = 8.0
    t = np.arange(int(dur * SR)) / SR
    period = 1.6
    on = (np.mod(t, period) < 0.8).astype(np.float32)
    # smooth the gate edges slightly to avoid clicks
    tone = 0.5 * np.sin(2 * np.pi * 220.0 * t).astype(np.float32)
    sig = (tone * on).astype(np.float32)
    stereo = np.stack([sig, sig], axis=-1)
    p = _path("dialogue.wav")
    fftools.encode_wav(stereo, p, sr=SR)
    return p


@functools.lru_cache(maxsize=1)
def music_wav() -> str:
    """8 s continuous bed: two low sines + light noise. Ducking fixture."""
    dur = 8.0
    n = int(dur * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(1234)
    bed = (
        0.25 * np.sin(2 * np.pi * 110.0 * t)
        + 0.15 * np.sin(2 * np.pi * 165.0 * t)
        + 0.05 * rng.standard_normal(n)
    ).astype(np.float32)
    stereo = np.stack([bed, bed], axis=-1)
    p = _path("music.wav")
    fftools.encode_wav(stereo, p, sr=SR)
    return p


@functools.lru_cache(maxsize=1)
def click_track_wav(bpm: float = 120.0, dur: float = 8.0) -> str:
    """Click track at a known BPM: short decaying 1 kHz transients on the beat."""
    n = int(dur * SR)
    sig = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm
    click_len = int(0.03 * SR)
    ct = np.arange(click_len) / SR
    click = (np.sin(2 * np.pi * 1000.0 * ct) * np.exp(-60.0 * ct)).astype(np.float32)
    beat = 0.0
    while beat < dur:
        start = int(beat * SR)
        end = min(n, start + click_len)
        sig[start:end] += click[: end - start]
        beat += period
    stereo = np.stack([sig, sig], axis=-1)
    p = _path(f"click_{int(bpm)}bpm.wav")
    fftools.encode_wav(stereo, p, sr=SR)
    return p


# ---- video fixtures ----------------------------------------------------

def _make_graded_video(name: str, eq: str, *, dur: float = 2.0) -> str:
    p = _path(name)
    cmd = [
        fftools.ffmpeg_path(), "-v", "error", "-y",
        "-f", "lavfi",
        "-i", f"testsrc2=size=320x180:rate=25:duration={dur}",
        "-vf", eq,
        "-pix_fmt", "yuv420p",
        "-frames:v", str(int(dur * 25)),
        p,
    ]
    fftools.run(cmd, timeout=60, check=True)
    return p


@functools.lru_cache(maxsize=1)
def graded_reference_mp4() -> str:
    """Warm grade of testsrc2."""
    return _make_graded_video(
        "ref_warm.mp4", "eq=gamma_r=1.5:gamma_g=1.0:gamma_b=0.6:saturation=1.25"
    )


@functools.lru_cache(maxsize=1)
def graded_target_mp4() -> str:
    """Cool grade of the same testsrc2 content."""
    return _make_graded_video(
        "target_cool.mp4", "eq=gamma_r=0.6:gamma_g=1.0:gamma_b=1.5:saturation=0.85"
    )


@functools.lru_cache(maxsize=1)
def plain_image_png() -> str:
    """A single flat-ish testsrc2 frame as a still."""
    p = _path("still.png")
    cmd = [
        fftools.ffmpeg_path(), "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=1:duration=1",
        "-frames:v", "1", p,
    ]
    fftools.run(cmd, timeout=30, check=True)
    return p


# ---- .cube parser (used to validate baked LUTs) ------------------------

def parse_cube(path: str) -> dict:
    """Tiny .cube parser. Returns {size, title, domain_min, domain_max, table}
    where table is an (size**3, 3) float array. Raises on malformed files."""
    size = None
    title = None
    domain_min = [0.0, 0.0, 0.0]
    domain_max = [1.0, 1.0, 1.0]
    rows = []
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("TITLE"):
                title = line.split(" ", 1)[1].strip().strip('"')
                continue
            if line.startswith("LUT_3D_SIZE"):
                size = int(line.split()[1])
                continue
            if line.startswith("DOMAIN_MIN"):
                domain_min = [float(x) for x in line.split()[1:4]]
                continue
            if line.startswith("DOMAIN_MAX"):
                domain_max = [float(x) for x in line.split()[1:4]]
                continue
            if line.startswith("LUT_1D_SIZE"):
                raise ValueError("1D LUT not expected")
            parts = line.split()
            if len(parts) == 3:
                rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
            else:
                raise ValueError(f"Malformed LUT row: {line!r}")
    if size is None:
        raise ValueError("Missing LUT_3D_SIZE")
    table = np.array(rows, dtype=np.float64)
    if table.shape != (size ** 3, 3):
        raise ValueError(f"Expected {size**3} rows, got {table.shape[0]}")
    if np.any(table < -1e-6) or np.any(table > 1.0 + 1e-6):
        raise ValueError("LUT values out of [0,1] range")
    return {
        "size": size,
        "title": title,
        "domain_min": domain_min,
        "domain_max": domain_max,
        "table": table,
    }
