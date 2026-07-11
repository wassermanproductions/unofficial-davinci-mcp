"""Broadcast-sane captions (.srt/.vtt) and YouTube chapters from a transcript.

`generate_captions` turns word-level timings into caption blocks that obey the
rules a human captioner follows: a line-length cap, a max lines-per-block, a
duration cap, breaks on sentence/clause boundaries and on long pauses, timing
snapped to the spoken words, a minimum inter-block gap, and no single-word
orphan lines. It writes an SRT or WebVTT sidecar next to the media.

`youtube_chapters` derives a chapter list - from provided timeline markers, or
from the transcript's own topic shifts (a long pause landing on a sentence
start) - and emits YouTube-description text (which always starts at 00:00).

Both read the cached/auto transcript via ``engines.transcribe`` and run in both
tiers. DaVinci Resolve *Studio* additionally exposes
``Timeline.CreateSubtitlesFromAudio(autoCaptionSettings)`` to build a subtitle
track live in the app - noted (not faked) in the result so the live tier can
offer it; the sidecar file here imports into any edition by hand.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from . import transcribe as _transcribe

_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?\s*$")
_CLAUSE_END_RE = re.compile(r"[,;:][\"')\]]?\s*$")
# A gap this long between words is a natural caption break.
_PAUSE_BREAK_S = 0.6
# A gap this long marks a possible chapter/topic shift.
_CHAPTER_PAUSE_S = 1.2


# --------------------------------------------------------------------------- #
# transcript loading
# --------------------------------------------------------------------------- #

def _load(media: str, transcript_json: Any, model: str, language: Optional[str]) -> dict[str, Any]:
    if isinstance(transcript_json, dict):
        data = transcript_json
    elif isinstance(transcript_json, str) and transcript_json not in ("", "auto"):
        p = Path(transcript_json).expanduser()
        if not p.exists():
            return {"ok": False, "error": f"Transcript JSON not found: {p}"}
        import json
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"Invalid transcript JSON: {exc}"}
    else:
        data = _transcribe.transcribe_media(media, language=language, model=model)
        if not data.get("ok"):
            return data
    words = data.get("words") or []
    if not words:
        return {"ok": False, "error": "Transcript has no word-level timings."}
    return {"ok": True, "words": words, "segments": data.get("segments") or [],
            "duration": data.get("duration")}


# --------------------------------------------------------------------------- #
# caption block construction
# --------------------------------------------------------------------------- #

def _word_text(w: dict[str, Any]) -> str:
    return (w.get("word") or "").strip()


def _wrap_lines(words: list[dict[str, Any]], max_line_chars: int, max_lines: int) -> list[str]:
    """Greedy line fill, then rebalance so the last line is never a lone word."""
    tokens = [_word_text(w) for w in words if _word_text(w)]
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        candidate = tok if not cur else f"{cur} {tok}"
        if len(candidate) > max_line_chars and cur:
            lines.append(cur)
            cur = tok
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    # Orphan fix: if the final line is a single word and the previous line has
    # room, pull the previous line's last word down so no word sits alone.
    if len(lines) >= 2 and len(lines[-1].split()) == 1:
        prev = lines[-2].split()
        if len(prev) >= 2:
            moved = prev.pop()
            lines[-2] = " ".join(prev)
            lines[-1] = f"{moved} {lines[-1]}"
    return lines[:max_lines] if max_lines else lines


def _build_blocks(
    words: list[dict[str, Any]],
    *,
    max_line_chars: int,
    max_lines: int,
    max_duration: float,
    min_gap: float,
) -> list[dict[str, Any]]:
    max_block_chars = max_line_chars * max_lines
    blocks: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []

    def _chars(ws: list[dict[str, Any]]) -> int:
        return len(" ".join(_word_text(w) for w in ws if _word_text(w)))

    def _flush() -> None:
        if not cur:
            return
        lines = _wrap_lines(cur, max_line_chars, max_lines)
        if not lines:
            cur.clear()
            return
        blocks.append({
            "start": round(float(cur[0]["start"]), 3),
            "end": round(float(cur[-1]["end"]), 3),
            "lines": lines,
            "words": [dict(w) for w in cur],
        })
        cur.clear()

    for i, w in enumerate(words):
        if not _word_text(w):
            continue
        # Break BEFORE adding if this word would overflow the block.
        if cur:
            dur = float(w["end"]) - float(cur[0]["start"])
            chars = _chars(cur + [w])
            if dur > max_duration or chars > max_block_chars:
                _flush()
        cur.append(w)
        text = _word_text(w)
        # Break AFTER a sentence end always; after a clause end once the block is
        # already substantial; and on a long pause to the next word.
        end_sentence = bool(_SENTENCE_END_RE.search(text))
        end_clause = bool(_CLAUSE_END_RE.search(text))
        next_gap = (float(words[i + 1]["start"]) - float(w["end"])) if i + 1 < len(words) else 1e9
        if end_sentence or (end_clause and _chars(cur) >= 0.6 * max_block_chars) or next_gap >= _PAUSE_BREAK_S:
            _flush()
    _flush()

    # Enforce a minimum gap between consecutive blocks (pull the earlier end in).
    for a, b in zip(blocks, blocks[1:]):
        if b["start"] - a["end"] < min_gap:
            a["end"] = round(max(a["start"] + 0.001, b["start"] - min_gap), 3)
    return blocks


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #

def _ts(seconds: float, *, vtt: bool) -> str:
    seconds = max(0.0, float(seconds))
    ms = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    hh, mm, ss = total // 3600, (total // 60) % 60, total % 60
    sep = "." if vtt else ","
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ms:03d}"


def _karaoke_line(words: list[dict[str, Any]], *, vtt: bool) -> str:
    """Inline per-word timestamps (WebVTT cue syntax)."""
    parts = []
    for w in words:
        tok = _word_text(w)
        if not tok:
            continue
        parts.append(f"<{_ts(w['start'], vtt=True)}>{tok}")
    return " ".join(parts)


def _render(blocks: list[dict[str, Any]], *, fmt: str, karaoke: bool) -> str:
    vtt = fmt == "vtt"
    out: list[str] = []
    if vtt:
        out.append("WEBVTT")
        out.append("")
    for idx, b in enumerate(blocks, start=1):
        if karaoke and vtt:
            body = _karaoke_line(b["words"], vtt=True)
        else:
            body = "\n".join(b["lines"])
        if vtt:
            out.append(f"{_ts(b['start'], vtt=True)} --> {_ts(b['end'], vtt=True)}")
            out.append(body)
            out.append("")
        else:
            out.append(str(idx))
            out.append(f"{_ts(b['start'], vtt=False)} --> {_ts(b['end'], vtt=False)}")
            out.append(body)
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# public: generate_captions
# --------------------------------------------------------------------------- #

def generate_captions(
    media: str,
    *,
    transcript_json: Any = None,
    format: str = "srt",
    max_line_chars: int = 42,
    max_lines: int = 2,
    max_duration: float = 6.0,
    min_gap: float = 0.09,
    karaoke: bool = False,
    output_path: Optional[str] = None,
    model: str = "base",
    language: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Build caption blocks and write an SRT/VTT sidecar. See the module docstring."""
    if format not in {"srt", "vtt"}:
        return {"ok": False, "error": "format must be 'srt' or 'vtt'."}
    if max_line_chars < 10:
        return {"ok": False, "error": "max_line_chars must be at least 10."}
    if max_lines < 1:
        return {"ok": False, "error": "max_lines must be at least 1."}
    if karaoke and format != "vtt":
        return {"ok": False, "error": "karaoke output requires format='vtt'."}

    media_path = str(Path(media).expanduser())
    loaded = _load(media_path, transcript_json, model, language)
    if not loaded.get("ok"):
        return loaded

    blocks = _build_blocks(
        loaded["words"],
        max_line_chars=max_line_chars,
        max_lines=max_lines,
        max_duration=max_duration,
        min_gap=min_gap,
    )
    if not blocks:
        return {"ok": False, "error": "No caption blocks could be built."}

    text = _render(blocks, fmt=format, karaoke=karaoke)

    if output_path:
        dest = Path(output_path).expanduser()
    else:
        dest = Path(media_path).with_suffix(f".{format}")

    public_blocks = [
        {"index": i + 1, "start": b["start"], "end": b["end"], "lines": b["lines"]}
        for i, b in enumerate(blocks)
    ]
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run and not confirm,
        "media": media_path,
        "format": format,
        "block_count": len(blocks),
        "output_path": str(dest),
        "blocks": public_blocks,
        "constraints": {
            "max_line_chars": max_line_chars, "max_lines": max_lines,
            "max_duration": max_duration, "min_gap": min_gap, "karaoke": karaoke,
        },
        "live_note": (
            "Resolve Studio can build a subtitle track live from audio via "
            "Timeline.CreateSubtitlesFromAudio(autoCaptionSettings); this sidecar "
            "imports into any edition (File > Import > Subtitle)."
        ),
    }
    if dry_run and not confirm:
        result["preview"] = text
        result["note"] = "Dry run. Set dry_run=false and confirm=true to write the sidecar."
        return result

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not write caption file: {exc}", "output_path": str(dest)}
    result["written"] = True
    return result


