#!/usr/bin/env python
"""In-app live bridge for Wasserman's Unofficial DaVinci MCP.

External scripting is a DaVinci Resolve Studio feature. But a script *run from
inside Resolve* - via Workspace > Scripts - is handed the same ``resolve``
scripting object on any edition, including the free one. This single file uses
that fact: launch it once per session from Resolve's Scripts menu and it starts
a tiny localhost HTTP server, inside Resolve, that executes a whitelisted set of
scripting calls against the live ``resolve`` object and returns JSON.

The MCP server (a separate, ordinary Python process) finds this bridge through a
small discovery file and drives Resolve through it exactly as if external
scripting were available. See bridge/README.md.

Design constraints this file honors:

* Runs on Resolve's *embedded* Python interpreter -> standard library ONLY, no
  third-party imports, and syntax kept compatible with Python 3.6+.
* Binds 127.0.0.1 only, never a routable interface.
* Every request (except the unauthenticated /health probe) must carry a random
  per-session bearer token.
* Only whitelisted root objects and method-name patterns can be called;
  everything else is refused.
* Threaded, so Resolve's UI stays responsive while a call is in flight.

This file is intentionally self-contained: users copy just this one script into
Resolve's Scripts/Utility folder.
"""

import atexit
import json
import os
import secrets
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


# --- Whitelist policy ------------------------------------------------------

# Named root objects a caller may start a chain from. Everything else must be a
# handle the bridge itself minted (and therefore already a descendant of the
# live ``resolve`` object), so no arbitrary Python object is ever reachable.
_ROOT_NAMES = ("resolve", "project_manager", "project", "media_pool",
               "current_timeline")

# Allowed method-name prefixes. The Resolve scripting API is CamelCase, so these
# verb prefixes cover the real surface (Get/Set/Add/Append/Create/Delete/
# Import/Export/Open/Grab/...) while blocking dunders, private attributes, and
# anything that does not read like a scripting call.
_ALLOWED_PREFIXES = (
    "Get", "Set", "Add", "Append", "Create", "Delete", "Import", "Export",
    "Open", "Grab", "Load", "Save", "Insert", "Refresh", "Start", "Stop",
    "Is", "Close", "Goto", "Duplicate", "Move", "Update", "Apply", "Clear",
    "Remove", "Enable", "Disable", "Render", "Auto", "Copy", "Paste",
    "Detect", "Transcribe", "Convert", "Link", "Unlink", "Reorder", "Restore",
    "Archive",
)


def method_is_allowed(method):
    """True when ``method`` is a safe, whitelisted scripting call name."""
    if not isinstance(method, str) or not method:
        return False
    if method.startswith("_"):
        return False
    return any(method.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


class BridgeError(Exception):
    """A request that the whitelist or the object model refused."""

    def __init__(self, message, status=400):
        Exception.__init__(self, message)
        self.status = status


# --- Discovery file --------------------------------------------------------


def discovery_path():
    """Location of the bridge discovery file.

    Honors ``UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE`` (used by tests and power
    users), then ``XDG_CONFIG_HOME``, then ~/.config. The MCP side computes the
    identical path.
    """
    override = os.environ.get("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE")
    if override:
        return override
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "unofficial-davinci-mcp", "bridge.json")


def write_discovery(path, info):
    """Write the discovery file as owner-only (0600) JSON."""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700)
    # Write then tighten permissions; open with 0600 from the start where we can.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, json.dumps(info).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remove_discovery(path):
    """Best-effort removal of the discovery file (on shutdown / on quit)."""
    try:
        os.remove(path)
    except OSError:
        pass


# --- Dispatcher: the live object model, safely reachable -------------------


