"""Shared tagged-input primitives for dual-source ingestion (S8.x).

Every message entering the fusion layer travels as a :class:`TaggedMessage` --
a frozen ``(tag, msg)`` pair whose :class:`SourceTag` records WHERE the message
came from (source id), WHICH transport generation produced it (session_gen --
bumped on every reconnect so post-reconnect bursts are distinguishable from
pre-drop replays), the producer-side sequence number within that generation,
and a receiver-side timestamp (:attr:`recv_ts`, informational -- ordering /
deadlines are always measured on ``time.monotonic()`` by consumers).

Identity rules encoded here deliberately:

- ``source_tag.source_id`` NEVER establishes GRE player identity or seating.
  Seat resolution comes exclusively from validated session metadata carried
  inside the stream itself (room_state / ConnectResp folded by the builders).
- ``session_gen`` bumps invalidate sequence floors rather than identities;
  adopting a higher generation resets duplicate suppression for that source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "SourceTag",
    "TaggedMessage",
    "SourceStatus",
    "SourceRegistry",
]


@dataclass(frozen=True)
class SourceTag:

    """Provenance stamp attached to every message entering fusion."""

    source_id: int          # logical slot index (0 | 1); NOT a seat number
    session_gen: int        # transport generation; bumped on each reconnect
    recv_seq: int           # producer-side sequence within this generation
    recv_ts: float          # receiver ts at ingress -- INFORMATIONAL only


@dataclass(frozen=True)
class TaggedMessage:

    """Fusion input wrapper pairing provenance with the decoded message."""

    tag: SourceTag
    msg: Any                # typically arenaonair.models.GreMessage


@dataclass(frozen=True)
class SourceStatus:

    """Point-in-time health/knowledge posture of one configured source."""

    source_id: int
    transport_ok: bool              # messages flowing recently / link alive
    chain_valid: bool               # GRE linkage continuity trusted this game
    baseline_ok: bool               # identity established + baseline folded
    last_recv_ts: float             # recv_ts of newest accepted message (-inf none)
    last_state_progress_ts: float   # recv_ts of newest state-progress message


_NO_TS = float("-inf")


def _blank_record() -> dict:

    return {
        "transport_ok": False,
        "chain_valid": False,
        "baseline_ok": False,
        "last_recv_ts": _NO_TS,
        "last_state_progress_ts": _NO_TS,
        "session_gen": -1,
        "recv_seq": -1,
    }


_STATE_PROGRESS_KIND_PREFIXES = (
    "gre.GameStateMessage",
    "room_state.",
)


class SourceRegistry:

    """Tracks per-source status evolution driven by ingestion events.

    Duplicate/replay suppression lives here so every consumer shares one
    policy:

    - a message whose ``session_gen`` is OLDER than the tracked generation is
      a stale pre-reconnect replay -> rejected;
    - within the current generation ``recv_seq`` must strictly increase ->
      duplicates/out-of-order replays rejected;
    - a NEWER generation is adopted outright and reseeds the sequence floor.
      Generation bumps may arrive either producer-side (forwarder stamps its
      own reconnect counter into tags) or operator-side via
      :meth:`mark_recovered`; both are tolerated symmetrically.
    """

    def __init__(self) -> None:

        self._recs: dict[int, dict] = {}

    # ------------------------------------------------------------------ #

    def _record(self, source_id: int) -> dict:

        rec = self._recs.get(source_id)
        if rec is None:
            rec = _blank_record()
            self._recs[source_id] = rec
        return rec

    def mark_message(self, tag: SourceTag, *, progressed: bool = True) -> bool:

        """Account one inbound tagged message; True when accepted as new.

        ``progressed`` marks state-progress traffic (game states / room
        transitions) versus inert chatter; only progress advances
        ``last_state_progress_ts``.
        """

        rec = self._record(tag.source_id)

        if tag.session_gen < rec["session_gen"]:
            return False                     # stale pre-reconnect replay

        if tag.session_gen == rec["session_gen"]:
            if tag.recv_seq <= rec["recv_seq"]:
                return False                 # duplicate / out-of-order replay

        rec["session_gen"] = tag.session_gen
        rec["recv_seq"] = tag.recv_seq

        rec["transport_ok"] = True
        rec["last_recv_ts"] = tag.recv_ts

        if progressed and tag.recv_ts > rec["last_state_progress_ts"]:
            rec["last_state_progress_ts"] = tag.recv_ts

        return True

    def mark_disconnected(self, source_id: int) -> None:

        """Transport dropped -- flags lower WITHOUT touching stream truths."""

        rec = self._record(source_id)
        rec["transport_ok"] = False

    def mark_recovered(self, source_id: int) -> int:

        """Link restored -- bump session generation so old replays die.

        Returns the NEW generation (-1 -> 0 on first contact).
        """

        rec = self._record(source_id)
        rec["session_gen"] += 1
        rec["recv_seq"] = -1                 # fresh sequence floor next gen
        return rec["session_gen"]

    def status(self, source_id: int) -> SourceStatus:

        """Materialize the frozen point-in-time posture of one source."""

        rec = self._record(source_id)
        return SourceStatus(
            source_id=source_id,
            transport_ok=bool(rec["transport_ok"]),
            chain_valid=bool(rec["chain_valid"]),
            baseline_ok=bool(rec["baseline_ok"]),
            last_recv_ts=rec["last_recv_ts"],
            last_state_progress_ts=rec["last_state_progress_ts"],
        )

    def set_flags(
        self,
        source_id: int,
        *,
        chain_valid: bool | None = None,
        baseline_ok: bool | None = None,
    ) -> None:

        """Operator-settable validation flags (fusion layer drives these)."""

        rec = self._record(source_id)
        if chain_valid is not None:
            rec["chain_valid"] = chain_valid
        if baseline_ok is not None:
            rec["baseline_ok"] = baseline_ok

    def is_state_progress(self, msg_kind: str | None) -> bool:

        """Classify a message kind as state-progress traffic."""

        kind = msg_kind or ""
        return any(kind.startswith(p) for p in _STATE_PROGRESS_KIND_PREFIXES)
