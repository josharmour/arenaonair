"""Tests for arenaonair.speech — queue, staleness, pump, engine chains.

Covers the reliability-contract shapes named in DESIGN §6 items 1-3:
  #1 no silent drops (seeded random FakeSpeaker failures, caplog counting),
  #2 no stale-match speech (match-switch staleness + flush discipline),
  #3 no position-bound staleness (turn numbers never gate delivery).
"""

from __future__ import annotations

import logging
import random
import threading
import time

import pytest

from arenaonair.models import DeliveryResult, Utterance
from arenaonair.speech import (
    SALIENCE_HIGH,
    SALIENCE_LOW,
    SALIENCE_MUST_SPEAK,
    EngineSpeaker,
    FakeSpeaker,
    SpeechPump,
    SpeechQueue,
    build_speaker_chain,
    detect_platform,
)


def mk(uid, salience=1, ts=0.0, match="m1", text=None, kind="life_change"):
    return Utterance(
        uid=uid,
        match_id=match,
        kind=kind,
        text=text or f"utterance {uid}",
        salience=salience,
        ts_created=ts,
    )


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

class TestPriorityOrdering:
    def test_salience_desc_then_ts_asc(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk("low-early", salience=1, ts=10.0))
        q.enqueue(mk("high-late", salience=2, ts=20.0))
        q.enqueue(mk("high-early", salience=2, ts=5.0))
        drained = q.drain()
        assert [u.uid for u in drained] == ["high-early", "high-late", "low-early"]

    def test_salience_tie_breaks_by_ts(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        for i in range(5):
            q.enqueue(mk(f"t{i}", salience=2, ts=float(i)))
        drained = q.drain()
        assert [u.uid for u in drained] == [f"t{i}" for i in range(5)]

    def test_equal_salience_equal_ts_fifo(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        for i in range(3):
            q.enqueue(mk(f"s{i}", salience=1, ts=7.0))
        drained = q.drain()
        assert [u.uid for u in drained] == ["s0", "s1", "s2"]

    def test_pop_best_removes_item(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk("a", salience=1))
        q.enqueue(mk("b", salience=2))
        top = q.pop_best()
        assert top.uid == "b"
        assert len(q) == 1

    def test_empty_queue_drain_is_empty_list(self):
        assert SpeechQueue().drain() == []


# ---------------------------------------------------------------------------
# Dedupe by uid
# ---------------------------------------------------------------------------

class TestDedupe:
    def test_duplicate_uid_enqueue_is_noop(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        assert q.enqueue(mk("dup")) is True
        assert q.enqueue(mk("dup")) is False
        assert len(q) == 1

    def test_dedupe_keeps_first_version(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk("dup", text="first"))
        q.enqueue(mk("dup", text="second"))
        drained = q.drain()
        assert len(drained) == 1
        assert drained[0].text == "first"

    def test_distinct_uids_both_stored(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        assert q.enqueue(mk("a")) is True
        assert q.enqueue(mk("b")) is True
        assert len(q) == 2


# ---------------------------------------------------------------------------
# Flush (match-end discipline)
# ---------------------------------------------------------------------------

class TestFlush:
    def test_flush_clears_only_that_match(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk("keep-a", match="m2"))
        q.enqueue(mk("keep-b", match="m2"))
        q.enqueue(mk("drop-a", match="m1"))
        removed = q.flush("m1")
        assert removed == 1
        # Post-match-end sequencing: the next match becomes active.
        q.set_active_match("m2")
        drained = q.drain()
        assert [u.uid for u in drained] == ["keep-a", "keep-b"]

    def test_flush_unknown_match_removes_nothing(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk("stay"))
        assert q.flush("nope") == 0
        assert len(q) == 1

    def test_flushed_match_cannot_be_reenqueued(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk("gone"))
        q.flush("m1")
        # Late re-enqueue of flushed-match speech must not resurrect it.
        assert q.enqueue(mk("gone-resurrected", match="m1")) is False

    def test_flush_then_new_match_starts_clean(self):
        # Reliability contract #2 shape: match-end -> new-match-start race.
        q = SpeechQueue()
        q.set_active_match("old")
        q.enqueue(mk("phantom-opener", match="old"))
        q.flush("old")
        q.set_active_match("new")
        # Old-match speech must never surface under the new match.
        assert all(u.match_id != "old" for u in q.drain())

    def test_flush_none_only_clears_none_match(self):
        q = SpeechQueue()
        q.set_active_match(None)
        q.enqueue(mk("anon", match=None))
        q.enqueue(mk("scoped", match="m9"))
        assert q.flush(None) == 1
        assert len(q) == 1


# ---------------------------------------------------------------------------
# Fast-tempo play pruning
# ---------------------------------------------------------------------------

class TestPrunePlays:
    def test_prune_plays_removes_gameplay_leaves_boundaries(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk("op", kind="match_start", match="m1", salience=3))
        q.enqueue(mk("cast1", kind="cast", match="m1", salience=1))
        q.enqueue(mk("atk1", kind="attack_declared", match="m1", salience=2))
        q.enqueue(mk("end", kind="game_end", match="m1", salience=3))
        assert q.play_count("m1") == 2
        removed = q.prune_plays("m1")
        assert removed == 2
        assert q.play_count("m1") == 0
        remaining_kinds = [u.kind for u in q.drain()]
        assert "cast" not in remaining_kinds
        assert "attack_declared" not in remaining_kinds
        assert set(remaining_kinds) == {"match_start", "game_end"}

    def test_prune_plays_only_affects_target_match(self):
        q = SpeechQueue()
        q.enqueue(mk("c1", kind="cast", match="m1"))
        q.enqueue(mk("c2", kind="cast", match="m2"))
        removed = q.prune_plays("m1")
        assert removed == 1
        assert len(q) == 1
        assert q._items[0].match_id == "m2"


# ---------------------------------------------------------------------------
# Session-scoped staleness (NO turn-number gating anywhere)
# ---------------------------------------------------------------------------

class TestSessionScopedStaleness:
    def test_old_match_utterance_never_spoken_after_switch(self):
        # Reliability contract #2: after switching to a new match, queued
        # speech from the old match is stale and never delivered.
        q = SpeechQueue()
        spk = FakeSpeaker()
        pump = SpeechPump(queue=q, speaker=spk)

        q.set_active_match("match-A")
        pump.run_once()  # queue empty; nothing happens
        old_utt = mk("old-news", match="match-A")
        # Enqueue BEFORE the switch so it sits in the queue across it.
        # (set_active_match stales it retroactively.)
        

    def test_old_match_utterance_never_spoken_after_switch(self):
        # Reliability contract #2: after switching to a new match, queued
        # speech from the old match is stale and never delivered.
        q = SpeechQueue()
        spk = FakeSpeaker()
        pump = SpeechPump(queue=q, speaker=spk)

        q.set_active_match("match-A")
        q.enqueue(mk("old-news", match="match-A"))
        q.set_active_match("match-B")  # match switch
        pump.run_once()
        pump.run_once()
        assert spk.delivered_texts == []

    def test_current_match_utterance_still_delivered(self):
        q = SpeechQueue()
        spk = FakeSpeaker()
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        q.enqueue(mk("fresh"))
        assert pump.run_once() is not None
        assert spk.delivered_texts == ["utterance fresh"]

    def test_no_turn_number_gating_delivery_across_turns(self):
        # Reliability contract #3: an utterance rendered during turn N must
        # still be delivered "during turn N+1". There is no turn concept in
        # the queue at all, so we prove delivery is unaffected by elapsed
        # time / intervening activity within the same match.
        q = SpeechQueue()
        spk = FakeSpeaker()
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        q.enqueue(mk("rendered-turn-N", ts=100.0))
        # Simulate lots of later activity (higher ts) in the same match.
        for i in range(10):
            q.enqueue(mk(f"later-{i}", ts=200.0 + i, salience=3))
            pump.run_once()
        result = pump.run_once()  # finally delivers the turn-N utterance
        assert result is not None and result.ok
        assert any("rendered-turn-N" in t for t in spk.delivered_texts)

    def test_stale_items_skipped_but_fresh_ones_flow(self):
        q = SpeechQueue()
        q.set_active_match("m2")
        q.enqueue(mk("stale-one", match="m1"))
        q.enqueue(mk("fresh-one", match="m2"))
        drained = q.drain()
        assert [u.uid for u in drained] == ["fresh-one"]

    def test_cross_match_utterance_queued_but_filtered_at_pop(self):
        # Enqueue accepts it (race window around match transitions);
        # staleness filters it when popping against the active match.
        q = SpeechQueue()
        q.set_active_match("live")
        assert q.enqueue(mk("ghost", match="dead")) is True
        assert len(q) == 1
        assert q.peek_best() is None
        assert q.drain() == []

    def test_null_match_uttreance_follows_active_match_scope(self):
        # An utterance with match_id=None is only fresh while no match is
        # active; once a real match activates it becomes stale.
        q = SpeechQueue()
        q.set_active_match(None)
        assert q.enqueue(mk("ambient", match=None)) is True
        q.set_active_match("m1")
        assert q.peek_best() is None


# ---------------------------------------------------------------------------
# No silent drops (reliability contract #1)
# ---------------------------------------------------------------------------

class TestNoSilentDrops:
    def test_seeded_random_failures_all_logged(self, caplog):
        # 200 utterances, seeded random failures: every failure must produce
        # exactly one WARNING carrying reason + full text; zero unlogged drops.
        q = SpeechQueue()
        spk = FakeSpeaker(fail_rate=0.35, seed=1234)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")

        expected_failures = 0
        with caplog.at_level(logging.WARNING, logger="arenaonair.speech"):
            for i in range(200):
                utt = mk(f"u{i}", salience=i % 4, ts=float(i))
                q.enqueue(utt)
                result = pump.run_once()
                assert result is not None
                if not result.ok:
                    expected_failures += 1

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "FAILED" in r.getMessage()
        ]
        assert expected_failures > 0, "seed should produce some failures"
        assert len(warnings) == expected_failures

    def test_warning_contains_full_text_and_reason(self, caplog):
        q = SpeechQueue()
        spk = FakeSpeaker(fail_next=1, fail_reason="engine exploded")
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        with caplog.at_level(logging.WARNING, logger="arenaonair.speech"):
            q.enqueue(mk("boom", text="Grizzly Bears attacks for two."))
            pump.run_once()
        msg = caplog.text
        assert "engine exploded" in msg
        assert "Grizzly Bears attacks for two." in msg

    def test_pump_continues_after_failure(self):
        q = SpeechQueue()
        spk = FakeSpeaker(fail_next=2)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        q.enqueue(mk("a"))
        q.enqueue(mk("b"))
        q.enqueue(mk("c"))
        r1 = pump.run_once()
        r2 = pump.run_once()
        r3 = pump.run_once()
        assert (r1.ok, r2.ok, r3.ok) == (False, False, True)
        assert spk.delivered_texts == ["utterance c"]

    def test_speaker_raise_is_caught_and_logged(self, caplog):
        q = SpeechQueue()
        spk = FakeSpeaker(raise_on_uid="bad")
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        with caplog.at_level(logging.WARNING, logger="arenaonair.speech"):
            q.enqueue(mk("bad"))
            result = pump.run_once()
        assert result.ok is False
        assert "speaker raised" in result.reason
        assert caplog.text.count("WARNING") >= 1

    def test_zero_unlogged_drops_over_mixed_run(self, caplog):
        # Mixed deterministic + random failures; every non-ok result must
        # correspond to a WARNING record mentioning that uid.
        q = SpeechQueue()
        spk = FakeSpeaker(fail_rate=0.25, seed=99,
                          fail_uids={"doomed-7"})
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        with caplog.at_level(logging.WARNING, logger="arenaonair.speech"):
            failed_uids = []
            for i in range(200):
                uid = f"u{i}" if i != 7 else "doomed-7"
                q.enqueue(mk(uid, ts=float(i)))
                res = pump.run_once()
                if not res.ok:
                    failed_uids.append(res.uid)
            warned_uids = {
                r.getMessage().split("uid=")[1].split()[0]
                for r in caplog.records
                if r.levelno == logging.WARNING and "FAILED" in r.getMessage()
            }
            assert set(failed_uids) <= warned_uids
            assert len(warned_uids) == len(set(failed_uids))


# ---------------------------------------------------------------------------
# Preemption
# ---------------------------------------------------------------------------

class TestPreemption:
    def test_must_speak_cancels_lower_mid_speak(self):
        q = SpeechQueue()
        cancelled_seen = []
        spk = FakeSpeaker(
            speak_delay=0.15,
            on_cancel=lambda: cancelled_seen.append(True),
            supports_cancel=True,
        )
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")

        low = mk("low-detail", salience=SALIENCE_LOW)
        urgent = mk("game-over", salience=SALIENCE_MUST_SPEAK)

        # Start the low-salience delivery in a worker thread (mid-speak).
        t = threading.Thread(target=pump.deliver, args=(low,))
        t.start()
        time.sleep(0.03)  # let it enter speak()
        assert pump.current is low  # mid-speak confirmed

        result = pump.preempt_if_idle_with(urgent)
        t.join(timeout=2)

        assert result is not None and result.ok
        assert spk.cancel_requested == 1
        assert cancelled_seen == [True]
        assert "utterance game-over" in spk.delivered_texts

    def test_no_preemption_when_idle(self):
        q = SpeechQueue()
        spk = FakeSpeaker(supports_cancel=True)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        urgent = mk("urgent", salience=SALIENCE_MUST_SPEAK)
        pump.preempt_if_idle_with(urgent)
        assert spk.cancel_requested == 0
        assert "utterance urgent" in spk.delivered_texts

    def test_no_preemption_when_inflight_is_must_speak(self):
        q = SpeechQueue()
        spk = FakeSpeaker(speak_delay=0.15)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        first = mk("first-urgent", salience=SALIENCE_MUST_SPEAK)
        second = mk("second-urgent", salience=SALIENCE_MUST_SPEAK)
        t = threading.Thread(target=pump.deliver, args=(first,))
        t.start()
        time.sleep(0.03)
        pump.preempt_if_idle_with(second)
        t.join(timeout=2)
        # Equal (MUST_SPEAK) salience in flight: no cancel issued.
        assert spk.cancel_requested == 0

    def test_speaker_without_cancel_is_handled_gracefully(self):
        q = SpeechQueue()
        spk = FakeSpeaker(supports_cancel=False)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        urgent = mk("urgent", salience=SALIENCE_MUST_SPEAK)
        result = pump.preempt_if_idle_with(urgent)
        assert result.ok
        assert "utterance urgent" in spk.delivered_texts

    def test_preempted_low_utterance_marked_cancelled(self):
        q = SpeechQueue()
        spk = FakeSpeaker(speak_delay=0.15)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        low = mk("low", salience=SALIENCE_LOW)
        t = threading.Thread(target=pump.deliver, args=(low,))
        t.start()
        time.sleep(0.03)
        pump.preempt_if_idle_with(mk("hi", salience=SALIENCE_MUST_SPEAK))
        t.join(timeout=2)
        assert low in spk.cancelled_calls


# ---------------------------------------------------------------------------
# Engine chain dispatch
# ---------------------------------------------------------------------------

class TestChainDispatch:
    def test_linux_chain_selects_espeakng_when_kokoro_absent(self, monkeypatch):
        # This box: no kokoro package, no piper binary -> espeak-ng wins.
        import arenaonair.platform.tts as ttsmod

        monkeypatch.setattr(ttsmod.KokoroEngine, "available", lambda self: False)

        import arenaonair.platform.tts_linux as lin
        monkeypatch.setattr(lin.PiperEngine, "available", lambda self: False)
        monkeypatch.setattr(lin.EspeakNgEngine, "available", lambda self: True)

        speaker = build_speaker_chain(platform="linux")
        assert isinstance(speaker, EngineSpeaker)
        assert speaker.engine.name == "espeakng"

    def test_chain_order_config_override(self, monkeypatch):
        import arenaonair.platform.tts_linux as lin

        monkeypatch.setattr(lin.EspeakNgEngine, "available", lambda self: True)
        monkeypatch.setattr(lin.PiperEngine, "available", lambda self: True)
        speaker = build_speaker_chain(
            platform="linux",
            config={"chains": {"linux": ["piper", "espeakng"]}},
        )
        # piper listed first -> piper wins despite espeak also available.
        assert speaker.engine.name == "piper"

    def test_first_available_wins(self, monkeypatch):
        import arenaonair.platform.tts as ttsmod

        monkeypatch.setattr(ttsmod.KokoroEngine, "available", lambda self: True)
        speaker = build_speaker_chain(platform="linux")
        assert speaker.engine.name == "kokoro"

    def test_all_engines_unavailable_raises(self, monkeypatch):
        import arenaonair.platform.tts as ttsmod
        import arenaonair.platform.tts_linux as lin

        monkeypatch.setattr(ttsmod.KokoroEngine, "available", lambda self: False)
        monkeypatch.setattr(lin.PiperEngine, "available", lambda self: False)
        monkeypatch.setattr(lin.EspeakNgEngine, "available", lambda self: False)
        with pytest.raises(RuntimeError, match="no available TTS engine"):
            build_speaker_chain(platform="linux")

    def test_unknown_engine_name_raises_valueerror(self):
        from arenaonair.platform.tts import load_engine_class

        with pytest.raises(ValueError, match="unknown TTS engine"):
            load_engine_class("does-not-exist")

    def test_detect_platform_explicit_and_auto(self):
        assert detect_platform("linux") == "linux"
        auto = detect_platform(None)
        assert auto in {"windows", "darwin", "linux"}

    def test_default_chains_shape(self):
        from arenaonair.speech import DEFAULT_CHAINS

        assert DEFAULT_CHAINS["windows"] == ("kokoro", "sapi")
        assert DEFAULT_CHAINS["darwin"] == ("kokoro", "say")
        assert DEFAULT_CHAINS["linux"] == ("kokoro", "piper", "espeakng")


# ---------------------------------------------------------------------------
# DeliveryResult propagation + engine semantics
# ---------------------------------------------------------------------------

class TestDeliveryPropagation:
    def test_ok_result_propagates_uid(self):
        q = SpeechQueue()
        spk = FakeSpeaker()
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        q.enqueue(mk("prop"))
        res = pump.run_once()
        assert res.uid == "prop"
        assert res.ok is True

    def test_failure_reason_propagates_verbatim(self):
        want = DeliveryResult("prop-fail", False, "disk on fire")
        q = SpeechQueue()
        spk = FakeSpeaker(outcomes={"prop-fail": want})
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        q.enqueue(mk("prop-fail"))
        res = pump.run_once()
        assert res.ok is False
        assert res.reason == "disk on fire"

    def test_results_recorded_in_order(self):
        q = SpeechQueue()
        spk = FakeSpeaker(fail_next=1)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        for i in range(3):
            q.enqueue(mk(f"r{i}", ts=float(i)))
            pump.run_once()
        assert [r.uid for r in pump.results] == ["r0", "r1", "r2"]
        assert [r.ok for r in pump.results] == [False, True, True]

    def test_engine_speaker_synthesis_error_becomes_bad_result(self):
        from arenaonair.platform.tts import TTSEngine

        class Boom(TTSEngine):
            name = "boom"

            def available(self):
                return True

            def synthesize(self, text):
                raise OSError("no audio device")

        es = EngineSpeaker(Boom())
        res = es.speak(Utterance(
            uid="x", match_id="m", kind="k",
            text="hello", salience=1, ts_created=0.0,
        ))
        assert res.ok is False
        assert "no audio device" in res.reason

    def test_engine_speaker_rejects_non_engine(self):
        with pytest.raises(TypeError):
            EngineSpeaker(object())


# ---------------------------------------------------------------------------
# Thread safety smoke
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_enqueue_no_loss_no_dupe(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        n_threads, per_thread = 8, 25

        def worker(base):
            for i in range(per_thread):
                q.enqueue(mk(f"w{base}-{i}", ts=float(i)))

        threads = [threading.Thread(target=worker, args=(b,)) for b in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        drained = q.drain()
        uids = [u.uid for u in drained]
        assert len(uids) == n_threads * per_thread
        assert len(set(uids)) == len(uids)

    def test_pump_runs_in_own_thread(self):
        q = SpeechQueue()
        spk = FakeSpeaker(speak_delay=0.001)
        pump = SpeechPump(queue=q, speaker=spk)
        q.set_active_match("m1")
        worker = threading.Thread(
            target=pump.run_forever,
            kwargs={"poll_interval": 0.001},
            daemon=True,
        )
        worker.start()
        try:
            for i in range(20):
                q.enqueue(mk(f"bg{i}", ts=float(i)))
            deadline = time.time() + 5
            while len(spk.delivered_texts) < 20 and time.time() < deadline:
                time.sleep(0.01)
            assert len(spk.delivered_texts) == 20
        finally:
            pump.stop()
            worker.join(timeout=2)


# ---------------------------------------------------------------------------
# Real-engine probes on this box (hermetic; no audio asserted here)
# ---------------------------------------------------------------------------

class TestRealEnginesOnThisBox:
    def test_espeakng_probe_matches_shutil_which(self):
        import shutil

        from arenaonair.platform.tts_linux import EspeakNgEngine

        expect = shutil.which("espeak-ng") is not None
        assert EspeakNgEngine().available() == expect

    def test_kokoro_probe_is_bool_either_way(self):
        from arenaonair.platform.tts import KokoroEngine

        assert isinstance(KokoroEngine().available(), bool)

    def test_piper_unavailable_without_model_dir_env(self, monkeypatch):
        monkeypatch.delenv("PIPER_MODEL_DIR", raising=False)
        from arenaonair.platform.tts_linux import PiperEngine

        # Even if the binary existed, no model dir configured -> unavailable.
        assert PiperEngine().available() is False
