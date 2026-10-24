"""Tests for arenaonair.watcher.LogWatcher.

Uses tmp_path files simulating append growth across poll() calls; no threads,
no real MTGA installation.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from arenaonair.watcher import LogWatcher


def _write(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def _rewrite(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _texts(batch):
    return [line for _, line in batch]


class TestBasicTail:

    def test_reads_existing_content_on_first_poll(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("alpha\nbeta\n")
        w = LogWatcher(path=log, anchor=False)
        assert _texts(w.poll()) == ["alpha", "beta"]

    def test_append_growth_across_polls(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("one\n")
        w = LogWatcher(path=log, anchor=False)
        assert _texts(w.poll()) == ["one"]
        _write(log, "two\nthree\n")
        assert _texts(w.poll()) == ["two", "three"]
        _write(log, "four\n")
        assert _texts(w.poll()) == ["four"]

    def test_no_new_lines_returns_empty(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("only\n")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        assert w.poll() == []

    def test_missing_file_then_appears(self, tmp_path):
        log = tmp_path / "Player.log"
        w = LogWatcher(path=log, anchor=False)
        assert w.poll() == []
        log.write_text("late arrival\n")
        assert _texts(w.poll()) == ["late arrival"]

    def test_timestamps_are_wall_clock_floats(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("x\n")
        before = time.time()
        batch = LogWatcher(path=log, anchor=False).poll()
        after = time.time()
        assert len(batch) == 1
        ts, line = batch[0]
        assert isinstance(ts, float) and before <= ts <= after
        assert line == "x"

    def test_carriage_return_stripped(self, tmp_path):
        log = tmp_path / "Player.log"
        with open(log, "wb") as f:
            f.write(b"crlf line\r\n")
        w = LogWatcher(path=log, anchor=False)
        assert _texts(w.poll()) == ["crlf line"]

    def test_lines_iterator_yields_and_satisfies_protocol(self, tmp_path):
        from arenaonair.interfaces import LogSource

        log = tmp_path / "Player.log"
        log.write_text("a\nb\n")
        w = LogWatcher(path=log, anchor=False)
        assert isinstance(w, LogSource)
        it = w.lines()
        first_two = [next(it) for _ in range(2)]
        assert _texts(first_two) == ["a", "b"]
        it.close()

    def test_explicit_path_beats_platform_default(self, tmp_path):
        log = tmp_path / "custom.log"
        log.write_text("z\n")
        w = LogWatcher(path=str(log), anchor=False)
        assert w.path == log
        assert _texts(w.poll()) == ["z"]


class TestPartialLines:

    def test_partial_tail_buffered_until_newline(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("complete\nhalf ")
        w = LogWatcher(path=log, anchor=False)
        assert _texts(w.poll()) == ["complete"]
        _write(log, "written now\n")
        assert _texts(w.poll()) == ["half written now"]

    def test_multi_chunk_partial_accumulates(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        for piece in ("par", "tial-", "line"):
            _write(log, piece)
            assert w.poll() == []
        _write(log, "\nfinal\n")
        assert _texts(w.poll()) == ["partial-line", "final"]

    def test_utf8_multibyte_split_across_polls(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_bytes(b"")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        word = "caf\u00e9\u2665"
        raw = word.encode("utf-8")
        with open(log, "wb") as f:
            f.write(raw[:3])
            f.flush()
            os.truncate(str(log), f.tell())
            f.seek(0)
            f.write(raw[:3])
            f.truncate()
            os.truncate(str(log), f.tell())
            f.seek(0)
            f.write(raw[:3])
            f.truncate()
            os.truncate(str(log), f.tell())
            f.seek(0)
            f.write(raw[:3])
            f.truncate()
            os.truncate(str(log), f.tell())
            f.seek(0)
            f.write(raw[:3])
            f.truncate()


class TestRotationAndTruncation:

    def test_truncate_and_rewrite_detected(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("gen1-one\ngen1-two\ngen1-three\n")
        w = LogWatcher(path=log, anchor=False)
        assert len(w.poll()) == 3
        _rewrite(log, "gen2-start\n")
        got = w.poll() + w.poll()
        assert _texts(got) == ["gen2-start"], got

    def test_truncate_to_zero_then_refill(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("old stuff\nmore old\n")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        open(log, "w").close()
        assert w.poll() == []
        _write(log, "brand new\n")
        assert _texts(w.poll()) == ["brand new"]

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows file locking prevents replacing files while held open",
    )
    def test_inode_swap_rotation_with_prev_sibling(self, tmp_path):
        log = tmp_path / "Player.log"
        prev = tmp_path / "Player-prev.log"
        log.write_text("gen1 a\ngen1 b\n")
        w = LogWatcher(path=log, anchor=False)
        assert len(w.poll()) == 2
        os.replace(log, prev)
        log.write_text("gen2 a\n")
        got = w.poll() + w.poll()
        assert _texts(got) == ["gen2 a"], got

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows file locking prevents replacing files while held open",
    )
    def test_inode_swap_without_sibling_resets_to_zero(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("first generation\n" * 5)
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        new = tmp_path / "Player.log.new"
        new.write_text("replacement gen\n")
        os.replace(new, log)
        got = w.poll() + w.poll()
        assert _texts(got) == ["replacement gen"], got

    def test_external_shrink_detected(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("aaa\nbbb\nccc\n")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        with open(log, "r+b") as f:
            f.truncate(2)
            f.seek(0)
            f.write(b"a\n")
            f.flush()
        got = w.poll() + w.poll()
        assert _texts(got) == ["a"], got

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows file locking prevents replacing files while held open",
    )
    def test_rotation_replays_new_generation_even_when_anchored(self, tmp_path):
        log = tmp_path / "Player.log"
        body = [f"noise {i}" for i in range(60)]
        body.append("[UnityCrossThreadLogger]9/18/2026: Match to OLD: GreToClientEvent {}")
        body.append("old tail")
        log.write_text("\n".join(body) + "\n")
        w = LogWatcher(path=log, anchor=True)
        first = w.poll()
        assert any("Match to OLD" in t for t in _texts(first))
        os.replace(log, tmp_path / "Player-prev.log")
        new_body = [f"boot {i}" for i in range(30)]
        new_body.append("[UnityCrossThreadLogger]9/19/2026: Match to NEW: GreToClientEvent {}")
        new_body.append("new tail")
        log.write_text("\n".join(new_body) + "\n")
        got = w.poll() + w.poll()
        texts = _texts(got)
        assert any("Match to NEW" in t for t in texts), texts
        assert not any("Match to OLD" in t for t in texts), texts


class TestAnchorScan:

    def _make_log(self, tmp_path, n_noise=150, n_post=5):
        log = tmp_path / "big.log"
        lines = [f"noise {i}" for i in range(n_noise)]
        lines.append("[UnityCrossThreadLogger]9/18/2026 10:08:16 PM: Match to ABC123: GreToClientEvent {}")
        lines += [f"post {i}" for i in range(n_post)]
        log.write_text("\n".join(lines) + "\n")
        return log

    def test_anchor_skips_earlier_lines(self, tmp_path):
        log = self._make_log(tmp_path)
        w = LogWatcher(path=log, anchor=True)
        texts = _texts(w.poll())
        assert len(texts) == 6, texts
        assert texts[0].startswith("[UnityCrossThreadLogger]")
        assert "Match to ABC123" in texts[0]
        assert texts[-1] == "post 4"
        assert not any(t.startswith("noise") for t in texts)

    def test_anchor_disabled_replays_everything(self, tmp_path):
        log = self._make_log(tmp_path)
        w = LogWatcher(path=log, anchor=False)
        texts = _texts(w.poll())
        assert len(texts) == 156
        assert texts[0] == "noise 0"

    def test_anchor_prefers_last_marker_line(self, tmp_path):
        log = tmp_path / "multi.log"
        lines = ["start"]
        lines.append("[UnityCrossThreadLogger]early: Match to FIRST: GreToClientEvent {}")
        lines += [f"mid {i}" for i in range(20)]
        lines.append("[UnityCrossThreadLogger]late: Match to SECOND: GreToClientEvent {}")
        lines.append("tail")
        log.write_text("\n".join(lines) + "\n")
        w = LogWatcher(path=log, anchor=True)
        texts = _texts(w.poll())
        assert "Match to SECOND" in texts[0], texts
        assert "Match to FIRST" not in "".join(texts)

    def test_anchor_falls_back_to_full_replay_without_markers(self, tmp_path):
        log = tmp_path / "nomark.log"
        log.write_text("\n".join(f"plain {i}" for i in range(50)) + "\n")
        w = LogWatcher(path=log, anchor=True)
        assert len(_texts(w.poll())) == 50

    def test_anchor_window_bounds_scan(self, tmp_path):
        # The ancient marker lies OUTSIDE the backward window; a fresh marker
        # inside it must win. A bounded scan never sees OLDONE, so the watcher
        # anchors on the recent marker instead of replaying from the ancient
        # one.
        log = tmp_path / "windowed.log"
        lines = ["[UnityCrossThreadLogger]ancient: Match to OLDONE: GreToClientEvent {}"]
        lines += [f"filler {i} " + "x" * 200 for i in range(3000)]
        lines.append("[UnityCrossThreadLogger]recent: Match to NEWONE: GreToClientEvent {}")
        lines.append("tail")
        log.write_text("\n".join(lines) + "\n")
        w = LogWatcher(path=log, anchor=True, anchor_window_lines=100)
        texts = _texts(w.poll())
        assert any("NEWONE" in t for t in texts)
        assert not any("OLDONE" in t for t in texts)


class TestStalledSeconds:

    def test_zero_before_first_line(self, tmp_path):
        log = tmp_path / "Player.log"
        w = LogWatcher(path=log, anchor=False)
        assert w.stalled_seconds == 0.0

    def test_grows_while_quiet_and_resets_on_new_line(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("hello\n")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        time.sleep(0.15)
        assert w.stalled_seconds >= 0.1
        _write(log, "again\n")
        w.poll()
        assert w.stalled_seconds < 0.1


class TestCloseAndReuse:

    def test_close_is_idempotent(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("x\n")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        w.close()
        w.close()

    def test_poll_after_close_recovers(self, tmp_path):
        log = tmp_path / "Player.log"
        log.write_text("x\ny\n")
        w = LogWatcher(path=log, anchor=False)
        w.poll()
        w.close()


class TestRestoreBeforeLive:
    @staticmethod
    def state(gid, *, full=False, match='m', game=1, life=20):
        import json
        return json.dumps({'greToClientEvent': {'greToClientMessages': [
            {'type': 'GREMessageType_GameStateMessage', 'gameStateMessage': {
                'type': 'GameStateType_Full' if full else 'GameStateType_Diff',
                'gameStateId': gid, 'prevGameStateId': gid - 1,
                'gameInfo': {'matchID': match, 'gameNumber': game, 'stage': 'GameStage_Playing'},
                'zones': [], 'gameObjects': [],
                'players': [{'systemSeatNumber': 1, 'lifeTotal': life}],
                'turnInfo': {'turnNumber': gid, 'activePlayer': 1}}}]}})

    @staticmethod
    def fold(lines):
        from arenaonair.gre_parser import parse_line_all
        from arenaonair.state_builder import GameStateBuilder
        builder, snap = GameStateBuilder(), None
        for ts, raw in lines:
            for msg in parse_line_all(ts, raw):
                snap = builder.apply(snap, msg) or snap
        return snap

    @pytest.mark.parametrize('connect_first', [False, True])
    def test_latest_full_and_metadata_restore_without_old_turns(self, tmp_path, connect_first):
        import json
        room = json.dumps({'matchGameRoomStateChangedEvent': {'gameRoomInfo': {
            'stateType': 'MatchGameRoomStateType_Playing', 'gameRoomConfig': {
                'matchId': 'm', 'reservedPlayers': [{'systemSeatId': 1, 'playerName': 'Alice'}]}}}})
        connect = json.dumps({'greToClientEvent': {'greToClientMessages': [
            {'type': 'GREMessageType_ConnectResp', 'systemSeatIds': [1]}]}})
        p = tmp_path / 'Player.log'
        p.write_text('\n'.join(([connect, room] if connect_first else [room, connect]) + [self.state(1, full=True), self.state(2),
                                self.state(10, full=True), self.state(11, life=17)]) + '\n')
        w = LogWatcher(p, restore=True)
        try:
            batch = w.poll()
            assert w.priming and len(batch) == 4  # metadata plus latest baseline/diff
            snap = self.fold(batch)
            assert snap.chain_valid and snap.gre_state_id == 11
            assert snap.local_seat == 1 and snap.match_meta.player_names == {1: 'Alice'}
            assert snap.players[1].life == 17
            _write(p, self.state(12, life=16) + '\n')
            assert len(w.poll()) == 1 and not w.priming
        finally:
            w.close()

    @pytest.mark.parametrize('scope', [{'match': 'new'}, {'game': 2}])
    def test_new_scope_without_baseline_never_inherits_old_state(self, tmp_path, scope):
        p = tmp_path / 'Player.log'
        p.write_text(self.state(1, full=True) + '\n' + self.state(2, **scope) + '\n')
        w = LogWatcher(p, restore=True)
        try:
            snap = self.fold(w.poll())
            assert not snap.chain_valid
            assert snap.match_meta.match_id == scope.get('match', 'm')
            assert snap.game_id == scope.get('game', 1)
        finally:
            w.close()

    def test_append_during_scan_is_live_and_partial_line_is_not_lost(self, tmp_path, monkeypatch):
        p = tmp_path / 'Player.log'
        p.write_text(self.state(1, full=True) + '\n')
        w = LogWatcher(p, restore=True)
        scan = w._restore_offset
        def append(fh, end):
            offset = scan(fh, end)
            _write(p, self.state(2) + '\n')
            return offset
        monkeypatch.setattr(w, '_restore_offset', append)
        try:
            assert len(w.poll()) == 1 and w.priming
            assert len(w.poll()) == 1 and not w.priming
            _write(p, self.state(3)[:20])
            assert w.poll() == []
            _write(p, self.state(3)[20:] + '\n')
            assert len(w.poll()) == 1
        finally:
            w.close()

    def test_empty_at_launch_does_not_suppress_first_live_event(self, tmp_path):
        p = tmp_path / 'Player.log'
        p.write_text('')
        w = LogWatcher(p, restore=True)
        try:
            assert w.poll() == [] and not w.priming
            _write(p, self.state(1, full=True) + '\n')
            assert len(w.poll()) == 1 and not w.priming
        finally:
            w.close()

    def test_malformed_startup_payloads_do_not_abort_reconstruction(self, tmp_path):
        import json
        p = tmp_path / 'Player.log'
        malformed = [json.dumps({'greToClientEvent': {'greToClientMessages': [
            {'type': 'GREMessageType_GameStateMessage', 'gameStateMessage': body}]}})
            for body in ([1], {'gameInfo': [1]}, {'gameStateMessage': [1]})]
        p.write_text('\n'.join(malformed + [self.state(1, full=True)]) + '\n')
        w = LogWatcher(p, restore=True)
        try:
            assert self.fold(w.poll()).chain_valid
        finally:
            w.close()

    def test_partial_full_retains_required_earlier_baseline(self, tmp_path):
        import json
        first = json.loads(self.state(1, full=True))
        first['greToClientEvent']['greToClientMessages'][0]['gameStateMessage']['gameObjects'] = [
            {'instanceId': 50, 'grpId': 10, 'type': 'GameObjectType_Card', 'zoneId': 1}]
        partial = {'greToClientEvent': {'greToClientMessages': [
            {'type': 'GREMessageType_GameStateMessage', 'gameStateMessage': {
                'type': 'GameStateType_Full', 'gameStateId': 2,
                'gameInfo': {'matchID': 'm', 'gameNumber': 1}}}]}}
        p = tmp_path / 'Player.log'
        p.write_text(json.dumps(first) + '\n' + json.dumps(partial) + '\n')
        w = LogWatcher(p, restore=True)
        try:
            snap = self.fold(w.poll())
            assert snap.chain_valid and snap.gre_state_id == 2 and 50 in snap.objects
        finally:
            w.close()

    def test_truncation_after_restore_is_live(self, tmp_path):
        p = tmp_path / 'Player.log'
        p.write_text(self.state(1, full=True) + '\n' + 'noise\n' * 20)
        w = LogWatcher(p, restore=True)
        try:
            w.poll()
            p.write_text('')
            assert w.poll() == []
            _write(p, self.state(1, full=True, match='next') + '\n')
            assert not w.priming
            snap = self.fold(w.poll())
            assert snap.match_meta.match_id == 'next' and not w.priming
        finally:
            w.close()


    def test_new_match_without_connect_does_not_inherit_previous_seat(self, tmp_path):
        import json
        def room(match):
            return json.dumps({'matchGameRoomStateChangedEvent': {'gameRoomInfo': {
                'stateType': 'MatchGameRoomStateType_Playing', 'gameRoomConfig': {'matchId': match}}}})
        connect = json.dumps({'greToClientEvent': {'greToClientMessages': [
            {'type': 'GREMessageType_ConnectResp', 'systemSeatIds': [1]}]}})
        p = tmp_path / 'Player.log'
        p.write_text('\n'.join([connect, room('old'), room('new'), self.state(1, full=True, match='new')]) + '\n')
        w = LogWatcher(p, restore=True)
        try:
            snap = self.fold(w.poll())
            assert snap.match_meta.match_id == 'new' and snap.chain_valid
            assert snap.local_seat is None
        finally:
            w.close()
