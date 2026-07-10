"""Entry point for ``python -m voice``."""

from __future__ import annotations

from .ptt import main

if __name__ == "__main__":
    raise SystemExit(main())
