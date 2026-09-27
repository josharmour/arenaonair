"""Regressions through real config, builders, app routes, and relay sockets."""
import asyncio
import json
import time
from dataclasses import replace

import pytest

from arenaonair.app import ArenaOnAirApp, main
from arenaonair.config import Config, load, resolve_booth
from arenaonair.models import Event, GameState, GreMessage, MatchMeta, TurnInfo
from arenaonair.sources import SourceTag, TaggedMessage
from arenaonair.state_builder import DualStateBuilder


def messages(seat, gid=1, turn=1, game=1, hand_gid=None):
    """Two genuine client perspectives, with hidden opponent identities."""
    return [
        GreMessage("room_state.playing", {"gameRoomConfig": {"matchId": "match"}}, 0),
        GreMessage("gre.ConnectResp", {"systemSeatIds": [seat], "deckMessage": {"deckCards": [11, 22]}}, 0),
        GreMessage("gre.GameStateMessage", {
            "type": "GameStateType_Full", "gameStateId": gid,
            "gameInfo": {"matchID": "match", "gameNumber": game, "stage": "GameStage_Play"},
            "turnInfo": {"turnNumber": turn, "activePlayer": 1},
            "players": [{"systemSeatNumber": n, "lifeTotal": 20} for n in (1, 2)],
            "zones": [{"zoneId": n, "type": "ZoneType_Hand", "ownerSeatId": n,
                       "objectInstanceIds": [n * 100]} for n in (1, 2)],
            "gameObjects": [{"instanceId": n * 100, "grpId": (hand_gid or n * 11) if seat == n else 0,
                             "ownerSeatId": n, "controllerSeatId": n,
                             "cardTypes": ["CardType_Instant"]} for n in (1, 2)],
        }, 0),
    ]


def raw(msg):
    if msg.kind.startswith("room_state"):
        return json.dumps({"matchGameRoomStateChangedEvent": {"gameRoomInfo": {
            **msg.payload, "stateType": "MatchGameRoomStateType_Playing"}}})
    kind = msg.kind.split(".", 1)[1]
    payload = {"gameStateMessage": msg.payload} if kind == "GameStateMessage" else dict(msg.payload)
    return json.dumps({"greToClientEvent": {"greToClientMessages": [{**payload, "type": "GREMessageType_" + kind}]}})


class Feed:
    def __init__(self, builder):
        self.b = builder
        self.seq = {}

    def __call__(self, sid, msgs, gen=0):
        for msg in msgs:
            key = (sid, gen)
            self.seq[key] = self.seq.get(key, 0) + 1
            self.b.ingest(TaggedMessage(SourceTag(sid, gen, self.seq[key], time.monotonic()), msg))
        return self.b.publish()


def test_nested_toml_and_layer_precedence(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[broadcast]\nmode="dual"\npreset="mixed_duo"\npbp_voice="af_bella"\n'
                      '[ingestion]\nmode="dual_file"\nlog_player1="one.log"\n')
    cfg = load(config)
    assert (cfg.broadcast_mode, cfg.log_player1) == ("dual", "one.log")
    assert resolve_booth(cfg)["pbp_voice"] == "af_bella"
    assert resolve_booth(load(config, booth_preset="sports_desk"))["pbp_voice"] == "am_adam"
    assert resolve_booth(load(config, tts_voice="af_heart"))["pbp_voice"] == "af_heart"
    assert resolve_booth(load(config, tts_voice="af_heart", pbp_voice="am_onyx"))["pbp_voice"] == "am_onyx"


def test_fusion_copies_private_cards_and_invalidates_on_loss():
    builder = DualStateBuilder(name_resolver=lambda gid: {11: "A", 22: "B"}.get(gid))
    feed = Feed(builder)
    single = feed(0, messages(1))
    assert single.objects[200].name is None
    fused = feed(1, messages(2))
    assert fused.objects[100].name == "A" and fused.objects[200].name == "B"
    assert all(k.hand_visible and k.hand_fresh_asof is not None for k in fused.seat_knowledge.values())
    builder.mark_source_lost(0)
    fallback = builder.publish()
    assert fallback.local_seat == 2
    assert set(fallback.player_decks) == {2}
    assert set(fallback.seat_knowledge) == {2}
    assert fallback.objects[100].name is None
    builder.mark_source_lost(1)
    assert builder.publish().seat_knowledge == {}


