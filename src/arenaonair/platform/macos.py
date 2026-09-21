"""macOS: ~/Library/Application Support/com.wizards.mtga/**/Player*.log glob."""

from __future__ import annotations

import re
from pathlib import Path

from .logpath import _first_existing

def _bases() -> list[Path]:
    return [
        Path.home() / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA",
        Path.home() / "Library" / "Application Support" / "com.wizards.mtga",
    ]


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

    Documented best effort (task spec): the canonical bases are
    ``~/Library/Logs/Wizards Of The Coast/MTGA`` (Steam / standard macOS Unity)
    and ``~/Library/Application Support/com.wizards.mtga`` (standalone/Epic).
    Beneath them Arena may use dated subdirectories, so we check direct locations,
    glob ``**/*.log``, prefer files literally named ``Player.log`` (then
    ``Player-prev.log``), and break ties by newest mtime.
    """
    direct = [
        Path.home() / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log",
        _base() / "Logs" / "Player.log",
        _base() / "Player.log",
    ]
    for cand in direct:
        if cand.is_file():
            return cand
    logs: list[Path] = []
    for base in _bases():
        if base.is_dir():
            logs.extend(p for p in base.glob("**/*.log") if p.is_file())
    if logs:
        logs.sort(key=lambda p: (_rank(p)[0], -_rank(p)[1]))
        return logs[0]
    return _first_existing(direct)