class Dispatcher(object):
    """Translate whitelisted requests into calls on the live ``resolve`` object.

    Objects returned by the scripting API are not JSON-serializable, so any
    non-primitive return value is stored in a per-session handle table and
    referenced by an opaque id. A later request (or a nested argument) refers to
    it by that id; the dispatcher rehydrates it before making the call. Because
    the table only ever holds objects the API itself returned, a caller can
    never reach an object outside the live ``resolve`` graph.
    """

    def __init__(self, resolve):
        self._resolve = resolve
        self._handles = {}
        self._counter = 0
        self._lock = threading.Lock()

    # -- health ------------------------------------------------------------

    def health(self):
        product = None
        version = None
        try:
            product = self._resolve.GetProductName()
        except Exception:
            product = None
        try:
            version = self._resolve.GetVersionString()
        except Exception:
            version = None
        edition = "studio" if (product and "Studio" in product) else "free"
        return {
            "ok": True,
            "app": product or "DaVinci Resolve",
            "edition": edition,
            "version": version,
        }

    # -- handle table ------------------------------------------------------

    def _mint(self, obj):
        with self._lock:
            self._counter += 1
            handle = "handle:%d" % self._counter
        self._handles[handle] = obj
        return handle

    def _named_root(self, name):
        if name == "resolve":
            return self._resolve
        pm = self._resolve.GetProjectManager()
        if name == "project_manager":
            return pm
        project = pm.GetCurrentProject() if pm else None
        if name == "project":
            return project
        if project is None:
            raise BridgeError("No DaVinci Resolve project is open.")
        if name == "media_pool":
            return project.GetMediaPool()
        if name == "current_timeline":
            return project.GetCurrentTimeline()
        raise BridgeError("Unknown root object: %r" % (name,))

    def _resolve_target(self, object_path):
        if object_path in _ROOT_NAMES:
            return self._named_root(object_path)
        if isinstance(object_path, str) and object_path.startswith("handle:"):
            if object_path in self._handles:
                return self._handles[object_path]
            raise BridgeError(
                "Unknown object handle (the bridge may have been restarted "
                "since this reference was issued).")
        raise BridgeError(
            "object_path must be a whitelisted root %s or a bridge-issued "
            "handle." % (list(_ROOT_NAMES),))

    # -- (de)serialization -------------------------------------------------

    def encode(self, value):
        """Turn a scripting return value into JSON-safe data."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [self.encode(item) for item in value]
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                out[key] = self.encode(item)
            return out
        # A live scripting object: keep it, hand back an opaque reference.
        return {"__ref__": self._mint(value),
                "__type__": type(value).__name__}

    def decode(self, value):
        """Rehydrate arguments, turning references back into live objects."""
        if isinstance(value, dict):
            ref = value.get("__ref__")
            if ref is not None:
                if ref in self._handles:
                    return self._handles[ref]
                raise BridgeError("Unknown object handle in arguments: %r" % (ref,))
            out = {}
            for key, item in value.items():
                out[key] = self.decode(item)
            return out
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        return value

    # -- the one operation -------------------------------------------------

    def call(self, object_path, method, args, kwargs):
        if not method_is_allowed(method):
            raise BridgeError("Method %r is not on the allow-list." % (method,))
        target = self._resolve_target(object_path)
        if target is None:
            raise BridgeError("The requested object is not available right now.")
        func = getattr(target, method, None)
        if func is None or not callable(func):
            raise BridgeError(
                "%s has no callable method %r." % (type(target).__name__, method))
        decoded_args = [self.decode(a) for a in (args or [])]
        decoded_kwargs = {}
        for key, item in (kwargs or {}).items():
            decoded_kwargs[key] = self.decode(item)
        try:
            result = func(*decoded_args, **decoded_kwargs)
        except Exception as exc:  # a real Resolve-side failure
            raise BridgeError(
                "DaVinci Resolve raised while running %s: %s"
                % (method, exc), status=500)
        return self.encode(result)


# --- HTTP layer ------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    # Silence the default per-request stderr logging so we do not spam Resolve's
    # console; errors still surface in JSON responses.
    def log_message(self, fmt, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, body):
        expected = self.server.bridge_token
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied = header[len("Bearer "):].strip()
        else:
            supplied = body.get("token") if isinstance(body, dict) else None
        return bool(supplied) and secrets.compare_digest(str(supplied), expected)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise BridgeError("Request body was not valid JSON.")

    def do_GET(self):
        if self.path == "/health":
            self._send(200, self.server.dispatcher.health())
            return
        self._send(404, {"ok": False, "error": "Not found: %s" % self.path})

    def do_POST(self):
        try:
            body = self._read_body()
        except BridgeError as exc:
            self._send(exc.status, {"ok": False, "error": str(exc)})
            return

        if self.path == "/shutdown":
            if not self._authorized(body):
                self._send(401, {"ok": False, "error": "Bad or missing token."})
                return
            self._send(200, {"ok": True, "stopping": True})
            # shutdown() must not run on the request thread or it deadlocks.
            threading.Thread(
                target=self.server.request_stop, name="bridge-shutdown").start()
            return

        if self.path != "/call":
            self._send(404, {"ok": False, "error": "Not found: %s" % self.path})
            return

        if not self._authorized(body):
            self._send(401, {"ok": False, "error": "Bad or missing token."})
            return

        object_path = body.get("object_path")
        method = body.get("method")
        args = body.get("args") or []
        kwargs = body.get("kwargs") or {}
        try:
            value = self.server.dispatcher.call(object_path, method, args, kwargs)
        except BridgeError as exc:
            self._send(exc.status, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # never let one call kill the server
            self._send(500, {"ok": False,
                             "error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._send(200, {"ok": True, "value": value})


class BridgeServer(socketserver.ThreadingMixIn, HTTPServer):
    """Threaded, localhost-only HTTP server carrying the dispatcher + token."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, resolve, host="127.0.0.1", port=0, token=None):
        HTTPServer.__init__(self, (host, port), _Handler)
        self.dispatcher = Dispatcher(resolve)
        self.bridge_token = token or secrets.token_hex(24)

    @property
    def port(self):
        return self.server_address[1]

    def request_stop(self):
        try:
            self.shutdown()
        except Exception:
            pass


