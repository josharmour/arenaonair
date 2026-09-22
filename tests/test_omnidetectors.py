"""Tests for the prerequisite-gated omniscient strategic detectors ([O4]).

Covers differ trap-armed / trap-sprung / bluff and story clash-of-outs:
positive, negative, stale-hand, unknown-mana and single-source cases.
Every detector must SUPPRESS (emit nothing) when its own prerequisites are
unmet, and every fired payload must contain only evidence-backed facts.
"""

from __future__ import annotations

from arenaonair import events as ev
from arenaonair.differ import EventDiffer
from arenaonair.models import (
    CardRef,
    GameState,
    GreMessage,
    MatchMeta,
    PlayerView,
    SeatKnowledge,
    TurnInfo,
    ZoneView,
)
from arenaonair.story import StoryModel


# ---------------------------------------------------------------------------
# Synthetic snapshot builders (house pattern from tests/test_differ.py)
# ---------------------------------------------------------------------------

def card(iid, types=("creature",), ctrl=1, name=None, grp_id=None):
    return CardRef(
        instance_id=iid,
        grp_id=grp_id,
        name=name,
        type_line=None,
        card_types=tuple(types),
        power=None,
        toughness=None,
        controller_seat=ctrl,
        owner_seat=ctrl,
    )


def make_state(snapshot_id, hands=None, lands=None, active_player=1,
               knowledge=None, match_id="test-match"):
    """Two-seat snapshot with per-seat visible hands and battlefield lands."""
    hands = hands or {}
    lands = lands or {}
    objects: dict[int, CardRef] = {}
    zones: dict[str, ZoneView] = {}
    bf_ids: list[int] = []

    for seat, n in lands.items():
        for i in range(n):
            iid = seat * 100 + i
            objects[iid] = card(iid, ("land",), seat)
            bf_ids.append(iid)

    for seat, cards in hands.items():
        ids = []
        for i, (name, types) in enumerate(cards):
            iid = seat * 1000 + i
            objects[iid] = card(iid, types, seat, name=name)
            ids.append(iid)
        zones[f"hand:{seat}"] = ZoneView(
            zone_id=10 + seat,
            zone_type="ZoneType_Hand",
            owner_seat=seat,
            object_ids=tuple(ids),
        )

    zones["battlefield:pub"] = ZoneView(
        zone_id=1, zone_type="ZoneType_Battlefield", owner_seat=None,
        object_ids=tuple(bf_ids))

    players = {seat: PlayerView(seat=seat, life=20) for seat in (1, 2)}
    return GameState(
        snapshot_id=snapshot_id,
        prev_snapshot_id=None if snapshot_id <= 1 else snapshot_id - 1,
        zones=zones,
        objects=objects,
        players=players,
        turn_info=TurnInfo(turn_number=None, active_player=active_player,
                           phase=None),
        match_meta=MatchMeta(match_id=match_id, format_name="Brawl_Ladder"),
        local_seat=None,
        seat_knowledge=knowledge or {},
    )


def sk(seat, hand_visible=True, hand_fresh_asof=None, deck_submitted=False,
       library_accounted=False, library_uncertainty="unknown"):
    return SeatKnowledge(
        seat=seat,
        hand_visible=hand_visible,
        hand_fresh_asof=hand_fresh_asof,
        deck_submitted=deck_submitted,
        library_accounted=library_accounted,
        library_uncertainty=library_uncertainty,
    )


def cast_action_msg(iid, seat, ts=0.0):
    """Synthetic ActionType_Cast message (differ's non-GRE fallback path)."""
    return GreMessage(
        kind="gre.GameStateMessage",
        payload={"actions": [
            {"seatId": seat,
             "action": {"actionType": "ActionType_Cast",
                        "instanceId": iid}},
        ]},
        ts=ts,
    )


def of_kind(events, kind):
    return [e for e in events if e.kind == kind]


FRESH_AT_5 = 5.0  # hand_fresh_asof matching snapshot clock 5


# ---------------------------------------------------------------------------
# detect_trap_armed
# ---------------------------------------------------------------------------

