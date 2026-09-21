"""Tests for arenaonair.state_builder.

Unit tests use hand-built GreMessage payloads derived from the recorded
fixture structure; the integration test replays fixtures/matches/match_01.jsonl
end-to-end by converting each record to GreMessage(s) directly (no gre_parser
dependency -- that module is being written concurrently).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arenaonair.models import GameState, GreMessage
from arenaonair.state_builder import GameStateBuilder

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCH_01 = REPO_ROOT / "fixtures" / "matches" / "match_01.jsonl"


# ---------------------------------------------------------------------------
# Fixture conversion helpers (mirror of the documented payload-unwrapping rules)
# ---------------------------------------------------------------------------

def records_to_messages(records):
    """Convert match_01-style records into an ordered list of GreMessage."""
    messages = []
    for rec in records:
        obj = rec["obj"]
        if rec["kind"] == "room_state":
            ev = obj.get("matchGameRoomStateChangedEvent", {})
            state_type = ev.get("gameRoomInfo", {}).get("stateType", "") or ""
            suffix = state_type.replace("MatchGameRoomStateType_", "").lower() \
                or "playing"
            messages.append(GreMessage(kind="room_state." + suffix,
                                       payload=obj, ts=0.0))
        elif rec["kind"] == "gre":
            for m in obj.get("greToClientEvent", {}).get("greToClientMessages", []):
                mtype = m.get("type", "")
                suffix = mtype.replace("GREMessageType_", "")
                if suffix == "GameStateMessage":
                    payload = m.get("gameStateMessage", {})
                elif suffix == "QueuedGameStateMessage":
                    payload = m  # builder unwraps inner gameStateMessage itself
                else:
                    payload = m.get(suffix[:1].lower() + suffix[1:], m)
                messages.append(GreMessage(kind="gre." + suffix,
                                           payload=payload, ts=0.0))
    return messages


def load_match_01_messages():
    records = [json.loads(line) for line in MATCH_01.read_text().splitlines()
               if line.strip()]
    return records_to_messages(records)


# ---------------------------------------------------------------------------
# Synthetic-message helpers
# ---------------------------------------------------------------------------

def full_state(**overrides):
    base = {
        "type": "GameStateType_Full",
        "gameStateId": 1,
        "players": [
            {"systemSeatNumber": 1, "lifeTotal": 20, "startingLifeTotal": 20,
             "maxHandSize": 7},
            {"systemSeatNumber": 2, "lifeTotal": 20, "startingLifeTotal": 20,
             "maxHandSize": 7},
        ],
        "turnInfo": {"activePlayer": 1},
        "zones": [
            {"zoneId": 1, "type": "ZoneType_Battlefield",
             "visibility": "Visibility_Public"},
            {"zoneId": 2, "type": "ZoneType_Hand", "ownerSeatId": 1,
             "visibility": "Visibility_Private"},
        ],
        "gameObjects": [],
    }
    base.update(overrides)
    return base


def diff_state(game_state_id, **overrides):
    base = {"type": "GameStateType_Diff", "gameStateId": game_state_id}
    base.update(overrides)
    return base


def gs_msg(payload, ts=0.0):
    return GreMessage(kind="gre.GameStateMessage", payload=payload, ts=ts)


def room_msg(payload=None):
    if payload is None:
        payload = {
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "stateType": "MatchGameRoomStateType_Playing",
                    "gameRoomConfig": {
                        "matchId": "abc-123",
                        "eventId": "Brawl_Ladder",
                        "reservedPlayers": [
                            {"systemSeatId": 1, "playerName": "Alice"},
                            {"systemSeatId": 2, "playerName": "Bob"},
                        ],
                    },
                }
            }
        }
    return GreMessage(kind="room_state.playing", payload=payload, ts=0.0)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestBasicFolding:

    def test_full_then_diff_chain(self):
        b = GameStateBuilder()
        s1 = b.apply(None, gs_msg(full_state()))
        assert isinstance(s1, GameState)
        assert s1.snapshot_id == 1
        assert s1.prev_snapshot_id is None

        s2 = b.apply(s1, gs_msg(diff_state(
            2, prevGameStateId=1,
            players=[{"systemSeatNumber": 1, "lifeTotal": 18}],
        )))
        assert s2.snapshot_id == 2
        assert s2.prev_snapshot_id == 1
        # patched player keeps unmentioned fields
        assert s2.players[1].life == 18
        assert s2.players[1].max_hand_size == 7
        # untouched player carried forward
        assert s2.players[2].life == 20

    def test_snapshot_ids_increment_per_applied_message(self):
        b = GameStateBuilder()
        snaps = b.replay([
            room_msg(),
            gs_msg(full_state()),
            gs_msg(diff_state(2)),
            gs_msg(diff_state(3)),
            GreMessage(kind="gre.UIMessage", payload={}, ts=0.0),
            gs_msg(diff_state(4)),
        ])
        assert [s.snapshot_id for s in snaps] == [1, 2, 3, 4, 5]

    def test_zone_replace_semantics(self):
        b = GameStateBuilder()
        b.apply(None, gs_msg(full_state(zones=[
            {"zoneId": 1, "type": "ZoneType_Battlefield",
             "objectInstanceIds": [10]},
        ])))
        s2 = b.apply(None, gs_msg(diff_state(2, zones=[
            {"zoneId": 1, "type": "ZoneType_Battlefield",
             "objectInstanceIds": [11, 12]},
        ])))
        bf = s2.zones["battlefield:pub"]
        assert bf.object_ids == (11, 12)

    def test_zone_without_ids_keeps_membership(self):
        b = GameStateBuilder()
        b.apply(None, gs_msg(full_state(zones=[
            {"zoneId": 1, "type": "ZoneType_Battlefield",
             "objectInstanceIds": [10]},
        ])))
        # stack zone appears with no ids: metadata refresh only
        s2 = b.apply(None, gs_msg(diff_state(2, zones=[
            {"zoneId": 5, "type": "ZoneType_Stack",
             "visibility": "Visibility_Public"},
        ])))
        assert s2.zones["battlefield:pub"].object_ids == (10,)
        assert s2.zones["stack:pub"].object_ids == ()

    def test_objects_merge_and_delete(self):
        b = GameStateBuilder()
        b.apply(None, gs_msg(full_state(gameObjects=[
            {"instanceId": 10, "grpId": 111,
             "cardTypes": ["CardType_Creature"],
             "power": {"value": 2}, "toughness": {"value": 2}},
        ])))
        s2 = b.apply(None, gs_msg(diff_state(
            2,
            gameObjects=[{"instanceId": 10, "power": {"value": 3}}],
            diffDeletedInstanceIds=[],
        )))
        obj = s2.objects[10]
        assert obj.power == 3
        assert obj.toughness == 2          # merged field retained
        assert obj.grp_id == 111

        s3 = b.apply(None, gs_msg(diff_state(3, diffDeletedInstanceIds=[10])))
        assert 10 not in s3.objects

    def test_turn_info_replacement(self):
        b = GameStateBuilder()
        b.apply(None, gs_msg(full_state(turnInfo={"activePlayer": 1})))
        s2 = b.apply(None, gs_msg(diff_state(2, turnInfo={
            "activePlayer": 2, "phase": "Phase_Combat",
            "step": "Step_DeclareAttack", "turnNumber": 5,
        })))
        ti = s2.turn_info
        assert ti.active_player == 2
        assert ti.phase == "combat/declareattack"
        assert ti.turn_number == 5

    def test_room_state_populates_meta(self):
        b = GameStateBuilder()
        s = b.apply(None, room_msg())
        assert s.match_meta.match_id == "abc-123"
        assert s.match_meta.format_name == "Brawl_Ladder"
        assert dict(s.match_meta.player_names) == {1: "Alice", 2: "Bob"}
        assert s.local_seat is None


class TestCardRefs:

    def test_cardref_fields(self):
        b = GameStateBuilder()
        s = b.apply(None, gs_msg(full_state(gameObjects=[
            {"instanceId": 7, "grpId": 81286,
             "superTypes": ["SuperType_Legendary"],
             "cardTypes": ["CardType_Planeswalker"],
             "subtypes": ["SubType_Tasha"],
             "loyalty": {"value": 4},
             "ownerSeatId": 1, "controllerSeatId": 1},
            {"instanceId": 8, "grpId": 72447,
             "cardTypes": ["CardType_Creature"],
             "subtypes": ["SubType_Beast"],
             "power": {"value": 5}, "toughness": {"value": 5},
             "ownerSeatId": 2, "controllerSeatId": 2},
        ])))
        pw = s.objects[7]
        assert pw.type_line == u"Legendary Planeswalker \u2014 Tasha"
        assert pw.card_types == ("planeswalker",)
        assert pw.loyalty == 4
        creature = s.objects[8]
        assert creature.type_line == u"Creature \u2014 Beast"
        assert creature.power == 5 and creature.toughness == 5

    def test_name_resolver_injection(self):
        b = GameStateBuilder(name_resolver=lambda gid: f"Card#{gid}")
        s = b.apply(None, gs_msg(full_state(gameObjects=[
            {"instanceId": 1, "grpId": 999},
            {"instanceId": 2},   # no grpId -> resolver not consulted
        ])))
        assert s.objects[1].name == f"Card#999"
        assert s.objects[2].name is None

    def test_minimal_object_tolerated(self):
        b = GameStateBuilder()
        s = b.apply(None, gs_msg(full_state(gameObjects=[
            {"instanceId": 55},   # ability-ish entry with almost no fields
        ])))
        ref = s.objects[55]
        assert ref.instance_id == 55
        assert ref.grp_id is None
        assert ref.card_types == ()
        assert ref.type_line is None

class TestRobustness:

    def test_uninterpretable_returns_none(self):
        b = GameStateBuilder()
        for payload in ({}, {"type": None}, {"type": ""},
                        {"type": "GameStateType_Weird"}, []):
            msg = GreMessage(kind="gre.GameStateMessage",
                             payload=payload, ts=0.0)
            assert b.apply(None, msg) is None

    def test_non_mapping_payloads_never_raise(self):
        b = GameStateBuilder()
        for payload in (None, "nope", 42, [], [1, 2]):
            msg = GreMessage(kind="gre.GameStateMessage",
                             payload=payload, ts=0.0)
            result = b.apply(None, msg)
            assert result is None or isinstance(result, GameState)

    def test_partial_payloads_never_raise(self):
        b = GameStateBuilder()
        nasty = [
            {"type": "GameStateType_Diff"},
            {"type": "GameStateType_Diff", "zones": [None, {}, []]},
            {"type": "GameStateType_Diff",
             "zones": [{"zoneId": None}, {"zoneId": ""}, {"type": None}]},
            {"type": "GameStateType_Diff",
             "gameObjects": [None, {}, {"instanceId": None}]},
            {"type": "GameStateType_Diff",
             "players": [None, {}, {"systemSeatNumber": None}]},
            {"type": "GameStateType_Diff", "diffDeletedInstanceIds": [None]},
            {"type": "GameStateType_Diff", "turnInfo": None},
            {"type": "GameStateType_Diff", "turnInfo": []},
        ]
        for payload in nasty:
            msg = GreMessage(kind="gre.GameStateMessage",
                             payload=payload, ts=0.0)
            result = b.apply(None, msg)
            assert result is None or isinstance(result, GameState)

    def test_malformed_room_state_never_raises(self):
        b = GameStateBuilder()
        for payload in (None, [], {}, {"garbage": 1},
                        {"matchGameRoomStateChangedEvent": None},
                        {"matchGameRoomStateChangedEvent":
                         {"gameRoomInfo": None}},
                        {"matchGameRoomStateChangedEvent":
                         {"gameRoomInfo": {"gameRoomConfig": None}}},
                        {"matchGameRoomStateChangedEvent":
                         {"gameRoomInfo":
                          {"gameRoomConfig":
                           {"reservedPlayers": [None]}}}}):
            msg = GreMessage(kind="room_state.playing",
                             payload=payload, ts=0.0)
            result = b.apply(None, msg)
            assert result is None or isinstance(result, GameState)

    def test_queued_game_state_unwrapped(self):
        b = GameStateBuilder()
        wrapper = GreMessage(
            kind="gre.QueuedGameStateMessage",
            payload={"gameStateMessage":
                     full_state()},
            ts=0.0)
        s = b.apply(None, wrapper)
        assert isinstance(s, GameState)
        assert s.snapshot_id == 1

    def test_unknown_kind_skipped(self):
        b = GameStateBuilder()
        assert b.apply(None, GreMessage(kind="gre.TimerStateMessage",
                                        payload={}, ts=0.0)) is None
        assert b.apply(None, GreMessage(kind="", payload={}, ts=0.0)) is None
        assert b.apply(None, "not-a-message") is None

    def test_failed_fold_leaves_state_usable(self):
        b = GameStateBuilder()
        s1 = b.apply(None, gs_msg(full_state()))
        # a handful of garbage messages must not poison the builder
        for payload in ({"type": "GameStateType_Diff", "zones": [[]]},
                        {"type": "GameStateType_Diff", "players": [[]]}):
            b.apply(s1, GreMessage(kind="gre.GameStateMessage",
                                   payload=payload, ts=0.0))
        s2 = b.apply(s1, gs_msg(diff_state(
            2, players=[{"systemSeatNumber": 1, "lifeTotal": 15}])))
        assert s2.players[1].life == 15


class TestImmutability:

    def test_older_snapshot_not_mutated_by_later_applies(self):
        b = GameStateBuilder()
        s1 = b.apply(None, gs_msg(full_state(zones=[
            {"zoneId": 1, "type": "ZoneType_Battlefield",
             "objectInstanceIds": [10]},
            {"zoneId": 2, "type": "ZoneType_Hand", "ownerSeatId": 1,
             "objectInstanceIds": [20]},
        ], gameObjects=[
            {"instanceId": 10, "grpId": 1,
             "cardTypes": ["CardType_Creature"],
             "power": {"value": 1}, "toughness": {"value": 1}},
        ])))
        bf_before = s1.zones["battlefield:pub"].object_ids
        obj_before = s1.objects[10]
        life_before = s1.players[1].life

        for n in range(2, 6):
            b.apply(s1 if n == 2 else None, gs_msg(diff_state(
                n,
                zones=[{"zoneId": 1, "type": "ZoneType_Battlefield",
                        "objectInstanceIds": [10, n * 100]}],
                gameObjects=[{"instanceId": 10,
                              "power": {"value": n}}],
                players=[{"systemSeatNumber": 1,
                          "lifeTotal": 20 - n}],
                diffDeletedInstanceIds=[] if n < 5 else [10],
            )))

        assert s1.zones["battlefield:pub"].object_ids == bf_before
        assert s1.objects[10].power == obj_before.power
        assert s1.players[1].life == life_before

    def test_snapshots_are_frozen_dataclasses(self):
        import dataclasses
        b = GameStateBuilder()
        s = b.apply(None, gs_msg(full_state()))
        assert dataclasses.is_dataclass(s)
        with pytest.raises(Exception):
            s.snapshot_id = 99   # type: ignore[misc]

    def test_zone_views_independent_between_snapshots(self):
        b = GameStateBuilder()
        s1 = b.apply(None, gs_msg(full_state(zones=[
            {"zoneId": 2, "type": "ZoneType_Hand", "ownerSeatId": 1,
             "objectInstanceIds": [20]},
        ])))
        s2 = b.apply(None, gs_msg(diff_state(2, zones=[
            {"zoneId": 2, "type": "ZoneType_Hand", "ownerSeatId": 1,
             "objectInstanceIds": []},
        ])))
        assert len(s1.zones["hand:1"].object_ids) == 1
        assert len(s2.zones["hand:1"].object_ids) == 0


class TestMatch01Replay:
    """End-to-end replay of the recorded fixture match."""

    @classmethod
    def setup_class(cls):
        cls.messages = load_match_01_messages()
        cls.snapshots = GameStateBuilder().replay(cls.messages)

    def test_no_exceptions_and_expected_volume(self):
        assert len(self.snapshots) > 300
        # ~362 GameStateMessages in the fixture; nearly all must fold cleanly
        gs_count = len([m for m in self.messages
                        if m.kind == "gre.GameStateMessage"])
        assert len(self.snapshots) >= gs_count - 5

    def test_some_life_changes_over_the_match(self):
        lives = {}
        changed = False
        for snap in self.snapshots:
            for seat, pv in snap.players.items():
                if pv.life is not None:
                    prior = lives.get(seat)
                    if prior is not None and prior != pv.life:
                        changed = True
                    lives[seat] = pv.life
        assert changed

    def test_battlefield_grows_then_shrinks_plausibly(self):
        sizes = []
        for snap in self.snapshots:
            zv = snap.zones.get("battlefield:pub")
            if zv is not None:
                sizes.append(len(zv.object_ids))
        assert sizes and max(sizes) > 0
        assert max(sizes) >= 5

    def test_final_snapshot_differs_from_initial(self):
        first = self.snapshots[0]
        last = self.snapshots[-1]
        assert first != last

    def test_every_zone_key_well_formed(self):
        import re
        pat = re.compile(r"^[a-z]+:(pub|[0-9]+)$")
        for snap in self.snapshots:
            for key in snap.zones:
                assert pat.match(key), key

    def test_prev_snapshot_chain_monotonic(self):
        ids = [s.snapshot_id for s in self.snapshots]
        assert ids == sorted(ids)
        assert ids[0] == 1

    def test_match_meta_from_fixture(self):
        metas = [s.match_meta for s in self.snapshots
                 if s.match_meta.match_id]
        assert metas
        mid = metas[0].match_id
        assert all(m.match_id == mid for m in metas)
