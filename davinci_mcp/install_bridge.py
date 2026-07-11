"""Install the in-app bridge script into DaVinci Resolve's Scripts folder.

Convenience wrapper so a user does not have to hand-copy the file:

    python -m davinci_mcp.install_bridge

It copies ``bridge/resolve_bridge.py`` into Resolve's per-user
``Fusion/Scripts/Utility`` folder, from where it appears under
Workspace > Scripts in every page. Run it once (again after upgrading). See
bridge/README.md for what to do next.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


SCRIPT_NAME = "resolve_bridge.py"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def bridge_source() -> Path | None:
    """Locate the packaged/cloned ``resolve_bridge.py`` to install."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "bridge" / SCRIPT_NAME,  # repo / editable install
        here.parent / "bridge" / SCRIPT_NAME,          # if ever co-packaged
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def scripts_utility_dir() -> Path:
    """Per-user Resolve Scripts/Utility folder (the docs' recommended location)."""
    if _is_macos():
        return (
            Path.home()
            / "Library/Application Support/Blackmagic Design/DaVinci Resolve"
            / "Fusion/Scripts/Utility"
        )
    # Linux, per Blackmagic's documented per-user path.
    return Path.home() / ".local/share/DaVinciResolve/Fusion/Scripts/Utility"


def install(dest_dir: Path | None = None) -> Path:
    """Copy the bridge script into the Scripts/Utility folder; return its path."""
    source = bridge_source()
    if source is None:
        raise FileNotFoundError(
            "Could not find bridge/resolve_bridge.py to install. Install this "
            "project from a clone (pip install -e .) or copy the script by hand "
            "as described in bridge/README.md."
        )
    target_dir = dest_dir or scripts_utility_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / SCRIPT_NAME
    shutil.copyfile(source, target)
    return target


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dest = Path(argv[0]).expanduser() if argv else None
    try:
        installed = install(dest)
    except (FileNotFoundError, OSError) as exc:
        print(f"Could not install the bridge: {exc}", file=sys.stderr)
        return 1
    print("Installed the DaVinci MCP bridge script:")
    print(f"  {installed}")
    print("Next: open DaVinci Resolve, then run Workspace > Scripts > resolve_bridge")
    print("once per session. The MCP server connects to it automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