class TestTrapArmed:

    def _armed_setup(self):
        knowledge = {1: sk(1, hand_fresh_asof=FRESH_AT_5)}
        prev = make_state(1)
        cur = make_state(
            5,
            hands={1: [("Counterspell", ("instant",))]},
            lands={1: 2},
            knowledge=knowledge,
        )
        return EventDiffer(), prev, cur

    def test_positive_armed_with_fresh_hand_and_known_mana(self):
        d, prev, cur = self._armed_setup()
        events = d.diff(prev, cur, [])
        armed = of_kind(events, ev.TRAP_ARMED)
        assert len(armed) == 1
        assert armed[0].payload["seat"] == 1
        assert armed[0].payload["threat_name"] == "Counterspell"

    def test_suppressed_when_mana_unknown(self):
        d, prev, _ = self._armed_setup()
        # Same fresh visible hand but ZERO visible lands -> mana unknown.
        cur = make_state(
            5,
            hands={1: [("Counterspell", ("instant",))]},
            lands={},
            knowledge={1: sk(1, hand_fresh_asof=FRESH_AT_5)},
        )
        events = d.diff(prev, cur, [])
        assert of_kind(events, ev.TRAP_ARMED) == []

    def test_suppressed_when_hand_stale(self):
        d, prev, _ = self._armed_setup()
        # hand_fresh_asof far behind the snapshot clock -> stale.
        cur = make_state(
            5,
            hands={1: [("Counterspell", ("instant",))]},
            lands={1: 2},
            knowledge={1: sk(1, hand_fresh_asof=-100.0)},
        )
        events = d.diff(prev, cur, [])
        assert of_kind(events, ev.TRAP_ARMED) == []

    def test_suppressed_when_hand_invisible(self):
        d = EventDiffer()
        prev = make_state(1)
        cur = make_state(
            5,
            hands={1: [("Counterspell", ("instant",))]},
            lands={1: 2},
            knowledge={1: sk(1, hand_visible=False, hand_fresh_asof=FRESH_AT_5)},
        )
        events = d.diff(prev, cur, [])
        assert of_kind(events, ev.TRAP_ARMED) == []

    def test_suppressed_when_no_reactive_card_visible(self):
        d = EventDiffer()
        prev = make_state(1)
        cur = make_state(
            5,
            hands={1: [("Grizzly Bears", ("creature",))]},
            lands={1: 2},
            knowledge={1: sk(1, hand_fresh_asof=FRESH_AT_5)},
        )
        events = d.diff(prev, cur, [])
        assert of_kind(events, ev.TRAP_ARMED) == []


# ---------------------------------------------------------------------------
# detect_trap_sprung
# ---------------------------------------------------------------------------

class TestTrapSprung:

    def _arm_then_spring(self):
        d = EventDiffer()
        prev0 = make_state(1)
        armed_state = make_state(
            5,
            hands={1: [("Counterspell", ("instant",))]},
            lands={1: 2},
            knowledge={1: sk(1, hand_fresh_asof=FRESH_AT_5)},
            active_player=2,
        )
        d.diff(prev0, armed_state, [])

        # Next window: seat 2 publicly casts a spell.
        sprung_state = make_state(6, active_player=2)
        msg = cast_action_msg(777, seat=2)
        events = d.diff(armed_state, sprung_state, [msg])
        return d, armed_state, sprung_state, events

    def test_positive_sprung_clears_entry(self):
        d, armed_state, sprung_state, events = self._arm_then_spring()
        sprung = of_kind(events, ev.TRAP_SPRUNG)
        assert len(sprung) == 1
        assert sprung[0].payload["victim_name"] is None or \
            isinstance(sprung[0].payload["victim_name"], str)
        # Registry entry cleared by the spring.
        assert d._armed_traps == {}

    def test_no_refire_without_re_arm(self):
        d, armed_state, sprung_state, _events = self._arm_then_spring()
        # Another public cast with NO re-arm in between -> nothing fires.
        later = make_state(7, active_player=2)
        msg2 = cast_action_msg(888, seat=2)
        events2 = d.diff(sprung_state, later, [msg2])
        assert of_kind(events2, ev.TRAP_SPRUNG) == []

    def test_requires_live_armed_entry(self):
        d = EventDiffer()
        prev = make_state(1)
        cur = make_state(5, active_player=2)
        msg = cast_action_msg(777, seat=2)
        events = d.diff(prev, cur, [msg])
        assert of_kind(events, ev.TRAP_SPRUNG) == []

    def test_expired_window_does_not_fire(self):
        d = EventDiffer(config={"trap_validity_window": 3.0})
        prev0 = make_state(1)
        armed_state = make_state(
            5,
            hands={1: [("Counterspell", ("instant",))]},
            lands={1: 2},
            knowledge={1: sk(1, hand_fresh_asof=FRESH_AT_5)},
            active_player=2,
        )
        d.diff(prev0, armed_state, [])
        # Clock jumps far past the validity window -> entry expired.
        late_state = make_state(5000, active_player=2)
        msg = cast_action_msg(777, seat=2)
        events = d.diff(armed_state, late_state, [msg])
        assert of_kind(events, ev.TRAP_SPRUNG) == []


