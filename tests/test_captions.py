"""Tests for captions + YouTube chapters (engines/captions.py).

Everything runs on a hand-built word-level transcript (no model needed): the
caption constraints are asserted against a parsed SRT, and chapter derivation is
checked for the mandatory 00:00 start.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines import captions  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures + a tiny SRT parser
# --------------------------------------------------------------------------- #

def _transcript() -> dict:
    """Three sentences with 1.5s pauses between them (chapter/caption breaks)."""
    sentences = [
        ["This", "is", "the", "first", "topic", "about", "cameras."],
        ["Now", "we", "discuss", "lenses", "and", "focus", "carefully."],
        ["Finally", "we", "cover", "editing", "inside", "resolve."],
    ]
    words = []
    t = 0.0
    for si, sent in enumerate(sentences):
        if si > 0:
            t += 1.5  # inter-sentence pause
        for tok in sent:
            words.append({"start": round(t, 3), "end": round(t + 0.3, 3), "word": tok})
            t += 0.4
    return {"ok": True, "words": words, "segments": [], "duration": round(t, 3)}


_SRT_TS = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d)")


def _ts_to_s(ts: str) -> float:
    h, m, s, ms = _SRT_TS.match(ts).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_srt(text: str) -> list[dict]:
    blocks = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        assert lines[0].strip().isdigit(), f"bad index line: {lines[0]!r}"
        start_ts, end_ts = [p.strip() for p in lines[1].split("-->")]
        blocks.append({
            "index": int(lines[0]),
            "start": _ts_to_s(start_ts),
            "end": _ts_to_s(end_ts),
            "lines": lines[2:],
        })
    return blocks


# --------------------------------------------------------------------------- #
# generate_captions
# --------------------------------------------------------------------------- #

def test_srt_parses_and_respects_constraints(tmp_path):
    media = str(tmp_path / "clip.mp4")
    out = str(tmp_path / "clip.srt")
    r = captions.generate_captions(
        media, transcript_json=_transcript(), format="srt",
        max_line_chars=42, max_lines=2, max_duration=6.0,
        output_path=out, dry_run=False, confirm=True,
    )
    assert r["ok"], r
    assert os.path.exists(out)
    text = open(out, encoding="utf-8").read()
    blocks = _parse_srt(text)
    assert len(blocks) == r["block_count"] >= 2

    prev_end = -1.0
    for b in blocks:
        # Line-length + max-lines constraints.
        assert len(b["lines"]) <= 2, b
        for ln in b["lines"]:
            assert len(ln) <= 42, (len(ln), ln)
        # Duration constraint + monotonic, non-overlapping, snapped timing.
        assert 0.0 < b["end"] - b["start"] <= 6.0 + 1e-6, b
        assert b["start"] >= prev_end - 1e-6, (b, prev_end)
        prev_end = b["end"]
        # No single-word orphan line when the block has room.
        if len(b["lines"]) == 2:
            assert len(b["lines"][1].split()) >= 1
    # Index numbering is 1..N.
    assert [b["index"] for b in blocks] == list(range(1, len(blocks) + 1))


def test_min_gap_between_blocks(tmp_path):
    media = str(tmp_path / "clip2.mp4")
    r = captions.generate_captions(
        media, transcript_json=_transcript(), format="srt",
        min_gap=0.09, dry_run=True,
    )
    assert r["ok"], r
    blocks = r["blocks"]
    for a, b in zip(blocks, blocks[1:]):
        assert b["start"] - a["end"] >= 0.09 - 1e-6, (a, b)


def test_vtt_format_and_header(tmp_path):
    media = str(tmp_path / "clip3.mp4")
    out = str(tmp_path / "clip3.vtt")
    r = captions.generate_captions(
        media, transcript_json=_transcript(), format="vtt",
        output_path=out, dry_run=False, confirm=True,
    )
    assert r["ok"], r
    text = open(out, encoding="utf-8").read()
    assert text.startswith("WEBVTT")
    assert "-->" in text and "." in text.split("-->")[1][:12]  # vtt uses '.' in ts


def test_karaoke_requires_vtt(tmp_path):
    media = str(tmp_path / "clip4.mp4")
    r = captions.generate_captions(
        media, transcript_json=_transcript(), format="srt", karaoke=True,
    )
    assert r["ok"] is False and "karaoke" in r["error"]


def test_karaoke_vtt_has_word_timestamps(tmp_path):
    media = str(tmp_path / "clip5.mp4")
    r = captions.generate_captions(
        media, transcript_json=_transcript(), format="vtt", karaoke=True,
        dry_run=True,
    )
    assert r["ok"], r
    assert "<00:00:" in r["preview"]  # inline per-word timestamps


def test_bad_format(tmp_path):
    media = str(tmp_path / "clip6.mp4")
    r = captions.generate_captions(media, transcript_json=_transcript(), format="xml")
    assert r["ok"] is False


# --------------------------------------------------------------------------- #
# youtube_chapters
# --------------------------------------------------------------------------- #

def test_chapters_from_transcript_start_at_zero(tmp_path):
    media = str(tmp_path / "clip.mp4")
    r = captions.youtube_chapters(
        media, transcript_json=_transcript(), min_chapter_s=1.0,
    )
    assert r["ok"], r
    assert r["chapters"][0]["time"] == 0.0
    assert r["description"].splitlines()[0].startswith("00:00")
    assert r["chapter_count"] >= 2  # multiple topics separated by pauses
    assert "dialogue-editing" in r["note"]


def test_chapters_from_markers(tmp_path):
    media = str(tmp_path / "clip.mp4")
    markers = [{"time": 0.0, "name": "Intro"}, {"time": 40.0, "name": "Demo"},
               {"time": 42.0, "name": "TooClose"}, {"time": 90.0, "name": "Outro"}]
    r = captions.youtube_chapters(media, timeline_markers=markers, min_chapter_s=20.0)
    assert r["ok"], r
    # The 42s marker is within 20s of 40s -> merged out.
    titles = [c["title"] for c in r["chapters"]]
    assert titles == ["Intro", "Demo", "Outro"], r["chapters"]
    assert r["description"].splitlines()[0] == "00:00 Intro"
