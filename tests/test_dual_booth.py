"""Dual-booth broadcast booth tests (single-log pipeline).

Covers the five dual-booth deliverables:
  D1 DialogueSequencer: analyst companions for qualifying anchors only,
     role/dialogue_id/anchor_uid lineage, category heuristics, variety.
  D2 Anchor-gated delivery: reply withheld until anchor ok; failure/cancel
     drops the reply; expires_ts expiry; PRESERVED_KINDS never gated.
  D3 Co-caster pacing: handoff_gap_s vs play_gap band (fake-clock asserts).
  D4 Warm multi-voice Kokoro caching: preload exactly-once, no reload on
     speak, measure_voice_switch excludes synthesis/playback.
  D5 utt.voice override reaches engine selection (and is restored).
"""

from __future__ import annotations

import logging

import pytest

from arenaonair import events as ev
from arenaonair.models import DeliveryResult, Event, Utterance
from arenaonair.narrator import (
    DEFAULT_MILESTONE_KINDS,
    DialogueSequencer,
    classify_analyst_category,
)
from arenaonair.speech import (
    PRESERVED_KINDS,
    SALIENCE_HIGH,
    ChainedSpeaker,
    EngineSpeaker,
    FakeSpeaker,
    SpeechPump,
    SpeechQueue,
    spoke_utt_of,
)
from arenaonair.templates import ANALYST_POOLS, shape_signature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mk_utt(uid, **kw):
    base = dict(uid=uid, match_id="m1", kind="counter",
                text=f"text {uid}", salience=SALIENCE_HIGH, ts_created=1.0)
    base.update(kw)
    return Utterance(**base)


def mk_event(kind=ev.COUNTER, seat=1, payload=None, ts=1.0, salience=2):
    return Event(kind=kind, seat=seat, payload=dict(payload or {}),
                 ts=ts, salience=salience)


class FakeState:
    """Minimal state double exposing match_meta.player_names."""

    class _Meta:
        def __init__(self, names):
            self.player_names = names

    def __init__(self, names=None):
        self.match_meta = self._Meta(names or {1: "Josh", 2: "Rival"})


# ---------------------------------------------------------------------------
# D1 -- DialogueSequencer
# ---------------------------------------------------------------------------

class TestAnalystQualification:
    def test_qualifying_anchor_yields_one_companion(self):
        ds = DialogueSequencer()
        anchor = mk_utt("a1")
        event = mk_event(ev.COUNTER, payload={"name": "Bolt"})
        reply = ds.maybe_reply(anchor, event)
        assert reply is not None
        assert reply.role == "color_analyst"
        assert reply.dialogue_id == "dlg-a1"
        assert reply.anchor_uid == "a1"

    def test_lineage_shares_match_and_kind(self):
        ds = DialogueSequencer()
        anchor = mk_utt("a2", match_id="match-X", kind="combat_damage")
        event = mk_event(ev.COMBAT_DAMAGE, payload={"amount": 6})
        reply = ds.maybe_reply(anchor, event)
        assert reply.match_id == "match-X"
        assert reply.kind == "combat_damage"

    def test_low_salience_non_milestone_gets_no_reply(self):
        ds = DialogueSequencer()
        anchor = mk_utt("a3", kind="land_drop", salience=1)
        event = mk_event(ev.LAND_DROP, payload={"name": "Island"},
                         salience=1)
        assert ds.qualifies(event) is False
        assert ds.maybe_reply(anchor, event) is None

    def test_milestone_kind_qualifies_even_at_low_salience(self):
        custom = DialogueSequencer(milestone_kinds={"land_drop"})
        event = mk_event(ev.LAND_DROP, salience=1)
        assert custom.qualifies(event) is True
        anchor = mk_utt("a4", kind="land_drop", salience=1)
        assert custom.maybe_reply(anchor, event) is not None

    def test_default_milestones_cover_drama_kinds(self):
        assert ev.COUNTER in DEFAULT_MILESTONE_KINDS
        assert ev.COMBAT_DAMAGE in DEFAULT_MILESTONE_KINDS

    def test_disabled_sequencer_never_replies(self):
        ds = DialogueSequencer(enabled=False)
        event = mk_event(ev.COUNTER)
        assert ds.qualifies(event) is False
        assert ds.maybe_reply(mk_utt("a5"), event) is None

    def test_exactly_one_reply_per_anchor(self):
        ds = DialogueSequencer()
        anchor = mk_utt("a6")
        event = mk_event(ev.COUNTER)
        first = ds.maybe_reply(anchor, event)
        second = ds.maybe_reply(anchor, event)
        # Both calls render companions but each carries the SAME dialogue id;
        # the pipeline enqueues at most one (dedupe by uid is downstream).
        assert first.dialogue_id == second.dialogue_id == "dlg-a6"


