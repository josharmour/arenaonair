"""Tests for arenaonair.story -- the deterministic narrative model.

Synthetic snapshot sequences are built directly from the frozen models
dataclasses (mirroring tests/test_state_builder.py construction patterns).
The integration test replays fixtures/matches/match_01.jsonl through
gre_parser + state_builder and feeds every published snapshot to StoryModel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arenaonair import events as ev
from arenaonair.models import (
    CardRef,
    GameState,
    MatchMeta,
    PlayerView,
    TurnInfo,
    ZoneView,
)
from arenaonair.story import (
    ARCS,
    DEFAULT_THRESHOLDS,
    StoryModel,
    classify_speculation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCH_01 = REPO_ROOT / "fixtures" / "matches" / "match_01.jsonl"


# ---------------------------------------------------------------------------
# Synthetic GameState builders
# ---------------------------------------------------------------------------

def card(iid: int, types=("creature",), power=None, toughness=None,
         ctrl: int = 1) -> CardRef:
    return CardRef(
        instance_id=iid,
        grp_id=None,
        name=None,
        type_line=None,
        card_types=tuple(types),
        power=power,
        toughness=toughness,
        controller_seat=ctrl,
        owner_seat=ctrl,
    )


def make_state(snapshot_id: int,
               lives=(20, 20),
               creatures: tuple[CardRef, ...] = (),
               lands: tuple[CardRef, ...] = (),
               hand_sizes: dict[int, int] | None = None,
               stack_cards: tuple[CardRef, ...] = (),
               active_player: int | None = 1) -> GameState:
    """Build a two-seat GameState snapshot directly from dataclasses."""
    objects: dict[int, CardRef] = {}
    bf_ids: list[int] = []
    for ref in creatures:
        objects[ref.instance_id] = ref
        bf_ids.append(ref.instance_id)
    for ref in lands:
        objects[ref.instance_id] = ref
        bf_ids.append(ref.instance_id)

    zones: dict[str, ZoneView] = {
        "battlefield:pub": ZoneView(zone_id=1,
                                    zone_type="ZoneType_Battlefield",
                                    owner_seat=None,
                                    object_ids=tuple(bf_ids)),
    }
    if stack_cards:
        for ref in stack_cards:
            objects[ref.instance_id] = ref
        zones["stack:pub"] = ZoneView(zone_id=9,
                                      zone_type="ZoneType_Stack",
                                      owner_seat=None,
                                      object_ids=tuple(r.instance_id
                                                       for r in stack_cards))
    if hand_sizes:
        for seat in sorted(hand_sizes):
            n = hand_sizes[seat]
            base = seat * 1000
            zones[f"hand:{seat}"] = ZoneView(
                zone_id=10 + seat,
                zone_type="ZoneType_Hand",
                owner_seat=seat,
                object_ids=tuple(base + i for i in range(n)))

    players = {
        seat: PlayerView(seat=seat, life=lives[idx],
                         starting_life=20, max_hand_size=7)
        for idx, seat in enumerate((1, 2))
    }
    return GameState(
        snapshot_id=snapshot_id,
        prev_snapshot_id=None if snapshot_id <= 1 else snapshot_id - 1,
        zones=zones,
        objects=objects,
        players=players,
        turn_info=TurnInfo(turn_number=None,
                           active_player=active_player, phase=None),
        match_meta=MatchMeta(match_id="test-match", format_name="Brawl_Ladder"),
        local_seat=None,
    )


def kinds_of(events) -> list[str]:
    return [e.kind for e in events]


def of_kind(events, kind: str) -> list:
    return [e for e in events if e.kind == kind]


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

class TestMomentum:

    def test_momentum_rises_on_life_damage(self):
        m = StoryModel()
        m.update(make_state(1))
        m.update(make_state(2, lives=(20, 14)))   # seat 2 took 6
        assert m._momentum.get(1, 0.0) == pytest.approx(6.0)
        assert m._momentum.get(2, 0.0) == pytest.approx(0.0)

    def test_momentum_decays_when_nothing_happens(self):
        m = StoryModel()
        m.update(make_state(1))
        m.update(make_state(2, lives=(20, 14)))
        assert m._momentum[1] == pytest.approx(6.0)
        # several quiet updates -> geometric decay at ~0.85/update
        expected = 6.0
        for sid in range(3, 8):
            m.update(make_state(sid))
            expected *= DEFAULT_THRESHOLDS["decay"]
            assert m._momentum[1] == pytest.approx(expected)

    def test_momentum_symmetric_negative_for_trailing_side(self):
        m = StoryModel()
        m.update(make_state(1))
        m.update(make_state(2, lives=(12, 20)))   # seat 1 took 8
        assert m._momentum.get(2, 0.0) == pytest.approx(8.0)
        assert m._momentum.get(1, 0.0) == pytest.approx(0.0)

    def test_momentum_clamped_at_cap(self):
        th = dict(DEFAULT_THRESHOLDS, momentum_cap=5.0)
        m = StoryModel(thresholds=th)
        m.update(make_state(1))
        m.update(make_state(2, lives=(20, 5)))    # 15 damage in one go
        assert m._momentum[1] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Arc transitions
# ---------------------------------------------------------------------------

class TestArcs:

    def test_no_arc_event_on_first_update(self):
        m = StoryModel()
        assert m.update(make_state(1)) == []

    def test_even_to_pulling_away_fires_exactly_once(self):
        m = StoryModel()
        assert m.update(make_state(1)) == []                 # baseline
        evs = m.update(make_state(2, lives=(20, 14)))        # big hit
        arcs = of_kind(evs, ev.NARRATIVE_ARC)
        assert len(arcs) == 1
        assert arcs[0].payload["from_arc"] == "even"
        assert arcs[0].payload["to_arc"] == "pulling_away"
        assert arcs[0].payload["leader_seat"] == 1
        # stable continuation emits nothing further (same lives, no new damage)
        assert of_kind(m.update(make_state(3, lives=(20, 14))), ev.NARRATIVE_ARC) == []
        assert of_kind(m.update(make_state(4, lives=(20, 14))), ev.NARRATIVE_ARC) == []

    def test_arc_transitions_are_threshold_crossings_only(self):
        m = StoryModel()
        m.update(make_state(1))
        seq_events = []
        seq_events += m.update(make_state(2, lives=(20, 14)))   # -> pulling_away
        seq_events += m.update(make_state(3, lives=(20, 14)))
        seq_events += m.update(make_state(4, lives=(20, 14)))
        arcs = of_kind(seq_events, ev.NARRATIVE_ARC)
        assert len(arcs) == 1   # one transition total across the run

    def test_comeback_brewing_when_trailer_deals_damage(self):
        m = StoryModel()
        m.update(make_state(1))
        # seat 2 falls behind on life...
        m.update(make_state(2, lives=(20, 8)))
        # ...then quietly grinds seat 1 down while staying behind on life
        got_comeback = False
        sid = 3
        la, lb = 20, 8
        for step in range(1, 6):
            la -= 2
            evs = m.update(make_state(sid, lives=(la, lb)))
            sid += 1
            arcs = of_kind(evs, ev.NARRATIVE_ARC)
            for arc in arcs:
                if arc.payload["to_arc"] == "comeback_brewing":
                    got_comeback = True
        assert got_comeback

    def test_standoff_from_big_quiet_boards(self):
        m = StoryModel()
        m.update(make_state(1))
        # develop two big boards cumulatively without dealing damage
        creatures = []
        sid = 2
        for extra in range(4):
            creatures.append(card(100 + 2 * extra, power=3, toughness=3, ctrl=1))
            creatures.append(card(101 + 2 * extra, power=3, toughness=3, ctrl=2))
            evs = m.update(make_state(sid, creatures=tuple(creatures)))
            sid += 1
        # eventually the classifier should settle on standoff
        assert m._arc == "standoff"

    def test_race_arc_not_required_but_valid_strings(self):
        assert set(ARCS) == {"even", "pulling_away", "comeback_brewing",
                             "standoff", "race"}

