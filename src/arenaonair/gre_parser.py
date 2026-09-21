"""Pure-function parser: MTGA Player.log lines -> GreMessage objects.

Player.log lines that matter are self-contained JSON objects (one per line,
emitted after a ``[UnityCrossThreadLogger]`` header line). Two envelope
families are recognized:

1. Room-state envelopes -- top-level JSON with key
   ``matchGameRoomStateChangedEvent`` whose ``gameRoomInfo`` carries
   ``matchId``, ``stateType`` (e.g. ``MatchGameRoomStateType_Playing``) and
   ``players``. Classified as ``room_state.<suffix>`` where suffix is the
   stateType minus the ``MatchGameRoomStateType_`` prefix, lowercased
   (``MatchGameRoomStateType_Playing`` -> ``room_state.playing``). The
   payload is the ``gameRoomInfo`` dict.

2. GRE envelopes -- top-level JSON with key ``greToClientEvent`` containing
   ``greToClientMessages``, a list of per-message objects each with a
   ``type`` like ``GREMessageType_GameStateMessage``. One log line can hold
   several messages; :func:`parse_line_all` returns every decodable message
   in order while :func:`parse_line` (the frozen
   ``arenaonair.interfaces.Parser`` seam) returns only the first.

Anything else -- plain text, Unity engine noise, truncated or unparseable
JSON -- yields ``None`` / ``[]``. These functions never raise for any input.

Timestamp precedence: the envelope's integer-string ``timestamp`` field
(milliseconds since epoch) wins when present and parseable; otherwise the
caller-supplied ``ts`` is used unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from .models import GreMessage

_ROOM_EVENT_KEY = "matchGameRoomStateChangedEvent"
_GRE_EVENT_KEY = "greToClientEvent"
_MESSAGES_KEY = "greToClientMessages"
_STATE_TYPE_KEY = "stateType"
_ROOM_STATE_PREFIX = "MatchGameRoomStateType_"
_GRE_TYPE_PREFIX = "GREMessageType_"


def _strip_prefix(value: str, prefix: str) -> str:
    """Remove ``prefix`` from ``value`` when present; otherwise keep as-is."""
    if value.startswith(prefix):
        return value[len(prefix):]
    return value


def _resolve_ts(envelope: dict[str, Any], fallback: float) -> float:
    """Envelope millisecond ``timestamp`` (int or digit string) -> seconds."""
    raw_ts = envelope.get("timestamp")
    try:
        return float(int(str(raw_ts))) / 1000.0
    except (TypeError, ValueError):
        return fallback


def _decode_json(raw: str) -> Any | None:
    """Best-effort JSON decode of one log line. Returns None on any failure.

    Tolerates a non-JSON prefix (e.g. a logger header glued onto the line)
    by retrying from the first ``{``.
    """
    if not isinstance(raw, str):
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        else:
            raw = str(raw)
    text = raw.strip()
    if not text:
        return None
    candidate = text
    brace = text.find("{")
    if brace > 0:
        candidate = text[brace:]
    try:
        return json.loads(candidate)
    except (ValueError, RecursionError):
        return None


def _classify_room_state(envelope: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return ``(kind, gameRoomInfo)`` for a room-state envelope, or None."""
    event = envelope.get(_ROOM_EVENT_KEY)
    if not isinstance(event, dict):
        return None
    game_room_info = event.get("gameRoomInfo")
    if not isinstance(game_room_info, dict):
        return None
    state_type = game_room_info.get(_STATE_TYPE_KEY)
    if not isinstance(state_type, str) or not state_type:
        return None
    suffix = _strip_prefix(state_type, _ROOM_STATE_PREFIX).lower()
    return f"room_state.{suffix}", game_room_info


def _classify_gre_message(message: Any) -> tuple[str, dict[str, Any]] | None:
    """Return ``(kind, payload)`` for one greToClientMessages entry, or None."""
    if not isinstance(message, dict):
        return None
    msg_type = message.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        return None
    suffix = _strip_prefix(msg_type, _GRE_TYPE_PREFIX)
    payload = dict(message)
    payload["type"] = suffix
    return f"gre.{suffix}", payload


def parse_line_all(ts: float, raw: str) -> list[GreMessage]:
    """Decode every message carried by one log line, in order.

    Pure and total: any input string produces a list (possibly empty) and
    never raises.
    """
    envelope = _decode_json(raw)
    if not isinstance(envelope, dict):
        return []

    if _ROOM_EVENT_KEY in envelope:
        classified = _classify_room_state(envelope)
        if classified is None:
            return []
        kind, payload = classified
        resolved = _resolve_ts(envelope, ts)
        return [
            GreMessage(
                kind=kind,
                payload=payload,
                ts=resolved,
                raw_len=len(raw),
            )
        ]

    if _GRE_EVENT_KEY in envelope:
        event = envelope[_GRE_EVENT_KEY]
        messages = event.get(_MESSAGES_KEY) if isinstance(event, dict) else None
        if not isinstance(messages, list):
            return []
        resolved = _resolve_ts(envelope, ts)
        out: list[GreMessage] = []
        for message in messages:
            classified = _classify_gre_message(message)
            if classified is None:
                continue
            kind, payload = classified
            out.append(
                GreMessage(
                    kind=kind,
                    payload=payload,
                    ts=resolved,
                    raw_len=len(raw),
                )
            )
        return out

    return []


def parse_line(ts: float, raw: str) -> GreMessage | None:
    """Frozen ``Parser`` seam: one log line in -> zero or one message out.

    Returns the FIRST decodable message on the line (a GRE envelope may hold
    several; the watcher pipeline uses :func:`parse_line_all` for those).
    Never raises for any input.
    """
    messages = parse_line_all(ts, raw)
    return messages[0] if messages else None
