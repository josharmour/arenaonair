"""Fold parsed GRE messages into immutable GameState snapshots.

The builder owns the authoritative working state (zones / objects / players /
turn info / match meta). Every :meth:`GameStateBuilder.apply` call folds one
message into a *staged copy* of that state and, on success, publishes a fresh,
deeply-independent :class:`~arenaonair.models.GameState` snapshot. Previously
returned snapshots are never mutated afterwards (copy-on-write).

Semantics implemented (see docs/DESIGN.md section 3.3):

- ``GameStateType_Full`` resets every authoritative field the message carries.
- ``GameStateType_Diff`` patches:
    * ``zones`` entries REPLACE that zone's membership when they carry
      ``objectInstanceIds``; entries without ids only refresh zone metadata.
    * ``gameObjects`` MERGE by ``instanceId`` (present fields overwrite).
    * ``diffDeletedInstanceIds`` REMOVE objects (and their zone membership).
    * ``players`` PATCH by ``systemSeatNumber``.
    * ``turnInfo`` replaces wholesale when present.
    * ``prevGameStateId`` chains to the prior snapshot via ``prev_snapshot_id``.
- ``room_state.*`` messages populate match identity (match id, format,
  seat -> player name).

Robustness contract: a malformed or partial payload never raises past
:meth:`apply`; an uninterpretable message yields ``None`` (= skip).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

from .models import (
    CardRef,
    GameState,
    GreMessage,
    MatchMeta,
    PlayerView,
    TurnInfo,
    ZoneView,
)

__all__ = ["GameStateBuilder"]

# A callable mapping grpId -> display card name (or None when unknown).
NameResolver = Callable[[int], "str | None"]


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def _unwrap_stat(raw: Any) -> "int | None":
    """GRE stats arrive as ``{"value": N}`` wrappers; tolerate plain ints."""
    if isinstance(raw, Mapping):
        raw = raw.get("value")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _as_int(value: Any) -> "int | None":
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _clean_type_line(parts: list) -> "str | None":
    joined = " ".join(p for p in parts if p)
    joined = joined.replace("--", "\u2014")
    joined = re.sub(r"\s*\u2014\s*", " \u2014 ", joined)
    joined = " ".join(joined.split())
    return joined or None


def _title(word: str) -> str:
    return word[:1].upper() + word[1:] if word else word


class _WorkingZone:
    """Mutable per-zone bookkeeping between snapshots."""

    __slots__ = ("zone_id", "zone_type", "owner_seat", "object_ids")

    def __init__(self, zone_id, zone_type, owner_seat, object_ids):
        self.zone_id = zone_id
        self.zone_type = zone_type
        self.owner_seat = owner_seat
        self.object_ids = object_ids

    def clone(self):
        return _WorkingZone(self.zone_id, self.zone_type, self.owner_seat,
                            list(self.object_ids))

    def key(self):
        owner = self.owner_seat if self.owner_seat is not None else "pub"
        return "%s:%s" % (self.zone_type, owner)


class GameStateBuilder:
    """Accumulates GRE messages into immutable GameState snapshots."""

    def __init__(self, name_resolver=None):
        self._name_resolver = name_resolver

        # Authoritative working state ------------------------------------------
        self._zones_by_id = {}
        self._objects = {}
        self._players = {}
        self._turn_info_raw = {}
        self._meta_match_id = None
        self._meta_format = None
        self._meta_names = {}

        self._snapshot_seq = 0          # last assigned snapshot_id
        self._last_prev_gre_id = None

    # ------------------------------------------------------------------ public API

    @staticmethod
    def _unwrap_payload(payload):
        """Accept either payload convention for GameStateMessage.

        gre_parser emits the per-message object (inner content nested under
        'gameStateMessage'); direct GreMessage construction may pass the inner
        dict itself. Normalize to the inner dict here.
        """
        if isinstance(payload, Mapping) and "gameStateMessage" in payload \
                and payload.get("type") not in ("GameStateType_Full",
                                                "GameStateType_Diff"):
            inner = payload.get("gameStateMessage")
            if isinstance(inner, Mapping):
                return inner
        return payload

    def apply(self, state, msg):
        """Fold one message; return the next snapshot or None to skip.

        ``state`` is accepted per the StateBuilder protocol but the builder's
        internal working state remains authoritative; callers may pass None.
        """
        if not isinstance(msg, GreMessage):
            return None
        kind = msg.kind or ""
        payload = msg.payload

        try:
            if kind.startswith("room_state"):
                match_id, fmt, names = self._parse_room_state(payload)
                if match_id is not None:
                    self._meta_match_id = match_id
                if fmt is not None:
                    self._meta_format = fmt
                if names:
                    merged = dict(self._meta_names)
                    merged.update(names)
                    self._meta_names = merged
                return self._publish(self._last_prev_gre_id)

            if kind == "gre.GameStateMessage":
                return self._fold_game_state(self._unwrap_payload(payload))

            # A queued game-state message wraps an inner gameStateMessage.
            if "QueuedGameStateMessage" in kind and isinstance(payload, Mapping):
                inner = payload.get("gameStateMessage")
                if isinstance(inner, Mapping):
                    return self._fold_game_state(self._unwrap_payload(inner))
                return None

            return None  # UIMessage / reqs / resp / timers / unknown kinds: skip
        except Exception:
            # Robustness contract: never raise past apply(); staging means a
            # failed fold leaves committed working state untouched.
            return None

    def replay(self, messages):
        """Fold an ordered message stream; one snapshot per applied message."""
        snapshots = []
        current = None
        for msg in messages:
            nxt = self.apply(current, msg)
            if nxt is not None:
                snapshots.append(nxt)
                current = nxt
        return snapshots

    # ------------------------------------------------------------ message handlers

    @staticmethod
    def _parse_room_state(payload):
        """Extract (match_id, format_name, seat->name) from a room_state payload."""
        match_id = None
        fmt = None
        names = {}
        if not isinstance(payload, Mapping):
            return match_id, fmt, names
        info = payload.get("matchGameRoomStateChangedEvent")
        if not isinstance(info, Mapping):
            info = payload  # tolerate pre-unwrapped gameRoomInfo dicts
        room = info.get("gameRoomInfo")
        if not isinstance(room, Mapping):
            # gre_parser emits gameRoomInfo itself as the payload.
            room = info if isinstance(info.get("gameRoomConfig"), Mapping) else None
        if not isinstance(room, Mapping):
            return match_id, fmt, names
        config = room.get("gameRoomConfig")
        if not isinstance(config, Mapping):
            return match_id, fmt, names
        mid = config.get("matchId")
        if isinstance(mid, str) and mid:
            match_id = mid
        eid = config.get("eventId")
        if isinstance(eid, str) and eid:
            fmt = eid
        for rp in config.get("reservedPlayers") or []:
            if not isinstance(rp, Mapping):
                continue
            seat = _as_int(rp.get("systemSeatId"))
            name = rp.get("playerName")
            if seat is not None and isinstance(name, str) and name:
                names[seat] = name
            if fmt is None and isinstance(rp.get("eventId"), str) and rp["eventId"]:
                fmt = rp["eventId"]
        return match_id, fmt, names

    def _fold_game_state(self, gsm):
        if not isinstance(gsm, Mapping):
            return None
        gstype = gsm.get("type")
        full = gstype == "GameStateType_Full"
        diff = gstype == "GameStateType_Diff"
        if not (full or diff):
            return None

        # Stage copies; commit only after a clean fold ---------------------------
        zones_by_id = {zid: z.clone() for zid, z in self._zones_by_id.items()}
        objects = {iid: o.copy() for iid, o in self._objects.items()}
        players = {seat: p.copy() for seat, p in self._players.items()}
        turn_raw = dict(self._turn_info_raw)
        prev_gre = self._last_prev_gre_id

        msg_zones = gsm.get("zones")
        msg_objects = gsm.get("gameObjects")
        msg_players = gsm.get("players")

        if full:
            # Reset every authoritative field the message carries.
            zones_by_id.clear()
            if isinstance(msg_objects, list):
                objects.clear()
            else:
                msg_objects = None  # keep prior objects when Full omits them
            if isinstance(msg_players, list):
                players.clear()
            else:
                msg_players = None
            ti_full = gsm.get("turnInfo")
            turn_raw = dict(ti_full) if isinstance(ti_full, Mapping) else {}
        else:
            prev_val = _as_int(gsm.get("prevGameStateId"))
            if prev_val is not None:
                prev_gre = prev_val

        # Zones --------------------------------------------------------------------
        if isinstance(msg_zones, list):
            for z in msg_zones:
                if not isinstance(z, Mapping):
                    continue
                zid_raw = z.get("zoneId")
                zid = _as_int(zid_raw)
                ztype_raw = z.get("type")
                ztype_str = (_strip_prefix(ztype_raw, "ZoneType_").lower()
                             if isinstance(ztype_raw, str) else "")
                owner_raw = z.get("ownerSeatId")
                owner = _as_int(owner_raw)
                key_owner = owner if owner is not None else "pub"
                key = "%s:%s" % (ztype_str, key_owner)

                existing = None
                if zid is not None:
                    existing = zones_by_id.get(zid)
                elif ztype_str:
                    for wz in zones_by_id.values():
                        if wz.zone_id is None and wz.key() == key:
                            existing = wz
                            break

                ids_present = ("objectInstanceIds" in z
                               and isinstance(z.get("objectInstanceIds"), list))
                ids_value = list(z["objectInstanceIds"]) if ids_present else []

                if existing is None:
                    wz = _WorkingZone(zid, ztype_str, owner, ids_value)
                    if zid is not None:
                        zones_by_id[zid] = wz
                    elif ztype_str:
                        synth = min(zones_by_id.keys(), default=0) - 1
                        wz.zone_id = synth
                        zones_by_id[synth] = wz
                else:
                    if ztype_str:
                        existing.zone_type = ztype_str
                    if owner is not None:
                        existing.owner_seat = owner
                    if zid is not None:
                        existing.zone_id = zid
                    # Membership REPLACES only when ids are reported.
                    if ids_present:
                        existing.object_ids = ids_value

        # Objects -------------------------------------------------------------------
        if isinstance(msg_objects, list):
            for go in msg_objects:
                if not isinstance(go, Mapping):
                    continue
                iid = _as_int(go.get("instanceId"))
                if iid is None:
                    continue
                base = objects.get(iid)
                merged = dict(base) if base else {}
                merged.update(go)
                objects[iid] = merged

        # Deletions -----------------------------------------------------------------
        deleted = gsm.get("diffDeletedInstanceIds")
        if isinstance(deleted, list):
            gone = {_as_int(x) for x in deleted}
            gone.discard(None)
            for iid in gone:
                objects.pop(iid, None)
            for wz in zones_by_id.values():
                wz.object_ids = [i for i in wz.object_ids if i not in gone]

        # Players -------------------------------------------------------------------
        if isinstance(msg_players, list):
            for p in msg_players:
                if not isinstance(p, Mapping):
                    continue
                seat = _as_int(p.get("systemSeatNumber"))
                if seat is None:
                    continue
                base = players.get(seat)
                merged = dict(base) if base else {}
                merged.update(p)
                players[seat] = merged

        # Turn info -----------------------------------------------------------------
        ti = gsm.get("turnInfo")
        if diff and isinstance(ti, Mapping):
            turn_raw = dict(ti)

        # Commit ----------------------------------------------------------------------
        self._zones_by_id = zones_by_id
        self._objects = objects
        self._players = players
        self._turn_info_raw = turn_raw
        self._last_prev_gre_id = prev_gre
        return self._publish(prev_gre)

    # ------------------------------------------------------------- snapshot build

    def _publish(self, prev_gre=None):
        self._snapshot_seq += 1

        zones = {}
        hand_sizes = {}
        for wz in self._zones_by_id.values():
            view = ZoneView(
                zone_id=wz.zone_id if wz.zone_id is not None else -1,
                zone_type=wz.zone_type,
                owner_seat=wz.owner_seat,
                object_ids=tuple(wz.object_ids),
            )
            zones[wz.key()] = view
            if wz.zone_type == "hand" and wz.owner_seat is not None:
                hand_sizes[wz.owner_seat] = len(wz.object_ids)

        objects_out = {}
        for iid, raw in self._objects.items():
            card_ref = self._build_card_ref(iid, raw)
            if card_ref is not None:
                objects_out[iid] = card_ref

        players_out = {}
        for seat, raw in self._players.items():
            life = _unwrap_stat(raw.get("lifeTotal"))
            starting_life = _unwrap_stat(raw.get("startingLifeTotal"))
            max_hand = _unwrap_stat(raw.get("maxHandSize"))
            hs_key = hand_sizes.get(seat)
            players_out[seat] = PlayerView(
                seat=seat,
                life=life,
                starting_life=starting_life,
                max_hand_size=max_hand,
                hand_size=hs_key,
            )

        turn_number = _unwrap_stat(self._turn_info_raw.get("turnNumber"))
        ap_raw = self._turn_info_raw.get("activePlayer")
        active_player = _as_int(ap_raw)
        phase_raw = self._turn_info_raw.get("phase")
        phase_step_raw = self._turn_info_raw.get("step")
        phase_parts = []
        if isinstance(phase_raw, str) and phase_raw:
            phase_parts.append(_strip_prefix(phase_raw, "Phase_").lower())
        if isinstance(phase_step_raw, str) and phase_step_raw:
            phase_parts.append(_strip_prefix(phase_step_raw, "Step_").lower())
        phase_combined = "/".join(phase_parts) or None

        turn_info = TurnInfo(
            turn_number=turn_number,
            active_player=active_player,
            phase=phase_combined,
        )

        match_meta = MatchMeta(
            match_id=self._meta_match_id,
            format_name=self._meta_format,
            player_names=dict(self._meta_names),
        )

        return GameState(
            snapshot_id=self._snapshot_seq,
            prev_snapshot_id=prev_gre,
            zones=zones,
            objects=objects_out,
            players=players_out,
            turn_info=turn_info,
            match_meta=match_meta,
            local_seat=None,
        )

    # --------------------------------------------------------------- object views

    def _build_card_ref(self, iid, raw):
        grp_id = _as_int(raw.get("grpId"))

        ctypes_raw = raw.get("cardTypes")
        card_types_list = []
        if isinstance(ctypes_raw, list):
            for t in ctypes_raw:
                if isinstance(t, str) and t:
                    card_types_list.append(
                        _strip_prefix(t.lower(), "cardtype_"))
        card_types_tuple = tuple(card_types_list)

        supers_raw = raw.get("superTypes")
        subs_raw = raw.get("subtypes")
        super_words = []
        if isinstance(supers_raw, list):
            for s in supers_raw:
                if isinstance(s, str) and s:
                    super_words.append(
                        _title(_strip_prefix(s.lower(), "supertype_")))
        sub_words = []
        if isinstance(subs_raw, list):
            for s in subs_raw:
                if isinstance(s, str) and s:
                    sub_words.append(
                        _title(_strip_prefix(s.lower(), "subtype_")))

        type_line_parts: list[str] = []
        if super_words:
            type_line_parts.append(" ".join(super_words))
        type_line_parts.extend(_title(t) for t in card_types_list)
        if sub_words:
            type_line_parts.append("\u2014")
            type_line_parts.extend(sub_words)
        elif card_types_list:
            type_line_parts.append("\u2014")
        type_line_cleaned: "str | None" = _clean_type_line(type_line_parts)

        name: "str | None" = None
        resolver = self._name_resolver
        if resolver is not None and grp_id is not None:
            try:
                resolved_name = resolver(grp_id)
            except Exception:
                resolved_name = None
            if isinstance(resolved_name, str) and resolved_name:
                name = resolved_name

        return CardRef(
            instance_id=iid,
            grp_id=grp_id,
            name=name,
            type_line=type_line_cleaned,
            card_types=card_types_tuple,
            power=_unwrap_stat(raw.get("power")),
            toughness=_unwrap_stat(raw.get("toughness")),
            loyalty=_unwrap_stat(raw.get("loyalty")),
            controller_seat=_as_int(raw.get("controllerSeatId")),
            owner_seat=_as_int(raw.get("ownerSeatId")),
        )