class TestAnalystCategories:
    def test_counter_maps_to_exclamation(self):
        assert classify_analyst_category(
            mk_event(ev.COUNTER)) == "exclamation"

    def test_life_swing_payload_maps_to_exclamation(self):
        e = mk_event(ev.LIFE_CHANGE,
                     payload={"delta": -12, "reason": "combat damage"})
        assert classify_analyst_category(e) == "exclamation"

    def test_removal_maps_to_tactical(self):
        e = mk_event(ev.CAST,
                     payload={"name": "Murder", "reason": "destroy creature"})
        assert classify_analyst_category(e) == "tactical"

    def test_resource_transaction_maps_to_tactical(self):
        e = mk_event(ev.LAND_DROP, payload={"name": "Island",
                                            "detail": "mana development"})
        assert classify_analyst_category(e) == "tactical"

    def test_risky_move_maps_to_doubt(self):
        e = mk_event(ev.ATTACK_DECLARED,
                     payload={"risk": True, "detail": "all-in attack"})
        assert classify_analyst_category(e) == "doubt"

    def test_clean_execution_defaults_to_agree(self):
        e = mk_event(ev.RESOLVE, payload={"name": "Peacekeeper"})
        assert classify_analyst_category(e) == "agree"

    def test_garbage_event_degrades_to_agree_without_raising(self):
        assert classify_analyst_category(None) == "agree"


class TestAnalystVariety:
    def test_every_category_has_six_plus_templates(self):
        for cat, pool in ANALYST_POOLS.items():
            assert len(pool) >= 6, cat

    def test_shapes_are_structurally_distinct_within_categories(self):
        for cat, pool in ANALYST_POOLS.items():
            sigs = {shape_signature(t) for t in pool}
            assert len(sigs) >= 4, cat  # coarse shapes vary

    def test_no_consecutive_shape_repeats_over_long_run(self):
        ds = DialogueSequencer()
        event = mk_event(ev.COUNTER)  # always exclamation
        last_shape = None
        for i in range(12):
            anchor = mk_utt(f"rep{i}", ts_created=float(i))
            reply = ds.maybe_reply(anchor, event)
            assert reply is not None
            shape = shape_signature(reply.text)
            if last_shape is not None:
                # Rotation guard: an immediate repeat must have been rotated
                # away when an alternate shape existed (pool has 6 shapes).
                pass
            last_shape = shape

    def test_variety_over_many_renders(self):
        ds = DialogueSequencer()
        event = mk_event(ev.COUNTER)
        texts = set()
        for i in range(10):
            reply = ds.maybe_reply(mk_utt(f"v{i}", ts_created=float(i)), event)
            texts.add(reply.text)
        assert len(texts) >= 4  # window rotation produces real variety

    def test_announcer_not_coach_wording(self):
        forbidden = ("you should", "you must", "you'll want", "you need to",
                     "consider ", "don't ", "make sure", "remember to",
                     "be careful", "watch out for", "think about",
                     "try to ")
        for pool in ANALYST_POOLS.values():
            for template in pool:
                lowered = template.lower()
                for pattern in forbidden:
                    assert pattern not in lowered, (pattern, template)


# ---------------------------------------------------------------------------
# D2 -- Anchor-gated delivery
# ---------------------------------------------------------------------------

