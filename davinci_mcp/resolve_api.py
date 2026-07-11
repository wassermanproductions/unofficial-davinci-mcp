"""Locate, import, and connect to the DaVinci Resolve scripting API.

External scripting is a DaVinci Resolve Studio feature. The free edition ships
the same app but refuses connections from outside processes, so a connection
attempt on free Resolve looks like an unreachable app. This module keeps the
three states apart and reports each one in plain language:

- ``not_installed``            - no Resolve app bundle and no scripting module
- ``app_not_running``          - module present but the app is not open
- ``free_edition_no_scripting`` - app installed and (likely) open, but external
                                  scripting is unavailable (free edition, or the
                                  Studio scripting preference is off)

Blackmagic's documented discovery is honored: ``RESOLVE_SCRIPT_API`` and
``RESOLVE_SCRIPT_LIB`` overrides first, then the standard macOS and Linux
install locations.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import sys
from pathlib import Path
from typing import Any


# --- Install locations -----------------------------------------------------

# macOS application bundles, newest naming first. Presence of any of these
# means Resolve is installed even when scripting is not reachable.
_MAC_APP_PATHS = [
    Path("/Applications/DaVinci Resolve/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve/DaVinci Resolve Studio.app"),
    Path("/Applications/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve Studio.app"),
    Path("/Applications/DaVinci Resolve 20/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve Studio 20/DaVinci Resolve Studio.app"),
    Path("/Applications/DaVinci Resolve 20.app"),
    Path("/Applications/DaVinci Resolve Studio 20.app"),
]

# Linux install roots (Blackmagic's default and the per-user variant).
_LINUX_APP_PATHS = [
    Path("/opt/resolve/bin/resolve"),
    Path("/home/resolve/bin/resolve"),
]

# Default scripting-API roots per platform (RESOLVE_SCRIPT_API when unset).
_MAC_API_DEFAULT = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
_LINUX_API_DEFAULTS = [
    Path("/opt/resolve/Developer/Scripting"),
    Path("/home/resolve/Developer/Scripting"),
]

# Default fusionscript library per platform (RESOLVE_SCRIPT_LIB when unset).
_MAC_LIB_DEFAULTS = [
    Path(
        "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
    ),
    Path(
        "/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app/Contents/Libraries/Fusion/fusionscript.so"
    ),
]
_LINUX_LIB_DEFAULTS = [
    Path("/opt/resolve/libs/Fusion/fusionscript.so"),
    Path("/home/resolve/libs/Fusion/fusionscript.so"),
]

# Extra module directories to try if the API root does not yield the module.
_MAC_MODULE_FALLBACKS = [
    Path(
        "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion"
    ),
]


def _is_macos() -> bool:
    return sys.platform == "darwin"


def installed_app_paths() -> list[str]:
    """Return the Resolve application paths that exist on this machine."""
    candidates = _MAC_APP_PATHS if _is_macos() else _LINUX_APP_PATHS
    return [str(path) for path in candidates if path.exists()]


def _script_lib_path() -> Path | None:
    """Best-effort location of the fusionscript shared library."""
    env_lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if env_lib and Path(env_lib).exists():
        return Path(env_lib)
    defaults = _MAC_LIB_DEFAULTS if _is_macos() else _LINUX_LIB_DEFAULTS
    for path in defaults:
        if path.exists():
            return path
    return None


def _module_search_dirs() -> list[Path]:
    """Directories that may contain ``DaVinciResolveScript.py``."""
    dirs: list[Path] = []
    env_api = os.environ.get("RESOLVE_SCRIPT_API")
    if env_api:
        dirs.append(Path(env_api).expanduser() / "Modules")
    if _is_macos():
        dirs.append(_MAC_API_DEFAULT / "Modules")
        dirs.extend(_MAC_MODULE_FALLBACKS)
    else:
        dirs.extend(root / "Modules" for root in _LINUX_API_DEFAULTS)
    # De-duplicate while preserving order; keep only existing directories.
    seen: set[str] = set()
    unique: list[Path] = []
    for path in dirs:
        key = str(path)
        if key not in seen and path.exists():
            seen.add(key)
            unique.append(path)
    return unique


def _unwrap_script_module(module: Any) -> Any:
    """Return the module that actually exposes ``scriptapp``.

    Blackmagic's ``DaVinciResolveScript.py`` is a loader shim: during its own
    import it loads ``fusionscript`` and swaps it into
    ``sys.modules['DaVinciResolveScript']``, keeping the real module in its
    ``script_module`` attribute. Depending on how the shim was imported, the
    object we hold can still be the pre-swap shim - so normalize to whichever
    object really has ``scriptapp``.
    """
    if hasattr(module, "scriptapp"):
        return module
    swapped = sys.modules.get("DaVinciResolveScript")
    if swapped is not None and hasattr(swapped, "scriptapp"):
        return swapped
    inner = getattr(module, "script_module", None)
    if inner is not None and hasattr(inner, "scriptapp"):
        return inner
    return module


def _import_module() -> tuple[Any | None, str | None, list[str]]:
    """Import ``DaVinciResolveScript``.

    Returns ``(module, import_error, searched_dirs)``. The module load needs
    ``RESOLVE_SCRIPT_LIB`` pointed at fusionscript; we set it from the detected
    library when the caller has not already provided one.
    """
    if not os.environ.get("RESOLVE_SCRIPT_LIB"):
        lib = _script_lib_path()
        if lib is not None:
            os.environ["RESOLVE_SCRIPT_LIB"] = str(lib)

    searched = _module_search_dirs()

    # A correctly configured PYTHONPATH lets the plain import succeed.
    try:
        module = importlib.import_module("DaVinciResolveScript")
        return _unwrap_script_module(module), None, [
            str(path) for path in searched
        ]
    except Exception as direct_exc:  # noqa: BLE001 - report, do not raise
        first_error = f"{type(direct_exc).__name__}: {direct_exc}"

    errors = [f"default import failed: {first_error}"]
    for directory in searched:
        module_file = directory / "DaVinciResolveScript.py"
        if not module_file.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "DaVinciResolveScript", module_file
            )
            if spec is None or spec.loader is None:
                errors.append(f"{module_file}: could not create import spec")
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return _unwrap_script_module(module), None, [str(path) for path in searched]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{module_file}: {type(exc).__name__}: {exc}")

    return None, "; ".join(errors), [str(path) for path in searched]


class ResolveStatus:
    """Outcome of a connection attempt, with a human-readable explanation."""

    REACHABLE = "reachable"
    NOT_INSTALLED = "not_installed"
    APP_NOT_RUNNING = "app_not_running"
    FREE_EDITION = "free_edition_no_scripting"
    SCRIPTING_ERROR = "scripting_error"

    def __init__(
        self,
        state: str,
        message: str,
        *,
        resolve: Any | None = None,
        product: str | None = None,
        version: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.state = state
        self.message = message
        self.resolve = resolve
        self.product = product
        self.version = version
        self.details = details or {}

    @property
    def reachable(self) -> bool:
        return self.state == self.REACHABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "product": self.product,
            "version": self.version,
            **self.details,
        }


def _studio_installed(app_paths: list[str]) -> bool:
    return any("Studio" in path for path in app_paths)


def _free_installed(app_paths: list[str]) -> bool:
    return any("Studio" not in path for path in app_paths)


def connect() -> ResolveStatus:
    """Attempt to reach a running DaVinci Resolve and classify the result.

    Studio's external scripting is tried first. When that is unavailable - the
    usual case on the free edition - an in-app bridge is looked for (see
    :mod:`bridge.resolve_bridge`): if one is running and answers its health
    probe, this returns a *reachable* status backed by a proxy that drives
    Resolve through the bridge, so every live tool works unchanged.
    """
    status = _connect_scripting()
    if status.reachable:
        return status
    bridge_status = _connect_bridge()
    if bridge_status is not None:
        return bridge_status
    return status


def _connect_scripting() -> ResolveStatus:
    """Attempt to reach a running DaVinci Resolve and classify the result."""
    app_paths = installed_app_paths()
    module, import_error, searched = _import_module()

    if module is None:
        if not app_paths:
            return ResolveStatus(
                ResolveStatus.NOT_INSTALLED,
                "DaVinci Resolve does not appear to be installed on this machine. "
                "Install it, or use the interchange tools to generate files you "
                "import manually.",
                details={
                    "import_error": import_error,
                    "searched_module_paths": searched,
                    "installed_app_paths": app_paths,
                },
            )
        # App present but scripting module missing: interchange still works.
        return ResolveStatus(
            ResolveStatus.FREE_EDITION,
            "DaVinci Resolve is installed but its scripting module could not be "
            "loaded. External scripting is a Studio feature; on the free edition "
            "use the interchange tools instead.",
            details={
                "import_error": import_error,
                "searched_module_paths": searched,
                "installed_app_paths": app_paths,
            },
        )

    try:
        resolve = module.scriptapp("Resolve")
    except Exception as exc:  # noqa: BLE001
        return ResolveStatus(
            ResolveStatus.SCRIPTING_ERROR,
            "The DaVinci Resolve scripting module loaded but the connection "
            "handshake failed.",
            details={
                "scripting_error": f"{type(exc).__name__}: {exc}",
                "installed_app_paths": app_paths,
            },
        )

    if resolve is None:
        # Module works, but scriptapp returned nothing. Two common causes:
        # the app is not open, or it is the free edition refusing the connection.
        if _free_installed(app_paths) and not _studio_installed(app_paths):
            return ResolveStatus(
                ResolveStatus.FREE_EDITION,
                "DaVinci Resolve is installed but did not accept the scripting "
                "connection. External scripting requires DaVinci Resolve Studio; "
                "on the free edition use the interchange tools instead.",
                details={"installed_app_paths": app_paths},
            )
        return ResolveStatus(
            ResolveStatus.APP_NOT_RUNNING,
            "DaVinci Resolve scripting is available but no running instance "
            "answered. Open DaVinci Resolve Studio and enable Preferences > "
            "System > General > External scripting using: Local, then retry.",
            details={"installed_app_paths": app_paths},
        )

    product = None
    version = None
    try:
        product = resolve.GetProductName()
    except Exception:  # noqa: BLE001
        product = None
    try:
        version = resolve.GetVersionString()
    except Exception:  # noqa: BLE001
        version = None

    return ResolveStatus(
        ResolveStatus.REACHABLE,
        "Connected to a running DaVinci Resolve instance.",
        resolve=resolve,
        product=product,
        version=version,
        details={"installed_app_paths": app_paths},
    )


# --- Convenience accessors used by the live tools --------------------------


def current_project(resolve: Any) -> tuple[Any | None, str | None]:
    """Return ``(project, error_message)`` for the open project."""
    manager = resolve.GetProjectManager()
    if not manager:
        return None, "DaVinci Resolve project manager is unavailable."
    project = manager.GetCurrentProject()
    if not project:
        return None, "No DaVinci Resolve project is open."
    return project, None


def media_pool(project: Any) -> tuple[Any | None, str | None]:
    """Return ``(media_pool, error_message)`` for the open project."""
    pool = project.GetMediaPool()
    if not pool:
        return None, "The current project has no accessible media pool."
    return pool, None


# --- Free-edition bridge connection ----------------------------------------
#
# External scripting is Studio-only, but a script running *inside* Resolve gets
# the same ``resolve`` object on any edition. The companion bridge script
# (bridge/resolve_bridge.py) exploits that: launched from Resolve's Scripts
# menu, it serves a whitelisted subset of the scripting API over localhost and
# advertises itself in a discovery file. Here we find that bridge and expose a
# proxy that mirrors the object model so the existing live tools work unchanged.

import json as _json  # noqa: E402 - kept local to the bridge section
import urllib.error as _urllib_error  # noqa: E402
import urllib.request as _urllib_request  # noqa: E402


class BridgeError(RuntimeError):
    """A call over the bridge failed (transport, auth, or Resolve-side)."""


def bridge_discovery_path() -> Path:
    """Path to the bridge discovery file - identical logic to the bridge script.

    Honors ``UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE``, then ``XDG_CONFIG_HOME``,
    then ~/.config.
    """
    override = os.environ.get("UNOFFICIAL_DAVINCI_MCP_BRIDGE_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "unofficial-davinci-mcp" / "bridge.json"


class _BridgeClient:
    """Minimal JSON-over-HTTP client for one bridge session."""

    def __init__(self, host: str, port: int, token: str, timeout: float = 15.0) -> None:
        self._base = f"http://{host}:{port}"
        self._token = token
        self._timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = _json.dumps(payload).encode("utf-8")
        request = _urllib_request.Request(
            self._base + path,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with _urllib_request.urlopen(request, timeout=self._timeout) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except _urllib_error.HTTPError as exc:  # 4xx/5xx carry a JSON body
            try:
                body = _json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {"error": f"HTTP {exc.code}"}
            raise BridgeError(body.get("error", f"HTTP {exc.code}")) from exc
        except OSError as exc:
            raise BridgeError(f"The DaVinci Resolve bridge is unreachable: {exc}") from exc

    def health(self) -> dict[str, Any]:
        request = _urllib_request.Request(self._base + "/health", method="GET")
        with _urllib_request.urlopen(request, timeout=self._timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    def call(self, object_path: str, method: str, args: list[Any]) -> Any:
        payload = {
            "token": self._token,
            "object_path": object_path,
            "method": method,
            "args": args,
        }
        result = self._post("/call", payload)
        if not result.get("ok"):
            raise BridgeError(result.get("error", "The bridge refused the call."))
        return result.get("value")

    def shutdown(self) -> None:
        self._post("/shutdown", {"token": self._token})


def _encode_bridge_arg(value: Any) -> Any:
    """Serialize a tool-side argument, turning proxies into bridge references."""
    if isinstance(value, _BridgeProxy):
        return {"__ref__": value._handle}
    if isinstance(value, dict):
        return {key: _encode_bridge_arg(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_bridge_arg(item) for item in value]
    return value


def _decode_bridge_value(client: "_BridgeClient", value: Any) -> Any:
    """Rehydrate a bridge response: references become proxies, recursively."""
    if isinstance(value, dict):
        ref = value.get("__ref__")
        if ref is not None:
            return _BridgeProxy(client, ref)
        return {key: _decode_bridge_value(client, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_bridge_value(client, item) for item in value]
    return value


class _BridgeMethod:
    """A bound, callable method name on a proxied object."""

    def __init__(self, client: "_BridgeClient", handle: str, name: str) -> None:
        self._client = client
        self._handle = handle
        self._name = name

    def __call__(self, *args: Any) -> Any:
        encoded = [_encode_bridge_arg(arg) for arg in args]
        value = self._client.call(self._handle, self._name, encoded)
        return _decode_bridge_value(self._client, value)


class _BridgeProxy:
    """Stand-in for a live Resolve object reached through the bridge.

    Attribute access yields a callable that performs the corresponding
    scripting call over HTTP, so ``resolve.GetProjectManager().GetCurrentProject()``
    and every other chain the live tools use behaves exactly as it would against
    the real object.
    """

    def __init__(self, client: "_BridgeClient", handle: str) -> None:
        # Stored via __dict__ to avoid recursing through __getattr__.
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_handle", handle)

    def __getattr__(self, name: str) -> "_BridgeMethod":
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _BridgeMethod(self._client, self._handle, name)

    def __repr__(self) -> str:
        return f"<BridgeProxy {self._handle}>"


def _read_discovery() -> dict[str, Any] | None:
    path = bridge_discovery_path()
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            info = _json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or "port" not in info or "token" not in info:
        return None
    return info


def _connect_bridge() -> ResolveStatus | None:
    """Look for a live in-app bridge; return a reachable status or ``None``.

    ``None`` means "no usable bridge" - the caller then keeps the original
    scripting-based status so the free-edition guidance is unchanged.
    """
    info = _read_discovery()
    if info is None:
        return None
    host = str(info.get("host") or "127.0.0.1")
    try:
        client = _BridgeClient(host, int(info["port"]), str(info["token"]))
        health = client.health()
    except Exception:  # noqa: BLE001 - a stale/dead bridge is simply "no bridge"
        return None
    if not isinstance(health, dict) or not health.get("ok"):
        return None

    product = health.get("app") or "DaVinci Resolve"
    version = health.get("version")
    app_paths = installed_app_paths()
    return ResolveStatus(
        ResolveStatus.REACHABLE,
        "Connected to DaVinci Resolve through the in-app bridge "
        "(free edition, live scripting via Workspace > Scripts).",
        resolve=_BridgeProxy(client, "resolve"),
        product=product,
        version=version,
        details={
            "edition": "free-via-bridge",
            "via_bridge": True,
            "bridge": {
                "host": host,
                "port": info.get("port"),
                "pid": info.get("pid"),
                "reported_edition": health.get("edition"),
            },
            "installed_app_paths": app_paths,
        },
    )
