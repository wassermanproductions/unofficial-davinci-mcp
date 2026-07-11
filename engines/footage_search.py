"""Search the *spoken content* of footage and cut selects from the hits.

`find_in_footage` transcribes each media file once (cached on disk via
``engines.transcribe``), then searches the word-level transcript so you can find
where a line was said across a whole shoot - not just filenames. Three modes:

  - 'phrase'    : the query as a consecutive run of words (the default).
  - 'all_words' : every occurrence of each query word, but only in files that
                  contain ALL of them (an AND gate at the file level).
  - 'regex'     : the query as a regular expression matched over the running
                  transcript text, mapped back to the covering word timings.

Every hit comes back with its start/end, the matched text, and a padded
``context`` window (``context_s`` of speech either side). Matching is
case-insensitive and spans any number of files; files with no speech (or no
transcript yet available) are reported separately.

With ``build_selects=true`` (confirm-gated) the hits, in order, become a selects
timeline: each hit padded with 0.5 s handles, exported as an FCPXML via the
interchange generator plus an ``assemble_edit``-compatible plan.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from . import assemble as _assemble
from . import media as _media
from . import transcribe as _transcribe

_SELECT_HANDLE = 0.5
_PUNCT_STRIP_RE = re.compile(r"^[^\w']+|[^\w']+$")


def _norm(word: str) -> str:
    return _PUNCT_STRIP_RE.sub("", (word or "").strip().lower())


def _gather_media(media: Any) -> tuple[list[str], Optional[str]]:
    """Accept a list of paths or a directory; return audio/video paths."""
    if isinstance(media, str):
        p = Path(media).expanduser()
        if p.is_dir():
            scan = _media.scan_media_folder(str(p))
            if not scan.get("ok"):
                return [], scan.get("error", "Could not scan media directory.")
            return [
                m["path"]
                for m in scan.get("media", [])
                if m.get("ok") and m.get("kind") in {"audio", "video"}
            ], None
        return [str(p)], None
    if isinstance(media, (list, tuple)):
        return [str(Path(m).expanduser()) for m in media], None
    return [], "media must be a path, a directory, or a list of paths."


def _context(
    words: list[dict[str, Any]], lo_idx: int, hi_idx: int, context_s: float
) -> tuple[str, float, float]:
    """Words within context_s of the hit on each side -> (text, start, end)."""
    hit_start = float(words[lo_idx]["start"])
    hit_end = float(words[hi_idx]["end"])
    ctx_lo = lo_idx
    while ctx_lo > 0 and hit_start - float(words[ctx_lo - 1]["start"]) <= context_s:
        ctx_lo -= 1
    ctx_hi = hi_idx
    while (
        ctx_hi < len(words) - 1
        and float(words[ctx_hi + 1]["end"]) - hit_end <= context_s
    ):
        ctx_hi += 1
    text = " ".join(w.get("word", "").strip() for w in words[ctx_lo:ctx_hi + 1]).strip()
    return text, float(words[ctx_lo]["start"]), float(words[ctx_hi]["end"])


def _hit(words: list[dict[str, Any]], file: str, lo: int, hi: int, context_s: float) -> dict[str, Any]:
    ctx_text, ctx_start, ctx_end = _context(words, lo, hi, context_s)
    return {
        "file": file,
        "start": round(float(words[lo]["start"]), 3),
        "end": round(float(words[hi]["end"]), 3),
        "text": " ".join(w.get("word", "").strip() for w in words[lo:hi + 1]).strip(),
        "context": ctx_text,
        "context_start": round(ctx_start, 3),
        "context_end": round(ctx_end, 3),
    }


def _search_phrase(
    words: list[dict[str, Any]], tokens: list[str], file: str, context_s: float
) -> list[dict[str, Any]]:
    norms = [_norm(w.get("word", "")) for w in words]
    k = len(tokens)
    hits = []
    for i in range(0, len(norms) - k + 1):
        if norms[i:i + k] == tokens:
            hits.append(_hit(words, file, i, i + k - 1, context_s))
    return hits


def _search_all_words(
    words: list[dict[str, Any]], tokens: list[str], file: str, context_s: float
) -> list[dict[str, Any]]:
    norms = [_norm(w.get("word", "")) for w in words]
    present = set(norms)
    if not all(t in present for t in tokens):
        return []  # file-level AND gate: must contain every query word
    token_set = set(tokens)
    return [
        _hit(words, file, i, i, context_s)
        for i, nw in enumerate(norms)
        if nw in token_set
    ]


def _search_regex(
    words: list[dict[str, Any]], pattern: "re.Pattern[str]", file: str, context_s: float
) -> list[dict[str, Any]]:
    # Build the running normalised text with a char-offset -> word-index map.
    spans: list[tuple[int, int, int]] = []
    pieces: list[str] = []
    cursor = 0
    for idx, w in enumerate(words):
        nw = _norm(w.get("word", ""))
        if not nw:
            continue
        if pieces:
            cursor += 1  # the separating space
        spans.append((cursor, cursor + len(nw), idx))
        pieces.append(nw)
        cursor += len(nw)
    text = " ".join(pieces)
    hits = []
    for m in pattern.finditer(text):
        s, e = m.start(), m.end()
        covered = [wi for (cs, ce, wi) in spans if cs < e and ce > s]
        if not covered:
            continue
        hits.append(_hit(words, file, covered[0], covered[-1], context_s))
    return hits


def find_in_footage(
    query: str,
    media: Any,
    *,
    mode: str = "phrase",
    context_s: float = 1.5,
    model: str = "base",
    language: Optional[str] = None,
    build_selects: bool = False,
    timeline_name: Optional[str] = None,
    output_path: Optional[str] = None,
    fps: int = 24,
    resolution: str = "1920x1080",
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Find spoken ``query`` across footage. See the module docstring for modes.

    ``build_selects`` (confirm-gated) exports the hits as a selects FCPXML.
    """
    if mode not in {"phrase", "all_words", "regex"}:
        return {"ok": False, "error": f"Unknown mode '{mode}' (phrase|all_words|regex)."}
    if not query or not str(query).strip():
        return {"ok": False, "error": "query must be a non-empty string."}

    paths, err = _gather_media(media)
    if err:
        return {"ok": False, "error": err}
    if not paths:
        return {"ok": False, "error": "No audio/video media found to search."}

    pattern = None
    tokens: list[str] = []
    if mode == "regex":
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            return {"ok": False, "error": f"Invalid regex: {exc}"}
    else:
        tokens = [t for t in (_norm(t) for t in query.split()) if t]
        if not tokens:
            return {"ok": False, "error": "query has no searchable words after normalisation."}

    hits: list[dict[str, Any]] = []
    files_searched: list[str] = []
    files_without_speech: list[dict[str, str]] = []

    for path in paths:
        result = _transcribe.transcribe_media(path, language=language, model=model)
        if not result.get("ok"):
            files_without_speech.append({"file": path, "reason": result.get("error", "transcription failed")})
            continue
        words = result.get("words") or []
        if not words:
            files_without_speech.append({"file": path, "reason": "no speech detected"})
            continue
        files_searched.append(path)
        if mode == "phrase":
            hits.extend(_search_phrase(words, tokens, path, context_s))
        elif mode == "all_words":
            hits.extend(_search_all_words(words, tokens, path, context_s))
        else:
            hits.extend(_search_regex(words, pattern, path, context_s))

    # Deterministic order: by file (search order), then time.
    file_rank = {p: i for i, p in enumerate(paths)}
    hits.sort(key=lambda h: (file_rank.get(h["file"], 0), h["start"]))

    out: dict[str, Any] = {
        "ok": True,
        "query": query,
        "mode": mode,
        "hit_count": len(hits),
        "hits": hits,
        "files_searched": files_searched,
        "files_without_speech": files_without_speech,
    }

    if not build_selects:
        return out

    if not hits:
        out["selects"] = {"ok": False, "error": "No hits to build a selects timeline from."}
        return out

    name = timeline_name or f"Selects — {query[:40]}"
    video_clips = [
        {
            "path": h["file"],
            "in": round(max(0.0, h["start"] - _SELECT_HANDLE), 3),
            "out": round(h["end"] + _SELECT_HANDLE, 3),
        }
        for h in hits
    ]
    plan = {
        "timeline": {"name": name, "fps": float(fps), "resolution": resolution},
        "video": video_clips,
    }
    out["selects_plan"] = plan
    out["dry_run"] = dry_run and not confirm
    fcpxml = _assemble.to_fcpxml(plan, output_path, dry_run=dry_run and not confirm)
    out["selects"] = fcpxml
    if dry_run and not confirm:
        out["note"] = (
            "Dry run. Set dry_run=false and confirm=true to write the selects FCPXML."
        )
    return out