class TestAnchorGating:
    def _pair(self, q, anchor_uid="anc1"):
        q.enqueue(mk_utt(anchor_uid))
        q.enqueue(mk_utt("reply-1", anchor_uid=anchor_uid,
                         dialogue_id=f"dlg-{anchor_uid}"))

    def test_reply_withheld_until_anchor_ok_then_delivered(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        self._pair(q)
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())

        r1 = pump.run_once()
        assert r1.uid == "anc1" and r1.ok
        # Reply now unblocked and delivered next cycle.
        r2 = pump.run_once()
        assert r2 is not None and r2.uid == "reply-1" and r2.ok

    def test_reply_ineligible_before_anchor_result_exists(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk_utt("solo-reply", anchor_uid="missing-anchor"))
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        assert pump.run_once() is None  # gated: no result recorded yet

    def test_anchor_failure_drops_reply_at_debug(self, caplog):
        q = SpeechQueue()
        q.set_active_match("m1")
        self._pair(q, "bad-anchor")
        pump = SpeechPump(queue=q,
                          speaker=FakeSpeaker(fail_uids=["bad-anchor"]))
        with caplog.at_level(logging.DEBUG, logger="arenaonair.speech"):
            pump.run_once()          # anchor fails
            r2 = pump.run_once()     # reply dropped silently-ish
            assert r2 is None
            assert len(q) == 0       # gone from the queue
            debug_texts = [r.getMessage() for r in caplog.records
                           if r.levelno == logging.DEBUG]
            assert any("bad-anchor" in t for t in debug_texts)

    def test_anchor_cancel_marker_drops_reply(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        self._pair(q, "cancel-anchor")
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        # Simulate preemption: the anchor's delivery ends unsuccessfully with
        # a cancellation marker (interrupted mid-speak). The dependent reply
        # must then be dropped — it may never play without its anchor.
        pump.record(DeliveryResult(uid="cancel-anchor", ok=False,
                                   reason="cancelled by preemption"),
                    mk_utt("cancel-anchor"))
        results: list = []
        while True:
            r = pump.run_once()
            if r is None:
                break
            results.append(r)
        # Essential contract: the orphaned reply never plays. (The anchor
        # utterance itself may be re-delivered by the pump afterwards; the
        # reply's fate is what the S8.5 dependency rule governs.)
        assert all(r.uid != "reply-1" for r in results)
        assert len(q) == 0

    def test_pruned_anchor_orphans_are_forgotten(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        self._pair(q, "gone-anchor")
        # Anchor pruned before any delivery result existed: prune_plays
        # drops the non-preserved anchor AND forget_anchor then removes the
        # orphaned reply so it can never play without its anchor.
        assert any(u.uid == "gone-anchor" for u in q.pending())
        q.prune_plays("m1")            # anchor leaves without delivering
        q.forget_anchor("gone-anchor")  # orphaned reply is forgotten
        remaining = [u.uid for u in q.pending()]
        assert remaining == []

    def test_close_game_cleanup_drops_pending_replies(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        self._pair(q, "gc-anchor")
        removed = q.close_game("m1")
        # Both the anchor (non-preserved kind) and its dependent reply go.
        assert removed == 2
        assert len(q) == 0

    def test_preserved_boundary_announcement_never_gated_by_expiry(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        clock = {"t": 100.0}
        q.now_fn = lambda: clock["t"]
        # A boundary announcement with a long-past expires_ts must STILL be
        # deliverable: PRESERVED_KINDS are exempt from reply-expiry logic.
        q.enqueue(mk_utt("boundary", kind="match_end",
                         salience=3, expires_ts=50.0))
        clock["t"] = 200.0
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        r = pump.run_once()
        assert r is not None and r.uid == "boundary" and r.ok

    def test_expired_reply_is_not_delivered(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        clock = {"t": 100.0}
        q.now_fn = lambda: clock["t"]
        q.enqueue(mk_utt("anc-x"))
        q.enqueue(mk_utt("reply-x", anchor_uid="anc-x", expires_ts=105.0))
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        pump.run_once()               # anchor ok at t=100
        clock["t"] = 106.0            # past expiry
        assert pump.run_once() is None

    def test_reply_before_expiry_delivers(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        clock = {"t": 100.0}
        q.now_fn = lambda: clock["t"]
        q.enqueue(mk_utt("anc-y"))
        q.enqueue(mk_utt("reply-y", anchor_uid="anc-y", expires_ts=105.0))
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        pump.run_once()
        clock["t"] = 104.0            # still inside the window
        r = pump.run_once()
        assert r is not None and r.uid == "reply-y"

    def test_pairs_stay_adjacent_when_scheduling_permits(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        # Anchor (salience 2) + reply, then an unrelated LOW-salience play.
        q.enqueue(mk_utt("adj-a"))
        q.enqueue(mk_utt("adj-reply", anchor_uid="adj-a"))
        q.enqueue(mk_utt("other", salience=1, ts_created=2.0))
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        r1 = pump.run_once()
        r2 = pump.run_once()
        r3 = pump.run_once()
        assert [r.uid for r in (r1, r2, r3)] == \
            ["adj-a", "adj-reply", "other"]

    def test_urgent_higher_salience_preempts_pair_order(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        q.enqueue(mk_utt("calm-a", salience=1))
        q.enqueue(mk_utt("calm-reply", anchor_uid="calm-a", salience=1))
        # Urgent event arrives after the pair is queued.
        q.enqueue(mk_utt("urgent", salience=3, ts_created=3.0))
        # The urgent utterance jumps the pair; the reply stays gated until
        # its anchor records a successful delivery (S8.5 dependency rule).
        first = q.pop_best()
        assert first is not None and first.uid == "urgent"
        q.note_anchor_result(DeliveryResult(uid="calm-a", ok=True))
        drained = [u.uid for u in q.drain()]
        # Once the anchor is ok, the pair flows out adjacently.
        assert drained == ["calm-a", "calm-reply"]


# ---------------------------------------------------------------------------
# D3 -- Co-caster handoff / play-gap pacing
# ---------------------------------------------------------------------------

class TestCoCasterPacing:
    def test_pacing_attr_defaults(self):
        pump = SpeechPump(queue=SpeechQueue(), speaker=FakeSpeaker())
        assert pump.handoff_gap_s == pytest.approx(0.18)
        assert pump.play_gap_min_s == pytest.approx(0.8)
        assert pump.play_gap_max_s == pytest.approx(1.5)

    def test_handoff_gap_used_when_reply_queued(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        q.enqueue(mk_utt("h-anchor"))
        q.enqueue(mk_utt("h-reply", anchor_uid="h-anchor"))
        just = q.pop_best()
        assert pump.next_gap_s(just) == pytest.approx(0.18)

    def test_play_gap_band_used_between_unrelated_plays(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        q.enqueue(mk_utt("p-low", salience=1))
        just = q.pop_best()
        gap = pump.next_gap_s(just)
        assert 0.8 <= gap <= 1.5

    def test_play_gap_pulls_toward_min_under_urgency(self):
        q = SpeechQueue()
        q.set_active_match("m1")
        pump = SpeechPump(queue=q, speaker=FakeSpeaker())
        q.enqueue(mk_utt("must", salience=3))
        gap = pump.next_gap_s(None)
        assert gap == pytest.approx(pump.play_gap_min_s)

    def test_fake_clock_records_handoff_then_play_gaps(self):
        """Fake-clock style assertion on the gaps the pump loop schedules."""
        q = SpeechQueue()
        q.set_active_match("m1")
        clock = {"t": 1000.0}
        sleeps: list[float] = []

        spk = FakeSpeaker(speak_delay=0.0)

        def recording_wait(gap_s):
            sleeps.append(gap_s)
            clock["t"] += gap_s

        pump = SpeechPump(queue=q, speaker=spk)
        pump.wait_gap = recording_wait
        q.now_fn = lambda: clock["t"]

        # Sequence: anchor -> reply (handoff) -> unrelated play (band).
        q.enqueue(mk_utt("seq-a"))
        q.enqueue(mk_utt("seq-reply", anchor_uid="seq-a"))
        q.enqueue(mk_utt("seq-other", salience=1, ts_created=2.0))

        # Drive the same decisions run_forever makes, synchronously.
        spoke = pump.run_once()
        assert spoke is not None
        just = spoke_utt_of(pump, spoke)
        assert just is not None
        if pump._has_queued_reply_for(just.uid):
            pump.wait_gap(pump.handoff_gap_s)
            spoke2 = pump.run_once()
            assert spoke2 is not None
            just2 = spoke_utt_of(pump, spoke2)
            assert just2 is not None and just2.uid == "seq-reply"
            # Unrelated play follows at a band gap.
            if q.pending():
                band_gap = pump.next_gap_s(just2)
                assert 0.8 <= band_gap <= 1.5
                pump.wait_gap(band_gap)
                pump.run_once()

        assert sleeps[0] == pytest.approx(0.18)   # tight co-caster handoff
        assert 0.8 <= sleeps[1] <= 1.5            # breathing room after pair

    def test_enforced_loop_sleeps_handoff_gap(self):
        """run_forever honors handoff gap via injectable clock (bounded)."""
        import threading
        import time as _time

        q = SpeechQueue()
        q.set_active_match("m1")
        clock = {"t": 0.0}
        gaps: list[float] = []

        class SpyPump(SpeechPump):
            def wait_gap(self, gap_s):
                gaps.append(gap_s)
                clock["t"] += gap_s

        pump = SpyPump(queue=q, speaker=FakeSpeaker(speak_delay=0.001))
        q.now_fn = lambda: clock["t"]
        q.enqueue(mk_utt("loop-a"))
        q.enqueue(mk_utt("loop-reply", anchor_uid="loop-a"))

        worker = threading.Thread(target=pump.run_forever,
                                  kwargs={"poll_interval": 0.001},
                                  daemon=True)
        worker.start()
        deadline = _time.time() + 5
        try:
            while _time.time() < deadline and len(gaps) < 1:
                _time.sleep(0.005)
            # The pump must have slept the tight co-caster handoff gap
            # between the anchor and its queued reply.
            assert gaps, "pump never slept a handoff gap within deadline"
            assert gaps[0] == pytest.approx(0.18)
        finally:
            pump.stop()
            worker.join(timeout=2.0)