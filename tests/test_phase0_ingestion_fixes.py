"""Focused Phase-0 regression tests (S7.6-S7.9).

Covers:
- S7.6 EventDiffer scope isolation (casts across matches with recycled
  instance ids, game-scoped dedup resets, match-scoped archetype/narrative
  isolation, reused-vs-fresh pipeline equivalence) and StoryModel narrative
  scope isolation.
- S7.7 LogWatcher idle-poll-with-partial no longer replays lines; genuine
  truncation and file replacement still recover.
- S7.8 counter classification requires affirmative evidence; ordinary
  resolution above another spell is not a counter; uncertain transitions
  never invent a countering player.
- S7.9 remaining_deck_cards zone-membership accounting: library-resident
  cards stay counted, moving out decrements, returning restores, generated
  copies/stale objects don't consume deck copies, uncertainty exposure.
"""

from __future__ import annotations

import os

import pytest

from arenaonair import events as ev
from arenaonair.differ import EventDiffer
from arenaonair.models import (
    CardRef,
    GameState,
    GreMessage,
    MatchMeta,
    PlayerView,
    TurnInfo,
    ZoneView,
)
from arenaonair.state_builder import remaining_deck_cards
from arenaonair.story import StoryModel
from arenaonair.watcher import LogWatcher


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def ref(iid, grp_id=None, name=None, types=("creature",), ctrl=1,
        power=None, toughness=None):
    return CardRef(
        instance_id=iid,
        grp_id=grp_id,
        name=name,
        type_line=None,
        card_types=tuple(types),
        power=power,
        toughness=toughness,
        controller_seat=ctrl,
        owner_seat=ctrl,
    )


def zone(zid, ztype, owner, iids):
    return ZoneView(zone_id=zid, zone_type=ztype, owner_seat=owner,
                    object_ids=tuple(iids))


def state(snapshot_id, match_id="m1", zones=None, objects=None,
          lives=(20, 20), active=1, turn_number=None):
    zones = zones if zones is not None else {
        "battlefield:pub": zone(1, "ZoneType_Battlefield", None, ()),
    }
    players = {
        seat: PlayerView(seat=seat, life=lives[idx],
                         starting_life=20, max_hand_size=7)
        for idx, seat in enumerate((1, 2))
    }
    return GameState(
        snapshot_id=snapshot_id,
        prev_snapshot_id=None if snapshot_id <= 1 else snapshot_id - 1,
        zones=zones,
        objects=objects or {},
        players=players,
        turn_info=TurnInfo(turn_number=turn_number, active_player=active,
                           phase=None),
        match_meta=MatchMeta(match_id=match_id, format_name="Brawl_Ladder"),
        local_seat=None,
    )


def of_kind(events, kind):
    return [e for e in events if e.kind == kind]


def gsm(payload):
    return GreMessage(kind="gre.GameStateMessage", payload=payload, ts=0.0)


def cast_action(iid, seat):
    return {"seatId": seat,
            "action": {"actionType": "ActionType_Cast", "instanceId": iid}}


def bf_zone(iids):
    return zone(1, "ZoneType_Battlefield", None, iids)


# ===========================================================================
# S7.6 -- EventDiffer scope isolation
# ===========================================================================

