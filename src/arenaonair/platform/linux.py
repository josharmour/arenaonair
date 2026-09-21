"""Linux: MTG Arena runs under Steam Proton; Player.log lives inside the
protonprefix of appid 2141910."""

from __future__ import annotations

from pathlib import Path

from .logpath import _first_existing

_APPID = "2141910"
_RELATIVE = (
    "pfx/drive_c/users/steamuser/AppData/LocalLow/"
    "Wizards Of The Coast/MTGA/Player.log"
)

def _steam_roots() -> tuple[Path, ...]:

    return (
        Path.home() / ".steam" / "steam" / "steamapps",
        Path.home() / ".local" / "share" / "Steam" / "steamapps",
    )


def default_player_log_path() -> Path:
    """Steam-Proton compatdata path for MTG Arena (appid 2141910).

    Two library roots are tried (the ``~/.steam/steam`` symlink farm and the
    real ``~/.local/share/Steam``); a custom library elsewhere on disk is not
    auto-discovered in v1 -- pass an explicit path to override.
    """
    candidates = [root / "compatdata" / _APPID / _RELATIVE for root in _steam_roots()]
    return _first_existing(candidates)
