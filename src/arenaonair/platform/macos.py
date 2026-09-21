"""macOS: ~/Library/Application Support/com.wizards.mtga/**/Player*.log glob."""

from __future__ import annotations

import re
from pathlib import Path

from .logpath import _first_existing

def _base() -> Path:

    return Path.home() / "Library" / "Application Support" / "com.wizards.mtga"

# Best-effort: the exact on-disk layout under com.wizards.mtga has varied
# across Arena versions (dated subdirectories such as Logs/2026-09-18/...).
# We glob recursively for *.log and rank by (name preference, mtime desc).
_NAME_RANK = (
    ("player.log", 0),          # exact current log
    ("player-prev.log", 1),     # previous-session log
)


def _rank(p: Path) -> tuple[int, float]:
    name = p.name.lower()
    for suffix, rank in _NAME_RANK:
        if name.endswith(suffix):
            return (rank, p.stat().st_mtime)
    return (2, p.stat().st_mtime)


def default_player_log_path() -> Path:
    """Locate Player.log on macOS via recursive glob.

    Documented best effort (task spec): the canonical base is
    ``~/Library/Application Support/com.wizards.mtga``; beneath it Arena uses
    dated subdirectories, so we glob ``**/*.log``, prefer files literally named
    ``Player.log`` (then ``Player-prev.log``), and break ties by newest mtime.
    """
    direct = [_base() / "Logs" / "Player.log", _base() / "Player.log"]
    for cand in direct:
        if cand.is_file():
            return cand
    if _base().is_dir():
        logs = sorted(
            (p for p in _base().glob("**/*.log") if p.is_file()),
            key=lambda p: (_rank(p)[0], -_rank(p)[1]),
        )
        if logs:
            return logs[0]
    return _first_existing(direct)
