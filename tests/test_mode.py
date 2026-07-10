"""Tier detection and capability reporting."""

from __future__ import annotations

from davinci_mcp import mode, resolve_api


def _status(state: str, **kw) -> resolve_api.ResolveStatus:
    return resolve_api.ResolveStatus(state, "msg", **kw)


def test_detect_live_when_reachable(monkeypatch):
    status = _status(
        resolve_api.ResolveStatus.REACHABLE,
        product="DaVinci Resolve Studio",
        version="20.0.0",
    )
    monkeypatch.setattr(resolve_api, "connect", lambda: status)
    detected = mode.detect()
    assert detected["tier"] == mode.LIVE
    assert "20.0.0" in detected["why"]
    assert detected["resolve_version"] == "20.0.0"


def test_detect_interchange_when_not_reachable(monkeypatch):
    status = _status(resolve_api.ResolveStatus.FREE_EDITION)
    monkeypatch.setattr(resolve_api, "connect", lambda: status)
    detected = mode.detect()
    assert detected["tier"] == mode.INTERCHANGE
    assert detected["resolve_state"] == resolve_api.ResolveStatus.FREE_EDITION


def test_capabilities_shape(monkeypatch):
    status = _status(resolve_api.ResolveStatus.APP_NOT_RUNNING)
    monkeypatch.setattr(resolve_api, "connect", lambda: status)
    caps = mode.capabilities()
    assert caps["ok"] is True
    assert caps["tier"] == mode.INTERCHANGE
    assert set(caps["optional_deps"]) == {"numpy", "librosa", "faster_whisper"}
    assert "live" in caps["tiers"] and "interchange" in caps["tiers"]


def test_ffmpeg_detection_returns_path_or_none():
    # ffmpeg is expected on this machine, but the API must never raise.
    path = mode.ffmpeg_path()
    assert path is None or path.endswith("ffmpeg")


def test_optional_deps_reports_numpy():
    deps = mode.optional_deps()
    assert deps["numpy"] is True  # declared as a hard dependency


def test_unwrap_script_module_handles_loader_shim():
    """Blackmagic's DaVinciResolveScript.py is a shim that swaps the real
    fusionscript module into sys.modules during import and keeps it in its
    script_module attribute; unwrap must find scriptapp either way."""
    import types

    from davinci_mcp.resolve_api import _unwrap_script_module

    real = types.ModuleType("fusionscript")
    real.scriptapp = lambda name: object()

    shim = types.ModuleType("DaVinciResolveScript")
    shim.script_module = real
    shim.load_dynamic = lambda *a: None

    assert _unwrap_script_module(shim) is real
    assert _unwrap_script_module(real) is real
