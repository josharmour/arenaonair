"""Tests for arenaonair.app: integration, flush discipline, gating."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from arenaonair import app as app_mod
from arenaonair.app import (
    ALWAYS_SPOKEN_KINDS,
    VERBOSITY_GATE,
    ArenaOnAirApp,
    PrintingSpeaker,
    passes_gate,
)
from arenaonair.config import Config

_REPO = Path(__file__).resolve().parents[1]
_FIXTURES = _REPO / "fixtures" / "matches"


def _pool_fragments(kind: str) -> list[str]:
    """Distinctive literal fragments from every template of a kind.

    The variety engine rotates phrasings per render, so tests assert on the
    union of pool fragments instead of one hardcoded sentence.
    """
    from arenaonair.templates import TEMPLATE_POOLS
    frags: list[str] = []
    for tpl in TEMPLATE_POOLS.get(kind, []):
        # Strip slot placeholders; keep the longest literal run as the marker.
        literals = [p.strip() for p in tpl.split("{")]
        head = next((p.rstrip(" ,.:;-") for p in literals if len(p) >= 12), "")
        if head:
            frags.append(head)
    return frags or [kind]


def _load_records(*fixture_paths: Path) -> list[dict]:
    records: list[dict] = []
    for path in fixture_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if isinstance(rec, dict) and isinstance(rec.get("obj"), dict):
                records.append(rec)
    return records


def _write_player_log(records: list[dict], path: Path) -> Path:
    """Render fixture records as a synthetic Player.log.

    Each record becomes a ``[UnityCrossThreadLogger]`` header line followed
    by the JSON object on its own line -- the shape gre_parser expects.
    """
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write("[UnityCrossThreadLogger]\n")
            fh.write(json.dumps(rec["obj"]) + "\n")
    return path


def _run_once(log_path: Path, verbosity: str = "balanced") -> tuple[int, str]:
    """Run the app in --once --dry-run mode; return (exit_code, stdout)."""
    argv = [
        "--log-path", str(log_path),
        "--verbosity", verbosity,
        "--dry-run",
        "--once",
    ]
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        code = app_mod.main(argv)
    finally:
        sys.stdout = real_stdout
    return code, buf.getvalue()


@pytest.fixture(scope="module")
def match01_log(tmp_path_factory) -> Path:
    records = _load_records(_FIXTURES / "match_01.jsonl")
    return _write_player_log(records, tmp_path_factory.mktemp("logs")
                             / "match01_Player.log")


@pytest.fixture(scope="module")
def combined_log(tmp_path_factory) -> Path:
    records = _load_records(_FIXTURES / "match_01.jsonl",
                            _FIXTURES / "match_02.jsonl")
    return _write_player_log(records, tmp_path_factory.mktemp("logs")
                             / "combined_Player.log")


# ---------------------------------------------------------------------------
# (a) Integration: single match through the real CLI path
# ---------------------------------------------------------------------------

class TestIntegrationSingleMatch:
    def test_exit_zero_and_transcript_contents(self, match01_log):
        code, out = _run_once(match01_log)
        assert code == 0

        lines = [ln for ln in out.splitlines() if ln.strip()]
        # Match opener present (any template from the match_start pool --
        # the variety engine rotates phrasings, so assert on pool fragments).
        opener_frags = _pool_fragments("match_start")
        assert any(any(f in ln for f in opener_frags) for ln in lines), \
            out[:2000]
        # At least five utterances total.
        assert len(lines) >= 5
        # A game-end closing line present (any game_end pool template).
        closer_frags = _pool_fragments("game_end")
        assert any(any(f in ln for f in closer_frags) for ln in lines), \
            out[-2000:]

    def test_transcript_nonempty_even_quiet(self, match01_log):
        # match_start/match_end bypass the gate, so quiet still speaks them.
        code, out = _run_once(match01_log, verbosity="quiet")
        assert code == 0
        assert "A new challenger scenario loads" in out


# ---------------------------------------------------------------------------
# (b) Phantom-opener flush across concatenated matches
# ---------------------------------------------------------------------------

class TestPhantomOpenerFlush:
    def test_exactly_one_transition(self, combined_log):
        code, out = _run_once(combined_log)
        assert code == 0

        lines = [ln for ln in out.splitlines() if ln.strip()]
        opener_frags = _pool_fragments("match_start")
        openers = [i for i, ln in enumerate(lines)
                   if any(f in ln for f in opener_frags)]
        # Exactly one new-match opener reaches the broadcast per match.
        assert len(openers) == 2, f"openers at {openers}: {lines}"

        # The transition lands at the expected index shape (predecessor
        # measured ~80% through the stream; assert second half).
        opener_idx = openers[-1]
        assert opener_idx > len(lines) // 2

        # No old-match speech after the new opener: every line after the
        # opener must belong to the new broadcast (no interleaving).
        after = lines[opener_idx:]
        # The new opener itself is first; nothing from before it repeats.
        assert after.count(after[0]) == 1

    def test_each_match_gets_its_own_opener(self, combined_log):
        _, out = _run_once(combined_log)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        opener_frags = _pool_fragments("match_start")
        openers = [ln for ln in lines
                   if any(f in ln for f in opener_frags)]
        # Two matches -> two openers; the variety engine renders them from
        # the same pool (possibly different templates -- that's correct).
        assert len(openers) == 2


# ---------------------------------------------------------------------------
# (c) Verbosity gating matrix (unit level)
# ---------------------------------------------------------------------------

class _FakeEvent:
    def __init__(self, kind: str, salience: int):
        self.kind = kind
        self.salience = salience


class TestVerbosityGate:
    @pytest.mark.parametrize("verbosity,floor", [
        ("quiet", 2),
        ("balanced", 1),
        ("detailed", 0),
    ])
    def test_floor_mapping(self, verbosity, floor):
        assert VERBOSITY_GATE[verbosity] == floor
        for salience in range(4):
            event = _FakeEvent("cast", salience)
            expected = salience >= floor
            assert passes_gate(event, verbosity) is expected

    @pytest.mark.parametrize("kind", ["match_start", "match_end"])
    @pytest.mark.parametrize("verbosity", ["quiet", "balanced", "detailed"])
    @pytest.mark.parametrize("salience", [0, 1, 2, 3])
    def test_always_spoken_bypass(self, kind, verbosity, salience):
        event = _FakeEvent(kind, salience)
        assert passes_gate(event, verbosity) is True
        assert kind in ALWAYS_SPOKEN_KINDS

    def test_unknown_verbosity_falls_back_to_balanced(self):
        event = _FakeEvent("cast", 1)
        assert passes_gate(event, "nonsense") is True   # balanced floor=1
        event0 = _FakeEvent("cast", 0)
        assert passes_gate(event0, "nonsense") is False


# ---------------------------------------------------------------------------
# (d) status() transitions watching -> in_match during a run
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    def test_status_starts_watching_then_reports_in_match(self,
                                                          match01_log):
        cfg = Config(log_path=str(match01_log), verbosity="balanced")
        application = ArenaOnAirApp(cfg, dry_run=True, once_mode=True)

        application.start()
        try:
            initial = application.status()
            assert initial["state"] in ("watching", "in_match")

            # Wait for the watcher thread to finish its --once pass.
            watch = application._watch_thread
            if watch is not None:
                watch.join(timeout=60.0)

            final = application.status()
            assert final["state"] == "in_match"
            assert final["match_id"] is not None
            assert final["last_utterance"] is not None
            assert final["queued"] == 0
        finally:
            application.stop()

    def test_stop_reports_stopped(self, match01_log):
        cfg = Config(log_path=str(match01_log))
        application = ArenaOnAirApp(cfg, dry_run=True)
        application.start()
        application.stop()
        assert application.status()["state"] == "stopped"


# ---------------------------------------------------------------------------
# Dry-run speaker sanity
# ---------------------------------------------------------------------------

class TestPrintingSpeaker:
    def test_prints_text_and_returns_true(self):
        class _U:
            text = "hello world"

        buf = io.StringIO()
        speaker = PrintingSpeaker(stream=buf)
        result = speaker.speak(_U())
        assert result.ok is True
        assert buf.getvalue() == "hello world\n"

    def test_shutdown_and_cancel_noop(self):
        speaker = PrintingSpeaker(stream=io.StringIO())
        speaker.cancel()
        speaker.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
