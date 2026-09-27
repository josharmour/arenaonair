"""Local cross-match memory: who you've played, how it went, what they cast.

One small sqlite file under the data directory. Nothing leaves the machine.
Only public information is stored: player names, results, turn counts, the
names of cards the opponent revealed, and a hash of the local decklist.

The booth uses it two ways: the template path speaks one short intro line at
the start of a match, and the generative booth receives ``history:*`` facts
it may cite.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    match_id   TEXT NOT NULL,
    game_id    TEXT NOT NULL,
    ended_at   REAL NOT NULL,
    format     TEXT,
    local_name TEXT,
    opp_name   TEXT,
    result     TEXT NOT NULL,          -- win | loss | unknown
    turns      INTEGER,
    deck_key   TEXT,
    opp_cards  TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (match_id, game_id)
);
CREATE INDEX IF NOT EXISTS games_opp ON games(opp_name);
"""


def deck_key(grp_ids) -> str | None:
    """Stable short id for a submitted decklist (order-insensitive)."""
    ids = sorted(int(g) for g in grp_ids or () if g is not None)
    if not ids:
        return None
    return hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class GameRecord:
    match_id: str
    game_id: str
    format: str | None
    local_name: str | None
    opp_name: str | None
    result: str                      # win | loss | unknown
    turns: int | None
    deck_key: str | None
    opp_cards: tuple[str, ...] = ()
    ended_at: float | None = None


@dataclass(frozen=True)
class Record:
    wins: int = 0
    losses: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses


class HistoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -----------------------------------------------------------

    def record_game(self, rec: GameRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec.match_id, str(rec.game_id), rec.ended_at or time.time(), rec.format,
                 rec.local_name, rec.opp_name, rec.result, rec.turns, rec.deck_key,
                 json.dumps(list(rec.opp_cards)[:60])))
            self._conn.commit()

    # -- reads ------------------------------------------------------------

    def _record(self, where: str, args: tuple, exclude_match: str | None) -> Record:
        sql = f"SELECT result, COUNT(*) FROM games WHERE {where}"
        if exclude_match:
            sql += " AND match_id != ?"
            args = args + (exclude_match,)
        with self._lock:
            rows = dict(self._conn.execute(sql + " GROUP BY result", args).fetchall())
        return Record(rows.get("win", 0), rows.get("loss", 0))

    def vs_opponent(self, opp_name: str, exclude_match: str | None = None) -> Record:
        return self._record("opp_name = ?", (opp_name,), exclude_match)

    def deck_record(self, key: str, exclude_match: str | None = None) -> Record:
        return self._record("deck_key = ?", (key,), exclude_match)

    def streak(self, exclude_match: str | None = None) -> tuple[str | None, int]:
        """(``win``/``loss``, length) of the current run of decided games."""
        sql = "SELECT result FROM games WHERE result != 'unknown'"
        args: tuple = ()
        if exclude_match:
            sql += " AND match_id != ?"
            args = (exclude_match,)
        with self._lock:
            rows = [r[0] for r in self._conn.execute(sql + " ORDER BY ended_at DESC LIMIT 50", args)]
        if not rows:
            return None, 0
        n = 1
        while n < len(rows) and rows[n] == rows[0]:
            n += 1
        return rows[0], n

    def opponent_cards(self, opp_name: str, exclude_match: str | None = None, limit: int = 5) -> list[str]:
        sql = "SELECT opp_cards FROM games WHERE opp_name = ?"
        args: tuple = (opp_name,)
        if exclude_match:
            sql += " AND match_id != ?"
            args += (exclude_match,)
        counts: Counter = Counter()
        with self._lock:
            for (raw,) in self._conn.execute(sql, args):
                try:
                    counts.update(set(json.loads(raw)))
                except (TypeError, ValueError):
                    continue
        return [name for name, _ in counts.most_common(limit)]

    def recent(self, limit: int = 20) -> list[GameRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT match_id, game_id, format, local_name, opp_name, result, turns, deck_key, opp_cards, ended_at "
                "FROM games ORDER BY ended_at DESC LIMIT ?", (limit,)).fetchall()
        return [GameRecord(*r[:8], tuple(json.loads(r[8] or "[]")), r[9]) for r in rows]

    # -- booth-facing summaries -------------------------------------------

    def facts(self, *, match_id, local_name, opp_name, deck) -> dict:
        """``history:*`` facts for the generative booth (current match excluded)."""
        out = {}
        if opp_name:
            rec = self.vs_opponent(opp_name, match_id)
            if rec.games:
                out["history:opponent"] = {
                    "opponent": opp_name, "local_player": local_name,
                    "previous_games": rec.games, "local_wins": rec.wins, "local_losses": rec.losses,
                    "cards_seen_before": self.opponent_cards(opp_name, match_id),
                    "source": "local_match_history"}
        kind, n = self.streak(match_id)
        if kind and n >= 2:
            out["history:streak"] = {"local_player": local_name, "result": kind,
                                     "consecutive_games": n, "source": "local_match_history"}
        key = deck_key(deck)
        if key:
            rec = self.deck_record(key, match_id)
            if rec.games >= 3:
                out["history:deck"] = {"local_player": local_name, "deck_wins": rec.wins,
                                       "deck_losses": rec.losses, "source": "local_match_history"}
        return out

    def intro_line(self, *, match_id, local_name, opp_name, deck) -> str | None:
        """One spoken line for the template booth, or None when nothing is notable."""
        me = local_name or "our player"
        if opp_name:
            rec = self.vs_opponent(opp_name, match_id)
            if rec.games:
                seen = self.opponent_cards(opp_name, match_id, limit=1)
                if rec.wins == rec.losses:
                    score = f"honours even at {_n(rec.wins)} apiece"
                elif rec.wins > rec.losses:
                    score = f"{me} leads {_n(rec.wins)} games to {_n(rec.losses)}"
                else:
                    score = f"{opp_name} leads {_n(rec.losses)} games to {_n(rec.wins)}"
                line = (f"We've seen this pairing before: {me} and {opp_name}, "
                        f"{_n(rec.games)} previous game{'s' if rec.games != 1 else ''}, {score}.")
                if seen:
                    line += f" Last time out, {opp_name} showed us {seen[0]}."
                return line
        kind, n = self.streak(match_id)
        if kind and n >= 3:
            return (f"{me} arrives on a {_n(n)}-game winning streak." if kind == "win"
                    else f"{me} is looking to snap a {_n(n)}-game skid.")
        key = deck_key(deck)
        if key:
            rec = self.deck_record(key, match_id)
            if rec.games >= 3:
                return f"This list has gone {_n(rec.wins)} and {_n(rec.losses)} for {me} so far."
        return None


_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
          "ten", "eleven", "twelve")


def _n(value: int) -> str:
    return _WORDS[value] if 0 <= value < len(_WORDS) else str(value)
