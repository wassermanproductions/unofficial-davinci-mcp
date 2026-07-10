"""FCPXML / EDL / marker-CSV generators: golden-style structural checks."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from davinci_mcp import tools_interchange as ix


def _clips(make_media):
    return [
        {"path": make_media("a", "video"), "duration_seconds": 2.0},
        {"path": make_media("b", "video"), "in_seconds": 1.0, "out_seconds": 3.0},
        {"path": make_media("music", "audio"), "kind": "audio", "duration_seconds": 4.0},
    ]


def test_fcpxml_dry_run_returns_plan(make_media, tmp_path):
    out = tmp_path / "t.fcpxml"
    result = ix.generate_fcpxml("MyEdit", _clips(make_media), output_path=str(out), dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["plan"]["clip_count"] == 3
    assert not out.exists()


def test_fcpxml_writes_wellformed_file(make_media, tmp_path):
    out = tmp_path / "edit.fcpxml"
    markers = [{"frame": 5, "name": "beat", "note": "hit"}]
    result = ix.generate_fcpxml(
        "MyEdit", _clips(make_media), output_path=str(out),
        frame_rate=24, markers=markers, dry_run=False,
    )
    assert result["ok"] is True
    assert out.exists()

    text = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE fcpxml>" in text
    assert f'version="{ix.FCPXML_VERSION}"' in text

    tree = ET.parse(out)
    root = tree.getroot()
    assert root.tag == "fcpxml"
    # 3 unique media assets + 1 format resource.
    assert len(root.findall("./resources/asset")) == 3
    spine = root.find("./library/event/project/sequence/spine")
    assert spine is not None
    # Two video clips tile the spine; the audio clip attaches to the first.
    spine_clips = spine.findall("asset-clip")
    assert len(spine_clips) == 2
    audio_children = spine_clips[0].findall("asset-clip")
    assert len(audio_children) == 1
    assert audio_children[0].get("lane") == "-1"
    # The marker at frame 5 lands inside the first clip.
    assert spine_clips[0].find("marker") is not None


def test_fcpxml_is_deterministic(make_media, tmp_path):
    clips = _clips(make_media)
    out1 = tmp_path / "one.fcpxml"
    out2 = tmp_path / "two.fcpxml"
    ix.generate_fcpxml("Edit", clips, output_path=str(out1), dry_run=False)
    ix.generate_fcpxml("Edit", clips, output_path=str(out2), dry_run=False)
    assert out1.read_bytes() == out2.read_bytes()


def test_fcpxml_missing_media_errors(tmp_path):
    result = ix.generate_fcpxml(
        "Edit", [{"path": str(tmp_path / "nope.mov"), "duration_seconds": 2}],
        output_path=str(tmp_path / "x.fcpxml"), dry_run=False,
    )
    assert result["ok"] is False
    assert result["missing_paths"]


def test_edl_structure_and_counts(make_media, tmp_path):
    out = tmp_path / "cut.edl"
    result = ix.generate_edl("CutList", _clips(make_media), output_path=str(out), dry_run=False)
    assert result["ok"] is True
    assert result["clip_count"] == 2  # audio excluded from EDL
    text = out.read_text(encoding="utf-8")
    assert text.startswith("TITLE: CutList")
    assert "FCM: NON-DROP FRAME" in text
    assert "001  AX" in text and "002  AX" in text
    assert "* FROM CLIP NAME:" in text


def test_edl_timecode_uses_source_in(make_media, tmp_path):
    # Second clip has in_seconds=1.0 at 24fps -> source in 00:00:01:00.
    out = tmp_path / "cut.edl"
    ix.generate_edl("CutList", _clips(make_media), output_path=str(out), frame_rate=24, dry_run=False)
    text = out.read_text(encoding="utf-8")
    assert "00:00:01:00" in text


def test_marker_csv_sorted_and_deterministic(tmp_path):
    markers = [
        {"frame": 30, "name": "second"},
        {"frame": 0, "name": "first", "color": "Red"},
    ]
    out = tmp_path / "m.markers.csv"
    result = ix.generate_marker_csv(markers, output_path=str(out), frame_rate=24, dry_run=False)
    assert result["ok"] is True
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "frame,timecode,seconds,name,color,note,duration"
    # Sorted by frame: "first" (frame 0) precedes "second" (frame 30).
    assert lines[1].startswith("0,00:00:00:00,0.000,first,Red")
    assert lines[2].startswith("30,00:00:01:06,1.250,second")


def test_marker_csv_dry_run(tmp_path):
    out = tmp_path / "m.csv"
    result = ix.generate_marker_csv([{"frame": 1, "name": "x"}], output_path=str(out), dry_run=True)
    assert result["dry_run"] is True
    assert not out.exists()
