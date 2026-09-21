"""Tests for arenaonair.watcher.LogWatcher.

Uses tmp_path files simulating append growth across poll() calls; no threads,
no real MTGA installation.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

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
