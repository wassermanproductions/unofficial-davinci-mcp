"""The free-edition live bridge: server protocol, whitelist, proxy, end-to-end.

These tests run the in-Resolve bridge server *standalone* - the same class that
runs inside Resolve, but with a fake ``resolve`` object injected since we are
not inside the app - and speak its real HTTP protocol on a random localhost
port. The proxy tests then drive :mod:`davinci_mcp.resolve_api`'s bridge client
against that server, ending with a full MCP-handler -> proxy -> bridge -> fake
round trip.
"""

from __future__ import annotations

import http.client
import json
import os
import stat

import pytest

from bridge import resolve_bridge as rb
from davinci_mcp import resolve_api


# --- Fakes standing in for the live ``resolve`` object ---------------------


class _FakeProject:
    def GetName(self):
        return "P1"


class _FakePM:
    def GetCurrentProject(self):
        return _FakeProject()


class _FakeResolve:
    """Minimal free-edition resolve object (no 'Studio' in the product name)."""

    def GetProductName(self):
        return "DaVinci Resolve"

    def GetVersionString(self):
        return "19.1.4"

    def GetProjectManager(self):
        return _FakePM()


class _Thing:
    def GetName(self):
        return "the-thing"


class _ArgFake:
    """Exercises passing a proxied object back as a call argument."""

    def GetProductName(self):
        return "DaVinci Resolve"

    def GetVersionString(self):
        return "19.0"

    def GetThing(self):
        return _Thing()

    def AddThing(self, thing):
        return thing.GetName()


# --- Test HTTP helper (does not raise on 4xx/5xx) --------------------------


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        return resp.status, data
    finally:
        conn.close()


TOKEN = "test-token-abc123"


@pytest.fixture
def start_bridge():
    """Start bridge servers on random ports; tear them down after the test."""
    servers = []

    def _start(resolve, token=TOKEN, write_file=False):
        server = rb.start_bridge(
            resolve, host="127.0.0.1", port=0, token=token, write_file=write_file
        )
        servers.append(server)
        return server

    yield _start

    for server in servers:
        try:
            server.request_stop()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        path = getattr(server, "discovery_file", None)
        if path:
            rb.remove_discovery(path)


# --- Protocol + whitelist --------------------------------------------------


def test_health_needs_no_auth(start_bridge):
    server = start_bridge(_FakeResolve())
    status, data = _request(server.port, "GET", "/health")
    assert status == 200
    assert data["ok"] is True
    assert data["app"] == "DaVinci Resolve"
    assert data["edition"] == "free"  # no 'Studio' in the product name
    assert data["version"] == "19.1.4"


def test_bad_token_is_rejected(start_bridge):
    server = start_bridge(_FakeResolve())
    status, data = _request(
        server.port, "POST", "/call",
        body={"token": "wrong", "object_path": "resolve", "method": "GetProductName"},
    )
    assert status == 401
    assert data["ok"] is False


def test_missing_token_is_rejected(start_bridge):
    server = start_bridge(_FakeResolve())
    status, _ = _request(
        server.port, "POST", "/call",
        body={"object_path": "resolve", "method": "GetProductName"},
    )
    assert status == 401


def test_whitelisted_call_succeeds(start_bridge):
    server = start_bridge(_FakeResolve())
    status, data = _request(
        server.port, "POST", "/call",
        body={"token": TOKEN, "object_path": "resolve",
              "method": "GetProductName", "args": []},
    )
    assert status == 200
    assert data["ok"] is True
    assert data["value"] == "DaVinci Resolve"


