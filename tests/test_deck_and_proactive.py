"""Tests for deck ingestion, hand/mana threshold, tutor anticipation, and archetype detection."""

from collections import Counter
import pytest

from arenaonair import events as ev
from arenaonair.carddb import (
    CardInfo,
    calculate_cmc,
    detect_archetype,
    is_bomb,
    is_sweeper,
    is_tutor,
)
from arenaonair.differ import EventDiffer
from arenaonair.models import (
    CardRef,
    Event,
    GameState,
    GreMessage,
    MatchMeta,
    PlayerView,
    TurnInfo,
    ZoneView,
)
from arenaonair.narrator import Narrator
from arenaonair.state_builder import GameStateBuilder, remaining_deck_cards
from arenaonair.story import StoryModel


# ---------------------------------------------------------------------------
# CardDB & Helper Tests
# ---------------------------------------------------------------------------

def test_calculate_cmc():
    assert calculate_cmc("{2}{W}{U}") == 4
    assert calculate_cmc("{1}{B}") == 2
    assert calculate_cmc("{X}{R}") == 1
    assert calculate_cmc("{2/W}{B}") == 3
    assert calculate_cmc("") == 0
    assert calculate_cmc(None) == 0


def test_is_sweeper():
    assert is_sweeper("Sunfall") is True
    assert is_sweeper("Farewell") is True
    assert is_sweeper("Wrath of God") is True
    assert is_sweeper("Depopulate") is True
    assert is_sweeper("Lightning Bolt") is False
    assert is_sweeper("") is False


def test_is_tutor():
    assert is_tutor("Demonic Tutor") is True
    assert is_tutor("Beseech the Mirror") is True
    assert is_tutor("Chord of Calling") is True
    assert is_tutor("Stoneforge Mystic") is True
    assert is_tutor("Counterspell") is False


def test_is_bomb():
    pw = CardInfo(name="Teferi", type_line="Planeswalker — Teferi", mana_cost="{3}{W}{U}", card_types=("planeswalker",))
    big = CardInfo(name="Atraxa", type_line="Legendary Creature — Angel", mana_cost="{3}{G}{W}{U}{B}", card_types=("creature",))
    small = CardInfo(name="Llanowar Elves", type_line="Creature — Elf Druid", mana_cost="{G}", card_types=("creature",))

    assert is_bomb(pw) is True
    assert is_bomb(big) is True
    assert is_bomb(small) is False


def test_detect_archetype():
    assert detect_archetype(["Sleight of Hand", "Arclight Phoenix"]) == "Izzet Phoenix"
    assert detect_archetype(["Kumano Faces Kakkazan", "Monastery Swiftspear"]) == "Mono-Red Aggro"
    assert detect_archetype(["Knight-Errant of Eos", "Gleeful Demolition"]) == "Boros Convoke"
    assert detect_archetype(["Forest", "Island"]) is None


# ---------------------------------------------------------------------------
# GameStateBuilder ConnectResp & Deck Tracking Tests
# ---------------------------------------------------------------------------

def test_builder_connect_resp_ingestion():
    builder = GameStateBuilder()
    msg = GreMessage(
        kind="gre.ConnectResp",
        payload={
            "systemSeatIds": [1],
            "connectResp": {
                "deckMessage": {
                    "deckCards": [75553, 75553, 93262],
                    "commanderCards": [103511],
                }
            }
        },
        ts=1.0,
    )
    state = builder.apply(None, msg)
    assert state is not None
    assert state.local_seat == 1
    assert state.player_deck == (75553, 75553, 93262)
    assert state.commander_cards == (103511,)


def test_remaining_deck_cards():
    deck = (101, 101, 102, 103)
    state = GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        zones={},
        objects={
            1: CardRef(instance_id=1, grp_id=101, name="Card A", type_line="", card_types=(), owner_seat=1),
            2: CardRef(instance_id=2, grp_id=102, name="Card B", type_line="", card_types=(), owner_seat=1),
        },
        players={1: PlayerView(seat=1, life=20)},
        turn_info=TurnInfo(turn_number=1, active_player=1, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Standard"),
        local_seat=1,
        player_deck=deck,
    )
    rem = remaining_deck_cards(state)
    assert rem[101] == 1  # 2 in deck, 1 observed
    assert rem[102] == 0  # 1 in deck, 1 observed
    assert rem[103] == 1  # 1 in deck, 0 observed


# ---------------------------------------------------------------------------
# Differ Proactive Commentator Tests
# ---------------------------------------------------------------------------

