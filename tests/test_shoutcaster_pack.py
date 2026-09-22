"""Tests for the Shoutcaster Vocabulary & Tactical Scenario Pack.

Verifies detection, deduplication, and template rendering for:
- Counter-Wars
- Combat Tricks
- Chump Blocking
- Topdeck Mode
- Hand Sculpting
- Unfair Play
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from arenaonair import events as ev
from arenaonair.differ import EventDiffer
from arenaonair.models import (
    CardRef,
    Event,
    GameState,
    MatchMeta,
    PlayerView,
    TurnInfo,
    ZoneView,
)
from arenaonair.narrator import Narrator
from arenaonair.story import StoryModel


def _card(iid: int, name: str, types=("creature",), power=None, toughness=None,
          ctrl: int = 1, grp_id: int | None = None) -> CardRef:
    return CardRef(
        instance_id=iid,
        grp_id=grp_id,
        name=name,
        type_line=" ".join(types).title(),
        card_types=tuple(types),
        power=power,
        toughness=toughness,
        controller_seat=ctrl,
        owner_seat=ctrl,
    )


def _make_state(snapshot_id: int = 1,
                turn: int = 1,
                phase: str = "Phase_Main1",
                active: int = 1,
                objects: dict[int, CardRef] | None = None,
                battlefield_iids: tuple[int, ...] = (),
                stack_iids: tuple[int, ...] = (),
                hand_sizes: dict[int, int] | None = None,
                match_id: str = "test-match") -> GameState:
    objs = dict(objects or {})
    zones = {
        "battlefield:pub": ZoneView(
            zone_id=1,
            zone_type="ZoneType_Battlefield",
            owner_seat=None,
            object_ids=battlefield_iids,
        ),
        "stack:pub": ZoneView(
            zone_id=9,
            zone_type="ZoneType_Stack",
            owner_seat=None,
            object_ids=stack_iids,
        ),
    }
    if hand_sizes:
        for seat, size in hand_sizes.items():
            zones[f"hand:{seat}"] = ZoneView(
                zone_id=10 + seat,
                zone_type="ZoneType_Hand",
                owner_seat=seat,
                object_ids=tuple(seat * 1000 + i for i in range(size)),
            )

    players = {
        1: PlayerView(seat=1, life=20, starting_life=20, max_hand_size=7),
        2: PlayerView(seat=2, life=20, starting_life=20, max_hand_size=7),
    }

    return GameState(
        snapshot_id=snapshot_id,
        prev_snapshot_id=None if snapshot_id <= 1 else snapshot_id - 1,
        match_meta=MatchMeta(match_id=match_id, format_name="Standard"),
        turn_info=TurnInfo(turn_number=turn, active_player=active, phase=phase),
        players=players,
        zones=zones,
        objects=objs,
        local_seat=1,
    )


class TestCounterWar:

    def test_counter_war_detection(self):
        differ = EventDiffer()
        c1 = _card(101, "Lightning Bolt", types=("instant",), ctrl=1)
        c2 = _card(102, "Counterspell", types=("instant",), ctrl=2)
        c3 = _card(103, "Negate", types=("instant",), ctrl=1)

        prev = _make_state(snapshot_id=1, stack_iids=(101,), objects={101: c1})
        cur = _make_state(
            snapshot_id=2,
            stack_iids=(101, 102, 103),
            objects={101: c1, 102: c2, 103: c3},
        )

        events = differ.detect_counter_war(prev, cur)
        assert len(events) == 1
        ev_obj = events[0]
        assert ev_obj.kind == ev.COUNTER_WAR
        assert ev_obj.payload["card_name"] == "Lightning Bolt"
        assert ev_obj.payload["stack_depth"] == 3
        assert ev_obj.payload["counter_count"] == 2

        # Deduplication check: repeated state does not refire
        next_state = _make_state(
            snapshot_id=3,
            stack_iids=(101, 102, 103),
            objects={101: c1, 102: c2, 103: c3},
        )
        assert differ.detect_counter_war(cur, next_state) == []


class TestCombatTrick:

    def test_combat_trick_during_combat_phase(self):
        differ = EventDiffer()
        trick = _card(201, "Giant Growth", types=("instant",), ctrl=1)

        prev = _make_state(snapshot_id=1, phase="Step_DeclareBlockers", stack_iids=())
        cur = _make_state(
            snapshot_id=2,
            phase="Step_DeclareBlockers",
            stack_iids=(201,),
            objects={201: trick},
        )

        events = differ.detect_combat_trick(prev, cur)
        assert len(events) == 1
        assert events[0].kind == ev.COMBAT_TRICK
        assert events[0].payload["card_name"] == "Giant Growth"
        assert "declareblockers" in events[0].payload["phase"]

    def test_no_combat_trick_in_main_phase(self):
        differ = EventDiffer()
        trick = _card(201, "Giant Growth", types=("instant",), ctrl=1)

        prev = _make_state(snapshot_id=1, phase="Phase_Main1", stack_iids=())
        cur = _make_state(
            snapshot_id=2,
            phase="Phase_Main1",
            stack_iids=(201,),
            objects={201: trick},
        )

        events = differ.detect_combat_trick(prev, cur)
        assert events == []


class TestChumpBlock:

    def test_chump_block_detection(self):
        differ = EventDiffer()
        # Mock detect_block_declared returning a block
        blocker = _card(301, "Soldier Token", types=("creature",), power=1, toughness=1, ctrl=1)
        attacker = _card(302, "Sheoldred, the Apocalypse", types=("creature",), power=4, toughness=5, ctrl=2)

        prev = _make_state(snapshot_id=1, battlefield_iids=(301, 302), objects={301: blocker, 302: attacker})
        cur = _make_state(snapshot_id=2, battlefield_iids=(301, 302), objects={301: blocker, 302: attacker})

        # Inject block event through detect_block_declared mock
        differ.detect_block_declared = MagicMock(return_value=[
            Event(
                kind=ev.BLOCK_DECLARED,
                seat=1,
                payload={
                    "blocks": [{
                        "blocker_instance_id": 301,
                        "attacker_instance_ids": [302],
                    }],
                },
                ts=1.0,
                salience=ev.SALIENCE_HIGH,
            )
        ])

        events = differ.detect_chump_block(prev, cur)
        assert len(events) == 1
        assert events[0].kind == ev.CHUMP_BLOCK
        assert events[0].payload["blocker_name"] == "Soldier Token"
        assert events[0].payload["attacker_name"] == "Sheoldred, the Apocalypse"
        assert events[0].payload["attacker_power"] == 4


class TestHandSculpting:

    def test_hand_sculpting_on_second_cantrip(self):
        differ = EventDiffer()
        opt = _card(401, "Opt", types=("instant",), ctrl=1)
        consider = _card(402, "Consider", types=("instant",), ctrl=1)

        # 1st cantrip
        s0 = _make_state(snapshot_id=1, turn=3, stack_iids=())
        s1 = _make_state(snapshot_id=2, turn=3, stack_iids=(401,), objects={401: opt})
        events1 = differ.detect_hand_sculpting(s0, s1)
        assert events1 == []

        # 2nd cantrip in same turn
        s2 = _make_state(snapshot_id=3, turn=3, stack_iids=(402,), objects={402: consider})
        events2 = differ.detect_hand_sculpting(s1, s2)
        assert len(events2) == 1
        assert events2[0].kind == ev.HAND_SCULPTING
        assert events2[0].payload["first_card"] == "Opt"
        assert events2[0].payload["second_card"] == "Consider"


class TestUnfairPlay:

    def test_unfair_play_turn_2(self):
        card_lookup = MagicMock()
        mock_info = MagicMock()
        mock_info.name = "Atraxa, Grand Unifier"
        mock_info.mana_cost = "{3}{G}{W}{U}{B}"  # CMC 7
        card_lookup.return_value = mock_info

        differ = EventDiffer(card_lookup=card_lookup)
        bomb = _card(501, "Atraxa, Grand Unifier", types=("creature",), grp_id=9999, ctrl=1)

        prev = _make_state(snapshot_id=1, turn=2, battlefield_iids=())
        cur = _make_state(snapshot_id=2, turn=2, battlefield_iids=(501,), objects={501: bomb})

        events = differ.detect_unfair_play(prev, cur)
        assert len(events) == 1
        assert events[0].kind == ev.UNFAIR_PLAY
        assert events[0].payload["card_name"] == "Atraxa, Grand Unifier"
        assert events[0].payload["cmc"] == 7
        assert events[0].payload["turn"] == 2

    def test_no_unfair_play_late_turn(self):
        card_lookup = MagicMock()
        mock_info = MagicMock()
        mock_info.name = "Atraxa, Grand Unifier"
        mock_info.mana_cost = "{3}{G}{W}{U}{B}"
        card_lookup.return_value = mock_info

        differ = EventDiffer(card_lookup=card_lookup)
        bomb = _card(501, "Atraxa, Grand Unifier", types=("creature",), grp_id=9999, ctrl=1)

        prev = _make_state(snapshot_id=1, turn=6, battlefield_iids=())
        cur = _make_state(snapshot_id=2, turn=6, battlefield_iids=(501,), objects={501: bomb})

        events = differ.detect_unfair_play(prev, cur)
        assert events == []


class TestTopdeckMode:

    def test_topdeck_mode_emission_and_deduplication(self):
        story = StoryModel()

        # Turn 1 with 3 cards
        s1 = _make_state(snapshot_id=1, turn=1, active=1, hand_sizes={1: 3, 2: 4})
        assert [e.kind for e in story.update(s1)] == []

        # Turn 2: active player 1 has 0 cards in hand
        s2 = _make_state(snapshot_id=2, turn=2, active=1, hand_sizes={1: 0, 2: 4})
        events = story.update(s2)
        topdeck_events = [e for e in events if e.kind == ev.TOPDECK_MODE]
        assert len(topdeck_events) == 1
        assert topdeck_events[0].seat == 1
        assert topdeck_events[0].salience == ev.SALIENCE_HIGH

        # Another snapshot in same turn does not refire
        s3 = _make_state(snapshot_id=3, turn=2, active=1, hand_sizes={1: 0, 2: 4})
        assert [e for e in story.update(s3) if e.kind == ev.TOPDECK_MODE] == []


class TestNarratorShoutcasterRendering:

    @pytest.mark.parametrize("kind,payload", [
        (ev.COUNTER_WAR, {"card_name": "Sheoldred", "depth": 3, "counter_count": 2}),
        (ev.COMBAT_TRICK, {"card_name": "Monstrous Rage", "phase": "combat"}),
        (ev.CHUMP_BLOCK, {"blocker_name": "Thopter", "attacker_name": "Colossus", "attacker_power": 6}),
        (ev.UNFAIR_PLAY, {"card_name": "Atraxa", "cmc": 7, "turn": 2}),
        (ev.TOPDECK_MODE, {"hand_size": 0, "seat": 1}),
        (ev.HAND_SCULPTING, {"count": 2, "first_card": "Opt", "second_card": "Consider"}),
    ])
    def test_render_all_scenarios(self, kind, payload):
        narrator = Narrator()
        state = _make_state(snapshot_id=10)
        event = differ_event = differ_mock = MagicMock()
        differ_mock.kind = kind
        differ_mock.seat = 1
        differ_mock.payload = payload
        differ_mock.salience = ev.SALIENCE_HIGH
        differ_mock.ts = 1.0

        utterance = narrator.render(differ_mock, state)
        assert utterance is not None
        assert utterance.text != ""
        assert utterance.kind == kind