@pytest.mark.parametrize("other", [dict(gid=2, turn=2), dict(game=2)])
def test_never_fuses_different_ticks_or_games(other):
    b = DualStateBuilder()
    feed = Feed(b)
    feed(0, messages(1))
    snap = feed(1, messages(2, **other))
    assert not b.last_publish_was_enriched
    assert len(snap.seat_knowledge) == 1


def test_broken_chain_and_reconnect_require_full_baseline():
    b = DualStateBuilder()
    feed = Feed(b)
    feed(0, messages(1))
    bad = GreMessage("gre.GameStateMessage", {"type": "GameStateType_Diff",
                     "gameStateId": 3, "prevGameStateId": 2}, 0)
    assert feed(0, [bad]).seat_knowledge == {}
    assert feed(0, messages(1, gid=4)).chain_valid
    b.mark_source_lost(0)
    b.mark_source_recovered(0)
    assert feed(0, [bad], gen=1).seat_knowledge == {}
    assert feed(0, messages(1, gid=5), gen=1).chain_valid


def test_unknown_hand_is_not_claimed_visible():
    b = DualStateBuilder()
    feed = Feed(b)
    msgs = messages(1)
    payload = dict(msgs[-1].payload)
    payload["gameObjects"] = []
    msgs[-1] = replace(msgs[-1], payload=payload)
    assert not feed(0, msgs).seat_knowledge[1].hand_visible


def test_app_applies_both_voices_and_handoff(monkeypatch):
    from test_llm_booth import Writer
    monkeypatch.setattr("arenaonair.llm_booth.JsonClient", lambda _: Writer())
    app = ArenaOnAirApp(Config(broadcast_mode="dual", booth_preset="sports_desk",
                             co_caster_delay_ms=220, narration_mode="llm", llm_coalesce=0), dry_run=True, once_mode=True)
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    app.start()
    try:
        app.queue.set_active_match("m")
        s = GameState(1, None, {}, {}, {}, TurnInfo(5, 1, "main"), MatchMeta("m", None))
        app._handle_event(Event("cast", 1, {"name": "Opt"}, 1., 2), s)
        app.llm.process(app.llm.take_work())
        pair = app.queue.pending()
        assert [(u.role, u.voice) for u in pair] == [("play_by_play", "am_adam"), ("color_analyst", "am_onyx")]
        assert pair[1].anchor_uid == pair[0].uid
        assert pair[1].expires_ts > time.monotonic()
        assert app.pump.handoff_gap_s == 0.0          # dry run: printed text needs no pacing
        assert ArenaOnAirApp(Config(co_caster_delay_ms=220)).handoff_gap_s == .22
    finally:
        app.stop()


@pytest.mark.parametrize("slot", ["--log-player1", "--log-player2"])
def test_cli_reads_one_numbered_log_with_missing_partner(slot, tmp_path, capsys):
    log = tmp_path / "Player.log"
    log.write_text("\n".join(raw(m) for m in messages(2)) + "\n")
    args = [slot, str(log), "--broadcast-mode", "dual", "--dry-run", "--once"]
    assert main(args) == 0
    assert capsys.readouterr().out.strip()


def test_second_source_does_not_repeat_public_events(monkeypatch):
    app = ArenaOnAirApp(Config(log_player1="one", log_player2="two"), dry_run=True, once_mode=True)
    emitted = []
    monkeypatch.setattr(app, "_handle_event", lambda event, *a, **kw: emitted.append(event.kind))
    try:
        for sid, seat in ((0, 1), (1, 2)):
            for msg in messages(seat):
                app._feed_source_line(sid, 0, time.monotonic(), raw(msg))
        assert emitted.count("game_start") == 1
        assert emitted.count("match_start") == 1
        assert app.fusion.last_publish_was_enriched
    finally:
        app.stop()


def test_real_relay_reaches_app_without_waiting_for_second_client(monkeypatch):
    from websockets.asyncio.client import connect
    app = ArenaOnAirApp(Config(relay_bind="127.0.0.1:0", poll_interval=.01), dry_run=True)
    app.start()
    async def send():
        async with connect(f"ws://127.0.0.1:{app.watcher.server.port}") as ws:
            await ws.recv()
            for msg in messages(2):
                await ws.send(json.dumps({"type": "log", "client_id": "player", "line": raw(msg)}))
            deadline = time.monotonic() + 3
            while app.status()["match_id"] is None and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            assert app.status()["match_id"] == "match"
    try:
        asyncio.run(send())
    finally:
        app.stop()


