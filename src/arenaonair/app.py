"""ArenaOnAir application wiring: log tail -> pipeline -> speech threads.

Two threads per DESIGN section 4:

* watcher thread -- tails the Player.log via LogWatcher, parses each line
  into GreMessages, folds them into snapshots, diffs them into events plus
  story beats, renders utterances through the verbosity gate, and enqueues
  them on the SpeechQueue.
* speech thread -- drains the queue through the speaker chain forever
  (SpeechPump.run_forever).

Per-message pipeline ordering (critical): each raw GreMessage joins the
msgs_since backlog BEFORE builder.apply runs -- game_start/game_end ride
inside the very message that publishes their snapshot, so feeding only
skipped-message backlogs would miss them entirely.

Match-transition discipline ("phantom opener" flush): when a freshly
published snapshot carries a NEW match_meta.match_id, the outgoing match's
queue is flushed BEFORE any new-match utterance is rendered or enqueued.
End-of-game discipline: a game_end/match_end closing line is enqueued,
awaited through delivery confirmation, and only then is its match sealed
via queue.flush.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from . import config as config_mod
from .differ import EventDiffer
from .gre_parser import parse_line_all
from .models import DeliveryResult
from .narrator import Narrator
from .speech import SpeechPump, SpeechQueue, build_speaker_chain
from .state_builder import GameStateBuilder
from .story import StoryModel
from .watcher import LogWatcher

logger = logging.getLogger(__name__)


class AppState:
    """Lifecycle states reported by ArenaOnAirApp.status()."""

    STARTING = "starting"
    WATCHING = "watching"
    IN_MATCH = "in_match"
    STOPPING = "stopping"
    STOPPED = "stopped"


#: Verbosity name -> minimum event salience cleared to speak.
VERBOSITY_GATE: dict[str, int] = {
    "quiet": 2,
    "balanced": 1,
    "detailed": 0,
}

#: Event kinds that bypass the verbosity gate entirely.
ALWAYS_SPOKEN_KINDS = frozenset({"match_start", "match_end"})

#: Consecutive idle polls before --once declares the stream done.
IDLE_POLLS_BEFORE_DONE = 2

#: Bound on waiting for the queue to drain between poll batches.
_DRAIN_WAIT_SECONDS = 10.0
_DRAIN_POLL_SLEEP = 0.002


def passes_gate(event, verbosity: str) -> bool:
    """True when ``event`` clears the verbosity gate (or bypasses it)."""
    kind = getattr(event, "kind", "")
    if kind in ALWAYS_SPOKEN_KINDS:
        return True
    floor = VERBOSITY_GATE.get(verbosity, VERBOSITY_GATE["balanced"])
    try:
        salience = int(getattr(event, "salience", 0))
    except (TypeError, ValueError):
        salience = 0
    return salience >= floor


class PrintingSpeaker:
    """--dry-run stand-in for the real speaker chain: prints to stdout."""

    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdout

    def speak(self, utterance) -> "DeliveryResult":
        uid = getattr(utterance, "uid", "") or ""
        try:
            print(utterance.text, file=self.stream)
            self.stream.flush()
        except Exception as exc:
            return DeliveryResult(uid=uid, ok=False,
                                  reason=f"dry-run print failed: {exc}")
        return DeliveryResult(uid=uid, ok=True)

    def cancel(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class ArenaOnAirApp:
    """Owns the two runtime threads and all pipeline state between them."""

    def __init__(self, config=None, *, dry_run=False, once_mode=False):
        self.config = config or config_mod.load()
        self.dry_run = bool(dry_run)
        self.once_mode = bool(once_mode)

        self.builder = GameStateBuilder()
        self.differ = EventDiffer()
        self.story = StoryModel(
            thresholds=self.config.story_thresholds or None)
        self.narrator = Narrator(window=self.config.window)
        self.queue = SpeechQueue()

        self.speaker = None          # built in start()
        self.pump = None
        self.watcher = None

        self._watch_thread = None
        self._speech_thread = None
        self._stop_event = threading.Event()

        self._state_lock = threading.Lock()
        self._state = AppState.STARTING
        self._match_id = None
        self._last_utterance = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn watcher + speech threads; non-blocking."""
        with self._state_lock:
            if self._watch_thread is not None \
                    and self._watch_thread.is_alive():
                return
            self._stop_event.clear()
            self._state = AppState.WATCHING

        if self.pump is None:
            if self.dry_run:
                speaker = PrintingSpeaker()
            else:
                chains_cfg = ({"chains": self.config.tts_chains}
                              if self.config.tts_chains else None)
                speaker = build_speaker_chain(
                    platform=self.config.tts_platform,
                    config=chains_cfg,
                )
            self.speaker = speaker
            self.pump = SpeechPump(queue=self.queue, speaker=speaker)

        self.watcher = LogWatcher(
            path=self.config.log_path,
            anchor=self.config.anchor,
        )

        self._speech_thread = threading.Thread(
            target=self._speech_loop,
            name="arenaonair-speech",
            daemon=True,
        )
        self._speech_thread.start()

        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            name="arenaonair-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def stop(self) -> None:
        """Signal both threads to wind down; join them; shut the speaker."""
        if self.pump is not None:
            self.pump.stop()
        self._stop_event.set()

        for thread in (self._watch_thread, self._speech_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)

        if self.speaker is not None:
            try:
                self.speaker.shutdown()
            except Exception:
                logger.debug("speaker shutdown raised", exc_info=True)

        with self._state_lock:
            self._state = AppState.STOPPED

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def _speech_loop(self) -> None:
        pump = self.pump
        if pump is None:  # pragma: no cover - defensive
            return
        interval = min(0.05, max(0.005, float(self.config.poll_interval)))
        try:
            pump.run_forever(poll_interval=interval)
        except Exception:  # pragma: no cover - defensive
            logger.exception("speech thread crashed")

    def _watch_loop(self) -> None:
        watcher = self.watcher
        if watcher is None:  # pragma: no cover - defensive
            return

        prev_snap = None
        backlog: list = []
        idle_polls = 0

        interval = max(0.005, float(self.config.poll_interval))

        while not self._stop_event.is_set():
            lines = watcher.poll()
            if not lines:
                idle_polls += 1
                if self.once_mode and idle_polls >= IDLE_POLLS_BEFORE_DONE:
                    logger.info("--once: watcher idle %d polls; done",
                                idle_polls)
                    return
                if self._stop_event.wait(interval):
                    return
                continue

            idle_polls = 0
            for ts, raw in lines:
                prev_snap, backlog = self._feed_line(prev_snap,
                                                     backlog,
                                                     ts,
                                                     raw)
                if self._stop_event.is_set():
                    return

            # Let this batch finish voicing before pulling more lines so
            # broadcast order tracks log order across snapshots.
            deadline = time.monotonic() + _DRAIN_WAIT_SECONDS
            while len(self.queue) > 0 and time.monotonic() < deadline \
                    and not self._stop_event.is_set():
                time.sleep(_DRAIN_POLL_SLEEP)


    # ------------------------------------------------------------------
    # Pipeline core
    # ------------------------------------------------------------------

    def _feed_line(self, prev_snap, backlog: list, ts: float, raw: str):
        """Advance the pipeline by every message carried on one raw line.

        Returns ``(prev_snap, backlog)`` for the caller to carry forward.
        """
        for msg in parse_line_all(ts, raw):
            # Backlog FIRST -- stage transitions travel inside the same
            # message that publishes their final snapshot.
            backlog.append(msg)

            snap = self.builder.apply(prev_snap, msg)
            if snap is None:
                continue

            snap_match_id = getattr(getattr(snap, "match_meta", None),
                                    "match_id", None)
            snap_match_id = str(snap_match_id) if snap_match_id else None

            previous_match_id = self._match_id

            if snap_match_id is not None and previous_match_id is not None \
                    and snap_match_id != previous_match_id:
                # Phantom-opener discipline FIRST -- seal the outgoing
                # match's queue before anything from this snapshot renders.
                removed = self.queue.flush(previous_match_id)
                logger.info(
                    "match transition %s -> %s; flushed %d queued "
                    "utterance(s)",
                    previous_match_id,
                    snap_match_id,
                    removed,
                )

            for event in self._events_for(snap, backlog, prev_snap):
                self._handle_event(event, snap,
                                   previous_match_id=previous_match_id)
            # Window consumed: the next diff must only see messages that
            # arrived after this snapshot.
            backlog = []

            if snap_match_id is not None \
                    and snap_match_id != self._match_id:
                self._match_id = snap_match_id
                self.queue.set_active_match(snap_match_id)
                with self._state_lock:
                    if self._state == AppState.WATCHING:
                        self._state = AppState.IN_MATCH

            prev_snap = snap

        return prev_snap, backlog

    def _events_for(self, snap, backlog: list, prev_snap) -> list:
        """Diff + story events for one newly published snapshot."""
        events = list(self.differ.diff(prev_snap, snap, backlog))
        events.extend(self.story.update(snap))
        return events

    def _handle_event(self, event, snap, previous_match_id=None) -> None:
        """Gate -> render -> enqueue one event (with end-of-game sealing)."""
        if not passes_gate(event, self.config.verbosity):
            return

        utt = self.narrator.render(event, snap)
        if utt is None:
            return

        if event.kind in ("game_end", "match_end"):
            # Closing line first; await its delivery; then seal the match
            # so no post-end speech can follow it. Seal the PREVIOUSLY
            # active match: a game_end detected alongside a match-id
            # change belongs to the outgoing broadcast, and flushing the
            # new id here would seal the incoming match before it starts.
            seal_target = previous_match_id \
                if previous_match_id is not None else utt.match_id
            self.queue.enqueue(utt)
            self._last_utterance = utt.text
            self._await_delivery(utt)
            removed = self.queue.flush(seal_target)
            logger.info("%s sealed match %s (flushed %d)",
                        event.kind, seal_target, removed)
            return

        if self.queue.enqueue(utt):
            self._last_utterance = utt.text

    def _await_delivery(self, utt) -> None:
        """Wait until the pump confirms delivery of ``utt`` (bounded)."""
        pump = self.pump
        if pump is None:
            return
        deadline = time.monotonic() + _DRAIN_WAIT_SECONDS
        while time.monotonic() < deadline and not self._stop_event.is_set():
            with pump.lock:
                delivered = any(r.uid == utt.uid for r in pump.results)
            if delivered:
                return
            time.sleep(_DRAIN_POLL_SLEEP)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Live snapshot for dashboards/tests."""
        with self._state_lock:
            state_value = str(self._state)
        if state_value == AppState.WATCHING and self._match_id is not None:
            state_value = AppState.IN_MATCH
        return {
            "state": state_value,
            "match_id": self._match_id,
            "queued": len(self.queue),
            "last_utterance": self._last_utterance,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arenaonair",
        description="Radio-style play-by-play narration for MTG Arena.",
    )
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="TOML config file (default "
                             "~/.arenaonair/config.toml)")
    parser.add_argument("--log-path", metavar="PATH", default=None,
                        help="Explicit Player.log location")
    parser.add_argument("--verbosity", choices=tuple(sorted(VERBOSITY_GATE)),
                        default=None,
                        help="quiet | balanced | detailed")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print utterances to stdout instead of speaking")
    parser.add_argument("--once", action="store_true",
                        help="Process until the watcher goes idle twice "
                             "consecutively, then exit (CI smoke mode)")
    return parser


def main(argv=None) -> int:
    """CLI entry point. Returns 0 on success."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)

    overrides: dict = {}
    if args.log_path is not None:
        overrides["log_path"] = args.log_path
    if args.verbosity is not None:
        overrides["verbosity"] = args.verbosity

    cfg = config_mod.load(args.config, **overrides)

    app = ArenaOnAirApp(cfg,
                        dry_run=args.dry_run,
                        once_mode=args.once)
    try:
        app.start()
        while app._watch_thread is not None \
                and app._watch_thread.is_alive():
            app._watch_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