# --------------------------------------------------------------------------- #
# public: youtube_chapters
# --------------------------------------------------------------------------- #

def _chapter_ts(seconds: float) -> str:
    total = int(round(seconds))
    hh, mm, ss = total // 3600, (total // 60) % 60, total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"


def _title_from(words: list[dict[str, Any]], start_idx: int, max_words: int = 6) -> str:
    picked = []
    for w in words[start_idx:start_idx + max_words]:
        tok = _word_text(w)
        if tok:
            picked.append(tok)
        if _SENTENCE_END_RE.search(tok):
            break
    title = " ".join(picked).strip().rstrip(".,;:!?")
    return title[:1].upper() + title[1:] if title else "Chapter"


def youtube_chapters(
    media: str,
    *,
    transcript_json: Any = None,
    timeline_markers: Optional[list[dict[str, Any]]] = None,
    min_chapter_s: float = 20.0,
    model: str = "base",
    language: Optional[str] = None,
) -> dict[str, Any]:
    """Derive YouTube chapters from timeline markers or transcript topic shifts.

    The description always starts at 00:00 (a YouTube requirement).
    """
    chapters: list[dict[str, Any]] = []

    if timeline_markers:
        ordered = sorted(timeline_markers, key=lambda m: float(m.get("time", 0.0)))
        last = -1e9
        for m in ordered:
            t = float(m.get("time", 0.0))
            if t - last < min_chapter_s and chapters:
                continue
            chapters.append({"time": round(t, 3), "title": str(m.get("name") or m.get("title") or "Chapter")})
            last = t
        source = "timeline_markers"
    else:
        media_path = str(Path(media).expanduser())
        loaded = _load(media_path, transcript_json, model, language)
        if not loaded.get("ok"):
            return loaded
        words = loaded["words"]
        # Candidate boundaries: a long pause landing on the next word.
        boundaries = [0]
        last_time = 0.0
        for i in range(1, len(words)):
            gap = float(words[i]["start"]) - float(words[i - 1]["end"])
            prev = _word_text(words[i - 1])
            starts_sentence = bool(_SENTENCE_END_RE.search(prev))
            if gap >= _CHAPTER_PAUSE_S and (starts_sentence or gap >= _CHAPTER_PAUSE_S * 1.5):
                t = float(words[i]["start"])
                if t - last_time >= min_chapter_s:
                    boundaries.append(i)
                    last_time = t
        for b in boundaries:
            chapters.append({"time": round(float(words[b]["start"]), 3) if b else 0.0,
                             "title": _title_from(words, b)})
        source = "transcript"

    if not chapters:
        chapters = [{"time": 0.0, "title": "Intro"}]
    # Force the first chapter to 00:00 (YouTube requires it).
    chapters[0]["time"] = 0.0

    lines = [f"{_chapter_ts(c['time'])} {c['title']}" for c in chapters]
    description = "\n".join(lines)
    doctrine = (
        "Chapters derived from speech topic shifts (long pauses on sentence "
        "starts) — see get_editing_knowledge('dialogue-editing') for the "
        "pause/boundary doctrine behind them."
    )

    return {
        "ok": True,
        "source": source,
        "chapter_count": len(chapters),
        "chapters": chapters,
        "description": description,
        "description_with_note": f"{description}\n\n{doctrine}",
        "note": doctrine,
    }


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

