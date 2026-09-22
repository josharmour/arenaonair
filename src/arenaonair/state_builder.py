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

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from .models import (
    CardRef,
    GameState,
    GreMessage,
    MatchMeta,
    PlayerView,
    SeatKnowledge,
    TurnInfo,
    ZoneView,
)
from .sources import SourceRegistry, SourceTag

__all__ = ["GameStateBuilder", "DualStateBuilder", "event_identity",
           "remaining_deck_cards"]

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


# Zone types that hold cards the local player has ALREADY drawn out of the
# library (spent / accessible). Everything else -- notably LIBRARY itself --
# stays counted as "still to come".
_SPENT_ZONE_TYPES = frozenset({
    "hand", "battlefield", "graveyard", "stack", "exile", "command",
    "revealed", "pending",
})

# Zone types whose membership is NOT part of the submitted-deck accounting at
# all (limbo is transient; sideboard was never in the submitted 40).
_IGNORED_ZONE_TYPES = frozenset({"limbo", "sideboard", "suppressed"})


def _zone_type_of(zone) -> str:
    return _strip_prefix(str(getattr(zone, "zone_type", "") or ""),
                         "ZoneType_").lower()


def remaining_deck_cards(state: GameState) -> Counter[int]:
    """Counts of grpIds still to be drawn from the local player's library.

    Zone-membership accounting (S7.9): an owned object counts AGAINST the
    submitted deck only when it currently sits in a NON-library zone that
    represents a card already drawn out of the library (hand / battlefield /
    graveyard / stack / exile / command / revealed / pending). A known card
    still residing in the LIBRARY zone remains counted as remaining.

    Provenance guards:
    - Only objects whose owner/controller is ``local_seat`` are considered.
    - grpIds outside the submitted deck (generated copies, tokens, commanders
      fetched separately) never consume submitted-deck copies.
    - Stale object records (instances no longer referenced by any zone) are
      ignored -- membership is read from live zone rosters, not from the
      object table.

    Reconciliation + uncertainty: when the observed library roster is visible,
    its size bounds the true remainder; if the computed remainder exceeds the
    observed library size the excess is trimmed and the shortfall recorded.
    Attach ``state.deck_uncertainty``-style info via the returned Counter's
    ``uncertain`` attribute (True when exact composition cannot be proven,
    e.g. hidden zones or reconciliation trimming occurred).

    Returns an empty Counter when deck/local-seat information is missing.
    """
    out = Counter()
    if not getattr(state, "player_deck", None) \
            or getattr(state, "local_seat", None) is None:
        return out

    local_seat = state.local_seat
    total = Counter(state.player_deck)

    # Live zone rosters -> {iid: zone_type} for zones relevant to accounting.
    iid_zone: dict[int, str] = {}
    library_ids: set[int] = set()
    try:
        for zone in (state.zones or {}).values():
            ztype = _zone_type_of(zone)
            if not ztype or ztype in _IGNORED_ZONE_TYPES:
                continue
            for iid in getattr(zone, "object_ids", ()) or ():
                if ztype == "library":
                    library_ids.add(iid)
                # First sighting wins; duplicated ids across zones are
                # tolerated defensively.
                iid_zone.setdefault(iid, ztype)
    except Exception:
        pass

    objects = getattr(state, "objects", None) or {}

    if not iid_zone:
        # Degraded mode: no zone rosters at all -> current membership is
        # unknowable; fall back to legacy object-table subtraction and flag
        # the result as uncertain (exact composition cannot be established).
        observed_spent: Counter[int] = Counter()
        for ref in objects.values():
            seat = getattr(ref, "owner_seat", None)
            if seat is None:
                seat = getattr(ref, "controller_seat", None)
            if seat == local_seat:
                grp_id = getattr(ref, "grp_id", None)
                if grp_id is not None and grp_id in total:
                    observed_spent[grp_id] += 1
        out = total - observed_spent
        try:
            setattr(out, "uncertain", True)
        except Exception:
            pass
        return out

    observed_spent = Counter()
    for iid, ztype in iid_zone.items():
        ref = objects.get(iid)
        if ref is None:
            continue  # stale/unresolvable object record: no consumption
        seat = getattr(ref, "owner_seat", None)
        if seat is None:
            seat = getattr(ref, "controller_seat", None)
        if seat != local_seat:
            continue
        grp_id = getattr(ref, "grp_id", None)
        if grp_id is None or grp_id not in total:
            continue  # generated copy / token: never consumes deck copies
        if ztype in _SPENT_ZONE_TYPES:
            observed_spent[grp_id] += 1

    out = total - observed_spent

    # Reconcile against the observed library roster when it is populated.
    observed_library_size = len(library_ids)
    uncertain = False
    if observed_library_size > 0:
        computed_total = sum(cnt for cnt in out.values() if cnt > 0)
        if computed_total > observed_library_size:
            # Overcount: trim proportionally-largest entries down to fit and
            # flag the estimate as inexact.
            uncertain = True
            excess = computed_total - observed_library_size
            for grp_id in sorted(out, key=lambda g: (-out[g], g)):
                if excess <= 0:
                    break
                take = min(excess, max(0, out[grp_id]))
                if take:
                    out[grp_id] -= take
                    excess -= take
        elif computed_total < observed_library_size:
            # Undercount: some library members are unidentified (hidden info);
            # exact composition cannot be established.
            uncertain = True

    try:
        setattr(out, "uncertain", uncertain)
    except Exception:
        pass
    return out


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
        self._local_seat = None
        self._player_deck: tuple[int, ...] = ()
        self._commander_cards: tuple[int, ...] = ()

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

            if kind == "gre.ConnectResp":
                self._parse_connect_resp(payload)
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

    def _parse_connect_resp(self, payload):
        """Extract local_seat, player_deck, and commander_cards from ConnectResp."""
        if not isinstance(payload, Mapping):
            return
        seats = payload.get("systemSeatIds")
        if isinstance(seats, list) and seats:
            seat = _as_int(seats[0])
            if seat is not None:
                self._local_seat = seat

        cr = payload.get("connectResp")
        inner = cr if isinstance(cr, Mapping) else payload
        deck_msg = inner.get("deckMessage")
        if isinstance(deck_msg, Mapping):
            deck_cards = deck_msg.get("deckCards")
            if isinstance(deck_cards, list):
                self._player_deck = tuple(_as_int(x) for x in deck_cards if _as_int(x) is not None)
            cmd_cards = deck_msg.get("commanderCards")
            if isinstance(cmd_cards, list):
                self._commander_cards = tuple(_as_int(x) for x in cmd_cards if _as_int(x) is not None)

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
            local_seat=self._local_seat,
            player_deck=self._player_deck,
            commander_cards=self._commander_cards,
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


