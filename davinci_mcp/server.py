"""Zero-dependency JSON-RPC 2.0 stdio MCP server.

Speaks the MCP stdio transport: newline-delimited JSON-RPC 2.0 on
stdin/stdout (not Content-Length framed). Implements the four methods an MCP
client needs - ``initialize``, ``notifications/initialized``, ``tools/list``,
``tools/call`` - plus ``ping``. Tool results are returned as JSON text content;
tool errors set ``isError`` on the result.

Message handling mirrors the proven Blockout MCP bridge, translated to Python.
Run it with ``python -m davinci_mcp``.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .registry import Registry, build_registry


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "unofficial-davinci-mcp"
SERVER_VERSION = "1.0.0"


def _write(out: TextIO, message: dict[str, Any]) -> None:
    out.write(json.dumps(message) + "\n")
    out.flush()


def _reply(out: TextIO, request_id: Any, result: dict[str, Any]) -> None:
    _write(out, {"jsonrpc": "2.0", "id": request_id, "result": result})


def _reply_error(out: TextIO, request_id: Any, code: int, message: str) -> None:
    _write(out, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _text_content(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, sort_keys=True)
    return [{"type": "text", "text": text}]


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Check arguments against the tool schema's top level.

    Returns a human-readable problem description, or None when the call is
    well-formed. Kept deliberately shallow (required keys + unknown keys) so a
    mistyped or missing parameter produces a message that names the valid
    parameters instead of a raw handler traceback.
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = [key for key in required if key not in arguments]
    unknown = (
        [key for key in arguments if key not in properties]
        if schema.get("additionalProperties") is False or properties
        else []
    )
    if not missing and not unknown:
        return None
    parts = []
    if missing:
        parts.append("missing required: " + ", ".join(sorted(missing)))
    if unknown:
        parts.append("unknown: " + ", ".join(sorted(unknown)))
    return "; ".join(parts) + ". Valid parameters: " + (", ".join(sorted(properties)) or "(none)")


def _handle_tool_call(out: TextIO, registry: Registry, request_id: Any, params: dict[str, Any]) -> None:
    name = (params or {}).get("name")
    arguments = (params or {}).get("arguments") or {}
    tool = registry.get(name)
    if tool is None:
        _reply(out, request_id, {"content": _text_content(f"Unknown tool: {name}"), "isError": True})
        return

    problem = _validate_arguments(tool.schema, arguments)
    if problem is not None:
        _reply(
            out,
            request_id,
            {"content": _text_content({"ok": False, "error": f"{name}: {problem}"}), "isError": True},
        )
        return

    try:
        result = tool.handler(arguments)
    except Exception as exc:  # noqa: BLE001 - never let one tool kill the server
        _reply(out, request_id, {"content": _text_content(f"{type(exc).__name__}: {exc}"), "isError": True})
        return

    is_error = isinstance(result, dict) and result.get("ok") is False
    _reply(out, request_id, {"content": _text_content(result), "isError": is_error})


def handle_message(out: TextIO, registry: Registry, message: dict[str, Any]) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        _reply(
            out,
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return
    if method == "notifications/initialized":
        return  # notification, no reply
    if method == "tools/list":
        _reply(out, request_id, {"tools": registry.list_mcp()})
        return
    if method == "tools/call":
        _handle_tool_call(out, registry, request_id, params)
        return
    if method == "ping":
        _reply(out, request_id, {})
        return

    # Unknown request gets method-not-found; notifications (no id) are ignored.
    if request_id is not None:
        _reply_error(out, request_id, -32601, f"Method not found: {method}")


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Read newline-delimited JSON-RPC from stdin until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    registry = build_registry()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # ignore non-JSON lines
        if not isinstance(message, dict):
            continue
        handle_message(stdout, registry, message)


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