def register(add_tool) -> None:
    add_tool(
        "generate_captions",
        {
            "type": "object",
            "properties": {
                "media": {"type": "string", "description": "Audio/video file to caption."},
                "transcript_json": {
                    "oneOf": [{"type": "object"}, {"type": "string"}],
                    "description": "Transcript dict or JSON path. Omit/'auto' to transcribe first.",
                },
                "format": {"type": "string", "enum": ["srt", "vtt"], "default": "srt"},
                "max_line_chars": {"type": "integer", "default": 42, "minimum": 10},
                "max_lines": {"type": "integer", "default": 2, "minimum": 1},
                "max_duration": {"type": "number", "default": 6.0, "minimum": 0.5},
                "min_gap": {"type": "number", "default": 0.09, "minimum": 0.0},
                "karaoke": {"type": "boolean", "default": False, "description": "Per-word timing tags (VTT only)."},
                "output_path": {"type": "string"},
                "model": {"type": "string", "default": "base"},
                "language": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["media"],
            "additionalProperties": False,
        },
        lambda params: generate_captions(
            params["media"],
            transcript_json=params.get("transcript_json"),
            format=params.get("format", "srt"),
            max_line_chars=params.get("max_line_chars", 42),
            max_lines=params.get("max_lines", 2),
            max_duration=params.get("max_duration", 6.0),
            min_gap=params.get("min_gap", 0.09),
            karaoke=params.get("karaoke", False),
            output_path=params.get("output_path"),
            model=params.get("model", "base"),
            language=params.get("language"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Build broadcast-sane SRT/VTT captions from a transcript (line-length, "
        "max-lines, duration limits, sentence/clause + pause breaks, speech-"
        "snapped timing, no orphan words, optional karaoke). Writes a sidecar; "
        "notes Resolve Studio's live CreateSubtitlesFromAudio.",
    )
    add_tool(
        "youtube_chapters",
        {
            "type": "object",
            "properties": {
                "media": {"type": "string", "description": "Audio/video file (for transcript-derived chapters)."},
                "transcript_json": {"oneOf": [{"type": "object"}, {"type": "string"}]},
                "timeline_markers": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional [{time, name}] markers to use as chapters instead of the transcript.",
                },
                "min_chapter_s": {"type": "number", "default": 20.0, "minimum": 1.0},
                "model": {"type": "string", "default": "base"},
                "language": {"type": "string"},
            },
            "required": ["media"],
            "additionalProperties": False,
        },
        lambda params: youtube_chapters(
            params["media"],
            transcript_json=params.get("transcript_json"),
            timeline_markers=params.get("timeline_markers"),
            min_chapter_s=params.get("min_chapter_s", 20.0),
            model=params.get("model", "base"),
            language=params.get("language"),
        ),
        "both",
        "Derive a YouTube chapter list from timeline markers or transcript topic "
        "shifts (long pauses on sentence starts) and emit description text that "
        "always starts at 00:00.",
    )
