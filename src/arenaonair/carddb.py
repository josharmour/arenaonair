"""grpId -> card name / type / cost resolution backed by a local SQLite cache.

MTGA GRE messages identify cards only by numeric ``grpId`` (with names as
numeric localization ids), which is useless for narration until mapped to real
card data.  Scryfall publishes bulk data whose ``arena_id`` field equals the
MTGA grpId on most cards; :mod:`arenaonair.tools.build_carddb` builds a local
SQLite cache from that data and this module reads it.

The cache lives outside the repo (default ``~/.cache/arenaonair/cards.sqlite``)
and is entirely optional: every lookup degrades gracefully to ``None`` when the
database is missing or the id is unknown, logging at DEBUG level at most once
per missing id.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".cache" / "arenaonair" / "cards.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    arena_id   INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    type_line  TEXT NOT NULL,
    mana_cost  TEXT NOT NULL,
    card_types TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CardInfo:
    """Resolved card identity for a single grpId."""

    name: str
    type_line: str
    mana_cost: str
    card_types: Tuple[str, ...]


def _csv_to_tuple(csv: str) -> Tuple[str, ...]:
    if not csv:
        return ()
    return tuple(t for t in csv.split(",") if t)


class CardDb:
    """Read-only accessor over the locally built card cache.

    Thread safety: a single shared connection guarded by an RLock.  SQLite
    connections are cheap to share this way for read-only workloads, and the
    lock keeps cursor use serialized across narrator/reader threads.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._missing_logged: set[int] = set()
        self._missing_warned_no_db = False
        self._open()

    # -- lifecycle ---------------------------------------------------------

    def _open(self) -> None:
        if not self._path.is_file():
            logger.debug("CardDb: cache file %s does not exist yet", self._path)
            self._warn_missing_db_once()
            return
        try:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("SELECT COUNT(*) FROM cards")
        except sqlite3.Error as exc:
            logger.warning("CardDb: cannot open cache %s: %s", self._path, exc)
            self._warn_missing_db_once()
            return
        self._conn = conn

    def _warn_missing_db_once(self) -> None:
        if not self._missing_warned_no_db:
            self._missing_warned_no_db = True
            logger.debug(
                "CardDb: no usable cache at %s; lookups will return None "
                "(build one with python -m tools.build_carddb)",
                self._path,
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- queries -----------------------------------------------------------

    def lookup(self, grp_id: int) -> Optional[CardInfo]:
        """Resolve a MTGA grpId to card info, or ``None`` if unknown."""
        if not isinstance(grp_id, int) or isinstance(grp_id, bool):
            return None
        with self._lock:
            conn = self._conn
            if conn is None:
                self._log_missing(grp_id)
                return None
            try:
                row = conn.execute(
                    "SELECT name, type_line, mana_cost, card_types "
                    "FROM cards WHERE arena_id = ?",
                    (grp_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                logger.warning("CardDb: lookup failed for %s: %s", grp_id, exc)
                return None
        if row is None:
            self._log_missing(grp_id)
            return None
        name, type_line, mana_cost, card_types_csv = row
        return CardInfo(
            name=name,
            type_line=type_line,
            mana_cost=mana_cost,
            card_types=_csv_to_tuple(card_types_csv),
        )

    def counts(self) -> Tuple[int, int]:
        """Diagnostics: ``(total_rows, resolved_sample)``.

        ``resolved_sample`` is how many of a small probe of well-known arena
        ids currently resolve; useful for detecting a truncated/partial cache.
        """
        with self._lock:
            if self._conn is None:
                return (0, 0)
            total = int(self._conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        probe = (75553, 81286, 69493, 9135, 103511)
        resolved = sum(1 for gid in probe if self.lookup(gid) is not None)
        return (total, resolved)

    def as_resolver(self):
        """Return a memoized (grp_id -> card_name) resolver for GameStateBuilder."""
        cache: dict[int, Optional[str]] = {}

        def resolve(grp_id: int) -> Optional[str]:
            if not isinstance(grp_id, int) or isinstance(grp_id, bool):
                return None
            if grp_id in cache:
                return cache[grp_id]
            try:
                info = self.lookup(grp_id)
            except Exception:
                info = None
            name = getattr(info, "name", None)
            cache[grp_id] = name if isinstance(name, str) and name else None
            return cache[grp_id]

        return resolve

    # -- internals ----------------------------------------------------------

    def _log_missing(self, grp_id: int) -> None:
        if grp_id in self._missing_logged:
            return
        self._missing_logged.add(grp_id)
        logger.debug("CardDb: grpId %s not found in cache", grp_id)