def test_config_cli_route_replaces_toml_route(tmp_path):
    from arenaonair.config import resolve_route
    path = tmp_path / 'config.toml'
    path.write_text('[ingestion]\nmode="dual_file"\nlog_player1="one"\nlog_player2="two"\n')
    cfg = load(path, log_path="solo")
    assert resolve_route(cfg)["route"] == "single"
    assert cfg.log_player1 is None and cfg.log_player2 is None


def test_silent_sources_do_not_refresh_private_facts():
    clock = [time.monotonic()]
    b = DualStateBuilder(monotonic=lambda: clock[0])
    Feed(b)(0, messages(1))
    before = b.publish().seat_knowledge[1].hand_fresh_asof
    clock[0] += 1
    assert b.publish().seat_knowledge[1].hand_fresh_asof == before
    clock[0] += 10
    assert not b.publish().seat_knowledge[1].hand_visible


def test_new_game_survives_primary_loss_without_rewinding():
    b = DualStateBuilder()
    f = Feed(b)
    f(0, messages(1, gid=10))
    f(1, messages(2, gid=10))
    assert f(0, messages(1, gid=1, game=2)).game_id == 2
    b.mark_source_lost(0)
    assert b.publish().game_id == 2
    assert not b.publish().seat_knowledge
    assert f(1, messages(2, gid=1, game=2)).game_id == 2
    assert set(b.publish().seat_knowledge) == {2}


def test_source_game_change_does_not_inherit_submitted_deck():
    from arenaonair.state_builder import GameStateBuilder
    b = GameStateBuilder()
    for msg in messages(1):
        snap = b.apply(None, msg)
    assert snap.player_deck
    snap = b.apply(snap, messages(1, game=2)[-1])
    assert not snap.player_deck
    assert snap.game_id == 2


def test_new_match_metadata_does_not_strand_primary_on_old_partner():
    b = DualStateBuilder()
    f = Feed(b)
    f(0, messages(1))
    f(1, messages(2))
    room = GreMessage("room_state.playing", {"gameRoomConfig": {"matchId": "new-match"}}, 0)
    f(0, [room])
    payload = dict(messages(1)[-1].payload)
    payload["gameInfo"] = {**payload["gameInfo"], "matchID": "new-match"}
    assert f(0, [messages(1)[1], replace(messages(1)[-1], payload=payload)]).match_meta.match_id == "new-match"
    b.mark_source_lost(0)
    assert b.publish().match_meta.match_id == "new-match"


def test_absent_zone_membership_does_not_mean_known_empty_library():
    from arenaonair.state_builder import GameStateBuilder, with_knowledge, remaining_deck_cards
    b = GameStateBuilder()
    payload = dict(messages(1)[-1].payload)
    payload["zones"] = [{"type": "ZoneType_Library", "ownerSeatId": 1, "zoneId": 5},
                        {"type": "ZoneType_Hand", "ownerSeatId": 1, "zoneId": 6}]
    b.apply(None, messages(1)[1])
    snap = b.apply(None, replace(messages(1)[-1], payload=payload))
    snap = with_knowledge(snap, [1], time.monotonic())
    assert remaining_deck_cards(snap).uncertain
    assert not snap.seat_knowledge[1].library_accounted
    assert not snap.seat_knowledge[1].hand_visible


def test_trap_requires_correct_untapped_mana_and_surviving_hand():
    from arenaonair.differ import EventDiffer
    from arenaonair.models import CardRef, ZoneView, SeatKnowledge
    d = EventDiffer()
    base = GameState(1, None,
        {"hand:1": ZoneView(1, "hand", 1, (10,)), "battlefield:pub": ZoneView(2, "battlefield", None, (20, 21))},
        {10: CardRef(10, 1, "Counterspell", "Instant", ("instant",), owner_seat=1),
         20: CardRef(20, 2, "Island", "Basic Land", ("land",), controller_seat=1, is_tapped=False),
         21: CardRef(21, 3, "Mountain", "Basic Land", ("land",), controller_seat=1, is_tapped=False)},
        {}, TurnInfo(1, 2, "main"), MatchMeta("m", None),
        seat_knowledge={1: SeatKnowledge(1, True, 1.)}, legal_actions={1: (10,)})
    assert not d.detect_trap_armed(None, base)
    good = replace(base, objects={**base.objects, 21: replace(base.objects[21], name="Island")})
    assert d.detect_trap_armed(None, good)
    lost = replace(good, seat_knowledge={})
    assert not d.detect_trap_sprung(good, lost)
    assert not d._armed_traps
    tapped = replace(good, objects={**good.objects, 21: replace(good.objects[21], is_tapped=True)})
    assert not d.detect_trap_armed(None, tapped)
    assert not d.detect_trap_armed(None, replace(good, legal_actions={}))


