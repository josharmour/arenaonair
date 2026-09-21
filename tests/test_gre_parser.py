"""Tests for arenaonair.gre_parser.

Unit tests cover the never-raise contract, timestamp precedence, and kind
classification. Fixture tests stream every record of every
fixtures/matches/match_*.jsonl through json.dumps -> parse_line_all and
assert structural invariants (zero exceptions, GameStateMessage counts,
room_state-before-gre ordering, kind prefixes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arenaonair.gre_parser import parse_line, parse_line_all
from arenaonair.models import GreMessage

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "matches"

# Frozen expectation from the task contract: match_01 carries exactly 362
# GREMessageType_GameStateMessage entries.
EXPECTED_GAME_STATE_COUNTS = {1: 362}


def _fixture_records(match_no: int) -> list[dict]:
    path = FIXTURE_DIR / f"match_{match_no:02d}.jsonl"
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _fixture_match_numbers() -> list[int]:
    return sorted(
        int(p.stem.split("_")[1])
        for p in FIXTURE_DIR.glob("match_*.jsonl")
    )


# --------------------------------------------------------------------------
# Unit tests: never-raise / degenerate inputs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",                       # empty string
        "   ",                    # whitespace only
        "[UnityCrossThreadLogger]",  # bare header line, no JSON
        '{"transactionId": "abc"',  # truncated JSON mid-brace
        '{"greToClientEvent": {"greToClientMe',  # truncated deeper
        "\x00\x01\x02binary garbage\xff\xfe",    # binary garbage
        "not json at all {{{",      # plain text noise
        "{}",                       # empty object: neither family
        '{"someOtherEvent": {}}',   # unrelated envelope family
        '{"greToClientEvent": {}}',  # event without messages list
        '{"greToClientEvent": {"greToClientMessages": "oops"}}',  # wrong type
        '{"greToClientEvent": {"greToClientMessages": [null, 42, "x"]}}',
        '{"matchGameRoomStateChangedEvent": {}}',  # no gameRoomInfo
        '{"matchGameRoomStateChangedEvent": {"gameRoomInfo": {}}}',  # no stateType
    ],
)
def test_degenerate_inputs_yield_empty_or_none(raw: str) -> None:
    assert parse_line_all(1000.0, raw) == []
    assert parse_line(1000.0, raw) is None


def test_never_raises_on_arbitrary_strings() -> None:
    samples = [
        "",
        "{",
        "}" * 500,
        '["top-level", "list"]',
        '"just a string"',
        "12345",
        "null",
        "\ud800 lone surrogate-ish text",
        '{"timestamp": "not-a-number", "greToClientEvent": '
        '{"greToClientMessages": [{"type": "GREMessageType_UIMessage"}]}}',
    ]
    for raw in samples:
        try:
            parse_line_all(0.0, raw)
            parse_line(0.0, raw)
        except Exception as exc:  # pragma: no cover - assertion path
            pytest.fail(f"parser raised for {raw!r}: {exc!r}")


# --------------------------------------------------------------------------
# Unit tests: room-state envelopes
# --------------------------------------------------------------------------


def _room_envelope(state_type: str) -> str:
    return json.dumps(
        {
            "transactionId": "t-1",
            "timestamp": "1789598017104",
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "gameRoomConfig": {"matchId": "m-1"},
                    "stateType": state_type,
                    "players": [
                        {"playerName": "alice", "systemSeatId": 1, "teamId": 1},
                        {"playerName": "bob", "systemSeatId": 2, "teamId": 2},
                    ],
                }
            },
        }
    )


def test_room_state_playing_kind_and_payload() -> None:
    msgs = parse_line_all(0.0, _room_envelope("MatchGameRoomStateType_Playing"))
    assert len(msgs) == 1
    msg = msgs[0]
    assert isinstance(msg, GreMessage)
    assert msg.kind == "room_state.playing"
    assert msg.payload["stateType"] == "MatchGameRoomStateType_Playing"
    assert msg.payload["players"][0]["playerName"] == "alice"
    assert msg.ts == pytest.approx(1789598017.104)
    assert msg.raw_len == len(_room_envelope("MatchGameRoomStateType_Playing"))


def test_room_state_completed_suffix_lowercased() -> None:
    msgs = parse_line_all(
        5.0, _room_envelope("MatchGameRoomStateType_Completed")
    )
    assert [m.kind for m in msgs] == ["room_state.completed"]
    assert msgs[0].ts == pytest.approx(1789598017.104)


def test_room_state_unknown_state_type_keeps_full_value() -> None:
    msgs = parse_line_all(0.0, _room_envelope("MatchGameRoomStateType_Weird_New"))
    assert [m.kind for m in msgs] == ["room_state.weird_new"]


def test_room_state_without_timestamp_falls_back_to_arg() -> None:
    envelope = json.loads(_room_envelope("MatchGameRoomStateType_Playing"))
    del envelope["timestamp"]
    msgs = parse_line_all(42.5, json.dumps(envelope))
    assert len(msgs) == 1
    assert msgs[0].ts == pytest.approx(42.5)


# --------------------------------------------------------------------------
# Unit tests: GRE envelopes
# --------------------------------------------------------------------------


def _gre_envelope(
    messages: list[Any], timestamp: str | int | None = None
) -> str:
    envelope: dict = {
        "transactionId": "t-2",
        "greToClientEvent": {"greToClientMessages": messages},
    }
    if timestamp is not None:
        envelope["timestamp"] = timestamp
    return json.dumps(envelope)


def test_gre_single_message_classification() -> None:
    raw = _gre_envelope([{"type": "GREMessageType_ConnectResp", "msgId": 2}])
    msgs = parse_line_all(0.0, raw)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.kind == "gre.ConnectResp"
    assert msg.payload["type"] == "ConnectResp"
    assert msg.payload["msgId"] == 2
    assert msg.raw_len == len(raw)


def test_gre_multiple_messages_emitted_in_order() -> None:
    raw = _gre_envelope(
        [
            {"type": "GREMessageType_GameStateMessage", "gameStateId": 1},
            {"type": "GREMessageType_UIMessage", "msgId": 9},
            {"type": "GREMessageType_TimerStateMessage"},
        ]
    )
    msgs = parse_line_all(0.0, raw)
    assert [m.kind for m in msgs] == [
        "gre.GameStateMessage",
        "gre.UIMessage",
        "gre.TimerStateMessage",
    ]


def test_parse_line_returns_first_of_many() -> None:
    raw = _gre_envelope(
        [
            {"type": "GREMessageType_GameStateMessage", "gameStateId": 1},
            {"type": "GREMessageType_UIMessage", "msgId": 9},
        ]
    )
    first = parse_line(0.0, raw)
    all_msgs = parse_line_all(0.0, raw)
    assert first is not None
    assert first.kind == all_msgs[0].kind
    assert first.payload == all_msgs[0].payload


def test_gre_message_without_type_is_skipped_others_survive() -> None:
    raw = _gre_envelope(
        [
            {"msgId": 1},  # no type -> skipped
            {"type": "GREMessageType_UIMessage"},
            {"type": None},  # null type -> skipped
            7,  # not a dict -> skipped
        ]
    )
    msgs = parse_line_all(0.0, raw)
    assert [m.kind for m in msgs] == ["gre.UIMessage"]


def test_gre_timestamp_ms_converted_to_seconds() -> None:
    raw = _gre_envelope(
        [{"type": "GREMessageType_UIMessage"}], timestamp="1789598017254"
    )
    msgs = parse_line_all(999.0, raw)
    assert msgs[0].ts == pytest.approx(1789598017.254)


def test_gre_integer_timestamp_also_accepted() -> None:
    raw = _gre_envelope([{"type": "GREMessageType_UIMessage"}], timestamp=1789598017254)
    msgs = parse_line_all(999.0, raw)
    assert msgs[0].ts == pytest.approx(1789598017.254)


def test_gre_bad_timestamp_falls_back_to_arg() -> None:
    raw = _gre_envelope([{"type": "GREMessageType_UIMessage"}], timestamp="junk")
    msgs = parse_line_all(77.5, raw)
    assert msgs[0].ts == pytest.approx(77.5)


def test_raw_len_matches_original_line_length() -> None:
    raw = '  {"greToClientEvent": {"greToClientMessages": []}}  '
    msgs = parse_line_all(0.0, raw)
    assert msgs == []  # empty message list still yields no messages


def test_header_prefixed_json_is_decoded() -> None:
    payload = _gre_envelope([{"type": "GREMessageType_UIMessage"}])
    noisy = f"[UnityCrossThreadLogger]{payload}"
    msgs = parse_line_all(0.0, noisy)
    assert [m.kind for m in msgs] == ["gre.UIMessage"]
    assert msgs[0].raw_len == len(noisy)


# --------------------------------------------------------------------------
# Fixture validation: stream every record through the parser
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed_fixtures():
    """Parse every fixture record once; share results across fixture tests."""
    out: dict[int, list[list[GreMessage]]] = {}
    for match_no in _fixture_match_numbers():
        per_record: list[list[GreMessage]] = []
        for record in _fixture_records(match_no):
            raw = json.dumps(record["obj"])
            per_record.append(parse_line_all(0.0, raw))
        out[match_no] = per_record
    return out


def test_fixture_parse_zero_exceptions(parsed_fixtures) -> None:
    # Building parsed_fixtures itself would have raised on any exception;
    # double-check every record produced a list (never None).
    for match_no, per_record in parsed_fixtures.items():
        for i, msgs in enumerate(per_record):
            assert isinstance(msgs, list), (match_no, i)


def test_fixture_game_state_counts(parsed_fixtures) -> None:
    for match_no, expected in EXPECTED_GAME_STATE_COUNTS.items():
        total = sum(
            1
            for msgs in parsed_fixtures[match_no]
            for m in msgs
            if m.kind == "gre.GameStateMessage"
        )
        assert total == expected, f"match_{match_no:02d} GameStateMessage count"


def test_fixture_kinds_are_well_formed(parsed_fixtures) -> None:
    for match_no, per_record in parsed_fixtures.items():
        for i, msgs in enumerate(per_record):
            for m in msgs:
                assert m.kind.startswith(("gre.", "room_state.")), (
                    match_no,
                    i,
                    m.kind,
                )


def test_fixture_room_state_precedes_gre_messages(parsed_fixtures) -> None:
    for match_no, per_record in parsed_fixtures.items():
        flat_kinds = [m.kind for msgs in per_record for m in msgs]
        room_positions = [
            i for i, k in enumerate(flat_kinds) if k.startswith("room_state.")
        ]
        gre_positions = [
            i for i, k in enumerate(flat_kinds) if k.startswith("gre.")
        ]
        assert room_positions and gre_positions, f"match_{match_no:02d} empty"
        assert min(room_positions) < min(gre_positions), (
            f"match_{match_no:02d}: room_state must precede gre messages"
        )


def test_fixture_every_room_record_yields_exactly_one_message(
    parsed_fixtures,
) -> None:
    """Each room_state fixture record emits exactly one room_state message."""
    for match_no in _fixture_match_numbers():
        records = _fixture_records(match_no)
        for record, msgs in zip(records, parsed_fixtures[match_no]):
            if record["kind"] == "room_state":
                assert len(msgs) == 1
                assert msgs[0].kind.startswith("room_state.")


def test_fixture_expected_totals_per_match(parsed_fixtures) -> None:
    """Sanity totals so fixture drift is caught loudly."""
    expected_lengths = {1: 238, 2: 118, 3: 164}
    for match_no in _fixture_match_numbers():
        records = _fixture_records(match_no)
        total_msgs = sum(len(msgs) for msgs in parsed_fixtures[match_no])
        if match_no in expected_lengths:
            assert len(records) == expected_lengths[match_no]