# ---------------------------------------------------------------------------
# detect_bluff
# ---------------------------------------------------------------------------

class TestBluff:

    def _bluff_cur(self):
        return make_state(
            5,
            hands={2: [("Counterspell", ("instant",)),
                       ("Negate", ("instant",))]},
            lands={2: 3},
            active_player=2,
            knowledge={2: sk(2, hand_fresh_asof=FRESH_AT_5)},
        )

    def test_positive_qualified_wording_from_real_cards(self):
        d = EventDiffer()
        prev = make_state(1)  # active player was 1 -> delay evidence for 2
        events = d.diff(prev, self._bluff_cur(), [])
        bluffs = of_kind(events, ev.BLUFF_DETECTED)
        assert len(bluffs) == 1
        summary = bluffs[0].payload["visible_hand_summary"]
        # Summary built from the ACTUAL visible cards only.
        assert "Counterspell" in summary
        assert "Negate" in summary
        assert "the hand we can see holds" in summary

    def test_suppressed_when_no_delay_evidence(self):
        d = EventDiffer()
        # Active player was ALREADY 2 before this window -> no priority
        # change -> no verified delay evidence.
        prev = make_state(4, active_player=2)
        events = d.diff(prev, self._bluff_cur(), [])
        assert of_kind(events, ev.BLUFF_DETECTED) == []

    def test_suppressed_when_hand_invisible(self):
        d = EventDiffer()
        prev = make_state(1)
        cur = make_state(
            5,
            hands={2: [("Counterspell", ("instant",))]},
            lands={2: 3},
            active_player=2,
            knowledge={2: sk(2, hand_visible=False,
                             hand_fresh_asof=FRESH_AT_5)},
        )
        events = d.diff(prev, cur, [])
        assert of_kind(events, ev.BLUFF_DETECTED) == []

    def test_suppressed_when_hand_stale(self):
        d = EventDiffer()
        prev = make_state(1)
        cur = make_state(
            5,
            hands={2: [("Counterspell", ("instant",))]},
            lands={2: 3},
            active_player=2,
            knowledge={2: sk(2, hand_fresh_asof=-100.0)},
        )
        events = d.diff(prev, cur, [])
        assert of_kind(events, ev.BLUFF_DETECTED) == []

    def test_suppressed_when_seat_actually_cast(self):
        d = EventDiffer()
        prev = make_state(1)
        cur = self._bluff_cur()
        # The seat itself cast this window -> no unexplained delay.
        msg = cast_action_msg(555, seat=2)
        events = d.diff(prev, cur, [msg])
        assert of_kind(events, ev.BLUFF_DETECTED) == []

    def test_suppressed_when_no_reactive_card(self):
        d = EventDiffer()
        prev = make_state(1)
        cur = make_state(
            5,
            hands={2: [("Grizzly Bears", ("creature",))]},
            lands={2: 3},
            active_player=2,
            knowledge={2: sk(2, hand_fresh_asof=FRESH_AT_5)},
        )
        events = d.diff(prev, cur, [])
        assert of_kind(events, ev.BLUFF_DETECTED) == []


# ---------------------------------------------------------------------------
# clash_of_outs (story model)
# ---------------------------------------------------------------------------

def story_state(snapshot_id, knowledge=None, deck=(101, 102), spent_iids=()):
    """Snapshot with a local submitted deck for remaining_deck_cards."""
    objects: dict[int, CardRef] = {}
    zones: dict[str, ZoneView] = {}
    bf_ids: list[int] = []
    for seat in (1, 2):
        for i in range(2):
            iid = seat * 100 + i
            objects[iid] = card(iid, ("land",), seat)
            bf_ids.append(iid)
    for n, iid in enumerate(spent_iids):
        objects[iid] = CardRef(
            instance_id=iid,
            grp_id=deck[n % len(deck)],
            name=f"Deck Card {deck[n % len(deck)]}",
            type_line=None,
            card_types=("instant",),
            controller_seat=1,
            owner_seat=1,
        )
    zones["battlefield:pub"] = ZoneView(
        zone_id=1, zone_type="ZoneType_Battlefield", owner_seat=None,
        object_ids=tuple(bf_ids))
    if spent_iids:
        zones["hand:1"] = ZoneView(
            zone_id=11, zone_type="ZoneType_Hand", owner_seat=1,
            object_ids=tuple(spent_iids))
    players = {seat: PlayerView(seat=seat, life=20) for seat in (1, 2)}
    return GameState(
        snapshot_id=snapshot_id,
        prev_snapshot_id=None if snapshot_id <= 1 else snapshot_id - 1,
        zones=zones,
        objects=objects,
        players=players,
        turn_info=TurnInfo(turn_number=None, active_player=1, phase=None),
        match_meta=MatchMeta(match_id="test-match",
                             format_name="Brawl_Ladder"),
        local_seat=1,
        player_deck=tuple(deck),
        seat_knowledge=knowledge or {},
    )


