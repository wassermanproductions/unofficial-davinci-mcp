"""End-to-end MCP protocol test over stdio.

Spawns ``python -m davinci_mcp`` as a subprocess, then drives the handshake and
two tool calls the way a real MCP client would: initialize, tools/list, a
resolve_capabilities call, and an FCPXML generation round-trip on tmp media.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _drive(requests: list[dict]) -> dict[int, dict]:
    """Send newline-delimited JSON-RPC, close stdin, collect responses by id."""
    payload = "".join(json.dumps(req) + "\n" for req in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "davinci_mcp"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    responses: dict[int, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "id" in message and message["id"] is not None:
            responses[message["id"]] = message
    return responses, proc


def test_initialize_and_tools_list():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    responses, _ = _drive(requests)

    init = responses[1]["result"]
    assert init["protocolVersion"] == "2024-11-05"
    assert init["serverInfo"]["name"] == "unofficial-davinci-mcp"

    tools = responses[2]["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "resolve_capabilities" in names
    assert "generate_fcpxml" in names
    assert "resolve_render" in names
    for tool in tools:
        assert "inputSchema" in tool and "description" in tool


def test_capabilities_tool_call():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "resolve_capabilities", "arguments": {}}},
    ]
    responses, _ = _drive(requests)
    result = responses[2]["result"]
    assert result.get("isError") in (False, None)
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["tier"] in ("live", "interchange")


def test_unknown_tool_is_error():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "does_not_exist", "arguments": {}}},
    ]
    responses, _ = _drive(requests)
    result = responses[2]["result"]
    assert result["isError"] is True


def test_method_not_found():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 9, "method": "nonsense/method", "params": {}},
    ]
    responses, _ = _drive(requests)
    assert responses[9]["error"]["code"] == -32601


def test_fcpxml_round_trip_over_protocol(make_media, tmp_path):
    clip = make_media("shot", "video")
    out = tmp_path / "roundtrip.fcpxml"
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "generate_fcpxml",
            "arguments": {
                "name": "RoundTrip",
                "clips": [{"path": clip, "duration_seconds": 2.0}],
                "output_path": str(out),
                "dry_run": False,
            },
        }},
    ]
    responses, _ = _drive(requests)
    result = responses[2]["result"]
    assert result.get("isError") in (False, None)
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert out.exists()
    assert "<fcpxml" in out.read_text(encoding="utf-8")
