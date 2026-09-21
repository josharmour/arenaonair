"""Story model: game dynamics -> narrative events (the caster layer).

Implements ``arenaonair.interfaces.StoryModel`` per DESIGN.md §3.6.

Consumes immutable GameState snapshots only (never talks to the differ) and
maintains a lightweight, fully deterministic story model:

- **Momentum index** per seat -- a decayed score fed by observable snapshot
  deltas (damage dealt to opposing life, net creatures deployed vs removed,
  stack objects resolving away for a player).
- **Arc classification** -- ``even`` / ``pulling_away`` / ``comeback_brewing``
  / ``standoff`` / ``race``, derived from the momentum gap, life gap,
  battlefield power totals and recent damage cadence.
  ``narrative_arc`` events fire ONLY on transitions between arcs.
- **Resource ledger** -- mana screw / mana flood streaks plus hand-size
  pressure; ``narrative_resource`` fires once per streak episode at the exact
  threshold crossing.
- **Beat memory** -- bounded ring buffer (~20 beats) of notable occurrences;
  ``narrative_callback`` fires when a stored beat echoes >=3 internal turns
  later and the global callback cooldown (>=6 turns) has elapsed.
- **Speculation hook** -- pure ``classify_speculation(state)`` detects e.g.
  race shaping from public state only; emitted at most once per arc segment.

Determinism guarantees:
- No randomness anywhere; iteration order is always sorted/deque order.
- Turn counting uses a monotonic internal counter incremented when the active
  player changes (snapshot ``turn_info`` is unreliable per Wave-A notes).
- Identical snapshot sequences always yield identical event streams.
- Event ``ts`` carries ``float(snapshot_id)`` -- snapshots expose no wall-clock,
  so the monotonic snapshot id is the deterministic stand-in downstream layers
  may rely on for ordering.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping

from . import events as ev
from .models import CardRef, Event, GameState

__all__ = ["StoryModel", "classify_speculation", "DEFAULT_THRESHOLDS", "ARCS"]

# Arc vocabulary (plain strings -- enum-ish but serializable).
ARCS = ("even", "pulling_away", "comeback_brewing", "standoff", "race")

#: Constructor-configurable thresholds with sane defaults.
DEFAULT_THRESHOLDS: dict[str, float] = {
    # --- momentum ---
    "decay": 0.85,              # multiplicative decay applied every update
    "damage_score": 1.0,        # momentum per point of damage dealt to opponent
    "deploy_score": 0.5,        # momentum per creature deployed onto battlefield
    "remove_score": 0.5,        # momentum lost per own creature leaving battlefield
    "resolve_score": 0.5,       # momentum per own stack object resolving away
    "momentum_cap": 12.0,       # symmetric clamp on the index magnitude
    # --- arc classification ---
    "pull_momentum_gap": 4.0,      # momentum gap needed to pull away...
    "pull_life_gap": 6,            # ...paired with this much life separation...
    "pull_cadence": 2.0,           # ...or a sustained recent-damage advantage
    "comeback_life_gap": 6,        # trailing-by-life floor for comeback brewing
    "comeback_cadence": 3.0,       # trailer's recent damage output needed
    "cadence_window": 4,           # internal turns counted as "recent"
    "race_margin": 1.0,            # board power must exceed opposing life x margin
    "standoff_power": 10,          # combined p+t each side needs for a standoff
    "standoff_quiet_damage": 1.0,  # max recent total damage during a standoff
    # --- resource ledger ---
    "screw_turns": 3,              # consecutive land-drop-less turns -> screw event
    "flood_lands_per_turn": 2,     # lands added per turn to count as flooding...
    "flood_turns": 3,              # ...for this many consecutive turns -> event
    "hand_pressure_drop": 3,       # cards below recent peak -> hand pressure...
    "hand_pressure_floor": 4,      # ...but only if the peak was at least this big
    # --- beat memory / callbacks ---
    "beat_buffer": 20,             # ring-buffer capacity for stored beats
    "big_creature_pt": 8,          # power+toughness worth calling a death notable
    "low_life_threshold": 5,       # "dropped low" line for survival beats
    "callback_min_distance": 3,    # minimum internal turns since the beat...
    "callback_cooldown": 6,        # ...and minimum turns since last callback


}


# ---------------------------------------------------------------------------
# Pure helpers operating on snapshots (tolerant of odd data)
# ---------------------------------------------------------------------------

def _norm_type(token: Any) -> str:
    """Normalize 'CardType_Creature' / 'creature' style tokens to lowercase."""
    s = str(token or "")
    return s.split("_", 1)[-1].lower()


def _zone_refs(state: GameState, zone_kind: str) -> dict[int, CardRef]:
    """iid -> CardRef for all objects in zones of the given kind (deduped)."""
    out: dict[int, CardRef] = {}
    try:
        for zone in state.zones.values():
            if _norm_type(getattr(zone, "zone_type", "")) != zone_kind:
                continue
            for iid in getattr(zone, "object_ids", ()) or ():
                if iid in out:
                    continue
                ref = state.objects.get(iid)
                if ref is not None:
                    out[iid] = ref

    except Exception:
        return {}
    return out


def _battlefield_refs(state: GameState) -> dict[int, CardRef]:
    return _zone_refs(state, "battlefield")


def _stack_refs(state: GameState) -> dict[int, CardRef]:
    return _zone_refs(state, "stack")


def _controller_of(ref: CardRef) -> int | None:
    ctrl = getattr(ref, "controller_seat", None)
    if ctrl is not None:
        return ctrl

    return getattr(ref, "owner_seat", None)


def _of_type(refs: Mapping[int, CardRef], type_name: str) -> set[int]:
    """iid subset whose card_types include the normalized type name."""
    out: set[int] = set()
    try:
        items = list(refs.items())
    except Exception:
        return out

    for iid, ref in items:
        types = getattr(ref, "card_types", ()) or ()
        if any(_norm_type(t) == type_name for t in types):
            out.add(iid)
    return out


def _group_by_controller(refs: Mapping[int, CardRef],
                         iids: set[int]) -> dict[int | None, list[CardRef]]:
    out: dict[int | None, list[CardRef]] = {}
    for iid in sorted(iids):
        ref = refs.get(iid)
        if ref is None:
            continue
        out.setdefault(_controller_of(ref), []).append(ref)
    return out


def _power_total(refs) -> int:
    total = 0
    for ref in refs:
        p = getattr(ref, "power", None)
        if isinstance(p, bool):
            continue
        if isinstance(p, (int, float)):
            total += int(p)
    return total


def _pt_total(refs) -> int:
    total = 0
    for ref in refs:
        for attr in ("power", "toughness"):
            v = getattr(ref, attr, None)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                total += int(v)
    return total


def _lives(state: GameState) -> dict[Any, int | None]:
    """seat -> life (None when unknown); keys iterated in sorted order."""
    out: dict[Any, int | None] = {}
    try:
        keys = sorted(state.players.keys(), key=str)
    except Exception:
        keys = list(state.players.keys())
    for seat in keys:
        pv = state.players[seat]
        out[seat] = getattr(pv, "life", None)
    return out


def _known_seats(state: GameState) -> list[int]:
    seats: set[int] = set()
    for seat in _lives(state):
        if isinstance(seat, int):
            seats.add(seat)
    for ref in _battlefield_refs(state).values():
        ctrl = _controller_of(ref)
        if isinstance(ctrl, int):
            seats.add(ctrl)
    return sorted(seats)


def _hand_sizes(state: GameState) -> dict[int | None, int]:
    """seat -> visible hand size (only seats with a hand zone appear)."""
    out: dict[int | None, int] = {}
    try:
        for zone in state.zones.values():
            if _norm_type(getattr(zone, "zone_type", "")) != "hand":
                continue
            owner = getattr(zone, "owner_seat", None)
            ids = getattr(zone, "object_ids", ()) or ()
            out[owner] = max(out.get(owner, 0), len(ids))
    except Exception:
        return {}
    return out


def _event(kind: str, seat: int | None, payload: Mapping[str, Any],
           ts: float) -> Event:
    """Build a narrative Event with kind-appropriate salience."""
    salience = {
        ev.NARRATIVE_ARC: ev.SALIENCE_HIGH,
        ev.NARRATIVE_RESOURCE: ev.SALIENCE_LOW,
        ev.NARRATIVE_CALLBACK: ev.SALIENCE_LOW,
        ev.NARRATIVE_SPECULATION: ev.SALIENCE_LOW,
    }.get(kind, ev.SALIENCE_LOW)
    return Event(kind=kind, seat=seat,
                 payload=dict(payload), ts=float(ts), salience=salience)


# ---------------------------------------------------------------------------
# Speculation hook (pure function of the snapshot alone)
# ---------------------------------------------------------------------------

def classify_speculation(state: GameState,
                         thresholds: Mapping[str, float] | None = None,
                         ) -> dict[str, Any] | None:
    """Detect race shaping from public state only.

    Returns e.g. ``{'kind': 'race_shaping', 'detail': ..., 'seats': [...],
    'leader_seat': ...}`` when two or more seats' attacking power totals both
    exceed the opposing life by the configured margin; ``None`` otherwise.
    Never raises; never inspects private zones.
    """
    th = DEFAULT_THRESHOLDS if thresholds is None else thresholds
    margin = float(th.get("race_margin", 1.0))

    lives = _lives(state)
    bf = _battlefield_refs(state)
    creatures = _group_by_controller(bf, _of_type(bf, "creature"))
    seats = _known_seats(state)
    if len(seats) < 2:
        return None

    qualifying: list[tuple[int, int]] = []   # (attacker_seat, lowest opposing life)
    for attacker in seats:
        opponents = [s for s in seats if s != attacker]
        int_lives = [v for v in (lives.get(o) for o in opponents)
                     if isinstance(v, int)]
        if not int_lives:
            continue
        lowest = min(int_lives)
        power = _power_total(creatures.get(attacker) or [])
        if power > lowest * margin and power > 0:
            qualifying.append((attacker, lowest))

    if len(qualifying) < 2:
        return None

    qualifying.sort(key=lambda t: (-_power_total(creatures.get(t[0]) or []), t[0]))
    leader_seat = qualifying[0][0]
    detail_parts = [
        f"seat {seat} board power threatens lethal"
        for seat, _life in qualifying
    ]
    return {
        "kind": "race_shaping",
        "detail": "; ".join(detail_parts),
        "seats": [seat for seat, _ in qualifying],
        "leader_seat": leader_seat,
        "margin": margin,
        "power_totals": {seat: _power_total(creatures.get(seat) or [])
                         for seat in seats},
    }


# ---------------------------------------------------------------------------
# Internal bookkeeping structures
# ---------------------------------------------------------------------------

class _SeatLedger:
    """Per-seat resource streak bookkeeping."""

    __slots__ = ("lands_added_this_turn", "land_free_streak", "flood_streak",
                 "flood_fired", "screw_fired", "hand_peak", "hand_fired")

    def __init__(self) -> None:
        self.lands_added_this_turn: int = 0   # lands added since last turn flip
        self.land_free_streak: int = 0        # consecutive turns without a land drop
        self.flood_streak: int = 0            # consecutive qualifying flood turns
        self.flood_fired: bool = False        # one event per flood episode
        self.screw_fired: bool = False        # one event per screw episode
        self.hand_peak: int | None = None     # recent max hand size
        self.hand_fired: bool = False         # one event per hand-pressure episode


class _Beat:
    """One memorable occurrence stored for later callbacks."""

    __slots__ = ("kind", "turn", "seat", "detail")

    def __init__(self, kind: str, turn: int,
                 seat: int | None, detail: str) -> None:
        self.kind = kind          # 'big_death' | 'trade' | 'survival'
        self.turn = turn          # internal turn counter when observed
        self.seat = seat          # seat the beat happened to / belongs to
        self.detail = detail      # reusable human-readable fragment


# ---------------------------------------------------------------------------
# The story model proper
# ---------------------------------------------------------------------------

class StoryModel:
    """Deterministic narrative model over GameState snapshots.

    Implements ``arenaonair.interfaces.StoryModel.update`` -- folds each new
    snapshot into momentum/arcs/resources/beats/speculation and returns the
    narrative events that crossed a threshold on this update. Never raises on
    odd snapshots; worst case it returns ``[]``.
    """

    def __init__(self,
                 thresholds: Mapping[str, float] | None = None,
                 **overrides: Any) -> None:
        cfg: dict[str, Any] = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            cfg.update(dict(thresholds))
        cfg.update(overrides)
        self._th: dict[str, Any] = cfg

        # momentum state
        self._momentum: dict[int | None, float] = {}
        self._prev_lives: dict[Any, int | None] = {}
        self._prev_bf_iids: set[int] = set()
        self._prev_stack_iids: set[int] = set()
        self._cadence: dict[int | None, deque[float]] = {}
        self._first_update_done: bool = False

        # turn tracking (monotonic internal counter; snapshot turn_info unused)
        self._turn: int = 0
        self._last_active: int | None = None

        # arc state
        self._arc: str | None = None          # no arc event on the first update

        # resource ledgers per seat
        self._ledgers: dict[int | None, _SeatLedger] = {}

        # beat memory + callback cooldown
        self._beats: deque[_Beat] = deque(maxlen=int(cfg["beat_buffer"]))
        self._last_callback_turn: int | None = None

        # speculation once-per-arc-segment guard
        self._spec_segment_used: bool = False

        # survival-beat tracking (dropped low -> stabilized)
        self._below_low_since: dict[int | None, int] = {}

    # -- public API ---------------------------------------------------------

    def update(self, state: GameState) -> list[Event]:
        """Fold a new snapshot into the story; return narrative events."""
        try:
            return self._update_inner(state)
        except Exception:
            return []

    # -- internals ----------------------------------------------------------

    def _update_inner(self, state: GameState) -> list[Event]:
        if state is None:
            return []
        ts = float(getattr(state, "snapshot_id", 0) or 0)

        active = getattr(getattr(state, "turn_info", None), "active_player", None)
        if active != self._last_active:
            self._advance_turn(active)

        candidates: list[tuple[int, Event]] = []

        cand_mom, beats_found, damage_this_update = self._fold_momentum(state, ts)
        self._store_beats(beats_found)
        candidates.extend([(0, e) for e in cand_mom])

        cand_res = self._fold_resources(state, ts)
        candidates.extend([(1, e) for e in cand_res])

        cand_arc = self._fold_arc(state, ts)
        candidates.extend([(0, e) for e in cand_arc])

        cand_cb = self._maybe_callback(ts)
        if cand_cb is not None:
            candidates.append((2, cand_cb))

        cand_spec = self._maybe_speculation(state, ts)
        if cand_spec is not None:
            candidates.append((3, cand_spec))

        # deterministic emission order: arc < resource < callback < speculation
        candidates.sort(key=lambda pair: (pair[0],))
        return [event for _prio, event in candidates]

    def _advance_turn(self, active: int | None) -> None:
        """Close out the previous internal turn and open the next one."""
        if self._last_active is None:
            # First activation: open turn 1 without closing a phantom turn 0.
            self._turn += 1
            self._last_active = active
            return
        th = self._th
        for seat in sorted(self._ledgers.keys(), key=str):
            ledger = self._ledgers[seat]
            lands = ledger.lands_added_this_turn
            if lands >= int(th["flood_lands_per_turn"]):
                ledger.flood_streak += 1
                if (ledger.flood_streak >= int(th["flood_turns"])
                        and not ledger.flood_fired):
                    ledger.flood_fired = True   # event raised by caller context
            else:
                ledger.flood_streak = 0
                ledger.flood_fired = False
            if lands == 0:
                ledger.land_free_streak += 1
            else:
                ledger.land_free_streak = 0
                ledger.screw_fired = False
            ledger.lands_added_this_turn = 0

        self._turn += 1
        self._last_active = active

    # -- momentum -----------------------------------------------------------

    def _fold_momentum(self, state: GameState,
                       ts: float) -> tuple[list[Event], list[_Beat],
                                           dict[int | None, float]]:
        """Update momentum from observable deltas; record notable beats.

        Returns (resource/none events reserved for future use, beats found,
        damage dealt per attacking seat this update).
        """
        th = self._th
        events: list[Event] = []
        beats: list[_Beat] = []
        damage: dict[int | None, float] = {}

        lives = _lives(state)
        seats = [s for s in _known_seats(state)]

        # --- life deltas -> damage attribution -----------------------------
        for seat in seats:
            cur = lives.get(seat)
            prev = self._prev_lives.get(seat)
            if isinstance(cur, int) and isinstance(prev, int) and cur < prev:
                dealt = float(prev - cur)
                for attacker in seats:
                    if attacker == seat:
                        continue
                    damage[attacker] = damage.get(attacker, 0.0) + dealt

        # --- battlefield creature deploy/remove ----------------------------
        bf = _battlefield_refs(state)
        creature_iids = _of_type(bf, "creature")
        deployed = creature_iids - self._prev_bf_iids
        removed = self._prev_bf_iids & set(bf.keys())
        removed = {iid for iid in (self._prev_bf_iids - set(bf.keys()))}
        # creatures that were on bf before but are gone now:
        gone_creatures: set[int] = set()
        for iid in self._prev_bf_iids - set(bf.keys()):
            gone_creatures.add(iid)

        deploy_counts: dict[int | None, int] = {}
        remove_counts: dict[int | None, int] = {}
        for iid in sorted(deployed):
            ref = bf.get(iid)
            if ref is not None:
                deploy_counts[_controller_of(ref)] = \
                    deploy_counts.get(_controller_of(ref), 0) + 1
        for iid in sorted(gone_creatures):
            # controller remembered from the previous snapshot's refs
            ref = getattr(self, "_prev_bf_refs", {}).get(iid)
            ctrl = _controller_of(ref) if ref is not None else None
            remove_counts[ctrl] = remove_counts.get(ctrl, 0) + 1

        # --- stack resolutions ----------------------------------------------
        stack_now = set(_stack_refs(state).keys())
        resolved_iids = self._prev_stack_iids - stack_now
        prev_stack_refs = getattr(self, "_prev_stack_refs", {})
        resolve_counts: dict[int | None, int] = {}
        for iid in sorted(resolved_iids):
            ref = prev_stack_refs.get(iid)
            ctrl = _controller_of(ref) if ref is not None else None
            resolve_counts[ctrl] = resolve_counts.get(ctrl, 0) + 1

        # --- apply decay + scores -------------------------------------------
        cap = float(th["momentum_cap"])
        all_ctrls: set[int | None] = set(self._momentum.keys())
        all_ctrls |= deploy_counts.keys()
        all_ctrls |= remove_counts.keys()
        all_ctrls |= resolve_counts.keys()
        all_ctrls |= damage.keys()
        for ctrl in sorted(all_ctrls, key=str):
            base = self._momentum.get(ctrl, 0.0) * float(th["decay"])
            base += float(damage.get(ctrl, 0.0)) * float(th["damage_score"])
            base += float(deploy_counts.get(ctrl, 0)) * float(th["deploy_score"])
            base -= float(remove_counts.get(ctrl, 0)) * float(th["remove_score"])
            base += float(resolve_counts.get(ctrl, 0)) * float(th["resolve_score"])
            self._momentum[ctrl] = max(-cap, min(cap, base))

        # --- cadence window (per attacking seat) -----------------------------
        window = int(th["cadence_window"])
        for ctrl in sorted(set(damage) | set(self._cadence), key=str):
            dq = self._cadence.setdefault(ctrl, deque(maxlen=window))
            dq.append(float(damage.get(ctrl, 0.0)))

        # --- beat memory: big creature deaths / trades -----------------------
        big_pt = int(th["big_creature_pt"])
        prev_bf_refs = getattr(self, "_prev_bf_refs", {})
        for iid in sorted(gone_creatures):
            ref = prev_bf_refs.get(iid)
            if ref is None:
                continue
            pt = ((ref.power or 0) if isinstance(ref.power, int) else 0) + \
                 ((ref.toughness or 0) if isinstance(ref.toughness, int) else 0)
            if pt >= big_pt:
                ctrl = _controller_of(ref)
                beats.append(_Beat("big_death", self._turn, ctrl,
                                   f"a {pt}/{pt}-stat creature left play"))

        # --- beat memory: survival (dropped below low-life, then stabilized) --
        low = int(th["low_life_threshold"])
        for seat in seats:
            cur = lives.get(seat)
            prev = self._prev_lives.get(seat)
            trans = _survival_transition(prev, cur, low)
            if trans == "dropped_low":
                self._below_low_since.setdefault(seat, self._turn)
            elif trans == "stabilized" and seat in self._below_low_since:
                since = self._below_low_since.pop(seat)
                beats.append(_Beat(
                    "survival", self._turn, seat,
                    f"seat {seat} dropped below {low} life on turn {since} "
                    f"and clawed back to {cur}"))

        # remember current refs for next update's removal attribution
        first_baseline = not self._first_update_done
        self._first_update_done = True
        if first_baseline:
            # Baseline snapshot: treat the existing board as pre-existing so the
            # opening state isn't scored as mass deployments/resolutions.
            deploy_counts = {}
            resolve_counts = {}
            gone_creatures = set()
            self._momentum = {ctrl: 0.0 for ctrl in self._momentum}
        self._prev_bf_refs = dict(bf)
        self._prev_stack_refs = dict(_stack_refs(state))
        self._prev_bf_iids = set(bf.keys())
        self._prev_stack_iids = stack_now
        self._prev_lives = dict(lives)

        return events, beats, damage

    def _recent_damage(self, ctrl: int | None) -> float:
        dq = self._cadence.get(ctrl)
        return sum(dq) if dq else 0.0

    def _max_recent_damage(self) -> float:
        return max((self._recent_damage(c) for c in self._cadence), default=0.0)

    # -- resources ------------------------------------------------------------

    def _fold_resources(self, state: GameState, ts: float) -> list[Event]:
        th = self._th
        events: list[Event] = []
        bf = _battlefield_refs(state)
        land_iids_by_ctrl: dict[int | None, set[int]] = {}
        lands_total = _of_type(bf, "land")
        for iid in lands_total:
            ref = bf.get(iid)
            if ref is not None:
                land_iids_by_ctrl.setdefault(_controller_of(ref), set()).add(iid)

        seats: list[int | None] = sorted(
            set(land_iids_by_ctrl) |
            {getattr(pv, "seat", None) for pv in state.players.values()},
            key=str)

        hand_sizes = _hand_sizes(state)

        for seat in seats:
            ledger = self._ledgers.setdefault(seat, _SeatLedger())

            # lands added since previous snapshot (within this turn bucket)
            cur_lands = land_iids_by_ctrl.get(seat, set())
            prev_lands = getattr(self, "_prev_land_iids", {}).get(seat, set())
            added_here = len(cur_lands - prev_lands)
            ledger.lands_added_this_turn += max(0, added_here)

            # screw event fires the moment the streak crosses the threshold
            if (ledger.land_free_streak >= int(th["screw_turns"])
                    and not ledger.screw_fired):
                ledger.screw_fired = True
                events.append(_event(
                    ev.NARRATIVE_RESOURCE, seat,
                    {"streak_kind": "screw",
                     "length": ledger.land_free_streak,
                     "detail": (f"seat {seat} has missed "
                                f"{ledger.land_free_streak} straight land drops")},
                    ts))

            # flood event fires when the streak crosses its threshold
            if (ledger.flood_streak >= int(th["flood_turns"])
                    and not ledger.flood_fired):
                ledger.flood_fired = True
                events.append(_event(
                    ev.NARRATIVE_RESOURCE, seat,
                    {"streak_kind": "flood",
                     "length": ledger.flood_streak,
                     "detail": (f"seat {seat} has drawn extra lands "
                                f"{ledger.flood_streak} turns running")},
                    ts))

            # hand-size pressure when visible
            hs_key = hand_sizes.get(seat)
            if isinstance(hs_key, int):
                peak = ledger.hand_peak
                if peak is None or hs_key > peak:
                    ledger.hand_peak = hs_key
                    ledger.hand_fired = False
                elif (peak >= int(th["hand_pressure_floor"])
                      and peak - hs_key >= int(th["hand_pressure_drop"])
                      and not ledger.hand_fired):
                    ledger.hand_fired = True
                    events.append(_event(
                        ev.NARRATIVE_RESOURCE, seat,
                        {"streak_kind": "hand_pressure",
                         "length": peak - hs_key,
                         "detail": (f"seat {seat}'s hand shrank from "
                                    f"{peak} to {hs_key}")},
                        ts))

        # remember land iids per controller for next update's delta
        if not self._first_update_done:
            land_iids_by_ctrl = {seat: set() for seat in land_iids_by_ctrl}
        self._prev_land_iids = land_iids_by_ctrl
        return events

    # -- arcs -------------------------------------------------------------------

    def _fold_arc(self, state: GameState, ts: float) -> list[Event]:
        th = self._th
        lives = _lives(state)
        seats_int = [s for s in _known_seats(state)]
        if len(seats_int) < 2:
            return []

        m1_raw = self._momentum.get(seats_int[0], 0.0)
        m2_raw = self._momentum.get(seats_int[-1], 0.0)
        gap = m1_raw - m2_raw

        l_first = lives.get(seats_int[0])
        l_last = lives.get(seats_int[-1])
        both_known = isinstance(l_first, int) and isinstance(l_last, int)
        life_gap = ((l_first - l_last)
                    if both_known else 0)

        bf = _battlefield_refs(state)
        creatures = _group_by_controller(bf, _of_type(bf, "creature"))
        powers = {seat: _pt_total(creatures.get(seat) or []) for seat in seats_int}
        quiet_max = self._max_recent_damage()

        # Trailer = whoever is behind on life (deterministic tie-break).
        if both_known:
            if l_first < l_last:
                trailer_seat = seats_int[0]
            elif l_last < l_first:
                trailer_seat = seats_int[-1]
            else:
                trailer_seat = min(seats_int[0], seats_int[-1])
            trailer_dmg = self._recent_damage(trailer_seat)
        else:
            trailer_dmg = quiet_max

        new_arc = self._classify_arc(gap=gap,
                                     life_gap=life_gap,
                                     max_power_pt=max(powers.values(), default=0),
                                     quiet_max=quiet_max,
                                     trailer_dmg=trailer_dmg)

        events: list[Event] = []
        if self._arc is not None and new_arc != self._arc:
            leader_seat = seats_int[0] if gap >= 0 else seats_int[-1]
            detail = (f"arc shifts from {self._arc} to {new_arc}; "
                      f"seat {leader_seat} leads the story")
            events.append(_event(
                ev.NARRATIVE_ARC, leader_seat,
                {"from_arc": self._arc, "to_arc": new_arc,
                 "leader_seat": leader_seat, "detail": detail},
                ts))
            # a new arc segment allows one fresh speculation
            self._spec_segment_used = False
        self._arc = new_arc
        return events

    def _classify_arc(self, gap: float, life_gap: int,
                      max_power_pt: int,
                      quiet_max: float,
                      trailer_dmg: float) -> str:
        """Arc from momentum gap, life gap, board size and damage cadence.

        Symmetric in seat order: positive ``gap``/``life_gap`` favor seat 0,
        negative favor seat 1; magnitudes decide.

        - ``standoff``: both boards developed (high combined p+t), little
          recent damage, no decisive momentum gap.
        - ``pulling_away``: decisive momentum gap paired with a real life gap
          in the SAME direction (|life_gap| >= threshold, sign matching gap).
        - ``comeback_brewing``: someone trails on life by a real margin while
          their recent damage output shows them fighting back.
        - ``even``: everything else.
        """
        th = self._th
        if (max_power_pt >= int(th["standoff_power"])
                and quiet_max <= float(th["standoff_quiet_damage"])
                and abs(gap) < float(th["pull_momentum_gap"])):
            return "standoff"
        if abs(gap) >= float(th["pull_momentum_gap"]):
            if abs(life_gap) >= int(th["pull_life_gap"]) \
                    and (life_gap > 0) == (gap > 0):
                return "pulling_away"
            if (abs(life_gap) >= int(th["comeback_life_gap"])
                    and trailer_dmg >= float(th["comeback_cadence"])):
                return "comeback_brewing"
            return "even"
        if (abs(life_gap) >= int(th["comeback_life_gap"])
                and trailer_dmg >= float(th["comeback_cadence"])):
            return "comeback_brewing"
        return "even"

    # -- callbacks --------------------------------------------------------------

    def _store_beats(self, beats: list[_Beat]) -> None:
        for beat in beats:
            self._beats.append(beat)

    def _maybe_callback(self, ts: float) -> Event | None:
        th = self._th
        if not self._beats:
            return None
        now = self._turn
        if (self._last_callback_turn is not None
                and now - self._last_callback_turn < int(th["callback_cooldown"])):
            return None
        for beat in reversed(self._beats):
            dist = now - beat.turn
            if dist >= int(th["callback_min_distance"]):
                self._last_callback_turn = now
                return _event(
                    ev.NARRATIVE_CALLBACK, beat.seat,
                    {"beat_kind": beat.kind,
                     "turn_distance": dist,
                     "ref_seat": beat.seat,
                     "detail": f"echoes an earlier beat: {beat.detail}"},
                    ts)
        return None

    # -- speculation --------------------------------------------------------------

    def _maybe_speculation(self, state: GameState, ts: float) -> Event | None:
        if self._spec_segment_used:
            return None
        spec = classify_speculation(state, self._th)
        if spec is None:
            return None
        self._spec_segment_used = True
        leader = spec.get("leader_seat")
        return _event(
            ev.NARRATIVE_SPECULATION,
            leader if isinstance(leader, int) else None,
            {"kind": spec.get("kind"),
             "detail": spec.get("detail"),
             "seats": spec.get("seats"),
             "leader_seat": leader},
            ts)


# ---------------------------------------------------------------------------
# Survival beats: dropped-low-then-stabilized detection (called from momentum)
# ---------------------------------------------------------------------------


def _survival_transition(prev_life: int | None, cur_life: int | None,
                         low: int) -> str | None:
    """Classify a life change as 'dropped_low' / 'stabilized' / None."""
    if not isinstance(cur_life, int):
        return None
    if cur_life < low:
        return "dropped_low"
    if isinstance(prev_life, int) and prev_life < low <= cur_life:
        return "stabilized"
    return None

