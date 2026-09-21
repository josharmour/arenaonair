"""Tests for tools/replay_harness.py -- QR6 drone scoring + determinism.

Runs the harness programmatically (imports, not subprocesses) over the
recorded fixtures and asserts the DESIGN section 6 contracts:

* contract 7 / QR6: drone_score == 0 and window_repeat_score <= 1
* contract 6: replaying twice yields an identical utterance list
* coverage sanity: enough utterances rendered to exercise the pipeline
Plus a synthetic bad-transcript unit test proving the drone scorer detects
consecutive same-shape duplicates.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS_DIR = _REPO_ROOT / "tools"
for _p in (str(_TOOLS_DIR), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import replay_harness as rh  # noqa: E402

FIXTURES = _REPO_ROOT / "fixtures" / "matches"


# ---------------------------------------------------------------------------
# Simple utterance stand-in for scorer unit tests
# ---------------------------------------------------------------------------

class FakeUtt:
    """Minimal utterance duck-type: only .text is consulted by the scorers."""

    def __init__(self, text):
        self.text = text


# ---------------------------------------------------------------------------
# Synthetic scorer unit tests (bad transcript detection)
# ---------------------------------------------------------------------------

class TestDroneScorerSynthetic:
    """Feed fake texts; prove consecutive same-shape dupes are detected."""

    def test_consecutive_dupes_detected(self):
        # Same first-two-words + same length bucket -> same shape signature.
        bad = [FakeUtt("He casts the spell quickly now."),
               FakeUtt("He casts the other spell right away.")]
        assert rh.drone_score(bad) > 0

    def test_identical_texts_are_drone(self):
        bad = [FakeUtt("Exactly the same line."),
               FakeUtt("Exactly the same line.")]
        assert rh.drone_score(bad) == 1

    def test_varied_shapes_score_zero(self):
        good = [FakeUtt("We're underway -- standard format, Alice versus Bob."),
                FakeUtt("Combat! Bodies are heading across the line."),
                FakeUtt("Ouch -- that stings for Bob."),
                FakeUtt("Alice is up to four lands now.")]
        assert rh.drone_score(good) == 0

    def test_empty_transcript_scores_zero(self):
        assert rh.drone_score([]) == 0

    def test_non_adjacent_same_shape_not_counted(self):
        # Same shape separated by a different-shaped utterance: not adjacent.
        utts = [FakeUtt("He casts the spell quickly now."),
                FakeUtt("Combat! Everyone attacks."),
                FakeUtt("He casts the other spell right away.")]
        assert rh.drone_score(utts) == 0


class TestWindowRepeatScorerSynthetic:
    def test_single_repeat_within_window_detected(self):
        # Duplicate at positions 0 and 7: both inside the first 8-slice.
        texts = (["Alpha line here."] + ["Filler %d." % i for i in range(6)]
                 + ["Alpha line here."])
        utts = [FakeUtt(t) for t in texts]
        assert rh.window_repeat_score(utts) == 1

    def test_double_repeat_within_window_detected(self):
        # One text three times inside a single window -> two repeats;
        # every other text unique so it does not dominate the max.
        texts = ["Thrice told tale."] * 3 \
            + ["A line.", "B line.", "C line.", "D line.", "E line."]
        utts = [FakeUtt(t) for t in texts]
        assert rh.window_repeat_score(utts) == 2

    def test_triple_occurrence_scores_two_repeats(self):
        # Exactly 8 utterances, one text three times -> repeats == 2.
        texts = ["Dup."] * 3 + ["A.", "B.", "C.", "D.", "E."]
        utts = [FakeUtt(t) for t in texts]
        assert rh.window_repeat_score(utts) == 2

    def test_far_apart_repeats_not_counted(self):
        # Distance 11 > window 8: no slice contains both Echo occurrences.
        texts = ["Echo."] + ["Filler %d." % i for i in range(10)] + ["Echo."]
        utts = [FakeUtt(t) for t in texts]
        assert rh.window_repeat_score(utts) == 0

    def test_unique_transcript_scores_zero(self):
        texts = ["Line %d." % i for i in range(12)]
        utts = [FakeUtt(t) for t in texts]
        assert rh.window_repeat_score(utts) == 0


# ---------------------------------------------------------------------------
# Fixture integration tests (programmatic harness runs)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name", ["match_01.jsonl", "match_03.jsonl"])
class TestFixtureReplay:
    """Full-pipeline replay of recorded fixtures through the harness."""

    def _run(self, fixture_name, determinism=False):
        report, passed = rh.run_replay(
            FIXTURES / fixture_name, determinism=determinism)
        return report, passed

    def test_drone_score_is_zero(self, fixture_name):
        report, _passed = self._run(fixture_name)
        assert report["drone_score"] == rh.DRONE_PASS_THRESHOLD

    def test_window_repeat_within_bound(self, fixture_name):
        report, _passed = self._run(fixture_name)
        assert report["window_repeat_score"] <= \
            rh.WINDOW_REPEAT_PASS_THRESHOLD

    def test_enough_utterances_rendered(self, fixture_name):
        report, _passed = self._run(fixture_name)
        assert report["total_utterances"] >= 30

    def test_pipeline_actually_processed_messages(self, fixture_name):
        report, _passed = self._run(fixture_name)
        assert report["messages"] > 0
        assert report["snapshots"] > 0
        assert report["total_events"] > 0

    def test_coverage_bookkeeping_adds_up(self, fixture_name):
        report, _passed = self._run(fixture_name)
        rendered_total = sum(c["rendered"]
                             for c in report["coverage_by_kind"].values())
        suppressed_total = sum(c["suppressed"]
                               for c in report["coverage_by_kind"].values())
        assert rendered_total == report["total_utterances"]
        assert rendered_total + suppressed_total == report["total_events"]

    def test_overall_verdict_passes(self, fixture_name):
        _report, passed = self._run(fixture_name)
        assert passed is True


class TestDeterminism:
    """Contract 6: identical replay -> identical utterance list."""

    @pytest.mark.parametrize("fixture_name", ["match_01.jsonl",
                                              "match_03.jsonl"])
    def test_replay_twice_identical(self, fixture_name):
        report, passed = rh.run_replay(FIXTURES / fixture_name,
                                       determinism=True)
        assert report.get("deterministic") is True
        assert passed is True

    def test_two_fresh_runs_produce_equal_lists(self):
        records = rh.load_records(FIXTURES / "match_01.jsonl")
        resolver = rh.make_name_resolver()
        first = rh.replay_fixture(records, name_resolver=resolver)
        second = rh.replay_fixture(records, name_resolver=resolver)
        t1 = rh._utterance_tuples(first["utterances"])
        t2 = rh._utterance_tuples(second["utterances"])
        assert t1 == t2
        assert len(t1) >= 30


class TestReportShape:
    def test_json_serializable_and_keyed(self):
        import json
        report, _passed = rh.run_replay(FIXTURES / "match_02.jsonl")
        expected_keys = {
            "drone_score", "drone_pass", "window_repeat_score",
            "window_repeat_pass", "coverage_by_kind", "total_events",
            "total_utterances", "snapshots", "messages", "events_per_sec",
            "elapsed_s", "fixture", "utterances",
        }
        assert expected_keys <= set(report)
        encoded = json.dumps(report)          # must not raise
        assert isinstance(encoded, str)

    def test_load_records_skips_bad_lines(self, tmp_path):
        good1 = '{"kind": "x", "obj": {"a": 1}}'
        good2 = '{"kind": "y", "obj": {"b": 2}}'
        bad_lines = ["not json at all", '{"no_obj_key": true}', "", "   ",
                     "[1, 2, 3]"]
        fixture = tmp_path / "mixed.jsonl"
        fixture.write_text(
            "\n".join([good1] + bad_lines + [good2]) + "\n",
            encoding="utf-8")
        records = rh.load_records(fixture)
        assert len(records) == 2