def test_fallback_engine_keeps_native_voice_on_role_and_live_changes():
    from arenaonair.platform.tts import TTSEngine
    from arenaonair.speech import ChainedSpeaker, _apply_voice_kwargs
    from arenaonair.models import Utterance, DeliveryResult
    class Engine(TTSEngine):
        name = "say"
        voice = "Samantha"
        def speak(self, source):
            assert self.voice == "Samantha"
            assert source.voice is None
            return DeliveryResult(source.uid, True)
    speaker = ChainedSpeaker([Engine()])
    speaker.set_voice("am_onyx")
    assert _apply_voice_kwargs("say", Engine, {}, "am_adam") == {}
    assert speaker.speak(Utterance("u", "m", "cast", "text", 1, 1., voice="am_adam")).ok


@pytest.mark.parametrize("fixture", ["match_01.jsonl", "match_02.jsonl"])
def test_numbered_single_log_keeps_recorded_public_event_coverage(fixture):
    from pathlib import Path
    from collections import Counter
    logs = [json.dumps(json.loads(line)["obj"]) for line in
            (Path(__file__).resolve().parents[1] / "fixtures" / "matches" / fixture).read_text().splitlines()]
    results = []
    for config in (Config(), Config(log_player1="one")):
        app = ArenaOnAirApp(config, dry_run=True, once_mode=True)
        seen = []
        app._handle_event = lambda e, *a, **kw: seen.append((e.kind, e.seat))
        prev, backlog = None, []
        try:
            for line in logs:
                if app.fusion:
                    app._feed_source_line(0, 0, time.monotonic(), line)
                else:
                    prev, backlog = app._feed_line(prev, backlog, time.monotonic(), line)
            results.append(Counter(seen))
        finally:
            app.stop()
    assert results[0] == results[1]
    assert sum(results[0].values()) > 10


def test_relay_reconnect_retains_source_and_increments_generation():
    from arenaonair.relay import RelayLogSource, RelayForwarder
    source = RelayLogSource("127.0.0.1:0")
    assert RelayForwarder("localhost:1", "one").client_id == RelayForwarder("localhost:1", "one").client_id
    source._receive(1, {"client_id": "one", "line": "old"})
    source._disconnected(1)
    source._receive(2, {"client_id": "one", "line": "new"})
    batch = source.poll()
    assert len(batch) == 1 and batch[0][0:2] == (0, 1) and batch[0][3] == "new"
    source._disconnected(1)  # late close must not mark replacement offline
    assert source.source_health[0]


def test_relay_file_rotation_invalidates_old_generation():
    from arenaonair.relay import RelayLogSource
    source = RelayLogSource("127.0.0.1:0")
    source._receive(1, {"client_id": "one", "log_generation": 0, "line": "old"})
    source._receive(1, {"client_id": "one", "log_generation": 1, "line": "rotated"})
    batch = source.poll()
    assert len(batch) == 1 and batch[0][0:2] == (0, 1) and batch[0][3] == "rotated"


def test_file_watcher_truncation_advances_generation(tmp_path):
    from arenaonair.watcher import MultiLogWatcher
    path = tmp_path / "Player.log"
    path.write_text("a long previous session\n")
    watcher = MultiLogWatcher([path], anchor=False)
    try:
        assert watcher.poll()
        before = watcher.generations[0]
        path.write_text("new\n")
        assert watcher.poll()[0][2] == "new"
        assert watcher.generations[0] == before + 1
    finally:
        watcher.close()


def test_forwarder_sends_receiver_compatible_source_and_file_generation(tmp_path):
    from arenaonair.relay import RelayForwarder
    path = tmp_path / "Player.log"
    path.write_text("first\nsecond\n")
    forwarder = RelayForwarder("localhost:1", str(path))
    frames = []
    class Socket:
        async def send(self, raw_frame):
            frames.append(json.loads(raw_frame))
            if len(frames) == 2:
                forwarder.stop()
    asyncio.run(forwarder._pump(Socket()))
    assert [f["line"] for f in frames] == ["first", "second"]
    assert all(f["log_generation"] == 0 and f["client_id"] == forwarder.client_id for f in frames)
