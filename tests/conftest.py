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

    def factory(name: str = "clip", kind: str = "video", seconds: float = 2.0) -> str:
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
    def __init__(self, name: str, path: str | None = None) -> None:
        self._name = name
        self._path = path

    def GetName(self) -> str:
        return self._name

    def GetClipProperty(self, key: str):
        if key == "File Path":
            return self._path or ""
        return ""


class FakeTimelineItem:
    def __init__(self, name: str, rec: _Recorder) -> None:
        self._name = name
        self._rec = rec

    def GetName(self) -> str:
        return self._name

    def SetLUT(self, node_index: int, lut_path: str) -> bool:
        self._rec.record("SetLUT", node_index, lut_path, clip=self._name)
        return True

    def SetCDL(self, cdl: dict) -> bool:
        self._rec.record("SetCDL", cdl, clip=self._name)
        return True


class FakeTimeline:
    def __init__(self, name: str, rec: _Recorder) -> None:
        self._name = name
        self._rec = rec
        self.items = [FakeTimelineItem("shotA", rec), FakeTimelineItem("shotB", rec)]

    def GetName(self) -> str:
        return self._name

    def AddMarker(self, frame, color, name, note, duration, custom) -> bool:
        self._rec.record("AddMarker", frame, color, name, note, duration, custom)
        return True

    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        self._rec.record("GetItemListInTrack", track_type, index)
        return self.items

    def GetMarkers(self) -> dict:
        return {}

    def GetStartFrame(self) -> int:
        return 0

    def GetEndFrame(self) -> int:
        return 240

    def GetSetting(self, key: str) -> str:
        return "24" if key == "timelineFrameRate" else ""


class FakeFolder:
    """Pool folder faithful to the real API surface the code walks."""

    def __init__(self) -> None:
        self.clips: list[FakeMediaPoolItem] = []
        self.subfolders: list["FakeFolder"] = []

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
        folder = FakeFolder()
        self.root.subfolders.append(folder)
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
            item = FakeMediaPoolItem(Path(p).stem, real)
            self.root.clips.append(item)
            new_items.append(item)
            existing.add(real)
        return new_items

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


class FakeProject:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self.pool = FakeMediaPool(rec)
        # Start with an active timeline so read/grade/marker/render tools have
        # something to operate on; create_timeline replaces it.
        self.current_timeline: FakeTimeline | None = FakeTimeline("Timeline 1", rec)

    def GetName(self) -> str:
        return "FakeProject"

    def GetMediaPool(self) -> FakeMediaPool:
        return self.pool

    def GetCurrentTimeline(self) -> FakeTimeline | None:
        return self.current_timeline or self.pool.timeline

    def SetCurrentTimeline(self, timeline) -> bool:
        self.current_timeline = timeline
        return True

    def GetTimelineCount(self) -> int:
        return 1 if self.GetCurrentTimeline() else 0

    def GetTimelineByIndex(self, index) -> FakeTimeline | None:
        return self.GetCurrentTimeline()

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
    def __init__(self, project: FakeProject) -> None:
        self._project = project

    def GetCurrentProject(self) -> FakeProject:
        return self._project


class FakeResolve:
    def __init__(self, rec: _Recorder) -> None:
        self.project = FakeProject(rec)

    def GetProjectManager(self) -> FakeProjectManager:
        return FakeProjectManager(self.project)

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
