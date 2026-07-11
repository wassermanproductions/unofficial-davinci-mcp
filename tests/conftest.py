"""Shared, generic test fixtures.

Kept minimal on purpose:
- ``make_media``   - a factory that renders tiny real media with ffmpeg.
- ``mock_resolve`` - a fake Resolve scripting object graph that records calls,
                     patched in for :func:`davinci_mcp.resolve_api.connect`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest


_FFMPEG_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]


def _find(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for directory in _FFMPEG_DIRS:
        candidate = Path(directory) / binary
        if candidate.is_file():
            return str(candidate)
    return None


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    binary = _find("ffmpeg")
    if not binary:
        pytest.skip("ffmpeg not available")
    return binary


@pytest.fixture
def make_media(ffmpeg_bin: str, tmp_path: Path) -> Callable[..., str]:
    """Return a factory that writes a short video or audio file and returns its path."""

    def factory(
        name: str = "clip",
        kind: str = "video",
        seconds: float = 2.0,
        desaturate: bool = False,
    ) -> str:
        if kind == "image":
            path = tmp_path / f"{name}.png"
            vf = "hue=s=0.12,eq=contrast=0.8" if desaturate else "null"
            cmd = [
                ffmpeg_bin, "-y", "-f", "lavfi",
                "-i", "testsrc=duration=1:size=320x240:rate=1",
                "-vf", vf, "-frames:v", "1", str(path),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            return str(path)
        if kind == "audio":
            path = tmp_path / f"{name}.wav"
            cmd = [
                ffmpeg_bin, "-y", "-f", "lavfi",
                "-i", f"sine=frequency=440:duration={seconds}",
                str(path),
            ]
        else:
            path = tmp_path / f"{name}.mp4"
            cmd = [
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=24",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-shortest", "-pix_fmt", "yuv420p", str(path),
            ]
        subprocess.run(cmd, check=True, capture_output=True)
        return str(path)

    return factory


# --- Fake Resolve scripting object graph -----------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeMediaPoolItem:
    def __init__(self, name: str, path: str | None = None, rec: _Recorder | None = None) -> None:
        self._name = name
        self._path = path
        self._rec = rec
        self._metadata: dict[str, str] = {}

    def GetName(self) -> str:
        return self._name

    def GetClipProperty(self, key: str):
        if key == "File Path":
            return self._path or ""
        if key == "Type":
            return "Video"
        return ""

    def GetMetadata(self, key: str | None = None):
        if key is None:
            return dict(self._metadata)
        return self._metadata.get(key, "")

    def SetMetadata(self, key: str, value: str) -> bool:
        if self._rec:
            self._rec.record("SetMetadata", key, value, clip=self._name)
        self._metadata[key] = value
        return True


class FakeGraph:
    """Node graph faithful to the Graph API surface the color tool touches."""

    def __init__(self, rec: _Recorder, clip: str) -> None:
        self._rec = rec
        self._clip = clip

    def GetNumNodes(self) -> int:
        return 1

    def ApplyGradeFromDRX(self, path: str, grade_mode: int) -> bool:
        self._rec.record("ApplyGradeFromDRX", path, grade_mode, clip=self._clip)
        return True


class FakeTimelineItem:
    def __init__(self, name: str, rec: _Recorder) -> None:
        self._name = name
        self._rec = rec
        self._media = FakeMediaPoolItem(name, f"/media/{name}.mov", rec)

    def GetName(self) -> str:
        return self._name

    def SetLUT(self, node_index: int, lut_path: str) -> bool:
        self._rec.record("SetLUT", node_index, lut_path, clip=self._name)
        return True

    def SetCDL(self, cdl: dict) -> bool:
        self._rec.record("SetCDL", cdl, clip=self._name)
        return True

    # --- edit support (delete + re-append workarounds) ---
    def GetMediaPoolItem(self) -> FakeMediaPoolItem:
        return self._media

    def GetSourceStartFrame(self) -> int:
        return 0

    def GetSourceEndFrame(self) -> int:
        return 100

    def GetStart(self, subframe_precision: bool = False) -> int:
        return 0

    # --- color versions + grades ---
    def CopyGrades(self, targets: list) -> bool:
        self._rec.record("CopyGrades", [t.GetName() for t in targets], clip=self._name)
        return True

    def GetVersionNameList(self, version_type: int) -> list[str]:
        self._rec.record("GetVersionNameList", version_type, clip=self._name)
        return ["Version 1"]

    def GetCurrentVersion(self) -> dict:
        return {"versionName": "Version 1", "versionType": 0}

    def AddVersion(self, name: str, version_type: int) -> bool:
        self._rec.record("AddVersion", name, version_type, clip=self._name)
        return True

    def LoadVersionByName(self, name: str, version_type: int) -> bool:
        self._rec.record("LoadVersionByName", name, version_type, clip=self._name)
        return True

    def GetNodeGraph(self, layer_index: int | None = None) -> FakeGraph:
        return FakeGraph(self._rec, self._name)


class FakeGalleryStill:
    """Opaque still handle; the real GalleryStill exposes no methods itself."""


class FakeTimeline:
    def __init__(self, name: str, rec: _Recorder) -> None:
        self._name = name
        self._rec = rec
        self.items = [FakeTimelineItem("shotA", rec), FakeTimelineItem("shotB", rec)]
        self._markers: dict[float, dict] = {}
        self._timecode = "01:00:00:00"

    def GetName(self) -> str:
        return self._name

    def AddMarker(self, frame, color, name, note, duration, custom) -> bool:
        self._rec.record("AddMarker", frame, color, name, note, duration, custom)
        self._markers[float(frame)] = {
            "color": color, "name": name, "note": note,
            "duration": duration, "customData": custom,
        }
        return True

    def DeleteMarkerAtFrame(self, frame) -> bool:
        self._rec.record("DeleteMarkerAtFrame", frame)
        return self._markers.pop(float(frame), None) is not None

    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        self._rec.record("GetItemListInTrack", track_type, index)
        return self.items

    def GetMarkers(self) -> dict:
        return dict(self._markers)

    def GetStartFrame(self) -> int:
        return 0

    def GetEndFrame(self) -> int:
        return 240

    def GetSetting(self, key: str) -> str:
        return "24" if key == "timelineFrameRate" else ""

    def GetTrackCount(self, track_type: str) -> int:
        return {"video": 1, "audio": 1, "subtitle": 0}.get(track_type, 0)

    def DeleteClips(self, items, ripple: bool = False) -> bool:
        self._rec.record("DeleteClips", [i.GetName() for i in items], ripple)
        for item in items:
            if item in self.items:
                self.items.remove(item)
        return True

    def DuplicateTimeline(self, new_name: str | None = None) -> "FakeTimeline":
        self._rec.record("DuplicateTimeline", new_name)
        return FakeTimeline(new_name or f"{self._name} copy", self._rec)

    def Export(self, file_name: str, export_type, export_subtype=None) -> bool:
        self._rec.record("Export", file_name, export_type, export_subtype)
        return True

    def InsertTitleIntoTimeline(self, title_name: str) -> FakeTimelineItem:
        self._rec.record("InsertTitleIntoTimeline", title_name)
        return FakeTimelineItem(title_name, self._rec)

    def SetCurrentTimecode(self, timecode: str) -> bool:
        self._rec.record("SetCurrentTimecode", timecode)
        self._timecode = timecode
        return True

    def GetCurrentTimecode(self) -> str:
        return self._timecode

    def GrabStill(self) -> FakeGalleryStill:
        self._rec.record("GrabStill")
        return FakeGalleryStill()


class FakeFolder:
    """Pool folder faithful to the real API surface the code walks."""

    def __init__(self, name: str = "Master") -> None:
        self._name = name
        self.clips: list[FakeMediaPoolItem] = []
        self.subfolders: list["FakeFolder"] = []

    def GetName(self) -> str:
        return self._name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subfolders)


class FakeMediaPool:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self.timeline: FakeTimeline | None = None
        self.root = FakeFolder()

    def GetRootFolder(self) -> FakeFolder:
        return self.root

    def AddSubFolder(self, root, name) -> FakeFolder:
        self._rec.record("AddSubFolder", root, name)
        folder = FakeFolder(name)
        root.subfolders.append(folder)
        return folder

    def SetCurrentFolder(self, folder) -> bool:
        self._rec.record("SetCurrentFolder", folder)
        return True

    def ImportMedia(self, paths) -> list[FakeMediaPoolItem]:
        """Model the real dedupe: items only for paths not already in the pool."""
        self._rec.record("ImportMedia", list(paths))
        import os as _os

        existing = {c.GetClipProperty("File Path") for c in self.root.clips}
        new_items = []
        for p in dict.fromkeys(paths):  # unique, order-preserving
            real = _os.path.realpath(p)
            if real in existing:
                continue
            item = FakeMediaPoolItem(Path(p).stem, real, self._rec)
            self.root.clips.append(item)
            new_items.append(item)
            existing.add(real)
        return new_items

    def DeleteTimelines(self, timelines) -> bool:
        self._rec.record("DeleteTimelines", [t.GetName() for t in timelines])
        return True

    def MoveClips(self, clips, target_folder) -> bool:
        self._rec.record("MoveClips", [c.GetName() for c in clips], target_folder.GetName())
        for clip in clips:
            if clip in self.root.clips:
                self.root.clips.remove(clip)
            target_folder.clips.append(clip)
        return True

    def RelinkClips(self, clips, folder_path) -> bool:
        self._rec.record("RelinkClips", [c.GetName() for c in clips], folder_path)
        return True

    def CreateEmptyTimeline(self, name) -> FakeTimeline:
        self._rec.record("CreateEmptyTimeline", name)
        self.timeline = FakeTimeline(name, self._rec)
        return self.timeline

    def CreateTimelineFromClips(self, name, items) -> FakeTimeline:
        self._rec.record("CreateTimelineFromClips", name, len(items))
        self.timeline = FakeTimeline(name, self._rec)
        return self.timeline

    def AppendToTimeline(self, payloads) -> list:
        self._rec.record("AppendToTimeline", payloads)
        return list(payloads)


class FakeGalleryStillAlbum:
    def __init__(self, rec: _Recorder, name: str = "Stills 1") -> None:
        self._rec = rec
        self._name = name
        self._stills: list = []

    def GetStills(self) -> list:
        return list(self._stills)

    def ExportStills(self, stills, folder_path, file_prefix, fmt) -> bool:
        self._rec.record("ExportStills", len(stills), folder_path, file_prefix, fmt)
        return True


class FakeGallery:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self._album = FakeGalleryStillAlbum(rec)

    def GetCurrentStillAlbum(self) -> FakeGalleryStillAlbum:
        return self._album

    def GetGalleryStillAlbums(self) -> list[FakeGalleryStillAlbum]:
        return [self._album]


class FakeProject:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self.pool = FakeMediaPool(rec)
        # Seed two timelines so list/switch/duplicate/delete have something to
        # address by name and index; create_timeline appends a third.
        self.timelines: list[FakeTimeline] = [
            FakeTimeline("Timeline 1", rec),
            FakeTimeline("Timeline 2", rec),
        ]
        self.current_timeline: FakeTimeline | None = self.timelines[0]
        self._gallery = FakeGallery(rec)
        self._settings: dict[str, str] = {
            "timelineFrameRate": "24",
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }

    def GetName(self) -> str:
        return "FakeProject"

    def GetMediaPool(self) -> FakeMediaPool:
        return self.pool

    def GetGallery(self) -> FakeGallery:
        return self._gallery

    def GetCurrentTimeline(self) -> FakeTimeline | None:
        return self.current_timeline or self.pool.timeline

    def SetCurrentTimeline(self, timeline) -> bool:
        self.current_timeline = timeline
        if timeline not in self.timelines:
            self.timelines.append(timeline)
        return True

    def GetTimelineCount(self) -> int:
        return len(self.timelines)

    def GetTimelineByIndex(self, index) -> FakeTimeline | None:
        if 1 <= index <= len(self.timelines):
            return self.timelines[index - 1]
        return None

    def GetSetting(self, key: str = ""):
        if not key:
            return dict(self._settings)
        return self._settings.get(key, "")

    def SetSetting(self, key: str, value: str) -> bool:
        self._rec.record("SetSetting", key, value)
        self._settings[key] = value
        return True

    def GetRenderJobStatus(self, job_id) -> dict:
        return {"JobStatus": "Complete", "CompletionPercentage": 100}

    def GetRenderJobList(self) -> list:
        return []

    def IsRenderingInProgress(self) -> bool:
        return False

    def LoadRenderPreset(self, name) -> bool:
        self._rec.record("LoadRenderPreset", name)
        return True

    def SetCurrentRenderFormatAndCodec(self, fmt, codec) -> bool:
        self._rec.record("SetCurrentRenderFormatAndCodec", fmt, codec)
        return True

    def SetRenderSettings(self, settings) -> bool:
        self._rec.record("SetRenderSettings", settings)
        return True

    def AddRenderJob(self) -> str:
        self._rec.record("AddRenderJob")
        return "job-1"

    def StartRendering(self, job_id) -> bool:
        self._rec.record("StartRendering", job_id)
        return True


class FakeProjectManager:
    def __init__(self, project: FakeProject, rec: _Recorder) -> None:
        self._project = project
        self._rec = rec

    def GetCurrentProject(self) -> FakeProject:
        return self._project

    def GetProjectListInCurrentFolder(self) -> list[str]:
        return ["FakeProject", "Archive 2024"]

    def LoadProject(self, name: str):
        self._rec.record("LoadProject", name)
        if name in ("FakeProject", "Archive 2024"):
            return self._project
        return None

    def CreateProject(self, name: str):
        self._rec.record("CreateProject", name)
        if name == "Duplicate Name":
            return None
        return self._project

    def SaveProject(self) -> bool:
        self._rec.record("SaveProject")
        return True


# Timeline export type/subtype constants, matching the documented resolve.EXPORT_*
# names the export action looks up via getattr on the resolve object.
_EXPORT_CONSTANTS = {
    "EXPORT_AAF": 0, "EXPORT_DRT": 1, "EXPORT_EDL": 2, "EXPORT_FCP_7_XML": 3,
    "EXPORT_FCPXML_1_8": 4, "EXPORT_FCPXML_1_9": 5, "EXPORT_FCPXML_1_10": 6,
    "EXPORT_TEXT_CSV": 7, "EXPORT_TEXT_TAB": 8, "EXPORT_OTIO": 9, "EXPORT_ALE": 10,
    "EXPORT_NONE": 100, "EXPORT_AAF_NEW": 101, "EXPORT_AAF_EXISTING": 102,
    "EXPORT_CDL": 103, "EXPORT_SDL": 104, "EXPORT_MISSING_CLIPS": 105,
}


class FakeResolve:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self.project = FakeProject(rec)
        self._page = "edit"
        for const_name, value in _EXPORT_CONSTANTS.items():
            setattr(self, const_name, value)

    def GetProjectManager(self) -> FakeProjectManager:
        return FakeProjectManager(self.project, self._rec)

    def OpenPage(self, page: str) -> bool:
        self._rec.record("OpenPage", page)
        self._page = page
        return True

    def GetCurrentPage(self) -> str:
        return self._page

    def GetProductName(self) -> str:
        return "DaVinci Resolve Studio"

    def GetVersionString(self) -> str:
        return "20.0.0"


@pytest.fixture
def mock_resolve(monkeypatch) -> _Recorder:
    """Patch resolve_api.connect to return a reachable fake and record API calls."""
    from davinci_mcp import resolve_api

    rec = _Recorder()
    fake = FakeResolve(rec)

    def fake_connect() -> resolve_api.ResolveStatus:
        return resolve_api.ResolveStatus(
            resolve_api.ResolveStatus.REACHABLE,
            "Connected (fake).",
            resolve=fake,
            product="DaVinci Resolve Studio",
            version="20.0.0",
        )

    monkeypatch.setattr(resolve_api, "connect", fake_connect)
    return rec
