"""Beat / onset grid for cut-to-music.

Primary path uses librosa (beat_track + onset detection) when it is installed.
Fallback path is a self-contained energy-envelope onset picker on ffmpeg-decoded
PCM -- documented as approximate. Output is a JSON-friendly dict consumed by
music_cut and assemble.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import fftools

_SR = 22050

try:  # optional
    import librosa  # type: ignore

    HAVE_LIBROSA = True
except Exception:  # pragma: no cover - environment dependent
    HAVE_LIBROSA = False


def _load_audio(path: str) -> np.ndarray:
    if HAVE_LIBROSA:
        y, _ = librosa.load(path, sr=_SR, mono=True)
        return y.astype(np.float32)
    return fftools.decode_pcm_mono(path, sr=_SR)


def _onset_envelope(y: np.ndarray, sr: int, hop: int) -> tuple[np.ndarray, np.ndarray]:
    """Spectral-flux-ish onset strength envelope via STFT magnitude diff.

    Returns (times, strength) with strength >= 0. Pure numpy, no scipy.
    """
    win = 1024
    if len(y) < win:
        y = np.pad(y, (0, win - len(y)))
    n_frames = 1 + (len(y) - win) // hop
    if n_frames < 1:
        n_frames = 1
    window = np.hanning(win).astype(np.float32)
    mags = np.empty((n_frames, win // 2 + 1), dtype=np.float32)
    for i in range(n_frames):
        start = i * hop
        frame = y[start:start + win]
        if len(frame) < win:
            frame = np.pad(frame, (0, win - len(frame)))
        spec = np.fft.rfft(frame * window)
        mags[i] = np.abs(spec)
    # Half-wave rectified spectral flux.
    diff = np.diff(mags, axis=0, prepend=mags[:1])
    flux = np.maximum(diff, 0.0).sum(axis=1)
    # Normalise.
    if flux.max() > 0:
        flux = flux / flux.max()
    times = np.arange(n_frames) * hop / sr
    return times, flux


def _pick_peaks(times: np.ndarray, strength: np.ndarray, *, min_gap_s: float) -> np.ndarray:
    """Simple peak picking: local maxima above adaptive threshold, min spacing."""
    if len(strength) < 3:
        return np.array([], dtype=float)
    # Adaptive threshold: local mean + fraction of std.
    thr = strength.mean() + 0.5 * strength.std()
    peaks = []
    last_t = -1e9
    dt = times[1] - times[0] if len(times) > 1 else 0.01
    min_gap = max(1, int(min_gap_s / dt)) if dt > 0 else 1
    i = 1
    n = len(strength)
    while i < n - 1:
        if strength[i] >= thr and strength[i] >= strength[i - 1] and strength[i] >= strength[i + 1]:
            if times[i] - last_t >= min_gap_s:
                peaks.append(i)
                last_t = times[i]
                i += min_gap
                continue
        i += 1
    return times[np.array(peaks, dtype=int)] if peaks else np.array([], dtype=float)


def _estimate_bpm_from_onsets(onset_times: np.ndarray) -> float:
    """Estimate tempo from inter-onset intervals via autocorrelation of a pulse."""
    if len(onset_times) < 2:
        return 0.0
    iois = np.diff(onset_times)
    iois = iois[(iois > 0.2) & (iois < 2.0)]  # 30-300 BPM band
    if len(iois) == 0:
        return 0.0
    # Use the median IOI as the beat period estimate (robust).
    period = float(np.median(iois))
    if period <= 0:
        return 0.0
    return 60.0 / period


def beat_grid(audio: str) -> dict[str, Any]:
    """Return bpm estimate, beat times, downbeat guess, onset strengths."""
    try:
        y = _load_audio(audio)
    except Exception as exc:
        return {"ok": False, "error": f"Audio load failed: {exc}", "audio": audio}
    if y.size == 0:
        return {"ok": False, "error": "Empty audio.", "audio": audio}

    duration = len(y) / _SR

    if HAVE_LIBROSA:
        hop = 512
        onset_env = librosa.onset.onset_strength(y=y, sr=_SR, hop_length=hop)
        tempo, beats = librosa.beat.beat_track(
            onset_envelope=onset_env, sr=_SR, hop_length=hop, units="frames"
        )
        beat_times = librosa.frames_to_time(beats, sr=_SR, hop_length=hop).tolist()
        onset_frames = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=_SR, hop_length=hop, units="frames"
        )
        onset_times = librosa.frames_to_time(onset_frames, sr=_SR, hop_length=hop)
        onset_strengths = onset_env[onset_frames] if len(onset_frames) else np.array([])
        # normalise strengths 0..1
        if onset_strengths.size and onset_strengths.max() > 0:
            onset_strengths = onset_strengths / onset_strengths.max()
        # Refine tempo from the actual beat spacing (more accurate on steady
        # material than librosa's global tempo scalar); fall back to it.
        bt = np.asarray(beat_times, dtype=float)
        if bt.size >= 2:
            diffs = np.diff(bt)
            mean_ioi = float(diffs.mean())      # averages out hop quantization
            med_ioi = float(np.median(diffs))
            # If mean and median broadly agree, the beats are regular -> use the
            # (unbiased) mean; otherwise the median is the safer robust estimate.
            ioi = mean_ioi if abs(mean_ioi - med_ioi) <= 0.15 * med_ioi else med_ioi
            bpm = 60.0 / ioi if ioi > 0 else float(np.atleast_1d(tempo)[0])
        else:
            bpm = float(np.atleast_1d(tempo)[0])
        method = "librosa"
    else:
        hop = 256
        times, flux = _onset_envelope(y, _SR, hop)
        # min gap from a rough tempo guess band (>= ~0.12s ~ 500 BPM ceiling)
        onset_times = _pick_peaks(times, flux, min_gap_s=0.12)
        onset_strengths = np.interp(onset_times, times, flux) if onset_times.size else np.array([])
        bpm = _estimate_bpm_from_onsets(onset_times)
        # Build a regular beat grid from bpm anchored on the first onset.
        if bpm > 0 and duration > 0:
            period = 60.0 / bpm
            start = onset_times[0] if onset_times.size else 0.0
            n = int((duration - start) / period) + 1
            beat_times = (start + np.arange(max(n, 0)) * period)
            beat_times = beat_times[beat_times <= duration].tolist()
        else:
            beat_times = onset_times.tolist()
        method = "energy_envelope_fallback"

    # Downbeat guess: assume 4/4, first beat is the downbeat; also report the
    # strongest onset near a beat as an alternative anchor.
    downbeat_time = beat_times[0] if beat_times else 0.0
    downbeat_period = None
    if len(beat_times) >= 2:
        beat_period = float(np.median(np.diff(beat_times)))
        downbeat_period = beat_period * 4.0  # 4/4 assumption

    return {
        "ok": True,
        "audio": audio,
        "method": method,
        "librosa": HAVE_LIBROSA,
        "sample_rate": _SR,
        "duration_seconds": round(duration, 4),
        "bpm": round(float(bpm), 3),
        "beat_times": [round(float(t), 4) for t in beat_times],
        "beat_count": len(beat_times),
        "downbeat_time": round(float(downbeat_time), 4),
        "downbeat_period_seconds": round(downbeat_period, 4) if downbeat_period else None,
        "onset_times": [round(float(t), 4) for t in np.atleast_1d(onset_times)],
        "onset_strengths": [round(float(s), 4) for s in np.atleast_1d(onset_strengths)],
        "approximate": not HAVE_LIBROSA,
    }


def register(add_tool) -> None:
    add_tool(
        "beat_grid",
        {
            "type": "object",
            "properties": {
                "audio": {"type": "string", "description": "Path to an audio or video file."}
            },
            "required": ["audio"],
            "additionalProperties": False,
        },
        lambda params: beat_grid(params["audio"]),
        "both",
        "Estimate tempo (BPM), beat times, a downbeat guess, and onset "
        "strengths. Uses librosa when available, else an approximate "
        "energy-envelope fallback.",
    )
