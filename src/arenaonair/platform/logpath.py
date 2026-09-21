"""Resolve the MTGA Player.log location for the current operating system.

Single public entry point::

    from arenaonair.platform import default_player_log_path

Dispatch happens HERE inside platform/ -- callers never branch on
``sys.platform`` themselves (DESIGN.md §3.1).
"""

from __future__ import annotations

import sys
from pathlib import Path


def default_player_log_path() -> Path:
    """Return the best-guess absolute path to MTGA's Player.log.

    Raises FileNotFoundError listing every candidate tried when none exists;
    callers may treat that as "MTGA not installed / not run yet" and retry later.
    """
    if sys.platform.startswith("win"):
        from .windows import default_player_log_path as _impl
    elif sys.platform == "darwin":
        from .macos import default_player_log_path as _impl
    elif sys.platform.startswith("linux"):
        from .linux import default_player_log_path as _impl
    else:
        raise RuntimeError(
            f"arenaonair.platform: unsupported sys.platform {sys.platform!r}; "
            "expected win32/darwin/linux"
        )
    return _impl()


def _first_existing(candidates: list[Path]) -> Path:
    """Return the first candidate that exists, else raise FileNotFoundError."""
    for cand in candidates:
        if cand.is_file():
            return cand
    tried = "\n  ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        "Could not locate MTGA Player.log; tried:\n  " + tried +
        "\n(Is MTG Arena installed/run at least once? Pass an explicit path "
        "to override.)"
    )
