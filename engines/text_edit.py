"""Transcript-driven (word-level) editing.

`cut_by_transcript` turns a word-level transcript into a cut plan: it drops
filler words, collapses over-long pauses at word granularity, and removes
immediate false-start repetitions — every cut carrying a human-readable REASON.
The output is a keep-range list plus an ``assemble_edit``-compatible plan (both
tiers) and, on confirm, a rendered preview WAV/MP4.

Where silence-only tightening (``dead_air.tighten_dialogue``) can only cut the
quiet, this operates on the words themselves, so it removes "um" that lands in
the middle of a phrase, a stammered restart, or a trailing "you know" that a
silence detector would keep. The two are complementary; the pause-collapse here
follows the same handle/head-tail logic as ``dead_air`` but at word boundaries.

Heuristics (documented, deterministic — see the module constants):

- Filler words. Default single-word fillers ("um", "uh", "erm", …) are removed.
  Guarded cases:
    * "like"  -> removed ONLY when it is isolated (a real pause on both sides)
      or immediately repeated ("like like"); never inside "I like this".
    * multi-word discourse markers ("you know", "sort of", "i mean") are matched
      and removed as a unit.
  A filler that STARTS a sentence/segment is kept (a leading "Um," is often a
  deliberate beat) unless it is immediately repeated — repetition reads as a
  stumble worth cutting.
- Pauses. A gap longer than ``max_pause`` between two words has its middle
  removed, leaving ``handle`` seconds of room tone against each neighbour and
  only when the removed span is at least ``min_cut`` long.
- False starts. An immediate repetition of a 2+ word sequence
  ("what I— what I mean is") drops the first occurrence.

Safety: if the plan would remove more than ``1 - keep_ratio_floor`` of the clip,
it is returned FLAGGED and NOT executed (no preview render), so an over-eager
pass can't silently gut a take.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from . import dead_air, media, transcribe

# Single-word fillers removed by default (normalised, lower-case, no punctuation).
_DEFAULT_FILLERS: frozenset[str] = frozenset({
    "um", "uh", "erm", "uhm", "umm", "uhh", "er", "eh", "ah", "hmm", "mhm",
})
# Discourse-marker phrases removed as a unit.
_DEFAULT_PHRASE_FILLERS: tuple[tuple[str, ...], ...] = (
    ("you", "know"),
    ("sort", "of"),
    ("kind", "of"),
    ("i", "mean"),
)
# Guarded: only cut when isolated or repeated, never mid-phrase.
_GUARDED_FILLERS: frozenset[str] = frozenset({"like"})
# Gap (seconds) on either side of "like" that makes it read as isolated filler.
_ISOLATION_GAP = 0.2

_SENTENCE_END_RE = re.compile(r"[.!?]\s*$")
_PUNCT_STRIP_RE = re.compile(r"^[^\w']+|[^\w']+$")


def _norm(word: str) -> str:
    """Lower-case a token and strip surrounding punctuation/whitespace."""
    return _PUNCT_STRIP_RE.sub("", (word or "").strip().lower())


def _load_words(
    media_path: str,
    transcript_json: Any,
    *,
    model: str,
    language: Optional[str],
) -> dict[str, Any]:
    """Return {ok, words, duration, segments, source} from a transcript dict, a
    JSON path, or by transcribing the media (``auto``)."""
    data: Optional[dict[str, Any]] = None
    source = "provided"
    if isinstance(transcript_json, dict):
        data = transcript_json
    elif isinstance(transcript_json, str) and transcript_json not in ("", "auto"):
        tp = Path(transcript_json).expanduser()
        if not tp.exists():
            return {"ok": False, "error": f"Transcript JSON not found: {tp}"}
        try:
            data = json.loads(tp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"Invalid transcript JSON: {exc}"}
    else:
        source = "auto"
        data = transcribe.transcribe_media(media_path, language=language, model=model)
        if not data.get("ok"):
            return data

    words = data.get("words") or []
    if not words:
        return {"ok": False, "error": "Transcript has no word-level timings.", "source": source}
    return {
        "ok": True,
        "source": source,
        "words": words,
        "segments": data.get("segments") or [],
        "duration": data.get("duration"),
    }


def _sentence_start_flags(words: list[dict[str, Any]]) -> list[bool]:
    """Mark words that begin a sentence: index 0, or a word whose predecessor
    ended with sentence punctuation."""
    flags = [False] * len(words)
    if words:
        flags[0] = True
    for i in range(1, len(words)):
        if _SENTENCE_END_RE.search(words[i - 1].get("word", "")):
            flags[i] = True
    return flags


def _detect_filler_cuts(
    words: list[dict[str, Any]],
    fillers: frozenset[str],
    phrase_fillers: tuple[tuple[str, ...], ...],
) -> list[dict[str, Any]]:
    """Return raw filler cut spans with reasons (before handles/merge)."""
    norms = [_norm(w.get("word", "")) for w in words]
    starts = _sentence_start_flags(words)
    cuts: list[dict[str, Any]] = []
    n = len(words)
    consumed = [False] * n

    # Multi-word discourse markers first (longest wins via ordering).
    for phrase in sorted(phrase_fillers, key=len, reverse=True):
        k = len(phrase)
        for i in range(0, n - k + 1):
            if any(consumed[i + j] for j in range(k)):
                continue
            if tuple(norms[i:i + k]) == phrase:
                # Sentence-start guard (unless repeated right after).
                repeated = i + 2 * k <= n and tuple(norms[i + k:i + 2 * k]) == phrase
                if starts[i] and not repeated:
                    continue
                for j in range(k):
                    consumed[i + j] = True
                cuts.append({
                    "type": "filler",
                    "text": " ".join(norms[i:i + k]),
                    "start": float(words[i]["start"]),
                    "end": float(words[i + k - 1]["end"]),
                })

    # Single-word fillers (plain + guarded).
    for i, nw in enumerate(norms):
        if consumed[i] or not nw:
            continue
        is_plain = nw in fillers
        is_guarded = nw in _GUARDED_FILLERS
        if not (is_plain or is_guarded):
            continue

        repeated = (i + 1 < n and norms[i + 1] == nw) or (i > 0 and norms[i - 1] == nw)

        if is_guarded and not repeated:
            # "like" only when isolated by real pauses on both sides.
            prev_gap = float(words[i]["start"]) - float(words[i - 1]["end"]) if i > 0 else _ISOLATION_GAP
            next_gap = float(words[i + 1]["start"]) - float(words[i]["end"]) if i + 1 < n else _ISOLATION_GAP
            if prev_gap < _ISOLATION_GAP or next_gap < _ISOLATION_GAP:
                continue

        if is_plain and starts[i] and not repeated:
            # Keep a deliberate leading "Um," that opens a sentence.
            continue

        consumed[i] = True
        cuts.append({
            "type": "filler",
            "text": nw,
            "start": float(words[i]["start"]),
            "end": float(words[i]["end"]),
        })
    return cuts


def _detect_restart_cuts(
    words: list[dict[str, Any]], *, min_len: int = 2, max_len: int = 6
) -> list[dict[str, Any]]:
    """Detect immediate repetition of a 2+ word sequence; cut the first copy."""
    norms = [_norm(w.get("word", "")) for w in words]
    n = len(words)
    cuts: list[dict[str, Any]] = []
    i = 0
    while i < n:
        matched = False
        upper = min(max_len, (n - i) // 2)
        for k in range(upper, min_len - 1, -1):
            first = norms[i:i + k]
            second = norms[i + k:i + 2 * k]
            if first == second and all(first):  # non-empty tokens only
                cuts.append({
                    "type": "restart",
                    "text": " ".join(first),
                    "start": float(words[i]["start"]),
                    "end": float(words[i + k]["start"]),  # up to the clean restart
                })
                i += k
                matched = True
                break
        if not matched:
            i += 1
    return cuts


def _detect_pause_cuts(
    words: list[dict[str, Any]],
    *,
    max_pause: float,
    handle: float,
    min_cut: float,
    duration: Optional[float],
) -> list[dict[str, Any]]:
    """Collapse gaps longer than ``max_pause`` between consecutive words, and any
    long lead-in/tail silence, leaving ``handle`` seconds against speech."""
    cuts: list[dict[str, Any]] = []
    n = len(words)
    if n == 0:
        return cuts

    def _add(gap_start: float, gap_end: float) -> None:
        if gap_end - gap_start <= max_pause:
            return
        rm_start = gap_start + handle
        rm_end = gap_end - handle
        if rm_end - rm_start >= min_cut:
            cuts.append({
                "type": "pause",
                "text": f"{round(gap_end - gap_start, 2)}s pause",
                "start": rm_start,
                "end": rm_end,
            })

    # Lead-in silence.
    _add(0.0, float(words[0]["start"]))
    # Inter-word gaps.
    for i in range(1, n):
        _add(float(words[i - 1]["end"]), float(words[i]["start"]))
    # Trailing silence.
    if duration:
        _add(float(words[-1]["end"]), float(duration))
    return cuts


def _apply_handles_to_word_cuts(
    cuts: list[dict[str, Any]],
    words: list[dict[str, Any]],
    *,
    handle: float,
) -> None:
    """For filler/restart word cuts, absorb adjacent silence but leave ``handle``
    seconds of room tone next to the neighbouring words. Mutates ``cuts``."""
    ends = [float(w["end"]) for w in words]
    starts = [float(w["start"]) for w in words]
    for c in cuts:
        if c["type"] == "pause":
            continue
        # Nearest word boundaries outside this cut.
        prev_end = max((e for e in ends if e <= c["start"] + 1e-6), default=0.0)
        next_start = min((s for s in starts if s >= c["end"] - 1e-6), default=c["end"])
        c["start"] = min(c["start"], prev_end + handle) if prev_end + handle <= c["start"] else c["start"]
        if next_start - handle >= c["end"]:
            c["end"] = next_start - handle


def _merge_cuts(cuts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort and merge overlapping/adjacent cuts, combining their reasons."""
    if not cuts:
        return []
    ordered = sorted(cuts, key=lambda c: (c["start"], c["end"]))
    merged: list[dict[str, Any]] = [dict(ordered[0], reasons=[_reason(ordered[0])])]
    for c in ordered[1:]:
        last = merged[-1]
        if c["start"] <= last["end"] + 1e-6:
            last["end"] = max(last["end"], c["end"])
            last["reasons"].append(_reason(c))
        else:
            merged.append(dict(c, reasons=[_reason(c)]))
    for m in merged:
        m.pop("type", None)
        m.pop("text", None)
        m["start"] = round(float(m["start"]), 3)
        m["end"] = round(float(m["end"]), 3)
    return merged