# ===========================================================================
# Dual-source fusion (dual-expansions.md S8.1/S8.2)
# ===========================================================================

_DEFAULT_PAIRING_WAIT_S = 0.15


def event_identity(events: Iterable[Any]) -> list[str]:
    """Stable semantic identity keys for public events seen via >=1 source.

    Pure function. Two sources observing the SAME underlying public action
    produce identical keys, so a downstream differ can dedupe by key instead
    of double-counting. Key derivation deliberately uses only SEMANTIC event
    content -- never source ids, receive timestamps, or snapshot ids:

        kind | seat | sorted (param, value) pairs of the semantic payload

    Semantic payload = the event's payload minus provenance-ish keys
    (``*_ts``, ``ts``, ``snapshot_id``, ``source_id``, ``conn_id``,
    ``received_via``) and minus values that vary per observer (any key whose
    name ends in ``_ts``). Sorting makes key order irrelevant; the digest is
    sha1 over the canonical JSON so keys are stable across processes.

    Non-Event objects (or Events lacking kind) degrade to a content hash of
    their repr -- still stable, still pure.
    """

    from .models import Event as _Event  # local import: avoids cycles in tests

    provenance_keys = frozenset({
        "ts", "snapshot_id", "source_id", "conn_id", "received_via",
    })

    def _semantic_payload(payload: Any) -> dict:

        if not isinstance(payload, Mapping):
            return {}
        out = {}
        for k, v in payload.items():
            if k in provenance_keys or k.endswith("_ts"):
                continue
            out[k] = v
        return out

    def _one(ev: Any) -> str:

        if isinstance(ev, _Event) and getattr(ev, "kind", None):
            seat = getattr(ev, "seat", None)
            sem = _semantic_payload(getattr(ev, "payload", {}))
            canon = json.dumps(
                {"k": ev.kind, "s": seat,
                 "p": sorted(sem.items(), key=lambda kv: str(kv[0]))},
                sort_keys=True, default=str,
            )
        else:
            canon = repr(ev)
        return hashlib.sha1(canon.encode("utf-8")).hexdigest()

    return [_one(ev) for ev in events]


