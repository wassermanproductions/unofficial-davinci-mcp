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