def register(add_tool) -> None:
    add_tool(
        "find_in_footage",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words/phrase or regex to find in the spoken audio."},
                "media": {
                    "oneOf": [
                        {"type": "string", "description": "A file or a directory to search."},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "A path, a directory, or a list of media paths.",
                },
                "mode": {"type": "string", "enum": ["phrase", "all_words", "regex"], "default": "phrase"},
                "context_s": {"type": "number", "default": 1.5, "minimum": 0.0,
                               "description": "Seconds of surrounding speech to include as context."},
                "model": {"type": "string", "default": "base", "description": "faster-whisper model for uncached files."},
                "language": {"type": "string"},
                "build_selects": {"type": "boolean", "default": False,
                                   "description": "Export the hits (0.5s handles) as a selects FCPXML."},
                "timeline_name": {"type": "string"},
                "output_path": {"type": "string"},
                "fps": {"type": "integer", "default": 24, "minimum": 1, "maximum": 240},
                "resolution": {"type": "string", "default": "1920x1080"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["query", "media"],
            "additionalProperties": False,
        },
        lambda params: find_in_footage(
            params["query"],
            params["media"],
            mode=params.get("mode", "phrase"),
            context_s=params.get("context_s", 1.5),
            model=params.get("model", "base"),
            language=params.get("language"),
            build_selects=params.get("build_selects", False),
            timeline_name=params.get("timeline_name"),
            output_path=params.get("output_path"),
            fps=params.get("fps", 24),
            resolution=params.get("resolution", "1920x1080"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Search the SPOKEN content of footage (word-level, cached transcripts) "
        "by phrase, all-words, or regex; return timestamped hits with context "
        "and, on confirm, cut a selects FCPXML with 0.5s handles. Feeds "
        "transcribe_media; reports files lacking speech.",
    )