def test_differ_detect_hand_online():
    # Card lookup mock
    cards = {
        201: CardInfo(name="Sheoldred, the Apocalypse", type_line="Legendary Creature", mana_cost="{2}{B}{B}", card_types=("creature",)),
    }
    differ = EventDiffer(card_lookup=lambda gid: cards.get(gid))

    # Prev state: 3 lands on battlefield, Sheoldred in hand
    prev = GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        zones={
            "battlefield:pub": ZoneView(zone_id=1, zone_type="battlefield", owner_seat=None, object_ids=(10, 11, 12)),
            "hand:1": ZoneView(zone_id=2, zone_type="hand", owner_seat=1, object_ids=(20,)),
        },
        objects={
            10: CardRef(instance_id=10, grp_id=1, name="Swamp", type_line="Basic Land — Swamp", card_types=("land",), controller_seat=1),
            11: CardRef(instance_id=11, grp_id=1, name="Swamp", type_line="Basic Land — Swamp", card_types=("land",), controller_seat=1),
            12: CardRef(instance_id=12, grp_id=1, name="Swamp", type_line="Basic Land — Swamp", card_types=("land",), controller_seat=1),
            20: CardRef(instance_id=20, grp_id=201, name="Sheoldred, the Apocalypse", type_line="", card_types=("creature",), owner_seat=1),
        },
        players={1: PlayerView(seat=1, life=20)},
        turn_info=TurnInfo(turn_number=4, active_player=1, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Standard"),
        local_seat=1,
    )

    # Cur state: 4th land played
    cur = GameState(
        snapshot_id=2,
        prev_snapshot_id=1,
        zones={
            "battlefield:pub": ZoneView(zone_id=1, zone_type="battlefield", owner_seat=None, object_ids=(10, 11, 12, 13)),
            "hand:1": ZoneView(zone_id=2, zone_type="hand", owner_seat=1, object_ids=(20,)),
        },
        objects={
            10: CardRef(instance_id=10, grp_id=1, name="Swamp", type_line="Basic Land — Swamp", card_types=("land",), controller_seat=1),
            11: CardRef(instance_id=11, grp_id=1, name="Swamp", type_line="Basic Land — Swamp", card_types=("land",), controller_seat=1),
            12: CardRef(instance_id=12, grp_id=1, name="Swamp", type_line="Basic Land — Swamp", card_types=("land",), controller_seat=1),
            13: CardRef(instance_id=13, grp_id=1, name="Swamp", type_line="Basic Land — Swamp", card_types=("land",), controller_seat=1),
            20: CardRef(instance_id=20, grp_id=201, name="Sheoldred, the Apocalypse", type_line="", card_types=("creature",), owner_seat=1),
        },
        players={1: PlayerView(seat=1, life=20)},
        turn_info=TurnInfo(turn_number=4, active_player=1, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Standard"),
        local_seat=1,
    )

    events = differ.detect_hand_online(prev, cur)
    assert len(events) == 1
    ev_item = events[0]
    assert ev_item.kind == ev.HAND_ONLINE
    assert ev_item.payload["card_name"] == "Sheoldred, the Apocalypse"
    assert ev_item.payload["land_count"] == 4
    assert ev_item.payload["cmc"] == 4


def test_differ_detect_tutor_anticipation():
    cards = {
        301: CardInfo(name="Demonic Tutor", type_line="Sorcery", mana_cost="{1}{B}", card_types=("sorcery",)),
        302: CardInfo(name="Sunfall", type_line="Sorcery", mana_cost="{3}{W}{W}", card_types=("sorcery",)),
    }
    differ = EventDiffer(card_lookup=lambda gid: cards.get(gid))

    prev = GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        zones={"stack:pub": ZoneView(zone_id=1, zone_type="stack", owner_seat=None, object_ids=())},
        objects={},
        players={1: PlayerView(seat=1, life=20)},
        turn_info=TurnInfo(turn_number=2, active_player=1, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Standard"),
        local_seat=1,
        player_deck=(301, 302, 302),
    )

    cur = GameState(
        snapshot_id=2,
        prev_snapshot_id=1,
        zones={"stack:pub": ZoneView(zone_id=1, zone_type="stack", owner_seat=None, object_ids=(50,))},
        objects={
            50: CardRef(instance_id=50, grp_id=301, name="Demonic Tutor", type_line="Sorcery", card_types=("sorcery",), controller_seat=1),
        },
        players={1: PlayerView(seat=1, life=20)},
        turn_info=TurnInfo(turn_number=2, active_player=1, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Standard"),
        local_seat=1,
        player_deck=(301, 302, 302),
    )

    events = differ.detect_tutor_anticipation(prev, cur)
    assert len(events) == 1
    assert events[0].kind == ev.TUTOR_ANTICIPATION
    assert events[0].payload["card_name"] == "Demonic Tutor"
    assert events[0].payload["target_name"] == "Sunfall"
    assert events[0].payload["target_count"] == 2


def test_differ_detect_graveyard_recursion():
    differ = EventDiffer()
    prev = GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        zones={
            "graveyard:1": ZoneView(zone_id=1, zone_type="graveyard", owner_seat=1, object_ids=(99,)),
            "stack:pub": ZoneView(zone_id=2, zone_type="stack", owner_seat=None, object_ids=()),
        },
        objects={99: CardRef(instance_id=99, grp_id=50, name="Faithless Looting", type_line="", card_types=("sorcery",), controller_seat=1)},
        players={1: PlayerView(seat=1, life=20)},
        turn_info=TurnInfo(turn_number=3, active_player=1, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Historic"),
    )
    cur = GameState(
        snapshot_id=2,
        prev_snapshot_id=1,
        zones={
            "graveyard:1": ZoneView(zone_id=1, zone_type="graveyard", owner_seat=1, object_ids=()),
            "stack:pub": ZoneView(zone_id=2, zone_type="stack", owner_seat=None, object_ids=(99,)),
        },
        objects={99: CardRef(instance_id=99, grp_id=50, name="Faithless Looting", type_line="", card_types=("sorcery",), controller_seat=1)},
        players={1: PlayerView(seat=1, life=20)},
        turn_info=TurnInfo(turn_number=3, active_player=1, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Historic"),
    )
    events = differ.detect_graveyard_recursion(prev, cur)
    assert len(events) == 1
    assert events[0].kind == ev.GRAVEYARD_RECURSION
    assert events[0].payload["card_name"] == "Faithless Looting"


def test_differ_detect_archetype():
    differ = EventDiffer()
    # Opponent (seat 2) plays Sleight of Hand then Arclight Phoenix
    prev = GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        zones={"battlefield:pub": ZoneView(zone_id=1, zone_type="battlefield", owner_seat=None, object_ids=())},
        objects={},
        players={1: PlayerView(seat=1, life=20), 2: PlayerView(seat=2, life=20)},
        turn_info=TurnInfo(turn_number=1, active_player=2, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Explorer"),
    )
    cur = GameState(
        snapshot_id=2,
        prev_snapshot_id=1,
        zones={"battlefield:pub": ZoneView(zone_id=1, zone_type="battlefield", owner_seat=None, object_ids=(101, 102))},
        objects={
            101: CardRef(instance_id=101, grp_id=1, name="Sleight of Hand", type_line="", card_types=("sorcery",), controller_seat=2),
            102: CardRef(instance_id=102, grp_id=2, name="Arclight Phoenix", type_line="", card_types=("creature",), controller_seat=2),
        },
        players={1: PlayerView(seat=1, life=20), 2: PlayerView(seat=2, life=20)},
        turn_info=TurnInfo(turn_number=2, active_player=2, phase="main"),
        match_meta=MatchMeta(match_id="m1", format_name="Explorer"),
    )
    events = differ.detect_archetype(prev, cur)
    assert len(events) == 1
    assert events[0].kind == ev.ARCHETYPE_DETECTED
    assert events[0].payload["archetype_name"] == "Izzet Phoenix"
    assert events[0].seat == 2


# ---------------------------------------------------------------------------
# StoryModel Outs Anticipation Tests
# ---------------------------------------------------------------------------

def test_story_outs_anticipation():
    cards = {
        901: CardInfo(name="Sunfall", type_line="Sorcery", mana_cost="{3}{W}{W}", card_types=("sorcery",)),
    }
    story = StoryModel(card_lookup=lambda gid: cards.get(gid))

    # Opponent (seat 2) has 10 power on board, local player (seat 1) at 4 life
    state = GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        zones={
            "battlefield:pub": ZoneView(zone_id=1, zone_type="battlefield", owner_seat=None, object_ids=(1,)),
        },
        objects={
            1: CardRef(instance_id=1, grp_id=5, name="Gargantuan Beast", type_line="Creature", card_types=("creature",), power=10, toughness=10, controller_seat=2),
        },
        players={1: PlayerView(seat=1, life=4), 2: PlayerView(seat=2, life=20)},
        turn_info=TurnInfo(turn_number=5, active_player=1, phase="upkeep"),
        match_meta=MatchMeta(match_id="m1", format_name="Standard"),
        local_seat=1,
        player_deck=(901, 901, 901),
    )

    events = story.update(state)
    outs = [e for e in events if e.kind == ev.OUTS_ANTICIPATION]
    assert len(outs) == 1
    assert outs[0].payload["target_name"] == "Sunfall"
    assert outs[0].payload["target_count"] == 3


# ---------------------------------------------------------------------------
# Narrator Rendering of New Kinds
# ---------------------------------------------------------------------------

def test_narrator_renders_new_kinds():
    narrator = Narrator()
    events_to_test = [
        Event(kind=ev.HAND_ONLINE, seat=1, payload={"card_name": "Sheoldred", "land_count": 4, "cmc": 4}, ts=1.0, salience=2),
        Event(kind=ev.TUTOR_ANTICIPATION, seat=1, payload={"card_name": "Demonic Tutor", "target_name": "Sunfall", "target_count": 2}, ts=2.0, salience=2),
        Event(kind=ev.OUTS_ANTICIPATION, seat=1, payload={"target_name": "Farewell", "target_count": 3}, ts=3.0, salience=2),
        Event(kind=ev.ARCHETYPE_DETECTED, seat=2, payload={"archetype_name": "Mono-Red Aggro", "signature_card": "Kumano Faces Kakkazan"}, ts=4.0, salience=2),
        Event(kind=ev.GRAVEYARD_RECURSION, seat=1, payload={"card_name": "Arclight Phoenix"}, ts=5.0, salience=2),
    ]

    for event in events_to_test:
        utt = narrator.render(event, None)
        assert utt is not None, f"Failed to render {event.kind}"
        assert len(utt.text) > 5, f"Utterance too short for {event.kind}: {utt.text}"
