"""Per-match public record: the input for recaps and cross-match memory.

The recorder sees every detected event (before verbosity gating) plus every
successfully spoken line. It keeps PUBLIC events only -- no hand, deck or
other private detector output -- and writes one JSON file per match under
``<data_dir>/matches`` (mode 0600). Finished games are also folded into the
:class:`~arenaonair.history.HistoryStore`.

Never raises into the pipeline: every public method swallows and logs.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from . import events as ev
from .history import GameRecord, HistoryStore, deck_key

logger = logging.getLogger(__name__)

#: Detector kinds that depend on private (hand/deck) knowledge.
PRIVATE_KINDS = frozenset({
    ev.HAND_ONLINE, ev.TUTOR_ANTICIPATION, ev.OUTS_ANTICIPATION, ev.TRAP_ARMED,
    ev.TRAP_SPRUNG, ev.BLUFF_DETECTED, ev.CLASH_OF_OUTS, ev.ARCHETYPE_DETECTED,
})
_KEEP_FIELDS = ("name", "grp_id", "instance_id", "from", "to", "delta", "amount", "count",
                "target_seat", "countered_by_name", "countered_by_seat", "winning_team_id",
                "reason", "arc", "from_arc", "to_arc", "streak_kind", "length", "attackers")
MAX_EVENTS_PER_GAME = 1500
MAX_MATCH_FILES = 200


def _safe_name(match_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(match_id))[:80] or "match"


def _compact(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:200] if isinstance(value, str) else value
    if isinstance(value, (list, tuple)):
        return [_compact(v) for v in value[:12] if isinstance(v, (str, int, float, dict))]
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in list(value.items())[:8]
                if k in ("name", "instance_id", "power") and isinstance(v, (str, int))}
    return None


def write_private_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


class MatchRecorder:
    def __init__(self, data_dir: str | Path, *, history: HistoryStore | None = None,
                 on_game_saved=None, clock=time.time):
        self.dir = Path(data_dir).expanduser() / "matches"
        self.history = history
        self.on_game_saved = on_game_saved      # callable(match_dict, game_dict)
        self.clock = clock
        self._lock = threading.RLock()
        self.match: dict | None = None
        self._game: dict | None = None
        self._intro_done: set = set()

    # -- identity helpers ---------------------------------------------------

    @staticmethod
    def _seats(snap):
        local = snap.local_seat
        opp = next((s for s in sorted(snap.players) if s != local), None) if local is not None else None
        return local, opp

    def _ensure_match(self, snap) -> dict | None:
        match_id = getattr(snap.match_meta, "match_id", None)
        if not match_id:
            return None
        if self.match is None or self.match["match_id"] != str(match_id):
            self.finish_match()
            self.match = {"match_id": str(match_id), "format": snap.match_meta.format_name,
                          "started_at": self.clock(), "local_seat": None, "names": {},
                          "deck_key": None, "games": [], "transcript": []}
            self._game = None
        m = self.match
        names = {str(k): v for k, v in dict(snap.match_meta.player_names).items() if v}
        if names:
            m["names"].update(names)
        if snap.local_seat is not None:
            m["local_seat"] = snap.local_seat
        if snap.player_deck and not m["deck_key"]:
            m["deck_key"] = deck_key(snap.player_deck)
            m["_deck"] = list(snap.player_deck)
        if m["format"] is None and snap.match_meta.format_name:
            m["format"] = snap.match_meta.format_name
        return m

    def _ensure_game(self, snap) -> dict:
        game_id = str(snap.game_id) if snap.game_id is not None else "1"
        if self._game is None or self._game["game_id"] != game_id:
            self._game = {"game_id": game_id, "events": [], "winner_seat": None, "result": "unknown",
                          "turns": None, "final_life": {}, "opp_cards": []}
            self.match["games"].append(self._game)
        return self._game

    # -- pipeline hooks -------------------------------------------------------

    def observe(self, event, snap) -> None:
        try:
            with self._lock:
                if self._ensure_match(snap) is None:
                    return
                game = self._ensure_game(snap)
                if game.get("closed") and event.kind != ev.MATCH_END:
                    return
                if event.kind not in PRIVATE_KINDS and len(game["events"]) < MAX_EVENTS_PER_GAME:
                    game["events"].append({
                        "kind": event.kind, "seat": event.seat, "turn": snap.turn_info.turn_number,
                        "salience": event.salience,
                        "data": {k: _compact(event.payload[k]) for k in _KEEP_FIELDS if k in event.payload}})
                local, opp = self._seats(snap)
                name = event.payload.get("name") if hasattr(event.payload, "get") else None
                if (opp is not None and event.seat == opp and name and event.kind in (ev.CAST, ev.LAND_DROP)
                        and name not in game["opp_cards"]):
                    game["opp_cards"].append(name)
                game["turns"] = snap.turn_info.turn_number
                game["final_life"] = {str(s): p.life for s, p in snap.players.items()}
                if event.kind == ev.GAME_END:
                    self._close_game(game, event, snap)
        except Exception:
            logger.warning("match recorder failed to record %s", getattr(event, "kind", "?"), exc_info=True)

    def spoken(self, result, utt) -> None:
        if not getattr(result, "ok", False):
            return
        try:
            with self._lock:
                if self.match is None or utt.match_id != self.match["match_id"]:
                    return
                self.match["transcript"].append({"t": round(self.clock() - self.match["started_at"], 2),
                                                 "role": utt.role, "text": utt.text})
        except Exception:
            logger.debug("transcript append failed", exc_info=True)

    def _close_game(self, game, event, snap) -> None:
        local, opp = self._seats(snap)
        winner = event.payload.get("winning_team_id")
        # Arena 1v1 team ids equal the seat ids.
        game["winner_seat"] = winner
        if winner is not None and local is not None:
            game["result"] = "win" if winner == local else "loss"
        game["closed"] = True
        m = self.match
        names = m["names"]
        # Without the local seat the record can't say who "you" were: keep the
        # match file for recaps but leave cross-match memory untouched.
        if self.history is not None and local is not None:
            self.history.record_game(GameRecord(
                m["match_id"], game["game_id"], m["format"],
                names.get(str(local)), names.get(str(opp)) if opp is not None else None,
                game["result"], game["turns"], m["deck_key"], tuple(game["opp_cards"]), self.clock()))
        self.save()
        if self.on_game_saved is not None:
            try:
                self.on_game_saved(self.public_match(), dict(game))
            except Exception:
                logger.warning("post-game hook failed", exc_info=True)

    # -- history-facing -------------------------------------------------------

    def _who(self, snap):
        local, opp = self._seats(snap)
        names = dict(snap.match_meta.player_names)
        return names.get(local), names.get(opp) if opp is not None else None

    def history_facts(self, snap) -> dict:
        if self.history is None or not snap.match_meta.match_id:
            return {}
        local_name, opp_name = self._who(snap)
        return self.history.facts(match_id=str(snap.match_meta.match_id), local_name=local_name,
                                  opp_name=opp_name, deck=snap.player_deck)

    def history_intro(self, snap) -> str | None:
        """The template booth's once-per-match memory line (None if nothing notable)."""
        if self.history is None:
            return None
        match_id = snap.match_meta.match_id
        if not match_id or match_id in self._intro_done:
            return None
        local_name, opp_name = self._who(snap)
        if not opp_name:
            return None              # wait until the pairing is known
        self._intro_done.add(match_id)
        try:
            return self.history.intro_line(match_id=str(match_id), local_name=local_name,
                                           opp_name=opp_name, deck=snap.player_deck)
        except Exception:
            logger.debug("history intro failed", exc_info=True)
            return None

    # -- persistence ----------------------------------------------------------

    def public_match(self) -> dict:
        with self._lock:
            m = dict(self.match or {})
            m.pop("_deck", None)
            return json.loads(json.dumps(m, default=str))

    def save(self) -> Path | None:
        with self._lock:
            if self.match is None:
                return None
            path = self.dir / (_safe_name(self.match["match_id"]) + ".json")
            try:
                write_private_json(path, self.public_match())
                self._prune()
            except OSError:
                logger.warning("could not save match record %s", path, exc_info=True)
                return None
            return path

    def _prune(self) -> None:
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-MAX_MATCH_FILES]:
            try:
                old.unlink()
            except OSError:
                pass

    def finish_match(self) -> None:
        with self._lock:
            if self.match is not None and any(g["events"] for g in self.match["games"]):
                self.save()
            self.match = None
            self._game = None


def latest_match_file(data_dir: str | Path) -> Path | None:
    files = sorted((Path(data_dir).expanduser() / "matches").glob("*.json"),
                   key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None
