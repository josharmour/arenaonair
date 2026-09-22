"""Dual-source ingestion stack tests (S8.x).

Covers:
- DualStateBuilder single-source immediacy, fused enrichment on validated
  same-match identity, pairing timeout on incompatible sources, and
  loss/recovery knowledge lowering with mandatory revalidation.
- MultiLogWatcher independent slots: configured-but-missing second file is
  admitted midstream without re-emitting slot 0 content or disturbing its
  offset.
- event_identity stability across observers.
- RelayServer roundtrip + auth rejection over a localhost ephemeral port.
- SourceRegistry dedup / stale-generation / recovery-generation semantics.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest

from arenaonair import events as ev
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
try:
    from arenaonair.relay import RelayServer
    _HAS_WEBSOCKETS = True
except ImportError:
    # CI runners without the optional 'websockets' package: relay tests
    # skip; everything else in this module still runs.
    RelayServer = None
    _HAS_WEBSOCKETS = False
from arenaonair.sources import (
    SourceRegistry,
    SourceTag,
    TaggedMessage,
)
from arenaonair.state_builder import DualStateBuilder, event_identity
from arenaonair.watcher import MultiLogWatcher


# ---------------------------------------------------------------------------
# Snapshot helpers (copied from test_phase0_ingestion_fixes.py -- standalone)
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


def gsm(payload):
    return GreMessage(kind="gre.GameStateMessage", payload=payload, ts=0.0)


def cast_action(iid, seat):
    return {"seatId": seat,
            "action": {"actionType": "ActionType_Cast", "instanceId": iid}}


def bf_zone(iids):
    return zone(1, "ZoneType_Battlefield", None, iids)


# ---------------------------------------------------------------------------
# DualStateBuilder feeding helpers
# ---------------------------------------------------------------------------

def room_state_msg(match_id):
    return GreMessage(
        kind="room_state.MatchGameRoomStateChangedEvent",
        payload={"matchGameRoomStateChangedEvent":
                 {"gameRoomInfo": {"gameRoomConfig": {"matchId": match_id}}}},
        ts=0.0)


def connect_resp_msg(local_seat):
    return GreMessage(
        kind="gre.ConnectResp",
        payload={"systemSeatIds": [local_seat],
                 "deckMessage": {"deckCards": [100 + local_seat]}},
        ts=0.0)


def full_state_msg(life_a=20, life_b=23):
    return GreMessage(
        kind="gre.GameStateMessage",
        payload={"type": "GameStateType_Full",
                 "zones": [],
                 "players": [
                     {"systemSeatNumber": 1, "lifeTotal": life_a},
                     {"systemSeatNumber": 2, "lifeTotal": life_b}]},
        ts=0.0)


class Feeder:
    """Feeds TaggedMessages into a DualStateBuilder with per-source seq/gen."""

    def __init__(self, builder):
        self.b = builder
        self.gen = {}
        self.seq = {}

    def _ensure(self, sid):

        if sid not in self.seq:
            self.seq[sid] = 0
            self.gen[sid] = 0

    def feed(self, sid, msgs):

        self._ensure(sid)
        out = []
        for m in msgs:
            self.seq[sid] += 1
            tag = SourceTag(sid, self.gen[sid], self.seq[sid],
                            float(self.seq[sid]))
            out.append(self.b.ingest(TaggedMessage(tag, m)))
        return out

    def bump_gen(self, sid):

        self._ensure(sid)
        self.gen[sid] += 1


class FakeClock:

    def __init__(self, start=100.0):

        self.now = start

    def __call__(self):

        return self.now

    def advance(self, dt):

        self.now += dt


# ===========================================================================
# 1. Single-source-from-start publishes immediately
# ===========================================================================

class TestSingleSourceImmediatePublish:

    def test_primary_alone_publishes_without_waiting(self):

        b = DualStateBuilder(monotonic=time.monotonic)
        f = Feeder(b)
        snaps = f.feed(0, [room_state_msg("m-solo"),
                           connect_resp_msg(1),
                           full_state_msg()])
        assert snaps[-1] is not None  # primary ingest returns its snapshot

        pub = b.publish()
        assert pub is not None
        assert pub.match_meta.match_id == "m-solo"
        assert b.last_publish_was_enriched is False

    def test_secondary_progress_absorbed_silently(self):

        b = DualStateBuilder(monotonic=time.monotonic)
        f = Feeder(b)
        f.feed(0, [room_state_msg("m-solo"), connect_resp_msg(1)])
        before = b.publish()

        # Secondary ingests: ingest() must return None (not broadcast).
        rets = f.feed(1, [room_state_msg("m-solo"), connect_resp_msg(2),
                          full_state_msg()])
        assert all(r is None for r in rets)

        # Primary unchanged meanwhile: publish still serves primary's view.
        pub = b.publish()
        assert pub.snapshot_id == before.snapshot_id \
            if before is not None else pub is not None


# ===========================================================================
# 2. MultiLogWatcher: missing second slot admitted midstream
# ===========================================================================

class TestMultiLogWatcherLateAdmission:

    def test_second_slot_admitted_midstream_without_disturbing_first(self):

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            fa = tdp / "a.log"
            fb = tdp / "b.log"

            fa.write_text("a-one\na-two\n")

            w = MultiLogWatcher([fa, fb], poll_interval=0.01, anchor=False)

            batch1 = w.poll()
            assert [(sid, line) for sid, _ts, line in batch1] == \
                [(0, "a-one"), (0, "a-two")]

            # Still nothing from the missing slot; repeated polls stay quiet.
            assert w.poll() == []

            # Slot B appears midstream with its own backlog.
            fb.write_text("b-one\nb-two\n")
            batch2 = w.poll()
            got_b = [(line) for sid, _ts, line in batch2 if sid == 1]
            got_a = [(line) for sid, _ts, line in batch2 if sid == 0]
            assert got_b == ["b-one", "b-two"]
            assert got_a == []  # slot A offset undisturbed: no re-emission

            # Both slots now track independently going forward.
            fa_open = open(fa, "a")
            fa_open.write("a-three\n")
            fa_open.close()
            fb_open = open(fb, "a")
            fb_open.write("b-three\n")
            fb_open.close()
            batch3 = w.poll()
            assert [(sid, line) for sid, _ts, line in batch3] == \
                [(0, "a-three"), (1, "b-three")]

            w.close()

    def test_missing_slot_poll_returns_empty_not_error(self):

        with tempfile.TemporaryDirectory() as td:
            only = Path(td) / "only.log"
            ghost = Path(td) / "ghost.log"
            only.write_text("x\n")

            w = MultiLogWatcher([only, ghost], anchor=False)
            batch = w.poll()
            assert [(sid, line) for sid, _ts, line in batch] == \
                [(0, "x")]
            assert w.poll() == []
            w.close()


# ===========================================================================
# 3. Fused enrichment only on validated same match identity
# ===========================================================================

class TestFusionMatchIdentityGate:

    def _boot_pair(self, match_a="m-shared", match_b="m-shared"):

        b = DualStateBuilder(monotonic=time.monotonic)
        f = Feeder(b)
        f.feed(0, [room_state_msg(match_a), connect_resp_msg(1),
                   full_state_msg()])
        f.feed(1, [room_state_msg(match_b), connect_resp_msg(2),
                   full_state_msg()])
        return b

    def test_same_match_fuses_enriched(self):

        b = self._boot_pair("m-shared", "m-shared")
        pub = b.publish()
        assert pub is not None
        assert b.last_publish_was_enriched is True
        # Both seats' private knowledge present after fusion.
        assert set(pub.seat_knowledge.keys()) == {1, 2}
        # Per-seat deck metadata merged from each source's ConnectResp.
        assert dict(pub.player_decks or {}) == {1: (101,), 2: (102,)}

    def test_different_match_ids_publish_primary_only_lowered(self):

        b = self._boot_pair("m-alpha", "m-beta")
        clock = FakeClock()
        b2 = DualStateBuilder(max_wait_s=0.05, monotonic=clock)
        f = Feeder(b2)
        f.feed(0, [room_state_msg("m-alpha"), connect_resp_msg(1),
                   full_state_msg()])
        f.feed(1, [room_state_msg("m-beta"), connect_resp_msg(2),
                   full_state_msg()])
        clock.advance(10.0)  # blow past the pairing window
        pub = b2.publish()
        assert pub is not None
        assert pub.match_meta.match_id == "m-alpha"  # primary view only
        assert b2.last_publish_was_enriched is False


# ===========================================================================
# 4. Pairing timeout bounded wait (injected fake clock)
# ===========================================================================

class TestPairingTimeout:

    def _boot_two_sources(self, b, f):

        f.feed(0, [room_state_msg("m-one"), connect_resp_msg(1),
                   full_state_msg()])
        f.feed(1, [room_state_msg("m-two"), connect_resp_msg(2),
                   full_state_msg()])

    def test_timeout_publishes_primary_with_enrichment_lowered(self):

        clock = FakeClock(start=1000.0)
        b = DualStateBuilder(max_wait_s=0.15, monotonic=clock)
        f = Feeder(b)
        self._boot_two_sources(b, f)

        # Inside the window: primary view publishes, enrichment not claimed.
        pub_inside = b.publish()
        assert pub_inside is not None
        assert b.last_publish_was_enriched is False

        # Advance past max_wait_s: timeout fires, primary-only, lowered.
        clock.advance(0.20)
        pub_after = b.publish()
        assert pub_after is not None
        assert pub_after.match_meta.match_id == "m-one"
        assert b.last_publish_was_enriched is False
        # Lowered: only the primary source's own seat knowledge survives.
        assert set(pub_after.seat_knowledge.keys()) <= {1}

    def test_single_source_never_opens_pairing_window(self):

        clock = FakeClock(start=0.0)
        b = DualStateBuilder(max_wait_s=0.15, monotonic=clock)
        f = Feeder(b)
        f.feed(0, [room_state_msg("m-solo"), connect_resp_msg(1),
                   full_state_msg()])
        pub = b.publish()
        assert pub is not None
        assert b.last_publish_was_enriched is False
        # Clock never advanced: proves no waiting occurred.
        assert clock.now == 0.0


# ===========================================================================
# 5. Source loss / recovery
# ===========================================================================

class TestSourceLossRecovery:

    def _fused_pair(self):

        b = DualStateBuilder(monotonic=time.monotonic)
        f = Feeder(b)
        f.feed(0, [room_state_msg("m-shared"), connect_resp_msg(1),
                   full_state_msg()])
        f.feed(1, [room_state_msg("m-shared"), connect_resp_msg(2),
                   full_state_msg()])
        pub = b.publish()
        assert pub is not None and b.last_publish_was_enriched is True
        return b, f

    def test_mark_source_lost_lowers_knowledge_keeps_seats(self):

        b, f = self._fused_pair()
        before = b.publish()
        assert set(before.players.keys()) == {1, 2}

        b.mark_source_lost(1)
        after = b.publish()
        assert after is not None
        # Seats/state intact...
        assert set(after.players.keys()) == {1, 2}
        assert after.match_meta.match_id == "m-shared"
        # ...but enrichment lowered: secondary's seat knowledge gone.
        assert b.last_publish_was_enriched is False
        assert set(after.seat_knowledge.keys()) <= {1}

    def test_recovery_requires_revalidation_before_enrichment(self):

        b, f = self._fused_pair()
        b.mark_source_lost(1)
        assert b.publish() is not None

        b.mark_source_recovered(1)
        f.bump_gen(1)

        # Publish BEFORE any fresh validated data: still not enriched.
        pub_pre = b.publish()
        assert pub_pre is not None
        assert b.last_publish_was_enriched is False

        # Fresh validated data re-ingested on the recovered source...
        f.feed(1, [room_state_msg("m-shared"), connect_resp_msg(2),
                   full_state_msg()])
        pub_post = b.publish()
        assert pub_post is not None
        # The recovered source revalidated its transport chain AND re-derived
        # its baseline (S8.2: recovery requires revalidation, then enrichment
        # resumes without restarting narration or changing seat identity).
        assert b.source_status(1).chain_valid is True
        assert b.last_publish_was_enriched is True
        # Enrichment restored knowledge for both seats again.
        assert set(pub_post.seat_knowledge.keys()) >= {1, 2}

    def test_registry_generation_bumped_on_recovery(self):

        reg = SourceRegistry()
        tag0 = SourceTag(1, 0, 5, 1.0)
        assert reg.mark_message(tag0) is True
        new_gen = reg.mark_recovered(1)
        assert new_gen == 1
        # Old-generation replay now rejected...
        assert reg.mark_message(SourceTag(1, 0, 6, 2.0)) is False
        # ...while the new generation admits from a fresh sequence floor.
        assert reg.mark_message(SourceTag(1, 1, 0, 3.0)) is True


# ===========================================================================
# 6. event_identity stability across observers
# ===========================================================================

class TestEventIdentity:

    def test_same_cast_via_two_sources_identical_keys(self):

        e_a = Event(kind=ev.CAST, seat=1,
                    payload={"instance_id": 42, "name": "Grizzly Bears"},
                    ts=100.0, salience=2)
        e_b = Event(kind=ev.CAST, seat=1,
                    payload={"instance_id": 42, "name": "Grizzly Bears",
                             "snapshot_id": 777, "source_id": 1,
                             "received_via": "relay", "cast_ts": 999.5},
                    ts=200.0, salience=2)
        keys = event_identity([e_a, e_b])
        assert len(keys) == 2
        assert keys[0] == keys[1]

    def test_different_actions_differ_and_order_is_irrelevant(self):

        e1 = Event(kind=ev.CAST, seat=1,
                   payload={"instance_id": 42}, ts=1.0, salience=2)
        e2 = Event(kind=ev.CAST, seat=2,
                   payload={"instance_id": 42}, ts=1.0, salience=2)
        e3 = Event(kind=ev.LAND_DROP, seat=1,
                   payload={"instance_id": 42}, ts=1.0, salience=2)
        keys = event_identity([e1, e2, e3])
        assert len(set(keys)) == 3

    def test_payload_key_order_does_not_change_key(self):

        ea = Event(kind=ev.CAST, seat=1,
                   payload={"a": 1, "b": 2}, ts=1.0, salience=2)
        eb = Event(kind=ev.CAST, seat=1,
                   payload={"b": 2, "a": 1}, ts=9.0, salience=2)
        keys = event_identity([ea, eb])
        assert keys[0] == keys[1]


# ===========================================================================
# 7. Relay roundtrip over localhost ephemeral port
# ===========================================================================

def _recv_json(ws, want_types, timeout=5.0, skip=()):

    """Next frame whose type is in want_types; ack/welcome-type handshake
    frames listed in ``skip`` are drained transparently."""

    async def _one():

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline -
                                             time.monotonic())
            except asyncio.TimeoutError:
                return None
            obj = json.loads(raw)
            if obj.get("type") in want_types:
                return obj
            if obj.get("type") not in skip:
                return obj
        return None

    return _one()


_HANDSHAKE = ("ack", "welcome")


class TestRelayRoundtrip:

    @pytest.mark.skipif(not _HAS_WEBSOCKETS,
                        reason="'websockets' package not installed")
    def test_auth_roundtrip_and_frame_receipt(self):

        async def scenario():

            server = RelayServer("127.0.0.1", 0, secret="s3cret")
            await server.start()
            assert server.port > 0  # ephemeral port reflected

            received = []

            async def subscriber():

                import websockets.asyncio.client as wsclient
                async with wsclient.connect(
                        f"ws://127.0.0.1:{server.port}") as sub_ws:
                    await sub_ws.send(json.dumps(
                        {"type": "auth", "token": "s3cret"}))
                    # Drain ack + welcome handshake frames.
                    ack = await _recv_json(sub_ws, {"ack"})
                    assert ack is not None
                    welcome = await _recv_json(sub_ws, {"welcome"})
                    assert isinstance(welcome["conn_id"], int)
                    # Then wait for the fanned-out log frame.
                    frame = await _recv_json(sub_ws, {"frame"})
                    received.append(frame)

            import websockets.asyncio.client as wsclient
            async with wsclient.connect(
                    f"ws://127.0.0.1:{server.port}") as sender:
                await sender.send(json.dumps(
                    {"type": "auth", "token": "s3cret"}))
                ack = await _recv_json(sender, {"ack"})
                assert ack is not None
                welcome = await _recv_json(sender, {"welcome"})
                assert isinstance(welcome["conn_id"], int)

                sub_task = asyncio.ensure_future(subscriber())
                # Give the subscriber a beat to authenticate + register.
                await asyncio.sleep(0.3)

                await sender.send(json.dumps({
                    "type": "log",
                    "line": "[UnityCrossThreadLogger] hello",
                    "ts_client_wallclock": 12345.6,
                }))
                echoed = await _recv_json(sender, {"frame"})
                assert echoed["payload"]["line"] == \
                    "[UnityCrossThreadLogger] hello"
                assert isinstance(echoed["conn_id"], int)

                got = await asyncio.wait_for(sub_task, timeout=5.0)
                assert got is None  # subscriber() returns nothing; side effects
                assert len(received) == 1
                assert received[0]["payload"]["type"] == "log"
                assert received[0]["payload"]["line"].endswith("hello")
                assert received[0]["conn_id"] == echoed["conn_id"]

            assert server.connection_count >= 0
            await server.close()

        asyncio.run(scenario())

    @pytest.mark.skipif(not _HAS_WEBSOCKETS,
                        reason="'websockets' package not installed")
    def test_wrong_secret_client_gets_closed(self):

        async def scenario():

            server = RelayServer("127.0.0.1", 0, secret="right-token")
            await server.start()

            import websockets.asyncio.client as wsclient
            async with wsclient.connect(
                    f"ws://127.0.0.1:{server.port}") as bad:
                await bad.send(json.dumps(
                    {"type": "auth", "token": "WRONG"}))
                err = await _recv_json(bad, {"error"})
                assert err == {"type": "error", "code": "bad_token"}
                # Server closes right after the error frame.
                closed = False
                try:
                    while True:
                        await asyncio.wait_for(bad.recv(), timeout=5.0)
                except Exception:
                    closed = True
                assert closed is True

            # A client sending a NON-auth first frame is also rejected.
            async with wsclient.connect(
                    f"ws://127.0.0.1:{server.port}") as sloppy:
                await sloppy.send(json.dumps(
                    {"type": "log", "line": "too eager"}))
                err2 = await _recv_json(sloppy, {"error"})
                assert err2["code"] == "auth_required"

            await server.close()

        asyncio.run(scenario())


# ===========================================================================
# 8. SourceRegistry unit behaviors
# ===========================================================================

class TestSourceRegistry:

    def test_duplicate_recv_seq_rejected(self):

        reg = SourceRegistry()
        t1 = SourceTag(0, 0, 1, 10.0)
        assert reg.mark_message(t1) is True
        assert reg.mark_message(SourceTag(0, 0, 1, 10.5)) is False
        assert reg.mark_message(SourceTag(0, 0, 0, 9.0)) is False  # out-of-order
        assert reg.mark_message(SourceTag(0, 0, 2, 11.0)) is True

    def test_stale_generation_replay_rejected(self):

        reg = SourceRegistry()
        assert reg.mark_message(SourceTag(1, 3, 7, 5.0)) is True
        # Older generation arriving later: pre-reconnect replay -> reject.
        assert reg.mark_message(SourceTag(1, 2, 99, 6.0)) is False
        # Newer generation adopted outright; sequence floor reseeds.
        assert reg.mark_message(SourceTag(1, 4, 0, 7.0)) is True

    def test_mark_recovered_bumps_generation_and_resets_floor(self):

        reg = SourceRegistry()
        assert reg.mark_message(SourceTag(0, 2, 50, 1.0)) is True
        new_gen = reg.mark_recovered(0)
        assert new_gen == 3
        # Same-generation traffic now rejected (floor reset to -1 -> seq must
        # exceed it within gen 3; but gen 2 tags are stale).
        assert reg.mark_message(SourceTag(0, 2, 51, 2.0)) is False
        # Next-generation traffic admitted from a fresh floor.
        assert reg.mark_message(SourceTag(0, 3, 0, 3.0)) is True

    def test_status_flags_and_disconnect(self):

        reg = SourceRegistry()
        st = reg.status(2)
        assert st.transport_ok is False and st.chain_valid is False
        reg.mark_message(SourceTag(2, 0, 1, 4.0))
        reg.set_flags(2, chain_valid=True, baseline_ok=True)
        st2 = reg.status(2)
        assert st2.transport_ok is True
        assert st2.chain_valid is True and st2.baseline_ok is True
        reg.mark_disconnected(2)
        st3 = reg.status(2)
        assert st3.transport_ok is False   # flags lower...
        assert st3.chain_valid is True     # ...stream truths untouched

    def test_unknown_source_status_materializes_blank(self):

        reg = SourceRegistry()
        st = reg.status(9)
        assert st.source_id == 9
        assert st.transport_ok is False and st.baseline_ok is False
