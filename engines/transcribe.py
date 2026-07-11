"""Local word-level transcription for any audio/video file.

`transcribe_media` extracts the audio to 16 kHz mono WAV with ffmpeg and runs
faster-whisper on-device (no network, no cloud key) to produce word-level
timestamps. It is the transcription source for the transcript-driven editing
tools in ``text_edit`` and works in BOTH tiers (free Resolve and Studio).

Note: DaVinci Resolve *Studio* has native, in-app transcription too
(``MediaPoolItem.TranscribeAudio`` / ``Timeline.CreateSubtitlesFromAudio``),
which lands text directly in the Resolve project. This engine is the portable
alternative: it runs anywhere ffmpeg + faster-whisper are installed, needs no
running Resolve, and returns machine-readable word timings the editing tools
consume — so a free-Resolve user gets the same transcript-driven workflow.

faster-whisper is an optional extra (the ``[voice]`` install). It is imported
lazily so importing this module — and running the deterministic editing tools on
a pre-made transcript — never requires it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from . import fftools, media

_INSTALL_HINT = (
    "faster-whisper is not installed. Install the voice extra:\n"
    "    pip install 'unofficial-davinci-mcp[voice]'"
)

# A filmmaker-vocabulary initial_prompt biases Whisper toward editing jargon,
# mirroring the push-to-talk bridge. Reuse voice/ptt's list when importable so
# the vocabulary stays in one place; fall back to a small local prompt so this
# engine has no hard dependency on the voice package.
try:  # pragma: no cover - trivial import guard
    from voice.ptt import build_initial_prompt as _build_initial_prompt
except Exception:  # pragma: no cover - voice package absent

    def _build_initial_prompt(extra_terms: Optional[Iterable[str]] = None) -> str:
        terms = [
            "timeline", "LUT", "LUFS", "J-cut", "L-cut", "Resolve",
            "DaVinci Resolve", "FCPXML", "EDL", "B-roll", "crossfade",
            "handles", "punch-in", "color match", "dead air",
        ]
        if extra_terms:
            terms.extend(extra_terms)
        return "Video editing dialogue using terms like " + ", ".join(terms) + "."


def _extract_audio_wav(src: str, *, sr: int = 16000, timeout: float = 900.0) -> str:
    """Decode ``src`` to a temporary 16 kHz mono 16-bit WAV and return its path."""
    out = tempfile.mkstemp(prefix="transcribe_", suffix=".wav")[1]
    cmd = [
        fftools.ffmpeg_path(), "-v", "error", "-y",
        "-i", src,
        "-ac", "1", "-ar", str(sr),
        "-c:a", "pcm_s16le",
        out,
    ]
    fftools.run(cmd, timeout=timeout, check=True)
    return out


def _cache_path(media_path: str, json_path: Optional[str]) -> Path:
    if json_path:
        return Path(json_path).expanduser()
    p = Path(media_path).expanduser()
    return p.with_suffix(p.suffix + ".transcript.json")


def transcribe_media(
    path: str,
    language: Optional[str] = None,
    model: str = "base",
    *,
    json_path: Optional[str] = None,
    cache: bool = True,
    refresh: bool = False,
    initial_prompt: Optional[str] = None,
    extra_terms: Optional[list[str]] = None,
    beam_size: int = 1,
    vad_filter: bool = True,
) -> dict[str, Any]:
    """Transcribe any audio/video file to word-level timings on-device.

    Returns ``{ok, segments, words, detected_language, duration, ...}`` where
    ``segments`` is ``[{start, end, text}]`` and ``words`` is
    ``[{start, end, word, confidence}]``. A JSON transcript is cached next to the
    media (``<file>.transcript.json``) or at ``json_path``; a fresh-enough cache
    is reused unless ``refresh`` is set.
    """
    p = str(Path(path).expanduser())
    if not Path(p).exists():
        return {"ok": False, "error": "File does not exist.", "path": p}

    probe = media.probe_one(p)
    kind = probe.get("kind", "audio")
    if kind not in {"audio", "video"}:
        return {"ok": False, "error": f"Unsupported media kind '{kind}'.", "path": p}

    cache_file = _cache_path(p, json_path)

    # Reuse a cached transcript when it is at least as new as the media.
    if cache and not refresh and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cache_file.stat().st_mtime >= Path(p).stat().st_mtime:
                cached["ok"] = True
                cached["cached"] = True
                cached["cache_path"] = str(cache_file)
                return cached
        except (OSError, json.JSONDecodeError):
            pass  # unreadable cache -> re-transcribe

    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except Exception:
        return {"ok": False, "error": _INSTALL_HINT, "path": p, "dependency": "faster-whisper"}

    prompt = initial_prompt if initial_prompt is not None else _build_initial_prompt(extra_terms)

    try:
        wav = _extract_audio_wav(p)
    except Exception as exc:
        return {"ok": False, "error": f"Audio extraction failed: {type(exc).__name__}: {exc}", "path": p}

    try:
        whisper = WhisperModel(model, device="cpu", compute_type="int8")
        segments_iter, info = whisper.transcribe(
            wav,
            language=language,
            initial_prompt=prompt,
            beam_size=beam_size,
            vad_filter=vad_filter,
            word_timestamps=True,
        )

        segments: list[dict[str, Any]] = []
        words: list[dict[str, Any]] = []
        for seg in segments_iter:
            segments.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": seg.text.strip(),
            })
            for w in (getattr(seg, "words", None) or []):
                entry: dict[str, Any] = {
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                    "word": w.word,
                }
                prob = getattr(w, "probability", None)
                if prob is not None:
                    entry["confidence"] = round(float(prob), 4)
                words.append(entry)
    except Exception as exc:
        return {"ok": False, "error": f"Transcription failed: {type(exc).__name__}: {exc}", "path": p}
    finally:
        try:
            Path(wav).unlink()
        except OSError:
            pass

    duration = probe.get("duration_seconds")
    if duration is None:
        duration = getattr(info, "duration", None)

    result: dict[str, Any] = {
        "ok": True,
        "cached": False,
        "path": p,
        "kind": kind,
        "model": model,
        "detected_language": getattr(info, "language", None),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        "duration": round(float(duration), 3) if duration else None,
        "segment_count": len(segments),
        "word_count": len(words),
        "segments": segments,
        "words": words,
    }

    if cache:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["cache_path"] = str(cache_file)
        except OSError as exc:
            result["cache_error"] = f"Could not write transcript cache: {exc}"

    return result


def register(add_tool) -> None:
    add_tool(
        "transcribe_media",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Audio or video file to transcribe."},
                "language": {"type": "string", "description": "Force a language (e.g. 'en'); omit to auto-detect."},
                "model": {
                    "type": "string",
                    "default": "base",
                    "description": "faster-whisper model size: tiny | base | small | medium | large-v3.",
                },
                "json_path": {"type": "string", "description": "Where to cache the transcript JSON (defaults next to the media)."},
                "cache": {"type": "boolean", "default": True, "description": "Cache the transcript and reuse a fresh copy."},
                "refresh": {"type": "boolean", "default": False, "description": "Ignore any cached transcript and re-run the model."},
                "extra_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra vocabulary (names, product terms) to bias decoding toward.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        lambda params: transcribe_media(
            params["path"],
            language=params.get("language"),
            model=params.get("model", "base"),
            json_path=params.get("json_path"),
            cache=params.get("cache", True),
            refresh=params.get("refresh", False),
            extra_terms=params.get("extra_terms"),
        ),
        "both",
        "Transcribe any audio/video file to word-level timings on-device with "
        "faster-whisper (no cloud). Returns segments + per-word start/end/"
        "confidence and caches a JSON transcript. Feeds cut_by_transcript. "
        "Works in both tiers; Resolve Studio also has native in-app transcription.",
    )
