"""EventDiffer: snapshot pairs + intervening GRE messages -> narratable Events.

Implements arenaonair.interfaces.Differ. Snapshots do not retain actions /
annotations / attackState / blockState / gameInfo, so those signals are
scanned directly from msgs_since_prev payloads (same unwrapping convention
as state_builder._unwrap_payload). Cast actions ACCUMULATE across consecutive
GameStateMessages inside GRE, so cross-window dedupe state lives on the
differ instance. Counter heuristic: a stack->graveyard disappearance counts
as a counter ONLY while other spells remain on the stack afterwards,
otherwise it classifies as a resolve. match_end hooks room_state.completed
(TODO: not present in recorded fixtures yet). NEVER RAISE: uninterpretable
input yields [].
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import events as ev
from .carddb import (
    calculate_cmc,
    detect_archetype,
    is_bomb,
    is_cantrip_or_filter,
    is_combat_trick,
    is_counterspell,
    is_sweeper,
    is_tutor,
)
from .models import Event, GameState, GreMessage
from .state_builder import remaining_deck_cards

__all__ = ["EventDiffer", "DEFAULT_DIFFER_CONFIG", "life_salience"]

DEFAULT_DIFFER_CONFIG = {
    "debounce_window": 1.5,
    "life_small_delta": 2,
    "life_low_result_threshold": 5,
    "life_mid_max": 9,
    "life_boundary": 10,
    "life_critical": 3,
    "board_creatures_delta": 2,
    "board_power_delta": 4,
    "suppress_repeats": False,
    # --- omniscient strategic detectors ([O2]); conservative defaults -----
    # Hand-freshness tolerance in clock units (real receiver ts when the
    # snapshot carries one, else float(snapshot_id)); older-than-this hands
    # are STALE and every dependent claim is suppressed.
    "trap_hand_fresh_tolerance": 5.0,
    # Armed traps expire after this much clock distance without springing.
    "trap_validity_window": 30.0,
    # Minimum visible lands backing a seat before its mana counts as KNOWN;
    # fewer/no visible lands -> mana unknown -> ARMED claim suppressed.
    "trap_min_lands": 1,
    # Bluff detector uses its own freshness gate on the same clock.
    "bluff_hand_fresh_tolerance": 5.0,
}

_ZONE_PREFIX = "zonetype_"


def _norm_zone_type(raw):
    """'ZoneType_Battlefield' -> 'battlefield'; tolerates already-normalized."""
    lowered = str(raw or "").lower()
    if lowered.startswith(_ZONE_PREFIX):
        lowered = lowered[len(_ZONE_PREFIX):]
    return lowered


def _norm_enum(raw):
    """Lower-cased enum: 'AttackState_Declared' -> 'attackstate_declared'."""
    return str(raw or "").lower()


def _as_int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _zone_object_ids(state, zone_types_norm):
    """Union of object ids over every zone whose normalized type matches."""
    wanted = {zone_types_norm} if isinstance(zone_types_norm, str) \
        else set(zone_types_norm)
    out = set()
    try:
        zones = state.zones if state is not None else None
        for zv in (zones or {}).values():
            if _norm_zone_type(getattr(zv, "zone_type", "")) in wanted:
                out.update(zv.object_ids or ())
    except Exception:
        return set()
    return out


def _unwrap_gsm(payload):
    """Mirror of state_builder._unwrap_payload for GameStateMessage payloads."""
    try:
        if ("gameStateMessage" in payload
                and payload.get("type") not in ("GameStateType_Full",
                                                "GameStateType_Diff")):
            inner = payload.get("gameStateMessage")
            if isinstance(inner, Mapping):
                return inner
    except Exception:
        pass
    return payload


def _gsm_of(msg):
    """Inner gameStateMessage dict for a GameState/QueuedGameState message."""
    payload = getattr(msg, "payload", None)
    if not isinstance(payload, Mapping):
        return {}
    kind = getattr(msg, "kind", "") or ""
    if "QueuedGameStateMessage" in kind:
        inner = payload.get("gameStateMessage")
        if isinstance(inner, Mapping):
            return inner
        return payload
    return _unwrap_gsm(payload)


def _detail_map(details):
    """Flatten GRE annotation details into {key: first_value}."""
    out = {}
    try:
        if isinstance(details, list):
            for d in details:
                if not isinstance(d, Mapping):
                    continue
                key = d.get("key")
                if not key:
                    continue
                vals = (d.get("valueInt32") or d.get("valueString")
                        or d.get("valueBool") or [])
                if vals:
                    out[key] = vals[0]
        elif isinstance(details, Mapping):
            out.update(details)
    except Exception:
        pass
    return out


def _annotation_types(ann):
    types = ann.get("type") or ann.get("annotationType") or []
    if isinstance(types, str):
        return [types]
    return [t for t in types if isinstance(t, str)]


def _iter_annotations(msgs):
    """Yield every annotation dict found across game-state payloads."""
    for msg in msgs:
        if not isinstance(msg, GreMessage):
            continue
        kind = getattr(msg, "kind", "") or ""
        if kind not in ("gre.GameStateMessage", "gre.QueuedGameStateMessage"):
            continue
        gsm = _gsm_of(msg)
        for ann in gsm.get("annotations") or []:
            if isinstance(ann, Mapping):
                yield ann


def _iter_actions(msgs):
    """Yield (seat_id_or_None, action_dict) for every actions[] entry."""
    for msg in msgs:
        if not isinstance(msg, GreMessage):
            continue
        kind = getattr(msg, "kind", "") or ""
        if kind not in ("gre.GameStateMessage", "gre.QueuedGameStateMessage"):
            continue
        gsm = _gsm_of(msg)
        for entry in gsm.get("actions") or []:
            if not isinstance(entry, Mapping):
                continue
            action = entry.get("action")
            if not isinstance(action, Mapping):
                continue
            yield _as_int(entry.get("seatId")), action


def life_salience(delta, resulting_life, cfg):
    """Salience ladder for life changes (EXACT contract):

    |delta| <= small (2): LOW unless resulting life < low-threshold (5) -> HIGH.
    small+1..mid (3..9):  LOW unless crossing the boundary (10) downward ->
                          HIGH; a negative delta landing below the boundary IS
                          that crossing (the drop started at/above it).
    >= boundary (10+):    HIGH.
    Resulting life < critical (3): MUST_SPEAK regardless.
    """
    small = cfg["life_small_delta"]
    low_thr = cfg["life_low_result_threshold"]
    mid_max = cfg["life_mid_max"]
    boundary = cfg["life_boundary"]
    critical = cfg["life_critical"]

    magnitude = abs(delta)
    if magnitude <= small:
        salience = ev.SALIENCE_LOW
        if resulting_life is not None and resulting_life < low_thr:
            salience = ev.SALIENCE_HIGH
    elif magnitude <= mid_max:
        salience = ev.SALIENCE_LOW
        if delta < 0 and resulting_life is not None \
                and resulting_life < boundary:
            salience = ev.SALIENCE_HIGH
    else:
        salience = ev.SALIENCE_HIGH

    if resulting_life is not None and resulting_life < critical:
        salience = ev.SALIENCE_MUST_SPEAK
    return salience


class EventDiffer:
    """Implements arenaonair.interfaces.Differ.

    Constructor takes an optional config dict overriding any subset of
    DEFAULT_DIFFER_CONFIG.
    """

    def __init__(self, config=None, card_lookup=None):
        self.cfg = dict(DEFAULT_DIFFER_CONFIG)
        try:
            self.cfg.update(dict(config or {}))
        except Exception:
            pass
        self.card_lookup = card_lookup
        # Cross-window dedupe / continuity state ---------------------------
        # SCOPE DISCIPLINE (S7.6): every accumulator below belongs to exactly
        # one scope and is cleared by _reset_game_scope() / _reset_match_scope()
        # before detectors run on the first snapshot of a new scope.
        #
        # GAME-scoped -- instance ids are recycled between games of a match,
        # boards are emptied between games and turn numbers restart:
        #   cast/tutor/recursion/trick/unfair dedup sets, board baselines,
        #   per-turn cantrip history, stage tracking.
        # MATCH-scoped -- opponent identity persists across games of one match:
        #   archetype evidence accumulation (+ its already-(match_id,key)-ed
        #   companions below).
        self._seen_casts = set()          # GAME-scoped (instanceId, seatId)
        self._last_match_id = None        # MATCH_START event emission guard
        self._scope_match_id = None       # scope-partition sentinel
        self._game_scope_open = False     # True between play-stage windows
        self._last_stage = None           # GAME-scoped stage tracking
        self._baseline_creatures = None   # GAME-scoped seat -> count at start
        self._baseline_power = None       # GAME-scoped seat -> summed power
        self._window_msgs = []            # set per diff() call
        self._seen_hand_online = set()    # MATCH-scoped (match_id, grp_id)
        self._detected_archetypes = set() # MATCH-scoped (match_id, seat)
        self._seen_tutors_cast = set()    # GAME-scoped instanceId set
        self._seen_graveyard_recursions = set()  # GAME-scoped instanceId set
        self._cards_played_by_seat = {}   # MATCH-scoped seat -> card names
        self._seen_combat_tricks = set()  # GAME-scoped instanceId set
        self._seen_chump_blocks = set()   # MATCH-scoped triple keys
        self._seen_unfair_plays = set()   # GAME-scoped instanceId set
        self._seen_counter_wars = set()   # MATCH-scoped (match_id,top_iid)
        self._cantrips_cast_this_turn = {} # GAME-scoped (turn, seat)->names
        self._armed_traps = {}            # GAME-scoped seat -> armed-trap entry ([O2])

    # ------------------------------------------------------- scope resets

    def _reset_game_scope(self):
        """Clear every GAME-scoped accumulator (new game began)."""
        self._seen_casts.clear()
        self._seen_tutors_cast.clear()
        self._seen_graveyard_recursions.clear()
        self._seen_combat_tricks.clear()
        self._seen_unfair_plays.clear()
        self._cantrips_cast_this_turn.clear()
        self._armed_traps.clear()
        self._baseline_creatures = None
        self._baseline_power = None
        self._last_stage = None

    def _reset_match_scope(self):
        """Clear every MATCH-scoped accumulator (new opponent/match)."""
        self._cards_played_by_seat.clear()
        self._detected_archetypes.clear()
        self._seen_hand_online.clear()
        self._seen_chump_blocks.clear()
        self._seen_counter_wars.clear()

    def _ensure_scope(self, cur):
        """Partition accumulators before detectors run on this window.

        Called at the top of every diff(); detects match changes via the
        snapshot's match_meta and game boundaries via GameStage transitions
        visible in the intervening messages. Resets happen BEFORE any detector
        reads dedup/history state so a fresh scope never inherits stale keys.
        """
        try:
            match_id = getattr(getattr(cur, "match_meta", None),
                               "match_id", None)
            if match_id != self._scope_match_id:
                self._scope_match_id = match_id
                self._reset_match_scope()
                self._reset_game_scope()
                self._game_scope_open = False

            stage_now = self._latest_stage(self._window_msgs)
            if stage_now == "gamestage_play":
                if not self._game_scope_open:
                    # First snapshot inside a newly opened game.
                    self._reset_game_scope()
                    self._game_scope_open = True
            elif stage_now == "gamestage_gameover":
                # Close the game scope so the next play-stage window starts a
                # fresh one even inside the same match. NOTE: deliberately
                # does NOT touch _last_stage -- detect_game_end needs to see
                # the pre-gameover value to fire its one GAME_END event.
                self._game_scope_open = False
        except Exception:
            pass

    # ------------------------------------------------------------------ API

    def diff(self, prev, cur, msgs_since_prev):
        """Snapshot pair + intervening messages -> ordered events. Never raises."""
        try:
            return self._diff_inner(prev, cur, msgs_since_prev or [])
        except Exception:
            return []

    def _diff_inner(self, prev, cur, msgs):
        self._window_msgs = list(msgs)
        # Scope partitioning FIRST: fresh scopes are reset before any
        # detector reads dedup/history state (S7.6).
        self._ensure_scope(cur)
        events = []
        events.extend(self.detect_game_start(prev, cur))
        events.extend(self.detect_game_end(prev, cur))
        events.extend(self.detect_turn_start(prev, cur))
        events.extend(self.detect_land_drop(prev, cur))
        events.extend(self.detect_cast(prev, cur))
        events.extend(self.detect_resolve_and_counter(prev, cur))
        events.extend(self.detect_attack_declared(prev, cur))
        events.extend(self.detect_block_declared(prev, cur))
        events.extend(self.detect_combat_damage(prev, cur))
        events.extend(self.detect_life_change(prev, cur))
        events.extend(self.detect_board_shift(prev, cur))
        events.extend(self.detect_match_start_end(prev, cur))
        events.extend(self.detect_hand_online(prev, cur))
        events.extend(self.detect_tutor_anticipation(prev, cur))
        events.extend(self.detect_graveyard_recursion(prev, cur))
        events.extend(self.detect_archetype(prev, cur))
        events.extend(self.detect_counter_war(prev, cur))
        events.extend(self.detect_combat_trick(prev, cur))
        events.extend(self.detect_chump_block(prev, cur))
        events.extend(self.detect_hand_sculpting(prev, cur))
        events.extend(self.detect_unfair_play(prev, cur))
        events.extend(self.detect_trap_armed(prev, cur))
        events.extend(self.detect_trap_sprung(prev, cur))
        events.extend(self.detect_bluff(prev, cur))

        base_ts = getattr(cur, "ts", None)
        stamped = []
        for idx, event in enumerate(events):
            ts = event.ts if event.ts is not None else (
                base_ts if base_ts is not None else idx * 0.01)
            stamped.append(Event(kind=event.kind, seat=event.seat,
                                 payload=dict(event.payload), ts=ts,
                                 salience=event.salience))

        merged = self.debounce(stamped)
        merged.sort(key=lambda e: e.ts)
        if self.cfg.get("suppress_repeats"):
            merged = self.suppress_repeats(merged)
        return merged

    # ------------------------------------------------------------ detectors

    def detect_land_drop(self, prev, cur):
        """Battlefield gains an object carrying 'land' in card_types."""
        try:
            prev_ids = _zone_object_ids(prev, ("battlefield",))
            cur_ids = _zone_object_ids(cur, ("battlefield",))
            drops = []
            for iid in sorted(cur_ids - prev_ids):
                ref = (cur.objects or {}).get(iid)
                if ref is not None and "land" in (ref.card_types or ()):
                    drops.append((getattr(ref, "controller_seat", None),
                                  getattr(ref, "name", None)))
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            return [Event(kind=ev.LAND_DROP,
                          seat=seat,
                          payload={"name": name},
                          ts=ts_base + idx * 0.01,
                          salience=ev.SALIENCE_LOW)
                    for idx, (seat, name) in enumerate(drops)]
        except Exception:
            return []

    def detect_cast(self, prev, cur):
        """Spells cast this window via ZoneTransfer annotations or stack entry.

        Falls back to ActionType_Cast actions for synthetic test messages where
        no real GRE game state is present.
        """
        try:
            objs = (cur.objects or {}) if cur else {}
            objs_prev = (prev.objects or {}) if prev else {}
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0

            # 1. Authoritative: ZoneTransfer annotations with CastSpell category
            for ann in _iter_annotations(self._window_msgs):
                types = _annotation_types(ann)
                if "AnnotationType_ZoneTransfer" not in types:
                    continue
                details = _detail_map(ann.get("details"))
                cat = str(details.get("category") or "")
                if "cast" not in cat.lower():
                    continue
                for aid in ann.get("affectedIds") or []:
                    iid = _as_int(aid)
                    if iid is None:
                        continue
                    ref = objs.get(iid) or objs_prev.get(iid)
                    seat = (getattr(ref, "controller_seat", None)
                            or getattr(ref, "owner_seat", None)
                            or _as_int(ann.get("affectorId")))
                    if seat is None and cur and cur.turn_info:
                        seat = cur.turn_info.active_player
                    key = (iid, seat)
                    if key in self._seen_casts:
                        continue
                    self._seen_casts.add(key)
                    name = getattr(ref, "name", None)
                    grp_id = getattr(ref, "grp_id", None)
                    out.append(Event(kind=ev.CAST,
                                     seat=seat,
                                     payload={"name": name, "grp_id": grp_id,
                                              "instance_id": iid},
                                     ts=ts_base + idx * 0.01,
                                     salience=ev.SALIENCE_HIGH))
                    idx += 1

            # 2. Stack additions for real spells
            is_real_game = any(_gsm_of(m).get("gameStateId") is not None
                               for m in self._window_msgs)
            if self._last_stage == "gamestage_play" or \
                    self._latest_stage(self._window_msgs) == "gamestage_play":
                prev_stack = _zone_object_ids(prev, ("stack",))
                cur_stack = _zone_object_ids(cur, ("stack",))
                for iid in sorted(cur_stack - prev_stack):
                    ref = objs.get(iid)
                    if ref is None:
                        continue
                    types = getattr(ref, "card_types", ()) or ()
                    if any(t in types for t in (
                            "creature", "instant", "sorcery", "enchantment",
                            "artifact", "planeswalker", "battle")):
                        seat = (getattr(ref, "controller_seat", None)
                                or getattr(ref, "owner_seat", None))
                        key = (iid, seat)
                        if key not in self._seen_casts:
                            self._seen_casts.add(key)
                            out.append(Event(
                                kind=ev.CAST,
                                seat=seat,
                                payload={"name": getattr(ref, "name", None),
                                         "grp_id": getattr(ref, "grp_id", None),
                                         "instance_id": iid},
                                ts=ts_base + idx * 0.01,
                                salience=ev.SALIENCE_HIGH))
                            idx += 1

            # 3. Synthetic test fallback (only when NOT a real GRE log)
            if not out and not is_real_game:
                for seat_id, action in _iter_actions(self._window_msgs):
                    if _norm_enum(action.get("actionType")) != "actiontype_cast":
                        continue
                    iid = _as_int(action.get("instanceId"))
                    key = (iid, seat_id)
                    if key in self._seen_casts:
                        continue
                    self._seen_casts.add(key)
                    ref = objs.get(iid) if iid is not None else None
                    name = getattr(ref, "name", None) if ref is not None else None
                    grp_id = (getattr(ref, "grp_id", None) if ref is not None
                              else _as_int(action.get("abilityGrpId")))
                    out.append(Event(kind=ev.CAST,
                                     seat=seat_id,
                                     payload={"name": name, "grp_id": grp_id,
                                              "instance_id": iid},
                                     ts=ts_base + idx * 0.01,
                                     salience=ev.SALIENCE_HIGH))
                    idx += 1
            return out
        except Exception:
            return []

    def detect_resolve_and_counter(self, prev, cur):
        """Stack objects disappearing this window -> resolve or counter.

        Evidence discipline (S7.8): a stack->graveyard disappearance alone is
        AMBIGUOUS -- it happens both when a spell is countered and when an
        instant/sorcery simply finishes resolving above another spell.
        Classification therefore requires affirmative evidence:

        - An AnnotationType_ZoneTransfer whose category mentions "counter"
          naming the instance -> COUNTER (authoritative).
        - A counterspell object sitting on the stack in ``prev`` while the
          victim disappears -> COUNTER attributed to that spell's controller.
        - Otherwise the transition is classified as a RESOLVE; no counter is
          asserted and no countering player is invented from seat ordering.
        """
        try:
            prev_stack = _zone_object_ids(prev, ("stack",))
            cur_stack = _zone_object_ids(cur, ("stack",))
            gone_ids = sorted(prev_stack - cur_stack)
            if not gone_ids:
                return []
            counter_evidence = self._counter_evidence(prev, cur, gone_ids)
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for iid in gone_ids:
                dest = self._destination_zone(prev, cur, iid)
                ref_cur = (cur.objects or {}).get(iid)
                ref_prev = (prev.objects or {}).get(iid) if prev else None
                ref = ref_cur if ref_cur is not None else ref_prev
                name = getattr(ref, "name", None) if ref is not None else None
                seat = getattr(ref, "controller_seat", None) \
                    if ref is not None else None
                actor = counter_evidence.get(iid)
                if dest == "graveyard" and actor is not None:
                    payload = {"name": name}
                    if actor.get("by_seat") is not None:
                        payload["countered_by_seat"] = actor["by_seat"]
                    if actor.get("by_name"):
                        payload["countered_by_name"] = actor["by_name"]
                    out.append(Event(
                        kind=ev.COUNTER,
                        seat=seat,
                        payload=payload,
                        ts=ts_base + idx * 0.01,
                        salience=ev.SALIENCE_HIGH))
                else:
                    if dest is None and name is None:
                        continue
                    out.append(Event(
                        kind=ev.RESOLVE,
                        seat=seat,
                        payload={"name": name,
                                 "instance_id": iid,
                                 "to_zone": dest},
                        ts=ts_base + idx * 0.01,
                        salience=ev.SALIENCE_HIGH))
                idx += 1
            return out
        except Exception:
            return []

    def _counter_evidence(self, prev, cur, gone_ids):
        """Affirmative counter evidence per disappeared instance id.

        Returns {iid: {"by_seat": int|None, "by_name": str|None}} containing
        ONLY ids with real evidence; absent key == unresolved transition.
        Two sources, checked in order of authority:

        1. ZoneTransfer annotations whose category mentions 'counter' and
           whose affectedIds include the vanished instance.
        2. A counterspell object that occupied the stack in ``prev`` -- its
           controller is credited with countering whatever else left the
           stack toward the graveyard this window.
        """
        evidence: dict[int, dict[str, Any]] = {}
        try:
            prev_stack = _zone_object_ids(prev, ("stack",))
            # 1. Explicit annotation category.
            for ann in _iter_annotations(self._window_msgs):
                types = _annotation_types(ann)
                if "AnnotationType_ZoneTransfer" not in types:
                    continue
                details = _detail_map(ann.get("details"))
                cat = str(details.get("category") or "").lower()
                if "counter" not in cat:
                    continue
                for aid in ann.get("affectedIds") or []:
                    iid = _as_int(aid)
                    if iid is None or iid not in gone_ids:
                        continue
                    affector = _as_int(ann.get("affectorId"))
                    by_name = None
                    affector_ref = ((cur.objects or {}).get(affector)
                                    or ((prev.objects or {}).get(affector)
                                        if prev else None))
                    if affector_ref is not None:
                        by_name = getattr(affector_ref, "name", None)
                    evidence[iid] = {"by_seat": affector, "by_name": by_name}

            # 2. Counterspell present on the previous stack.
            if len(evidence) < len(gone_ids):
                prev_objs = (prev.objects or {}) if prev else {}
                for iid in sorted(prev_stack):
                    ref = prev_objs.get(iid)
                    name = getattr(ref, "name", None) if ref else None
                    if not name or not is_counterspell(name):
                        continue
                    controller = getattr(ref, "controller_seat", None) \
                        or getattr(ref, "owner_seat", None)
                    for victim in gone_ids:
                        if victim in evidence:
                            continue
                        evidence[victim] = {"by_seat": controller,
                                            "by_name": name}
        except Exception:
            return evidence
        return evidence

    def _destination_zone(self, prev, cur, iid):
        """Where did instance ``iid`` land? Best-effort zone-type lookup."""
        try:
            zones_cur = (cur.zones or {}) if cur is not None else {}
            for zv in zones_cur.values():
                if iid in (zv.object_ids or ()):
                    return _norm_zone_type(zv.zone_type)
            zones_prev = (prev.zones or {}) if prev is not None else {}
            for zv in zones_prev.values():
                zt = _norm_zone_type(zv.zone_type)
                if zt == "stack":
                    continue
                if iid in (zv.object_ids or ()):
                    return zt
            return None
        except Exception:
            return None

    def detect_attack_declared(self, prev, cur):
        """DeclareAttackersReq msgs OR attackState transitions this window."""
        try:
            msgs = self._window_msgs
            attackers_out = []
            seen_iids = set()

            for msg in msgs:
                kind = getattr(msg, "kind", "") or ""
                if kind != "gre.DeclareAttackersReq":
                    continue
                payload = getattr(msg, "payload", {}) or {}
                req = payload.get("declareAttackersReq")
                if not isinstance(req, Mapping):
                    continue
                for att in req.get("attackers") or []:
                    if not isinstance(att, Mapping):
                        continue
                    iid = _as_int(att.get("attackerInstanceId"))
                    if iid is None or iid in seen_iids:
                        continue
                    seen_iids.add(iid)
                    target = _as_int(att.get("selectedDamageRecipient"))
                    if target is None:
                        recips = att.get("legalDamageRecipients") or []
                        for rec in recips:
                            psid = _as_int(rec.get("playerSystemSeatId")) \
                                if isinstance(rec, Mapping) else None
                            if psid is not None:
                                target = psid
                                break
                    attackers_out.append({"instance_id": iid,
                                          "target_seat": target})

            # Corroboration: attackState transitions on gameObjects in payloads.
            objs_cur = (cur.objects or {}) if cur is not None else {}
            objs_prev = (prev.objects or {}) if prev is not None else {}
            for msg in msgs:
                kind = getattr(msg, "kind", "") or ""
                if kind not in ("gre.GameStateMessage",
                                "gre.QueuedGameStateMessage"):
                    continue
                gsm = _gsm_of(msg)
                for go in gsm.get("gameObjects") or []:
                    if not isinstance(go, Mapping):
                        continue
                    state_norm = _norm_enum(go.get("attackState"))
                    if state_norm not in ("attackstate_declared",
                                          "attackstate_attacking"):
                        continue
                    iid2 = _as_int(go.get("instanceId"))
                    if iid2 is None or iid2 in seen_iids:
                        continue
                    seen_iids.add(iid2)
                    ai_target = None
                    ai = go.get("attackInfo")
                    if isinstance(ai, Mapping):
                        ai_target = _as_int(ai.get("targetId"))
                    attackers_out.append({"instance_id": iid2,
                                          "target_seat": ai_target})

            total_power = 0
            power_known = False
            for entry in attackers_out:
                ref3 = objs_cur.get(entry["instance_id"]) \
                    or objs_prev.get(entry["instance_id"])
                entry["name"] = getattr(ref3, "name", None) \
                    if ref3 is not None else None
                power_val = getattr(ref3, "power", None) \
                    if ref3 is not None else None
                if power_val is not None:
                    power_known = True
                    total_power += power_val

            if not attackers_out:
                return []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            payload_total_power: Any = total_power if power_known else None
            return [Event(kind=ev.ATTACK_DECLARED,
                          seat=None,
                          payload={"attackers": attackers_out,
                                   "total_power": payload_total_power},
                          ts=ts_base,
                          salience=ev.SALIENCE_HIGH)]
        except Exception:
            return []

    def detect_block_declared(self, prev, cur):
        """DeclareBlockersReq msgs OR blockState transitions this window."""
        try:
            msgs = self._window_msgs
            blocks_out = []
            seen_blockers = set()

            for msg in msgs:
                kind = getattr(msg, "kind", "") or ""
                if kind != "gre.DeclareBlockersReq":
                    continue
                payload = getattr(msg, "payload", {}) or {}
                req = payload.get("declareBlockersReq")
                if not isinstance(req, Mapping):
                    continue
                for blk in req.get("blockers") or []:
                    if not isinstance(blk, Mapping):
                        continue
                    bid = _as_int(blk.get("blockerInstanceId"))
                    if bid is None or bid in seen_blockers:
                        continue
                    seen_blockers.add(bid)
                    att_ids = []
                    selected = blk.get("selectedAttackerInstanceIds")
                    chosen = selected if isinstance(selected, list) \
                        else blk.get("attackerInstanceIds")
                    for aid in chosen or []:
                        aid_int = _as_int(aid)
                        if aid_int is not None:
                            att_ids.append(aid_int)
                    blocks_out.append({"blocker_instance_id": bid,
                                       "attacker_instance_ids": att_ids})

            # Corroboration: blockState transitions on gameObjects in payloads.
            for msg in msgs:
                kind = getattr(msg, "kind", "") or ""
                if kind not in ("gre.GameStateMessage",
                                "gre.QueuedGameStateMessage"):
                    continue
                gsm = _gsm_of(msg)
                for go in gsm.get("gameObjects") or []:
                    if not isinstance(go, Mapping):
                        continue
                    state_norm = _norm_enum(go.get("blockState"))
                    if state_norm not in ("blockstate_blocked",
                                          "blockstate_blocking"):
                        continue
                    bid2 = _as_int(go.get("instanceId"))
                    if bid2 is None or bid2 in seen_blockers:
                        continue
                    seen_blockers.add(bid2)
                    blocking_ids = []
                    bi = go.get("blockingInfo") or go.get("blockInfo")
                    if isinstance(bi, Mapping):
                        for aid2 in bi.get("attackerInstanceIds") or []:
                            aid2_int = _as_int(aid2)
                            if aid2_int is not None:
                                blocking_ids.append(aid2_int)
                    blocks_out.append({"blocker_instance_id": bid2,
                                       "attacker_instance_ids": blocking_ids})

            if not blocks_out:
                return []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            return [Event(kind=ev.BLOCK_DECLARED,
                          seat=None,
                          payload={"blocks": blocks_out},
                          ts=ts_base,
                          salience=ev.SALIENCE_HIGH)]
        except Exception:
            return []

    def detect_combat_damage(self, prev, cur):
        """AnnotationType_DamageDealt where target is a player seat."""
        try:
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            emitted_pairs = set()
            for ann in _iter_annotations(self._window_msgs):
                types = _annotation_types(ann)
                if "AnnotationType_DamageDealt" not in types:
                    continue
                details = _detail_map(ann.get("details"))
                amount = details.get("damage")
                amount_int = _as_int(amount)
                if amount_int is None or amount_int <= 0:
                    continue
                source_iid = _as_int(ann.get("affectorId"))
                for target in ann.get("affectedIds") or []:
                    target_int = _as_int(target)
                    if target_int not in (1, 2):
                        continue
                    pair_key = (source_iid, target_int, amount_int)
                    if pair_key in emitted_pairs:
                        continue
                    emitted_pairs.add(pair_key)
                    target_seat = target_int
                    source_ref = ((cur.objects or {}).get(source_iid)
                                  or ((prev.objects or {}).get(source_iid)
                                      if prev else None))
                    attacker_seat = (getattr(source_ref, "controller_seat", None)
                                     or getattr(source_ref, "owner_seat", None))
                    if attacker_seat is None:
                        attacker_seat = 1 if target_seat == 2 else 2
                    out.append(Event(
                        kind=ev.COMBAT_DAMAGE,
                        seat=attacker_seat,
                        payload={"amount": amount_int,
                                 "source_instance": source_iid,
                                 "target_seat": target_seat},
                        ts=ts_base + idx * 0.01,
                        salience=ev.SALIENCE_HIGH))
                    idx += 1
            return out
        except Exception:
            return []

    def detect_life_change(self, prev, cur):
        """players[seat].life differs prev -> cur; salience per ladder."""
        try:
            players_prev = (prev.players or {}) if prev is not None else {}
            players_cur = (cur.players or {}) if cur is not None else {}
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for seat in sorted(players_cur):
                life_now = getattr(players_cur[seat], "life", None)
                before = players_prev.get(seat)
                life_before = getattr(before, "life", None) \
                    if before is not None else None
                if life_now is None or life_before is None \
                        or life_now == life_before:
                    continue
                delta = life_now - life_before
                salience = life_salience(delta, life_now, self.cfg)
                out.append(Event(kind=ev.LIFE_CHANGE,
                                 seat=seat,
                                 payload={"from": life_before,
                                          "to": life_now,
                                          "delta": delta},
                                 ts=ts_base + idx * 0.01,
                                 salience=salience))
                idx += 1
            return out
        except Exception:
            return []

    def detect_board_shift(self, prev, cur):
        """Creature-count / summed-power deltas crossing thresholds."""
        try:
            counts_now, powers_now = self._board_stats(cur)
            counts_before, powers_before = self._board_stats(prev)
            seats = set(counts_now) | set(counts_before) \
                | set(powers_now) | set(powers_before)
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for seat in sorted(s for s in seats if s is not None):
                c_now = counts_now.get(seat, 0)
                c_before = counts_before.get(seat, 0)
                p_now = powers_now.get(seat, 0)
                p_before = powers_before.get(seat, 0)
                dc = c_now - c_before
                dp = p_now - p_before
                fired = abs(dc) >= self.cfg["board_creatures_delta"] \
                    or abs(dp) >= self.cfg["board_power_delta"]
                doubled_cumulative = False
                base_c = (self._baseline_creatures or {}).get(seat, 0)
                base_p = (self._baseline_power or {}).get(seat, 0)
                if base_c and c_now >= base_c * 2 and c_now > c_before:
                    doubled_cumulative = True
                if base_p and p_now >= base_p * 2 and p_now > p_before:
                    doubled_cumulative = True
                if not (fired or doubled_cumulative):
                    continue
                out.append(Event(kind=ev.BOARD_SHIFT,
                                 seat=seat,
                                 payload={"creatures_before": c_before,
                                          "creatures_after": c_now,
                                          "power_before": p_before,
                                          "power_after": p_now},
                                 ts=ts_base + idx * 0.01,
                                 salience=ev.SALIENCE_LOW))
                idx += 1
            # Update cumulative baselines once a game has a real board.
            if self._baseline_creatures is None and counts_now:
                self._baseline_creatures = dict(counts_now)
                self._baseline_power = dict(powers_now)
            return out
        except Exception:
            return []

    def _board_stats(self, state):
        """Per-seat creature counts and summed power from a snapshot."""
        counts: dict[Any, int] = {}
        powers: dict[Any, int] = {}
        try:
            bf_ids = _zone_object_ids(state, ("battlefield",))
            objs = (state.objects or {}) if state is not None else {}
            for iid in bf_ids:
                ref4 = objs.get(iid)
                if ref4 is None:
                    continue
            for iid in bf_ids:
                ref4 = objs.get(iid)
                if ref4 is None:
                    continue
                ctypes = getattr(ref4, "card_types", ()) or ()
                if "creature" not in ctypes:
                    continue
                seat = getattr(ref4, "controller_seat", None)
                counts[seat] = counts.get(seat, 0) + 1
                power_val = getattr(ref4, "power", None)
                if power_val is not None:
                    powers[seat] = powers.get(seat, 0) + power_val
        except Exception:
            pass
        return counts, powers

    def detect_turn_start(self, prev, cur):
        """turn_info.active_player changes prev -> cur; salience FILLER."""
        try:
            ap_now = getattr(getattr(cur, "turn_info", None), "active_player",
                             None)
            ap_before = getattr(getattr(prev, "turn_info", None),
                                "active_player", None) if prev else None
            if ap_now is None or ap_before is None or ap_now == ap_before:
                return []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            return [Event(kind=ev.TURN_START,
                          seat=ap_now,
                          payload={"active_player": ap_now},
                          ts=ts_base,
                          salience=ev.SALIENCE_FILLER)]
        except Exception:
            return []

    def detect_game_start(self, prev, cur):
        """First snapshot of a game: stage Start -> Play transition."""
        try:
            stage_now = self._latest_stage(self._window_msgs)
            stage_before = self._last_stage
            if stage_now != "gamestage_play":
                return []
            if stage_before == "gamestage_play":
                return []
            self._last_stage = stage_now
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            return [Event(kind=ev.GAME_START,
                          seat=None,
                          payload={},
                          ts=ts_base,
                          salience=ev.SALIENCE_MUST_SPEAK)]
        except Exception:
            return []

    def _latest_stage(self, msgs):
        """Last gameInfo.stage seen across window payloads (normalized)."""
        stage = None
        try:
            for msg in msgs:
                kind = getattr(msg, "kind", "") or ""
                if kind not in ("gre.GameStateMessage",
                                "gre.QueuedGameStateMessage"):
                    continue
                gsm = _gsm_of(msg)
                gi = gsm.get("gameInfo")
                if isinstance(gi, Mapping) and gi.get("stage"):
                    stage = _norm_enum(gi.get("stage"))
        except Exception:
            pass
        return stage

    def detect_game_end(self, prev, cur):
        """gameInfo.stage becomes GameStage_GameOver this window."""
        try:
            stage_now = self._latest_stage(self._window_msgs)
            if stage_now != "gamestage_gameover":
                return []
            if self._last_stage == "gamestage_gameover":
                return []
            self._last_stage = stage_now
            winner, reason = self._game_result(self._window_msgs)
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            payload: dict[str, Any] = {}
            if winner is not None:
                payload["winning_team_id"] = winner
            if reason is not None:
                payload["reason"] = reason
            return [Event(kind=ev.GAME_END,
                          seat=None,
                          payload=payload,
                          ts=ts_base,
                          salience=ev.SALIENCE_MUST_SPEAK)]
        except Exception:
            return []

    def _game_result(self, msgs):
        """(winningTeamId, reason) from the last results[] seen this window."""
        winner = None
        reason = None
        try:
            for msg in msgs:
                kind = getattr(msg, "kind", "") or ""
                if kind not in ("gre.GameStateMessage",
                                "gre.QueuedGameStateMessage"):
                    continue
                gsm = _gsm_of(msg)
                gi = gsm.get("gameInfo")
                if not isinstance(gi, Mapping):
                    continue
                results = gi.get("results")
                if not isinstance(results, list):
                    continue
                for res in results:
                    if not isinstance(res, Mapping):
                        continue
                    scope = res.get("scope")
                    if scope == "MatchScope_Game" or scope is None:
                        w = _as_int(res.get("winningTeamId"))
                        rsn = res.get("reason")
                        if w is not None:
                            winner = w
                        if isinstance(rsn, str) and rsn:
                            reason = rsn
        except Exception:
            pass
        return winner, reason

    def detect_match_start_end(self, prev, cur):
        """match_start on match_id appearing/changing; match_end TODO.

        match_end hooks room_state.completed which does not occur in the
        recorded fixtures yet -- forward-compatible path only.
        """
        try:
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0

            match_id_now = getattr(getattr(cur, "match_meta", None),
                                   "match_id", None)
            match_id_before = self._last_match_id
            if match_id_now and match_id_now != match_id_before:
                self._last_match_id = match_id_now
                out.append(Event(kind=ev.MATCH_START,
                                 seat=None,
                                 payload={"match_id": match_id_now},
                                 ts=ts_base,
                                 salience=ev.SALIENCE_MUST_SPEAK))

            for msg in self._window_msgs:
                kind = getattr(msg, "kind", "") or ""
                if kind == "room_state.completed":
                    # TODO: verify payload shape once completed-room fixtures
                    # exist; emit conservatively with no payload extras.
                    out.append(Event(kind=ev.MATCH_END,
                                     seat=None,
                                     payload={},
                                     ts=getattr(msg, "ts", ts_base),
                                     salience=ev.SALIENCE_MUST_SPEAK))
                    break
            return out
        except Exception:
            return []

    def _get_card_info(self, grp_id):
        if not grp_id:
            return None
        if self.card_lookup:
            try:
                return self.card_lookup(grp_id)
            except Exception:
                return None
        return None

    def _land_count_for_seat(self, state, seat):
        if not state:
            return 0
        bf_ids = _zone_object_ids(state, ("battlefield",))
        count = 0
        for iid in bf_ids:
            ref = (getattr(state, "objects", None) or {}).get(iid)
            if not ref:
                continue
            owner = getattr(ref, "controller_seat", None)
            if owner is None:
                owner = getattr(ref, "owner_seat", None)
            if owner == seat and "land" in (getattr(ref, "card_types", ()) or ()):
                count += 1
        return count

    def detect_hand_online(self, prev, cur):
        """When local player's land count increases, detect cards in hand reaching playable mana threshold."""
        try:
            local_seat = getattr(cur, "local_seat", None)
            if local_seat is None:
                return []
            match_id = getattr(getattr(cur, "match_meta", None), "match_id", None) or "local"

            prev_lands = self._land_count_for_seat(prev, local_seat)
            cur_lands = self._land_count_for_seat(cur, local_seat)
            if cur_lands <= prev_lands or cur_lands < 2:
                return []

            hand_zone = None
            for zv in (getattr(cur, "zones", None) or {}).values():
                if _norm_zone_type(getattr(zv, "zone_type", "")) == "hand" and getattr(zv, "owner_seat", None) == local_seat:
                    hand_zone = zv
                    break
            if not hand_zone or not getattr(hand_zone, "object_ids", ()):
                return []

            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            for iid in hand_zone.object_ids:
                ref = (getattr(cur, "objects", None) or {}).get(iid)
                if not ref:
                    continue
                grp_id = getattr(ref, "grp_id", None)
                if grp_id is None or (match_id, grp_id) in self._seen_hand_online:
                    continue

                info = self._get_card_info(grp_id)
                name = getattr(ref, "name", None) or (getattr(info, "name", None) if info else None)
                if not name:
                    continue

                if "land" in (getattr(ref, "card_types", ()) or ()):
                    continue

                mana_cost = getattr(info, "mana_cost", "") if info else ""
                cmc = calculate_cmc(mana_cost)
                if cmc <= 0:
                    continue

                if prev_lands < cmc <= cur_lands:
                    is_notable = (info and (is_bomb(info) or is_sweeper(name) or is_tutor(name))) or cmc >= 3
                    if is_notable:
                        self._seen_hand_online.add((match_id, grp_id))
                        salience = ev.SALIENCE_HIGH if (info and is_bomb(info)) else ev.SALIENCE_LOW
                        out.append(Event(
                            kind=ev.HAND_ONLINE,
                            seat=local_seat,
                            payload={
                                "card_name": name,
                                "name": name,
                                "land_count": cur_lands,
                                "cmc": cmc,
                            },
                            ts=ts_base,
                            salience=salience,
                        ))
                        break
            return out
        except Exception:
            return []

    def detect_tutor_anticipation(self, prev, cur):
        """When a tutor is cast onto the stack, identify prime targets remaining in the library."""
        try:
            prev_stack = _zone_object_ids(prev, ("stack",))
            cur_stack = _zone_object_ids(cur, ("stack",))
            new_ids = sorted(cur_stack - prev_stack)
            if not new_ids:
                return []

            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for iid in new_ids:
                if iid in self._seen_tutors_cast:
                    continue
                ref = (getattr(cur, "objects", None) or {}).get(iid)
                if not ref:
                    continue
                name = getattr(ref, "name", None)
                if not name or not is_tutor(name):
                    continue
                self._seen_tutors_cast.add(iid)
                seat = getattr(ref, "controller_seat", None) or getattr(ref, "owner_seat", None)

                target_name = "key answer"
                target_count = 1
                if seat == getattr(cur, "local_seat", None):
                    rem = remaining_deck_cards(cur)
                    if rem:
                        # S7.9: respect corrected accounting -- when exact
                        # composition is uncertain, do not claim exact counts.
                        rem_uncertain = bool(getattr(rem, "uncertain", False))
                        candidates: list[tuple[str, int, int]] = []
                        for gid, cnt in rem.items():
                            info = self._get_card_info(gid)
                            cname = getattr(info, "name", None) if info else None
                            if not cname:
                                continue
                            prio = 1
                            if is_sweeper(cname):
                                prio = 3
                            elif info and is_bomb(info):
                                prio = 2
                            candidates.append((cname, cnt, prio))
                        if candidates:
                            candidates.sort(key=lambda c: (-c[2], -c[1], c[0]))
                            target_name = candidates[0][0]
                            target_count = None if rem_uncertain \
                                else candidates[0][1]

                out.append(Event(
                    kind=ev.TUTOR_ANTICIPATION,
                    seat=seat,
                    payload={
                        "card_name": name,
                        "name": name,
                        "target_name": target_name,
                        "target_count": target_count,
                    },
                    ts=ts_base + idx * 0.01,
                    salience=ev.SALIENCE_HIGH,
                ))
                idx += 1
            return out
        except Exception:
            return []

    def detect_graveyard_recursion(self, prev, cur):
        """Detect objects moving from graveyard to stack or battlefield."""
        try:
            if not prev or not cur:
                return []
            prev_gy = _zone_object_ids(prev, ("graveyard",))
            cur_active = _zone_object_ids(cur, ("stack", "battlefield"))
            recursed = sorted(prev_gy & cur_active)
            if not recursed:
                return []
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for iid in recursed:
                if iid in self._seen_graveyard_recursions:
                    continue
                self._seen_graveyard_recursions.add(iid)
                ref = (getattr(cur, "objects", None) or {}).get(iid) or (getattr(prev, "objects", None) or {}).get(iid)
                name = getattr(ref, "name", None)
                if not name:
                    continue
                seat = getattr(ref, "controller_seat", None) or getattr(ref, "owner_seat", None)
                out.append(Event(
                    kind=ev.GRAVEYARD_RECURSION,
                    seat=seat,
                    payload={
                        "card_name": name,
                        "name": name,
                    },
                    ts=ts_base + idx * 0.01,
                    salience=ev.SALIENCE_HIGH,
                ))
                idx += 1
            return out
        except Exception:
            return []

    def detect_archetype(self, prev, cur):
        """Fingerprint competitive archetype from early played cards or companion."""
        try:
            match_id = getattr(getattr(cur, "match_meta", None), "match_id", None) or "local"
            prev_bf = _zone_object_ids(prev, ("battlefield",))
            cur_bf = _zone_object_ids(cur, ("battlefield",))
            new_bf = sorted(cur_bf - prev_bf)

            for iid in new_bf:
                ref = (getattr(cur, "objects", None) or {}).get(iid)
                if not ref:
                    continue
                name = getattr(ref, "name", None)
                seat = getattr(ref, "controller_seat", None) or getattr(ref, "owner_seat", None)
                if name and seat is not None:
                    self._cards_played_by_seat.setdefault(seat, set()).add(name)

            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for seat, played_cards in list(self._cards_played_by_seat.items()):
                if (match_id, seat) in self._detected_archetypes:
                    continue
                archetype = detect_archetype(played_cards)
                if archetype:
                    self._detected_archetypes.add((match_id, seat))
                    sig_card = sorted(played_cards)[0]
                    out.append(Event(
                        kind=ev.ARCHETYPE_DETECTED,
                        seat=seat,
                        payload={
                            "archetype_name": archetype,
                            "signature_card": sig_card,
                        },
                        ts=ts_base + idx * 0.01,
                        salience=ev.SALIENCE_HIGH,
                    ))
                    idx += 1
            return out
        except Exception:
            return []

    def detect_counter_war(self, prev, cur):
        """Detect when multiple counterspells/instants fight across the stack."""
        try:
            match_id = getattr(getattr(cur, "match_meta", None), "match_id", None) or "local"
            cur_stack = _zone_object_ids(cur, ("stack",))
            if len(cur_stack) < 2:
                return []

            counter_names = []
            top_iid = None
            bottom_name = "spell"
            for idx, iid in enumerate(sorted(cur_stack)):
                ref = (getattr(cur, "objects", None) or {}).get(iid)
                if not ref:
                    continue
                name = getattr(ref, "name", None)
                if idx == 0 and name:
                    bottom_name = name
                top_iid = iid
                if name and is_counterspell(name):
                    counter_names.append(name)

            if len(counter_names) >= 2 or (len(cur_stack) >= 3 and len(counter_names) >= 1):
                key = (match_id, top_iid)
                if key not in self._seen_counter_wars:
                    self._seen_counter_wars.add(key)
                    ts_base = getattr(cur, "ts", 0.0) or 0.0
                    return [Event(
                        kind=ev.COUNTER_WAR,
                        seat=None,
                        payload={
                            "card_name": bottom_name,
                            "name": bottom_name,
                            "initial_spell": bottom_name,
                            "depth": len(cur_stack),
                            "stack_depth": len(cur_stack),
                            "counter_count": len(counter_names),
                        },
                        ts=ts_base,
                        salience=ev.SALIENCE_HIGH,
                    )]
            return []
        except Exception:
            return []

    def detect_combat_trick(self, prev, cur):
        """Detect instant-speed buffs or interactions deployed during combat steps."""
        try:
            phase = (getattr(getattr(cur, "turn_info", None), "phase", "") or "").lower()
            in_combat = any(s in phase for s in ("declareattackers", "declareblockers", "begincombat", "firststrike", "combat"))
            if not in_combat:
                return []

            prev_stack = _zone_object_ids(prev, ("stack",))
            cur_stack = _zone_object_ids(cur, ("stack",))
            new_ids = sorted(cur_stack - prev_stack)
            if not new_ids:
                return []

            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for iid in new_ids:
                if iid in self._seen_combat_tricks:
                    continue
                ref = (getattr(cur, "objects", None) or {}).get(iid)
                if not ref:
                    continue
                name = getattr(ref, "name", None)
                types = getattr(ref, "card_types", ()) or ()
                is_instant = "instant" in types or (name and is_combat_trick(name))
                if not is_instant:
                    continue

                self._seen_combat_tricks.add(iid)
                seat = getattr(ref, "controller_seat", None) or getattr(ref, "owner_seat", None)
                out.append(Event(
                    kind=ev.COMBAT_TRICK,
                    seat=seat,
                    payload={
                        "card_name": name or "a combat trick",
                        "name": name or "a combat trick",
                        "phase": phase,
                    },
                    ts=ts_base + idx * 0.01,
                    salience=ev.SALIENCE_HIGH,
                ))
                idx += 1
            return out
        except Exception:
            return []

    def detect_chump_block(self, prev, cur):
        """Detect sacrificial blocks where a small creature blocks a lethal or heavy attacker."""
        try:
            match_id = getattr(getattr(cur, "match_meta", None), "match_id", None) or "local"
            blocks = self.detect_block_declared(prev, cur)
            if not blocks:
                return []
            payload = blocks[0].payload or {}
            block_list = payload.get("blocks") or []
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for blk in block_list:
                bid = blk.get("blocker_instance_id")
                aids = blk.get("attacker_instance_ids") or []
                if not bid or not aids:
                    continue
                aid = aids[0]
                key = (match_id, bid, aid)
                if key in self._seen_chump_blocks:
                    continue

                b_ref = (getattr(cur, "objects", None) or {}).get(bid) or (getattr(prev, "objects", None) or {}).get(bid)
                a_ref = (getattr(cur, "objects", None) or {}).get(aid) or (getattr(prev, "objects", None) or {}).get(aid)
                if not b_ref or not a_ref:
                    continue

                bp = getattr(b_ref, "power", None) or 0
                bt = getattr(b_ref, "toughness", None) or 1
                ap = getattr(a_ref, "power", None) or 0
                at = getattr(a_ref, "toughness", None) or 1

                if ap >= bt and bp < at and (ap >= 3 or ap >= bt * 2):
                    self._seen_chump_blocks.add(key)
                    b_name = getattr(b_ref, "name", None) or "blocker"
                    a_name = getattr(a_ref, "name", None) or "attacker"
                    seat = getattr(b_ref, "controller_seat", None) or getattr(b_ref, "owner_seat", None)
                    out.append(Event(
                        kind=ev.CHUMP_BLOCK,
                        seat=seat,
                        payload={
                            "card_name": b_name,
                            "blocker_name": b_name,
                            "attacker_name": a_name,
                            "attacker_power": ap,
                        },
                        ts=ts_base + idx * 0.01,
                        salience=ev.SALIENCE_LOW,
                    ))
                    idx += 1
            return out
        except Exception:
            return []

    def detect_hand_sculpting(self, prev, cur):
        """Detect when multiple cantrips or filtering spells are cast in a single turn."""
        try:
            prev_stack = _zone_object_ids(prev, ("stack",))
            cur_stack = _zone_object_ids(cur, ("stack",))
            new_ids = sorted(cur_stack - prev_stack)
            if not new_ids:
                return []

            turn = getattr(getattr(cur, "turn_info", None), "turn_number", 0) or 0
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for iid in new_ids:
                ref = (getattr(cur, "objects", None) or {}).get(iid)
                if not ref:
                    continue
                name = getattr(ref, "name", None)
                if not name or not is_cantrip_or_filter(name):
                    continue
                seat = getattr(ref, "controller_seat", None) or getattr(ref, "owner_seat", None)
                key = (turn, seat)
                cast_list = self._cantrips_cast_this_turn.setdefault(key, [])
                cast_list.append(name)
                if len(cast_list) == 2:
                    out.append(Event(
                        kind=ev.HAND_SCULPTING,
                        seat=seat,
                        payload={
                            "count": 2,
                            "card_name": name,
                            "first_card": cast_list[0],
                            "second_card": name,
                        },
                        ts=ts_base + idx * 0.01,
                        salience=ev.SALIENCE_LOW,
                    ))
                    idx += 1
            return out
        except Exception:
            return []

    def detect_unfair_play(self, prev, cur):
        """Detect high-CMC (>= 6) bombs entering the battlefield early (turn <= 4)."""
        try:
            prev_bf = _zone_object_ids(prev, ("battlefield",))
            cur_bf = _zone_object_ids(cur, ("battlefield",))
            new_ids = sorted(cur_bf - prev_bf)
            if not new_ids:
                return []

            turn = getattr(getattr(cur, "turn_info", None), "turn_number", 0) or 0
            if turn > 4 or turn <= 0:
                return []

            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for iid in new_ids:
                if iid in self._seen_unfair_plays:
                    continue
                ref = (getattr(cur, "objects", None) or {}).get(iid)
                if not ref:
                    continue
                types = getattr(ref, "card_types", ()) or ()
                if "land" in types:
                    continue
                grp_id = getattr(ref, "grp_id", None)
                info = self._get_card_info(grp_id) if grp_id else None
                mana_cost = getattr(info, "mana_cost", "") if info else ""
                cmc = calculate_cmc(mana_cost)
                if cmc < 6:
                    continue

                self._seen_unfair_plays.add(iid)
                name = getattr(ref, "name", None) or (getattr(info, "name", None) if info else "a threat")
                seat = getattr(ref, "controller_seat", None) or getattr(ref, "owner_seat", None)
                out.append(Event(
                    kind=ev.UNFAIR_PLAY,
                    seat=seat,
                    payload={
                        "card_name": name,
                        "name": name,
                        "cmc": cmc,
                        "turn": turn,
                    },
                    ts=ts_base + idx * 0.01,
                    salience=ev.SALIENCE_HIGH,
                ))
                idx += 1
            return out
        except Exception:
            return []

    # ------------------------------------------- omniscient strategic ([O2])
    #
    # Honesty discipline for these detectors:
    # - Every claim gates on ITS OWN SeatKnowledge prerequisites (never the
    #   blanket is_omniscient flag).
    # - Unsupported states SUPPRESS entirely -- emit nothing rather than guess.
    # - The deterministic clock is float(snapshot_id) when the snapshot carries
    #   no wall-clock ts (same stand-in story.py documents).

    def _clock(self, state):
        """Receiver ts when present, else float(snapshot_id) stand-in."""
        try:
            ts = getattr(state, "ts", None)
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                return float(ts)
            return float(getattr(state, "snapshot_id", 0) or 0)
        except Exception:
            return 0.0

    def _seat_knowledge(self, state, seat):
        try:
            sk = (getattr(state, "seat_knowledge", None) or {}).get(seat)
            return sk
        except Exception:
            return None

    def _hand_fresh(self, sk, clock, tolerance):
        """True only when hand_fresh_asof exists and is within tolerance."""
        try:
            if sk is None or not getattr(sk, "hand_visible", False):
                return False
            hfa = getattr(sk, "hand_fresh_asof", None)
            if not isinstance(hfa, (int, float)) or isinstance(hfa, bool):
                return False
            return abs(float(clock) - float(hfa)) <= float(tolerance)
        except Exception:
            return False

    def _visible_hand_refs(self, state, seat):
        """Actual visible hand contents for ``seat`` ([] when invisible)."""
        out = []
        try:
            objs = (getattr(state, "objects", None) or {})
            for zone in (getattr(state, "zones", None) or {}).values():
                if _norm_zone_type(getattr(zone, "zone_type", "")) != "hand":
                    continue
                if getattr(zone, "owner_seat", None) != seat:
                    continue
                for iid in getattr(zone, "object_ids", ()) or ():
                    ref = objs.get(iid)
                    if ref is not None:
                        out.append(ref)
        except Exception:
            return []
        return out

    def _known_mana_lands(self, state, seat):
        """Count of VISIBLE lands backing ``seat``; 0 == mana unknown.

        Mana availability is claimed ONLY from observable battlefield lands.
        No visible lands -> unknown -> callers must SUPPRESS.
        """
        try:
            bf_ids = _zone_object_ids(state, ("battlefield",))
            objs = (getattr(state, "objects", None) or {})
            count = 0
            for iid in bf_ids:
                ref = objs.get(iid)
                if ref is None:
                    continue
                owner = (getattr(ref, "controller_seat", None)
                         or getattr(ref, "owner_seat", None))
                if owner == seat and "land" in (getattr(ref,
                                                    "card_types",
                                                    ()) or ()):
                    count += 1
            return count
        except Exception:
            return 0

    def _public_casts_this_window(self, prev, cur):
        """[{name, seat}] of public casts observed this window.

        Mirrors detect_cast's authoritative sources without touching its
        dedupe state so sibling detectors stay independent.
        """
        out = []
        try:
            objs = (cur.objects or {}) if cur else {}
            objs_prev = (prev.objects or {}) if prev else {}
            seen_iids = set()

            for ann in _iter_annotations(self._window_msgs):
                types = _annotation_types(ann)
                if "AnnotationType_ZoneTransfer" not in types:
                    continue
                details = _detail_map(ann.get("details"))
                if "cast" not in str(details.get("category") or "").lower():
                    continue
                for aid in ann.get("affectedIds") or []:
                    iid = _as_int(aid)
                    if iid is None or iid in seen_iids:
                        continue
                    seen_iids.add(iid)
                    ref = objs.get(iid) or objs_prev.get(iid)
                    seat = (getattr(ref, "controller_seat", None)
                            or getattr(ref, "owner_seat", None)
                            or _as_int(ann.get("affectorId")))
                    out.append({"name": getattr(ref, "name", None),
                                "seat": seat})

            prev_stack = _zone_object_ids(prev, ("stack",))
            cur_stack = _zone_object_ids(cur, ("stack",))
            spell_types = ("creature", "instant", "sorcery", "enchantment",
                           "artifact", "planeswalker", "battle")
            for iid in sorted(cur_stack - prev_stack):
                if iid in seen_iids:
                    continue
                ref = objs.get(iid)
                if ref is None:
                    continue
                if not any(t in (getattr(ref, "card_types", ()) or ())
                           for t in spell_types):
                    continue
                seen_iids.add(iid)
                out.append({"name": getattr(ref, "name", None),
                            "seat": getattr(ref, "controller_seat", None)
                            or getattr(ref, "owner_seat", None)})

            is_real_game = any(_gsm_of(m).get("gameStateId") is not None
                               for m in self._window_msgs)
            if not out and not is_real_game:
                for seat_id, action in _iter_actions(self._window_msgs):
                    if (_norm_enum(action.get("actionType"))
                            != "actiontype_cast"):
                        continue
                    iid = _as_int(action.get("instanceId"))
                    if iid is None or iid in seen_iids:
                        continue
                    seen_iids.add(iid)
                    ref = objs.get(iid) if iid is not None else None
                    out.append({
                        "name": (getattr(ref, "name", None)
                                 if ref is not None else None),
                        "seat": seat_id})
        except Exception:
            return out
        return out

    def detect_trap_armed(self, prev, cur):
        """ARMED: fresh visible hand holds a reactive card behind known mana.

        Prerequisites (each independently required; ANY miss -> []):
        - target seat's hand_visible AND hand_fresh_asof within tolerance;
        - mana KNOWN for that seat from visible battlefield lands
          (< trap_min_lands visible lands == unknown -> SUPPRESS);
        - a reactive card actually sits in the visible hand;
        - no live armed entry already registered for that seat.
        """
        try:
            clock = self._clock(cur)
            tol = self.cfg.get("trap_hand_fresh_tolerance", 5.0)
            min_lands = int(self.cfg.get("trap_min_lands", 1))
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0

            knowledge = getattr(cur, "seat_knowledge", None) or {}
            for seat in sorted(knowledge):
                sk = knowledge.get(seat)
                if sk is None:
                    continue
                if not self._hand_fresh(sk, clock, tol):
                    continue  # stale/invisible hand -> suppress entirely
                lands_seen = self._known_mana_lands(cur, seat)
                if lands_seen < min_lands:
                    continue  # mana unknown -> conservative SUPPRESS
                # Already armed? one live trap per seat per game.
                existing = self._armed_traps.get(seat)
                if existing is not None:
                    # Lazily expire stale entries so they never refire.
                    if clock - existing["armed_clock"] \
                            > float(self.cfg.get("trap_validity_window",
                                                 30.0)):
                        self._armed_traps.pop(seat, None)
                    else:
                        continue

                threat_name = None
                threat_grp = None
                for ref in self._visible_hand_refs(cur, seat):
                    name = getattr(ref, "name", None)
                    if name and is_counterspell(name):
                        threat_name = name
                        threat_grp = getattr(ref, "grp_id", None)
                        break
                if threat_name is None:
                    continue  # nothing reactive visibly held -> no claim

                self._armed_traps[seat] = {
                    "threat_name": threat_name,
                    "grp_id": threat_grp,
                    "armed_clock": clock,
                }
                out.append(Event(
                    kind=ev.TRAP_ARMED,
                    seat=seat,
                    payload={"seat": seat, "threat_name": threat_name},
                    ts=ts_base,
                    salience=ev.SALIENCE_LOW))
            return out
        except Exception:
            return []

    def detect_trap_sprung(self, prev, cur):
        """SPRUNG: a public cast walks into a LIVE armed trap in-window.

        Requires an armed registry entry whose validity window still covers
        the cast; matching clears the entry so stale traps never refire.
        """
        try:
            if not self._armed_traps:
                return []
            clock = self._clock(cur)
            window = float(self.cfg.get("trap_validity_window", 30.0))

            # Expire dead entries first.
            for seat in sorted(list(self._armed_traps)):
                entry = self._armed_traps.get(seat)
                if entry is not None and clock - entry["armed_clock"] > window:
                    self._armed_traps.pop(seat, None)

            casts = self._public_casts_this_window(prev, cur)
            if not casts:
                return []

            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for trap_seat in sorted(list(self._armed_traps)):
                entry = self._armed_traps[trap_seat]
                for cast in casts:
                    caster = cast.get("seat")
                    if caster is None or caster == trap_seat:
                        continue  # own cast never springs own trap
                    victim_name = cast.get("name")
                    self._armed_traps.pop(trap_seat, None)  # clear: no refire
                    out.append(Event(
                        kind=ev.TRAP_SPRUNG,
                        seat=caster,
                        payload={"victim_name": victim_name},
                        ts=ts_base + idx * 0.01,
                        salience=ev.SALIENCE_HIGH))
                    idx += 1
                    break  # one spring per armed entry per window
            return out
        except Exception:
            return []

    def detect_bluff(self, prev, cur):
        """Qualified priority-delay observation from VERIFIED facts only.

        Fires ONLY when ALL hold:
        - the seat's hand is visible AND fresh (own tolerance gate);
        - mana is known from visible battlefield lands;
        - verified delay evidence: the active player CHANGED to this seat this
          window (they demonstrably received priority) AND they publicly cast
          nothing this window;
        - the visible hand actually holds a reactive card.
        Payload carries ONLY a template-facing summary string built from the
        REAL visible cards -- never an outright intent assertion.
        """
        try:
            clock = self._clock(cur)
            tol = self.cfg.get("bluff_hand_fresh_tolerance", 5.0)

            active_now = getattr(getattr(cur, "turn_info", None),
                                 "active_player", None)
            active_before = (getattr(getattr(prev, "turn_info", None),
                                     "active_player", None)
                             if prev else None)
            delay_evidence = (active_now is not None
                              and active_now != active_before)

            casts_by_me: set[int | None] = set()
            for cast in self._public_casts_this_window(prev, cur):
                casts_by_me.add(cast.get("seat"))

            knowledge = getattr(cur, "seat_knowledge", None) or {}
            out = []
            ts_base = getattr(cur, "ts", 0.0) or 0.0
            idx = 0
            for seat in sorted(knowledge):
                sk = knowledge.get(seat)
                if sk is None:
                    continue
                if not self._hand_fresh(sk, clock, tol):
                    continue  # invisible/stale hand -> suppress entirely
                if not delay_evidence or active_now != seat:
                    continue  # no verified priority delay -> suppress
                if seat in casts_by_me:
                    continue  # they acted; no delay to explain
                if self._known_mana_lands(cur, seat) < 1:
                    continue  # mana unknown -> suppress

                held_names: list[str] = []
                reactive_seen = False
                for ref in self._visible_hand_refs(cur, seat):
                    name = getattr(ref, "name", None)
                    if not name:
                        continue  # summary uses REAL cards only
                    held_names.append(name)
                    if is_counterspell(name):
                        reactive_seen = True
                if not reactive_seen or not held_names:
                    continue

                counts: dict[str, int] = {}
                for name in held_names:
                    counts[name] = counts.get(name, 0) + 1
                parts = []
                for name in sorted(counts):
                    n = counts[name]
                    word = {2: "two", 3: "three", 4: "four",
                            5: "five"}.get(n)
                    parts.append(f"{word} {name}s" if word and n > 1
                                 else f"{n} {name}" if n > 1 else name)
                summary = ("the hand we can see holds "
                           + ", ".join(parts))
                out.append(Event(
                    kind=ev.BLUFF_DETECTED,
                    seat=seat,
                    payload={"visible_hand_summary": summary},
                    ts=ts_base + idx * 0.01,
                    salience=ev.SALIENCE_LOW))
                idx += 1
            return out
        except Exception:
            return []

    # ------------------------------------------------------- debounce / merge

    def debounce(self, events):
        """Merge correlated events within the configured window.

        - cast + resolve of the same instance -> single RESOLVE with
          {'cast_name', ..., 'resolved': True}
        - cast + counter -> single COUNTER with {'cast_name', ...}
        - multiple land drops in one window -> one LAND_DROP with count/names.
        """
        try:
            window = float(self.cfg.get("debounce_window", 1.5))
            if window <= 0 or len(events) <= 1:
                return list(events)
            ordered = sorted(events, key=lambda e: e.ts)
            clusters: list[list[Event]] = [[ordered[0]]]
            for event in ordered[1:]:
                anchor_ts = clusters[-1][0].ts
                if event.ts - anchor_ts <= window:
                    clusters[-1].append(event)
                else:
                    clusters.append([event])
            merged: list[Event] = []
            for cluster in clusters:
                merged.extend(self._merge_cluster(cluster))
            return merged
        except Exception:
            return list(events)

    def _merge_cluster(self, cluster):
        """Apply merge rules to one time-cluster of events."""
        try:
            by_kind: dict[str, list[Event]] = {}
            for event in cluster:
                by_kind.setdefault(event.kind, []).append(event)

            # Multiple land drops -> one aggregated LAND_DROP.
            lands = by_kind.get(ev.LAND_DROP) or []
            if len(lands) > 1:
                names = [e.payload.get("name") for e in lands]
                seats = [e.seat for e in lands]
                seat_first: Any = next((s for s in seats if s is not None),
                                       None)
                agg = Event(kind=ev.LAND_DROP,
                            seat=seat_first,
                            payload={"count": len(lands), "names": names},
                            ts=min(e.ts for e in lands),
                            salience=max(e.salience for e in lands))
                cluster2: list[Event] = []
                consumed_agg = False
                for event in cluster:
                    if event.kind == ev.LAND_DROP:
                        if not consumed_agg:
                            cluster2.append(agg)
                            consumed_agg = True
                        continue
                    cluster2.append(event)
                cluster = cluster2

            # cast+resolve / cast+counter merges keyed by instance id.
            casts_by_iid: dict[Any, Event] = {}
            for event in by_kind.get(ev.CAST) or []:
                iid = event.payload.get("instance_id")
                if iid is not None:
                    casts_by_iid[iid] = event

            drop_ids = set()
            additions: list[Event] = []
            for kind_merged, flag_key in ((ev.RESOLVE, "resolved"),
                                          (ev.COUNTER, "countered")):
                for event in by_kind.get(kind_merged) or []:
                    iid2 = event.payload.get("instance_id")
                    if iid2 is None or iid2 not in casts_by_iid:
                        continue
                    cast_event = casts_by_iid[iid2]
                    cast_name = cast_event.payload.get("name")
                    merged_payload = dict(event.payload)
                    merged_payload["cast_name"] = cast_name
                    merged_payload[flag_key] = True
                    merged_event = Event(kind=event.kind,
                                         seat=event.seat,
                                         payload=merged_payload,
                                         ts=min(event.ts, cast_event.ts),
                                         salience=max(event.salience,
                                                      cast_event.salience))
                    drop_ids.add(id(cast_event))
                    drop_ids.add(id(event))
                    additions.append(merged_event)

            if drop_ids:
                cluster = [e for e in cluster if id(e) not in drop_ids]
                cluster.extend(additions)
            return cluster
        except Exception:
            return list(cluster)

    # ----------------------------------------------------- repetition suppress

    def suppress_repeats(self, events):
        """Drop events whose (kind, seat, core payload) repeats consecutively."""
        try:
            seen_sig: Any = None
            out: list[Event] = []
            for event in events:
                sig = (event.kind, event.seat,
                       tuple(sorted((k, str(v)) for k, v
                                    in event.payload.items())))
                if self.cfg.get("suppress_repeats") and sig == seen_sig:
                    continue
                seen_sig = sig
                out.append(event)
            return out
        except Exception:
            return list(events)