# --- Session lifecycle -----------------------------------------------------


def start_bridge(resolve, host="127.0.0.1", port=0, token=None, write_file=True):
    """Start the bridge server on a background thread.

    Returns the :class:`BridgeServer`. The server is already serving when this
    returns, so Resolve's UI thread is never blocked.
    """
    server = BridgeServer(resolve, host=host, port=port, token=token)
    thread = threading.Thread(
        target=server.serve_forever, name="unofficial-davinci-bridge")
    thread.daemon = True
    thread.start()
    server.bridge_thread = thread

    if write_file:
        info = {
            "port": server.port,
            "token": server.bridge_token,
            "pid": os.getpid(),
            "edition": server.dispatcher.health().get("edition"),
            "version": server.dispatcher.health().get("version"),
            "host": host,
        }
        path = discovery_path()
        write_discovery(path, info)
        atexit.register(remove_discovery, path)
        server.discovery_file = path
    return server


def _acquire_resolve():
    """Get the live ``resolve`` object when running inside Resolve.

    Inside Resolve's Console / Scripts menu the interpreter exposes ``resolve``
    (and often ``bmd``) as builtins. When run standalone we fall back to the
    scripting module, which only succeeds under Studio - the whole point of this
    bridge is to run *inside* Resolve, so that fallback is a courtesy.
    """
    import builtins
    existing = getattr(builtins, "resolve", None)
    if existing is not None:
        return existing
    if "resolve" in globals() and globals()["resolve"] is not None:
        return globals()["resolve"]
    try:
        bmd = __import__("DaVinciResolveScript")
        return bmd.scriptapp("Resolve")
    except Exception:
        return None


def main():
    resolve_obj = _acquire_resolve()
    if resolve_obj is None:
        sys.stderr.write(
            "Unofficial DaVinci MCP bridge: could not find the 'resolve' "
            "object. Run this from inside DaVinci Resolve via Workspace > "
            "Scripts, or the Console.\n")
        return None
    server = start_bridge(resolve_obj)
    health = server.dispatcher.health()
    print("Unofficial DaVinci MCP bridge is live.")
    print("  app:     %s (%s edition)" % (health.get("app"), health.get("edition")))
    print("  version: %s" % health.get("version"))
    print("  address: 127.0.0.1:%d" % server.port)
    print("  discovery file: %s" % getattr(server, "discovery_file", "(none)"))
    print("Leave DaVinci Resolve open; the MCP server will connect automatically.")
    print("Re-run this script if you restart Resolve.")
    # Keep a reference alive on the module so the server is not garbage
    # collected after this function returns to Resolve.
    global _ACTIVE_SERVER
    _ACTIVE_SERVER = server
    return server


_ACTIVE_SERVER = None


if __name__ == "__main__":
    main()
