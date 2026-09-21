"""Windows: %LOCALAPPDATA%\\..\\LocalLow\\Wizards Of The Coast\\MTGA\\Player.log."""

from __future__ import annotations

import os
from pathlib import Path

from .logpath import _first_existing

_WIZARDS_DIR = "Wizards Of The Coast"
_MTGA_DIR = "MTGA"


def default_player_log_path() -> Path:
    """Canonical Windows Player.log location.

    Unity stores its LocalLow logs under
    ``C:\\Users\\<user>\\AppData\\LocalLow\\Wizards Of The Coast\\MTGA``.
    There is no LOCALLOW env var; the convention is derived from
    ``%LOCALAPPDATA%`` (``...\\AppData\\Local`` -> sibling ``LocalLow``),
    falling back to ``Path.home() / AppData / LocalLow``.
    """
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        lad = Path(local_app_data)
        # %LOCALAPPDATA% normally ends in ...\AppData\Local; go up one level
        # and join LocalLow. If it doesn't end in Local, still try the sibling.
        if lad.name.lower() == "local":
            candidates.append(lad.parent / "LocalLow" / _WIZARDS_DIR / _MTGA_DIR / "Player.log")
        candidates.append(lad.parent / "LocalLow" / _WIZARDS_DIR / _MTGA_DIR / "Player.log")
    candidates.append(
        Path.home() / "AppData" / "LocalLow" / _WIZARDS_DIR / _MTGA_DIR / "Player.log"
    )
    return _first_existing(candidates)