def _reason(c: dict[str, Any]) -> dict[str, Any]:
    return {"type": c["type"], "text": c["text"]}


def _keep_ranges(cuts: list[dict[str, Any]], duration: float) -> list[dict[str, float]]:
    keep: list[dict[str, float]] = []
    cursor = 0.0
    for c in cuts:
        if c["start"] > cursor + 1e-6:
            keep.append({"start": round(cursor, 3), "end": round(c["start"], 3)})
        cursor = max(cursor, c["end"])
    if cursor < duration - 1e-6:
        keep.append({"start": round(cursor, 3), "end": round(duration, 3)})
    if not keep:
        keep = [{"start": 0.0, "end": round(duration, 3)}]
    return keep


def _assemble_plan(
    media_path: str, kind: str, keep: list[dict[str, float]], probe: dict[str, Any], name: str
) -> dict[str, Any]:
    """Build an ``assemble_edit``-compatible plan from the keep ranges."""
    fps = probe.get("fps") or 24.0
    width = probe.get("width")
    height = probe.get("height")
    resolution = f"{width}x{height}" if width and height else "1920x1080"
    audio = [{"path": media_path, "in": k["start"], "out": k["end"]} for k in keep]
    plan: dict[str, Any] = {
        "timeline": {"name": name, "fps": float(fps), "resolution": resolution},
        "audio": audio,
    }
    if kind == "video":
        plan["video"] = [{"path": media_path, "in": k["start"], "out": k["end"]} for k in keep]
    return plan