class TestDifferMatchScopeIsolation:
    """Consecutive matches with overlapping instance ids both emit casts."""

    def test_consecutive_matches_both_emit_casts_for_same_instance_id(self):
        d = EventDiffer()
        # Match A: cast instance 42 via synthetic action.
        prev_a = state(1, match_id="match-A")
        cur_a = state(2, match_id="match-A")
        msg_a = gsm({"actions": [cast_action(42, 1)]})
        first = of_kind(d.diff(prev_a, cur_a, [msg_a]), ev.CAST)
        assert len(first) == 1
        assert first[0].payload["instance_id"] == 42

        # Match B (new match id): SAME instance id 42 must cast again.
        prev_b = state(3, match_id="match-B")
        cur_b = state(4, match_id="match-B")
        msg_b = gsm({"actions": [cast_action(42, 1)]})
        second = of_kind(d.diff(prev_b, cur_b, [msg_b]), ev.CAST)
        assert len(second) == 1
        assert second[0].payload["instance_id"] == 42

    def test_new_match_inherits_no_archetype_evidence(self):
        d = EventDiffer()
        # Match A: opponent (seat 2) PLAYS two Mono-Red Aggro signature cards
        # (they must ENTER the battlefield inside the diff window to register
        # as plays -- cards already on the battlefield in `prev` are not new).
        sig1 = ref(10, name="Monastery Swiftspear", ctrl=2)
        sig2 = ref(11, name="Kumano Faces Kakkazan", ctrl=2)
        s1 = state(1, match_id="match-A",
                   zones={"battlefield:pub": bf_zone(())},
                   objects={})
        s2 = state(2, match_id="match-A",
                   zones={"battlefield:pub": bf_zone((10, 11))},
                   objects={10: sig1, 11: sig2})
        arch_a = of_kind(d.diff(s1, s2, []), ev.ARCHETYPE_DETECTED)
        assert len(arch_a) == 1
        assert arch_a[0].seat == 2

        # Match B: same seats play the SAME first card only -> no archetype
        # may be inferred from match A's accumulated evidence.
        b1 = state(3, match_id="match-B",
                   zones={"battlefield:pub": bf_zone(())},
                   objects={})
        b2 = state(4, match_id="match-B",
                   zones={"battlefield:pub": bf_zone((10,))},
                   objects={10: sig1})
        assert of_kind(d.diff(b1, b2, []), ev.ARCHETYPE_DETECTED) == []

    def test_reused_pipeline_equivalent_to_fresh_for_new_match(self):
        """Same new-match input through a reused differ == fresh differ."""
        def build_stream():
            sig1 = ref(10, name="Monastery Swiftspear", ctrl=2)
            sig2 = ref(11, name="Kumano Faces Kakkazan", ctrl=2)
            s1 = state(1, match_id="warmup",
                       zones={"battlefield:pub": bf_zone((10,))},
                       objects={10: sig1})
            s2 = state(2, match_id="warmup",
                       zones={"battlefield:pub": bf_zone((10, 11))},
                       objects={10: sig1, 11: sig2})
            # New match under test:
            n1 = state(3, match_id="new-match")
            n2 = state(4, match_id="new-match")
            n3 = state(5, match_id="new-match")
            msg_cast = gsm({"actions": [cast_action(777, 2)]})
            return [(s1, s2, []), (s2, n1, []),
                    (n1, n2, [msg_cast]), (n2, n3, [])]

        reused = EventDiffer()
        out_reused = []
        for prev_, cur_, msgs in build_stream():
            out_reused.extend(reused.diff(prev_, cur_, msgs))

        # Equivalence is asserted for the NEW-MATCH segment only: the reused
        # pipeline legitimately emits extra events for the warmup segment
        # (board_shift, its own match_start) that the fresh pipeline never
        # observes because it starts at the new-match boundary. Even within
        # the new-match segment the match_start may land on the transition
        # window (reused) vs the first observed window (fresh), so compare
        # event MULTISETS rather than raw ordering.
        fresh_pairs = build_stream()[2:]
        fresh = EventDiffer()
        out_fresh = []
        for prev_, cur_, msgs in fresh_pairs:
            out_fresh.extend(fresh.diff(prev_, cur_, msgs))

        norm = sorted((e.kind, e.seat) for e in out_reused[2:])
        norm_fresh = sorted((e.kind, e.seat) for e in out_fresh)
        assert norm == norm_fresh


class TestDifferGameScopeResets:
    """Games within one match reset game-scoped dedup."""

    IID = 900001

    def _cast_window(self):
        prev_ = state(10)
        cur_ = state(11)
        return prev_, cur_, gsm({"actions": [cast_action(self.IID, 1)]})

    def test_game_scoped_cast_dedup_resets_between_games(self):
        d = EventDiffer()
        # Game 1: stage transitions + cast of IID.
        g1_prev = state(20)
        g1_cur = state(21)
        stage_start = gsm({"gameInfo": {"stage": "GameStage_Start"}})
        stage_play = gsm({"gameInfo": {"stage": "GameStage_Play"}})
        d.diff(g1_prev, g1_cur, [stage_start])
        first = of_kind(d.diff(g1_cur, state(22), [stage_play]),
                        ev.GAME_START)
        assert len(first) == 1

        cast_prev = state(23)
        cast_cur = state(24)
        msg_cast_1 = gsm({"actions": [cast_action(self.IID, 1)]})
        assert len(of_kind(d.diff(cast_prev, cast_cur,
                                  [msg_cast_1]), ev.CAST)) == 1

        # Game over closes game scope.
        over_prev = state(25)
        over_cur = state(26)
        stage_over = gsm({"gameInfo": {"stage": "GameStage_GameOver"}})
        assert len(of_kind(d.diff(over_prev, over_cur,
                                  [stage_over]), ev.GAME_END)) == 1

        # Game 2 (same match): same instance id must cast again.
        g2_prev = state(27)
        g2_cur = state(28)
        d.diff(g2_prev, g2_cur, [stage_start])
        assert len(of_kind(d.diff(g2_cur, state(29), [stage_play]),
                           ev.GAME_START)) == 1
        msg_cast_2 = gsm({"actions": [cast_action(self.IID, 1)]})
        second_game_casts = of_kind(d.diff(state(30), state(31),
                                           [msg_cast_2]), ev.CAST)
        assert len(second_game_casts) == 1

    def test_board_baseline_resets_between_games(self):
        d = EventDiffer()
        # Game 1: seat 1 develops three creatures -> baseline set.
        creeps_g1 = (ref(1010 + i) for i in range(3))
        creeps_g1 = tuple(ref(1010 + i) for i in range(3))
        b1_prev = state(40)
        b1_cur = state(41,
                       zones={"battlefield:pub": bf_zone((1010,) * 0 +
                                                         tuple(range(
                                                             1010,
                                                             1013)))},
                       objects={i: ref(i) for i in range(1010, 1013)})
        