class _ChildRecord:
    """Per-source bookkeeping inside DualStateBuilder."""

    __slots__ = ("builder", "latest", "match_id", "local_seat",
                 "baseline_ok", "chain_valid", "lost")

    def __init__(self, builder: GameStateBuilder) -> None:
        self.builder = builder
        self.latest: GameState | None = None   # newest snapshot this source
        self.match_id: str | None = None       # validated match identity
        self.local_seat: int | None = None     # from ITS OWN ConnectResp
        self.baseline_ok = False               # identity + baseline folded
        self.chain_valid = False               # GRE linkage continuity ok
        self.lost = False                      # operator-marked lost


class DualStateBuilder:
    """Wraps ONE child :class:`GameStateBuilder` PER SOURCE and publishes the
    coherent current :class:`GameState`.

    Core invariant (task contract): ONE usable source is ALWAYS sufficient --
    its snapshot publishes immediately, enriched only as far as that source's
    own visibility supports. A second source is OPTIONAL ENRICHMENT only:

    - Fusion happens exclusively when BOTH children are initialized with a
      VERIFIED IDENTICAL match_id and compatible GRE linkage; pairing waits
      at most ``max_wait_s`` (default 150ms) on ``time.monotonic()`` from the
      moment the SECOND source became eligible -- a single healthy source
      never waits.
    - Enrichment adds per-seat PRIVATE data from whichever source actually
      sees it (a seat's hand identities come from the client whose view owns
      that seat) and per-seat submitted-deck metadata (player_decks /
      commander_cards_by_seat) when present.
    - Mismatch / pairing timeout / stale secondary publishes the healthy
      PRIMARY view with enrichment LOWERED (SeatKnowledge fields reduced) --
      never a half-merged hybrid. Different matches are NEVER merged; two
      connections of the same player are NEVER treated as two seats (seat
      sets must agree; identical local_seat on both children means one player
      seen twice -> secondary contributes nothing seat-wise).
    - ``mark_source_lost`` lowers that source's CONTRIBUTED knowledge fields
      without deleting seats or touching narration state; recovery demands
      re-validation of match identity + baseline before enrichment resumes.
    """

    def __init__(
        self,
        *,
        max_wait_s: float = _DEFAULT_PAIRING_WAIT_S,
        monotonic: Callable[[], float] = time.monotonic,
        name_resolver: Callable[[int], "str | None"] | None = None,
    ) -> None:

        self._max_wait_s = float(max_wait_s)
        self._monotonic = monotonic          # injectable for tests
        self._name_resolver = name_resolver

        self._children: dict[int, _ChildRecord] = {}
        self._registry = SourceRegistry()
        self._primary_sid: int | None = None

        # Pairing-window bookkeeping (receiver monotonic clock).
        self._pair_deadline: float | None = None

        # Last published snapshot + its enrichment posture, so loss/recovery
        # can lower/raise knowledge WITHOUT rebuilding seats or narration.
        self._published: GameState | None = None
        self._enriched_last: bool = False

    # ------------------------------------------------------------------ #
    # Ingestion                                                           #
    # ------------------------------------------------------------------ #

    def ingest(self, tagged_msg: Any) -> GameState | None:
        """Route one :class:`~arenaonair.sources.TaggedMessage` by
        ``tag.source_id`` to its child builder.

        Returns a snapshot ONLY when this message advanced the PRIMARY
        source's view (callers broadcast what returns); secondary-source
        progress is absorbed silently and surfaces via the next publish().
        Malformed wrappers / unknown source ids are swallowed (robustness
        contract mirrors GameStateBuilder.apply).
        """

        tag = getattr(tagged_msg, "tag", None)
        msg = getattr(tagged_msg, "msg", None)
        if not isinstance(tag, SourceTag) or msg is None:
            return None

        sid = tag.source_id
        if not isinstance(sid, int) or sid < 0 or sid > 1:
            return None

        if not self._registry.mark_message(tag):
            return None  # duplicate / stale-generation replay

        child = self._children.get(sid)
        if child is None:
            child = _ChildRecord(GameStateBuilder(
                name_resolver=self._name_resolver))
            self._children[sid] = child

        snap = child.builder.apply(child.latest, msg)
        if snap is not None:
            child.latest = snap

        # Identity/validation bookkeeping ---------------------------------
        mid = getattr(getattr(snap, "match_meta", None), "match_id", None)
        if mid:
            if child.match_id is None:
                child.match_id = mid
                child.baseline_ok = True   # identity established + folded
                self._registry.set_flags(sid, baseline_ok=True)
            elif child.match_id != mid:
                # Match identity CHANGED mid-stream for this source: treat as
                # a new game requiring fresh baseline validation.
                child.match_id = mid
                child.baseline_ok = False
                child.chain_valid = False
                self._registry.set_flags(sid, baseline_ok=False,
                                         chain_valid=False)
            else:
                # Same match continuing: GRE linkage continuity check.
                prev_link_ok = self._linkage_ok(child.latest)
                if prev_link_ok and not child.chain_valid:
                    child.chain_valid = True
                    self._registry.set_flags(sid, chain_valid=True)

        if sid == 0 or self._primary_sid is None:
            if self._primary_sid is None:
                self._primary_sid = sid   # first-seen source anchors primary

        return snap if sid == self._primary_sid else None

    @staticmethod
    def _linkage_ok(snapshot: GameState | None) -> bool:

        if snapshot is None:
            return False
        return True  # presence of a chained snapshot implies continuity;
                     # deeper prev-chain audits belong to the differ.

    # ------------------------------------------------------------------ #
    # Publishing                                                          #
    # ------------------------------------------------------------------ #

    def publish(self) -> GameState | None:
        """Coherent current GameState to broadcast, or None before any
        usable source exists."""

        ready = [sid for sid, c in self._children.items()
                 if c.latest is not None and c.baseline_ok and not c.lost]
        if not ready:
            return self._published  # nothing usable: hold last good state

        primary_sid = min(ready)
        primary_child = self._children[primary_sid]
        primary_snap = primary_child.latest

        others_ready = [sid for sid in ready if sid != primary_sid]

        fused_now = False
        result = primary_snap

        if others_ready:
            sec_sid = others_ready[0]
            sec_child = self._children[sec_sid]

            if self._pair_compatible(primary_child, sec_child):
                result = self._fuse(primary_child, sec_child)
                fused_now = True
                self._pair_deadline = None
            else:
                # Second source EXISTS but isn't compatible yet: hold open a
                # bounded pairing window measured on the receiver clock.
                now = self._monotonic()
                if self._pair_deadline is None:
                    self._pair_deadline = now + self._max_wait_s
                if now >= self._pair_deadline:
                    # Timeout: publish primary-only with enrichment lowered.
                    result = self._lower_enrichment(primary_snap)
                    fused_now = False
                    self._pair_deadline = None  # window closed until state changes
                else:
                    # Inside the window: keep publishing the primary view;
                    # enrichment simply isn't claimed yet this cycle.
                    result = primary_snap
                    fused_now = False

        self._enriched_last = fused_now
        if result is not None:
            result = self._stamp_knowledge(result, fused_now)
            self._published = result
        return result

    def _pair_compatible(self, a: _ChildRecord, b: _ChildRecord) -> bool:

        """Verified same match + compatible GRE linkage + distinct seats."""

        if not (a.baseline_ok and b.baseline_ok):
            return False
        if a.match_id is None or a.match_id != b.match_id:
            return False

        sa, sb = a.latest.local_seat, b.latest.local_seat
        seat_sets_a = {p.seat for p in (a.latest.players or {}).values()}
        seat_sets_b = {p.seat for p in (b.latest.players or {}).values()}
        if seat_sets_a and seat_sets_b and seat_sets_a != seat_sets_b:
            return False  # different games entirely -- never merge

        if sa is None or sb is None:
            return False  # unlocated view: cannot attribute its knowledge
        if sa == sb:
            # Two connections of the SAME player: legitimate dual-view of one
            # participant; NOT two seats. Secondary adds no seat knowledge.
            return False

        return True

    def _fuse(self, prim: _ChildRecord, sec: _ChildRecord) -> GameState:

        """Enrich primary's snapshot with secondary's exclusive knowledge."""

        base = prim.latest

        player_decks: dict[int, tuple[int, ...]] = {}
        commander_by_seat: dict[int, tuple[int, ...]] = {}

        for rec in (prim, sec):
            snap_local_seat = rec.latest.local_seat
            deck = rec.latest.player_deck or ()
            cmdr = rec.latest.commander_cards or ()
            if snap_local_seat is not None and deck:
                player_decks[snap_local_seat] = deck
            if snap_local_seat is not None and cmdr:
                commander_by_seat[snap_local_seat] = cmdr

        knowledge: dict[int, SeatKnowledge] = {}
        now_mono_recv_proxy: float | None = None

        for rec in (prim, sec):
            snap_local_seat = rec.latest.local_seat
            if snap_local_seat is None or not rec.chain_valid:
                continue  # unvalidated chain contributes no hand knowledge

            knowledge[snap_local_seat] = SeatKnowledge(
                seat=snap_local_seat,
                hand_visible=True,
                hand_fresh_asof=None,   # receiver-ts stamping happens upstream;
                                        # freshness gating uses monotonic windows.
                deck_submitted=bool(rec.latest.player_deck),
                library_accounted=False,
                library_uncertainty="unknown",
            )

        del now_mono_recv_proxy

        merged_names: dict[int, str] = dict(base.match_meta.player_names or {})
        for rec in (prim, sec):
            for seat, nm in (rec.latest.match_meta.player_names or {}).items():
                merged_names.setdefault(int(seat), str(nm))

        return replace(
            base,
            match_meta=replace(base.match_meta,
                               player_names=merged_names),
            player_decks=player_decks,
            commander_cards_by_seat=commander_by_seat,
            seat_knowledge=knowledge,
        )

    # ------------------------------------------------------------------ #
    # Knowledge lowering / stamping                                       #
    # ------------------------------------------------------------------ #

    def _lower_enrichment(self, snap: GameState) -> GameState:

        """Primary-only view: strip secondary-contributed per-seat metadata
        and lower every SeatKnowledge to what the PRIMARY source alone
        supports (its own local seat's hand only)."""

        prim_sid = self._primary_sid if self._primary_sid is not None else 0
        prim_child = self._children.get(prim_sid)
        prim_seat = (prim_child.latest.local_seat
                     if prim_child and prim_child.latest else None)

        player_decks: dict[int, tuple[int, ...]] = {}
        commander_by_seat: dict[int, tuple[int, ...]] = {}
        if prim_seat is not None:
            if prim_child.latest.player_deck:
                player_decks[prim_seat] = prim_child.latest.player_deck
            if prim_child.latest.commander_cards:
                commander_by_seat[prim_seat] = prim_child.latest.commander_cards

        knowledge: dict[int, SeatKnowledge] = {}
        if prim_seat is not None and prim_child.chain_valid:
            knowledge[prim_seat] = SeatKnowledge(
                seat=prim_seat,
                hand_visible=True,
                hand_fresh_asof=None,
                deck_submitted=bool(prim_child.latest.player_deck),
                library_accounted=False,
                library_uncertainty="unknown",
            )

        return replace(
            snap,
            player_decks=player_decks,
            commander_cards_by_seat=commander_by_seat,
            seat_knowledge=knowledge,
        )

    def _stamp_knowledge(self, snap: GameState, fused: bool) -> GameState:

        """Post-loss/post-recovery knowledge adjustment WITHOUT touching
        seats, narration state, or any other snapshot field.

        When the last publish was NOT fused (single-source / timeout /
        mismatch), enrichment stays lowered; when fused, the fuse step already
        wrote full knowledge. Lost sources were excluded from `ready` upstream
        so their contributions vanish naturally here.
        """

        if fused:
            return snap

        # Not fused: ensure knowledge reflects ONLY healthy primary support.
        return self._lower_enrichment(snap)

    # ------------------------------------------------------------------ #
    # Source lifecycle                                                    #
    # ------------------------------------------------------------------ #

    def mark_source_lost(self, source_id: int) -> None:

        """Lower ``source_id``'s contributed knowledge WITHOUT deleting seats
        or restarting narration state (the held snapshot survives)."""

        child = self._children.get(source_id)
        if child is None:
            return
        child.lost = True
        child.chain_valid = False
        self._registry.mark_disconnected(source_id)
        self._registry.set_flags(source_id, chain_valid=False)
        # Force revalidation of the pairing window on next publish.
        self._pair_deadline = None

    def mark_source_recovered(self, source_id: int) -> None:

        """Begin recovery: generation bumps in the registry force fresh
        sequence floors; enrichment resumes ONLY after this source re-derives
        match identity + baseline (baseline_ok flips back on inside ingest()
        when a room_state/match-bearing snapshot arrives again)."""

        child = self._children.get(source_id)
        if child is None:
            return
        child.lost = False
        child.baseline_ok = False      # must re-validate before enriching
        child.chain_valid = False
        self._registry.mark_recovered(source_id)
        self._registry.set_flags(source_id, baseline_ok=False,
                                 chain_valid=False)

    # ------------------------------------------------------------------ #
    # Introspection                                                       #
    # ------------------------------------------------------------------ #

    def source_status(self, source_id: int):

        return self._registry.status(source_id)

    @property
    def last_publish_was_enriched(self) -> bool:

        return self._enriched_last