def cut_by_transcript(
    media: str,
    transcript_json: Any = None,
    *,
    remove_fillers: bool = True,
    custom_fillers: Optional[list[str]] = None,
    max_pause: float = 0.6,
    tighten_only: bool = False,
    keep_ratio_floor: float = 0.5,
    remove_restarts: bool = True,
    handle: float = 0.08,
    min_cut: float = 0.2,
    model: str = "base",
    language: Optional[str] = None,
    timeline_name: Optional[str] = None,
    output_path: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Build a word-level cut plan from a transcript. See the module docstring
    for the heuristics. ``transcript_json`` may be a dict, a path, or None/``auto``
    to transcribe ``media`` first."""
    media_path = str(Path(media).expanduser())

    loaded = _load_words(media_path, transcript_json, model=model, language=language)
    if not loaded.get("ok"):
        return loaded
    words = loaded["words"]

    # Probe is best-effort so hand-built transcripts work without a real file.
    probe = _probe_best_effort(media_path)
    kind = probe.get("kind", "audio")
    duration = loaded.get("duration") or probe.get("duration_seconds")
    if not duration:
        # Fall back to the last word's end time.
        duration = float(words[-1]["end"]) if words else 0.0
    duration = float(duration)

    # --- Build raw cuts -----------------------------------------------------
    raw: list[dict[str, Any]] = []
    fillers = set(_DEFAULT_FILLERS)
    if custom_fillers:
        fillers |= {_norm(f) for f in custom_fillers if _norm(f)}
    fillers = frozenset(fillers)

    if not tighten_only and remove_fillers:
        raw += _detect_filler_cuts(words, fillers, _DEFAULT_PHRASE_FILLERS)
    if not tighten_only and remove_restarts:
        raw += _detect_restart_cuts(words)

    _apply_handles_to_word_cuts(raw, words, handle=handle)

    raw += _detect_pause_cuts(
        words, max_pause=max_pause, handle=handle, min_cut=min_cut, duration=duration
    )

    merged = _merge_cuts(raw)
    keep = _keep_ranges(merged, duration)

    total_removed = round(sum(c["end"] - c["start"] for c in merged), 3)
    new_duration = round(sum(k["end"] - k["start"] for k in keep), 3)
    keep_ratio = round(new_duration / duration, 4) if duration else 1.0

    name = timeline_name or f"{Path(media_path).stem} (transcript cut)"
    plan: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run and not confirm,
        "media": media_path,
        "kind": kind,
        "transcript_source": loaded["source"],
        "source_duration_seconds": round(duration, 3),
        "parameters": {
            "remove_fillers": remove_fillers, "remove_restarts": remove_restarts,
            "tighten_only": tighten_only, "max_pause": max_pause,
            "handle": handle, "min_cut": min_cut, "keep_ratio_floor": keep_ratio_floor,
            "custom_fillers": sorted(fillers - _DEFAULT_FILLERS),
        },
        "cuts": merged,
        "cut_count": len(merged),
        "keep_ranges": keep,
        "removed_seconds": total_removed,
        "cut_duration_seconds": new_duration,
        "keep_ratio": keep_ratio,
        "assemble_plan": _assemble_plan(media_path, kind, keep, probe, name),
    }

    # --- Safety floor -------------------------------------------------------
    if duration and keep_ratio < keep_ratio_floor:
        plan["executed"] = False
        plan["warning"] = (
            f"Plan keeps only {keep_ratio:.0%} of the clip (floor is "
            f"{keep_ratio_floor:.0%}); returned for review and NOT rendered. "
            "Raise max_pause / lower removal aggressiveness, or lower keep_ratio_floor "
            "to override."
        )
        return plan

    if dry_run and not confirm:
        plan["note"] = "Set dry_run=false and confirm=true to render a cut preview."
        return plan
    if not confirm:
        return {"ok": False, "error": "confirm must be true when dry_run is false."}

    # --- Render preview (reuse dead_air's concat renderer) ------------------
    if len(keep) == 1 and keep[0]["start"] == 0.0 and abs(keep[0]["end"] - duration) < 1e-3:
        plan["preview"] = {"path": None, "note": "No cuts to apply; preview skipped."}
        return plan
    if not Path(media_path).exists():
        plan["preview"] = {"path": None, "note": "Source media not available to render a preview."}
        return plan
    try:
        out = dead_air._render_preview(media_path, kind, keep, output_path)
        plan["preview"] = {"path": out, "kind": kind}
    except Exception as exc:
        plan["ok"] = False
        plan["error"] = f"Preview render failed: {type(exc).__name__}: {exc}"
    return plan


def _probe_best_effort(media_path: str) -> dict[str, Any]:
    try:
        if Path(media_path).exists():
            p = media.probe_one(media_path)
            if p.get("ok"):
                return p
    except Exception:
        pass
    return {"kind": "audio"}


def register(add_tool) -> None:
    add_tool(
        "cut_by_transcript",
        {
            "type": "object",
            "properties": {
                "media": {"type": "string", "description": "Audio or video file to cut."},
                "transcript_json": {
                    "oneOf": [{"type": "object"}, {"type": "string"}],
                    "description": "Transcript dict or JSON path. Omit or 'auto' to transcribe the media first.",
                },
                "remove_fillers": {"type": "boolean", "default": True},
                "custom_fillers": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Extra filler words/phrases to remove.",
                },
                "max_pause": {"type": "number", "default": 0.6, "description": "Collapse gaps longer than this (seconds)."},
                "tighten_only": {"type": "boolean", "default": False, "description": "Only collapse pauses; keep all words."},
                "keep_ratio_floor": {"type": "number", "default": 0.5, "description": "Refuse to render if the plan keeps less than this fraction."},
                "remove_restarts": {"type": "boolean", "default": True},
                "handle": {"type": "number", "default": 0.08, "description": "Room-tone handle left against speech (seconds)."},
                "min_cut": {"type": "number", "default": 0.2, "description": "Minimum pause-cut length (seconds)."},
                "model": {"type": "string", "default": "base", "description": "faster-whisper model when transcribing automatically."},
                "language": {"type": "string", "description": "Force a language for auto transcription."},
                "timeline_name": {"type": "string"},
                "output_path": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["media"],
            "additionalProperties": False,
        },
        lambda params: cut_by_transcript(
            params["media"],
            params.get("transcript_json"),
            remove_fillers=params.get("remove_fillers", True),
            custom_fillers=params.get("custom_fillers"),
            max_pause=params.get("max_pause", 0.6),
            tighten_only=params.get("tighten_only", False),
            keep_ratio_floor=params.get("keep_ratio_floor", 0.5),
            remove_restarts=params.get("remove_restarts", True),
            handle=params.get("handle", 0.08),
            min_cut=params.get("min_cut", 0.2),
            model=params.get("model", "base"),
            language=params.get("language"),
            timeline_name=params.get("timeline_name"),
            output_path=params.get("output_path"),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Word-level transcript edit: drop filler words, collapse long pauses, and "
        "remove false-start repetitions, each cut carrying a reason. Returns a "
        "keep-range plan + an assemble_edit-compatible plan, and on confirm a "
        "rendered preview. Refuses to gut a take past keep_ratio_floor.",
    )
