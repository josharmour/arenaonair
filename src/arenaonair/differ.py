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
from .models import Event, GameState, GreMessage

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

    def __init__(self, config=None):
        self.cfg = dict(DEFAULT_DIFFER_CONFIG)
        try:
            self.cfg.update(dict(config or {}))
        except Exception:
            pass
        # Cross-window dedupe / continuity state
        self._seen_casts = set()          # (instanceId, seatId) tuples
        self._last_match_id = None
        self._last_stage = None
        self._baseline_creatures = None   # seat -> count at match start
        self._baseline_power = None       # seat -> summed power at match start
        self._window_msgs = []            # set per diff() call

    # ------------------------------------------------------------------ API

    def diff(self, prev, cur, msgs_since_prev):
        """Snapshot pair + intervening messages -> ordered events. Never raises."""
        try:
            return self._diff_inner(prev, cur, msgs_since_prev or [])
        except Exception:
            return []

    def _diff_inner(self, prev, cur, msgs):
        self._window_msgs = list(msgs)
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

        Counter heuristic: a stack->graveyard disappearance counts as a counter
        ONLY when another spell remains on the stack afterwards; otherwise it
        classifies as a resolve.
        """
        try:
            prev_stack = _zone_object_ids(prev, ("stack",))
            cur_stack = _zone_object_ids(cur, ("stack",))
            gone_ids = sorted(prev_stack - cur_stack)
            if not gone_ids:
                return []
            others_remain = bool(cur_stack)
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
                countered = dest == "graveyard" and others_remain
                if countered:
                    out.append(Event(
                        kind=ev.COUNTER,
                        seat=seat,
                        payload={"name": name,
                                 "countered_by_seat": self._counter_seat(cur)},
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

    def _counter_seat(self, cur):
        """Best-effort opposing seat of whoever countered."""
        try:
            seats = set()
            for zv in ((cur.zones or {}) if cur is not None else {}).values():
                if zv.owner_seat is not None:
                    seats.add(zv.owner_seat)
            ordered = sorted(s for s in seats if s is not None)
            return ordered[0] if ordered else None
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