def test_bearer_header_is_accepted(start_bridge):
    server = start_bridge(_FakeResolve())
    status, data = _request(
        server.port, "POST", "/call",
        body={"object_path": "resolve", "method": "GetVersionString"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert data["value"] == "19.1.4"


@pytest.mark.parametrize("method", ["Nuke", "eval", "__class__", "_secret", "run"])
def test_non_whitelisted_method_is_rejected(start_bridge, method):
    server = start_bridge(_FakeResolve())
    status, data = _request(
        server.port, "POST", "/call",
        body={"token": TOKEN, "object_path": "resolve", "method": method},
    )
    assert status == 400
    assert data["ok"] is False
    assert "allow-list" in data["error"]


def test_unknown_root_object_is_rejected(start_bridge):
    server = start_bridge(_FakeResolve())
    status, data = _request(
        server.port, "POST", "/call",
        body={"token": TOKEN, "object_path": "os", "method": "GetProductName"},
    )
    assert status == 400
    assert data["ok"] is False


def test_object_handles_round_trip(start_bridge):
    """A returned live object becomes an opaque handle you can call again."""
    server = start_bridge(_FakeResolve())
    _, data = _request(
        server.port, "POST", "/call",
        body={"token": TOKEN, "object_path": "resolve", "method": "GetProjectManager"},
    )
    ref = data["value"]["__ref__"]
    assert ref.startswith("handle:")
    _, data2 = _request(
        server.port, "POST", "/call",
        body={"token": TOKEN, "object_path": ref, "method": "GetCurrentProject"},
    )
    ref2 = data2["value"]["__ref__"]
    _, data3 = _request(
        server.port, "POST", "/call",
        body={"token": TOKEN, "object_path": ref2, "method": "GetName"},
    )
    assert data3["value"] == "P1"


def test_unknown_handle_is_rejected(start_bridge):
    server = start_bridge(_FakeResolve())
    status, data = _request(
        server.port, "POST", "/call",
        body={"token": TOKEN, "object_path": "handle:999", "method": "GetName"},
    )
    assert status == 400
    assert data["ok"] is False


# --- Discovery file lifecycle ----------------------------------------------


def test_discovery_file_is_written_0600(monkeypatch, tmp_path, start_bridge):
    disco = tmp_path / "bridge.json"
    monkeypatch.setenv("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE", str(disco))
    server = start_bridge(_FakeResolve(), write_file=True)

    assert disco.exists()
    mode = stat.S_IMODE(os.stat(disco).st_mode)
    assert mode == 0o600, f"discovery file mode was {oct(mode)}, want 0o600"

    info = json.loads(disco.read_text())
    assert info["port"] == server.port
    assert info["token"] == TOKEN
    assert info["pid"] == os.getpid()
    assert info["edition"] == "free"
    assert info["version"] == "19.1.4"


def test_discovery_file_removed_on_shutdown(monkeypatch, tmp_path):
    disco = tmp_path / "bridge.json"
    monkeypatch.setenv("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE", str(disco))
    rb.write_discovery(str(disco), {"port": 1, "token": "x"})
    assert disco.exists()
    rb.remove_discovery(str(disco))
    assert not disco.exists()


# --- Proxy: resolve_api driving the bridge ---------------------------------


def test_proxy_navigates_the_object_model(start_bridge):
    server = start_bridge(_FakeResolve())
    client = resolve_api._BridgeClient("127.0.0.1", server.port, TOKEN)
    proxy = resolve_api._BridgeProxy(client, "resolve")

    assert proxy.GetProductName() == "DaVinci Resolve"
    manager = proxy.GetProjectManager()
    assert isinstance(manager, resolve_api._BridgeProxy)
    project = manager.GetCurrentProject()
    assert project.GetName() == "P1"


def test_proxy_passes_objects_back_as_arguments(start_bridge):
    """A proxy handed back as a call argument is rehydrated on the bridge side."""
    server = start_bridge(_ArgFake())
    client = resolve_api._BridgeClient("127.0.0.1", server.port, TOKEN)
    proxy = resolve_api._BridgeProxy(client, "resolve")

    thing = proxy.GetThing()
    assert isinstance(thing, resolve_api._BridgeProxy)
    assert proxy.AddThing(thing) == "the-thing"


def test_connect_discovers_and_returns_a_reachable_bridge(
    monkeypatch, tmp_path, start_bridge
):
    disco = tmp_path / "bridge.json"
    monkeypatch.setenv("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE", str(disco))
    start_bridge(_FakeResolve(), write_file=True)

    # Force the Studio scripting path to be unavailable, so the bridge is used.
    monkeypatch.setattr(
        resolve_api, "_connect_scripting",
        lambda: resolve_api.ResolveStatus(
            resolve_api.ResolveStatus.FREE_EDITION, "Studio scripting unavailable."
        ),
    )

    status = resolve_api.connect()
    assert status.reachable
    assert status.details["edition"] == "free-via-bridge"
    assert status.details["via_bridge"] is True
    assert isinstance(status.resolve, resolve_api._BridgeProxy)
    assert status.product == "DaVinci Resolve"


def test_detect_reports_bridge_tier(monkeypatch, tmp_path, start_bridge):
    from davinci_mcp import mode

    disco = tmp_path / "bridge.json"
    monkeypatch.setenv("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE", str(disco))
    start_bridge(_FakeResolve(), write_file=True)
    monkeypatch.setattr(
        resolve_api, "_connect_scripting",
        lambda: resolve_api.ResolveStatus(
            resolve_api.ResolveStatus.FREE_EDITION, "unavailable"
        ),
    )
    detected = mode.detect()
    assert detected["tier"] == mode.LIVE
    assert "in-app bridge" in detected["why"]


def test_connect_ignores_a_dead_bridge(monkeypatch, tmp_path):
    """A stale discovery file (no server behind it) must not claim reachable."""
    disco = tmp_path / "bridge.json"
    monkeypatch.setenv("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE", str(disco))
    # Point at a port nothing is listening on.
    rb.write_discovery(str(disco), {"port": 6, "token": "x", "host": "127.0.0.1"})
    original = resolve_api.ResolveStatus(
        resolve_api.ResolveStatus.FREE_EDITION, "Studio scripting unavailable."
    )
    monkeypatch.setattr(resolve_api, "_connect_scripting", lambda: original)

    status = resolve_api.connect()
    assert not status.reachable
    assert status is original  # fell back to the unchanged scripting status


# --- End to end: MCP handler -> proxy -> bridge -> fake resolve -------------


class _E2EProject:
    def GetName(self):
        return "FakeProject"

    def GetCurrentTimeline(self):
        return _E2ETimeline()

    def GetTimelineCount(self):
        return 1

    def GetTimelineByIndex(self, index):
        return _E2ETimeline()


class _E2ETimeline:
    def GetName(self):
        return "Timeline 1"

    def GetMarkers(self):
        return {}

    def GetStartFrame(self):
        return 0

    def GetEndFrame(self):
        return 240

    def GetSetting(self, key):
        return "24" if key == "timelineFrameRate" else ""


class _E2EPM:
    def GetCurrentProject(self):
        return _E2EProject()


class _E2EResolve:
    def GetProductName(self):
        return "DaVinci Resolve"

    def GetVersionString(self):
        return "19.1.4"

    def GetProjectManager(self):
        return _E2EPM()


def test_project_summary_end_to_end(monkeypatch, tmp_path, start_bridge):
    from davinci_mcp.registry import build_registry

    disco = tmp_path / "bridge.json"
    monkeypatch.setenv("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE", str(disco))
    start_bridge(_E2EResolve(), write_file=True)
    monkeypatch.setattr(
        resolve_api, "_connect_scripting",
        lambda: resolve_api.ResolveStatus(
            resolve_api.ResolveStatus.FREE_EDITION, "unavailable"
        ),
    )

    registry = build_registry()
    tool = registry.get("resolve_project_summary")
    result = tool.handler({})

    assert result["ok"] is True
    assert result["project_name"] == "FakeProject"
    assert result["timeline_count"] == 1
    assert result["current_timeline"]["name"] == "Timeline 1"
    assert result["current_timeline"]["frame_rate"] == "24"