class TestClashOfOuts:

    def _both_exact_knowledge(self):
        return {
            1: sk(1, deck_submitted=True, library_accounted=True,
                  library_uncertainty="exact"),
            2: sk(2, deck_submitted=True, library_accounted=True,
                  library_uncertainty="exact"),
        }

    def test_double_exact_includes_numeric_extras(self):
        sm = StoryModel()
        state = story_state(1, knowledge=self._both_exact_knowledge(),
                            spent_iids=(500,))
        events = sm.update(state)
        clashes = of_kind(events, ev.CLASH_OF_OUTS)
        assert len(clashes) == 1
        payload = clashes[0].payload
        assert "pressure_desc" in payload
        assert isinstance(payload["seat_a_outs"], int)
        assert isinstance(payload["seat_b_outs"], int)

    def test_single_source_hedged_text_without_numerics(self):
        sm = StoryModel()
        knowledge = {
            1: sk(1, deck_submitted=True, library_accounted=True,
                  library_uncertainty="exact"),
        }
        state = story_state(1, knowledge=knowledge)
        events = sm.update(state)
        clashes = of_kind(events, ev.CLASH_OF_OUTS)
        assert len(clashes) == 1
        payload = clashes[0].payload
        assert "seat_a_outs" not in payload
        assert "seat_b_outs" not in payload
        desc = payload["pressure_desc"]
        assert "answers may still be hiding" in desc

    def test_uncertain_marker_omits_numerics_everywhere(self):
        sm = StoryModel()
        knowledge = self._both_exact_knowledge()
        # Degraded zone rosters -> remaining_deck_cards flags 'uncertain';
        # even with both seats' SK exact the numerics must be omitted.
        base = story_state(1, knowledge=knowledge)
        broken = GameState(**{**base.__dict__, "zones": {}})
        events = sm.update(broken)
        clashes = of_kind(events, ev.CLASH_OF_OUTS)
        if clashes:
            payload = clashes[0].payload
            assert "seat_a_outs" not in payload
            assert "seat_b_outs" not in payload
            assert "answers may still be hiding" in payload["pressure_desc"]

    def test_no_supported_seat_emits_nothing(self):
        sm = StoryModel()
        state = story_state(1)  # empty seat_knowledge -> nobody qualifies
        events = sm.update(state)
        assert of_kind(events, ev.CLASH_OF_OUTS) == []

    def test_estimated_uncertainty_downgrades_to_hedged(self):
        sm = StoryModel()
        knowledge = {
            1: sk(1, deck_submitted=True, library_accounted=True,
                  library_uncertainty="estimated"),
            2: sk(2),
        }
        state = story_state(1, knowledge=knowledge)
        events = sm.update(state)
        clashes = of_kind(events, ev.CLASH_OF_OUTS)
        for clash in clashes:
            assert "seat_a_outs" not in clash.payload
            assert "seat_b_outs" not in clash.payload

    def test_single_source_does_not_crash_on_odd_snapshots(self):
        sm = StoryModel()
        state = story_state(1, knowledge={
            1: sk(1, deck_submitted=True, library_accounted=True,
                  library_uncertainty="exact")})
        odd = GameState(**{**state.__dict__, "players": {}, "zones": {},
                           "objects": {}})
        events = sm.update(odd)  # must never raise
        assert isinstance(events, list)


# ---------------------------------------------------------------------------
# House-pattern robustness: garbage in -> [], never raises
# ---------------------------------------------------------------------------

class TestRobustness:

    def test_detectors_never_raise_on_garbage(self):
        d = EventDiffer()
        garbage_cur = make_state(5)
        object.__setattr__(garbage_cur, "seat_knowledge", None)
        events = d.diff(make_state(1), garbage_cur,
                        [GreMessage(kind="gre.GameStateMessage",
                                    payload={"nonsense": True}, ts=0)])
        assert isinstance(events, list)

        sm = StoryModel()
        events2 = sm.update(None)
        assert events2 == []
