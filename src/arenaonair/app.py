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
from dataclasses import replace
import logging
import sys
if __name__ == "__main__" and sys.version_info < (3, 11):
    import os
    from pathlib import Path
    launcher = Path(__file__).resolve().parents[2] / "run.sh"
    if launcher.is_file():
        os.execv("/bin/bash", ["bash", str(launcher), *sys.argv[1:]])
    raise SystemExit("ArenaOnAir requires Python 3.11 or newer.")
# Also support direct execution of src/arenaonair/app.py with a modern Python.
if __name__ == "__main__" and not __package__:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "arenaonair"

import os
import threading
import time
from pathlib import Path

from . import config as config_mod
from . import events as ev
from .carddb import DEFAULT_DB_PATH, CardDb
from .differ import EventDiffer
from .gre_parser import parse_line_all
from .models import DeliveryResult, Utterance
from .narrator import Narrator
from . import names
from . import personas
from .llm_booth import GenerativeBooth
from .card_knowledge import CardKnowledge
from .pacing import compute_pacing
from .speech import (
    PRESERVED_KINDS,
    SALIENCE_MUST_SPEAK,
    SpeechPump,
    SpeechQueue,
    build_speaker_chain,
)
from .state_builder import GameStateBuilder, DualStateBuilder, with_knowledge
from .sources import SourceTag, TaggedMessage
from .story import StoryModel
from .watcher import LogWatcher, MultiLogWatcher

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

#: Scripted booth: routine lines each commentary focus leaves unsaid (salient ones always play).
FOCUS_SKIPS: dict[str, frozenset] = {
    "calls": frozenset({"narrative_resource", "narrative_callback", "narrative_speculation"}),
    "balanced": frozenset(),
    "analysis": frozenset({"land_drop", "resolve", "turn_start"}),
}

#: Consecutive idle polls before --once declares the stream done.
IDLE_POLLS_BEFORE_DONE = 2

#: Bound on waiting for the queue to drain between poll batches.
_DRAIN_WAIT_SECONDS = 30.0  # slow-runner headroom (macOS CI)
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

        carddb_path = getattr(self.config, "carddb_path", None) or DEFAULT_DB_PATH
        try:
            self.carddb = CardDb(carddb_path)
            name_resolver = self.carddb.as_resolver()
        except Exception as exc:
            logger.warning("could not initialize card database: %s", exc)
            self.carddb = None
            name_resolver = None

        card_lookup = self.carddb.lookup if self.carddb is not None else None
        self.route = config_mod.resolve_route(self.config)["route"]
        self.builder = GameStateBuilder(name_resolver=name_resolver)
        self.fusion = DualStateBuilder(name_resolver=name_resolver) if self.route in ("dual_file", "relay_server") else None
        self._source_sequences = {}
        self._source_generations = {}
        self._source_backlogs = {}
        self._source_prev = None
        self._public_seen = set()
        self._source_health = {}
        self.differ = EventDiffer(card_lookup=card_lookup)
        self.story = StoryModel(
            thresholds=self.config.story_thresholds or None,
            card_lookup=card_lookup,
        )
        self.narrator = Narrator(window=self.config.window)
        from .analyst import TemplateAnalyst
        lines = getattr(self.config, "analyst_lines_per_turn", None)
        self.analyst = TemplateAnalyst(card_lookup=card_lookup, lines_per_turn=1 if lines is None else lines)
        # Voice routing is independent of the ingestion source count.
        booth = config_mod.resolve_booth(self.config)
        self.persona = personas.get(booth.get("persona"))
        booth["style"] = self.persona.style if self.persona else None
        self.booth = booth
        self.booth_mode = booth["mode"]
        self.hole_cards = config_mod.hole_cards_enabled(self.config)
        self.warnings: list[str] = []
        self.queue = SpeechQueue()
        self.narration_mode = ("legacy" if self.dry_run and not self.config.llm_base_url else "llm") if self.config.narration_mode == "auto" else self.config.narration_mode
        self.llm = (GenerativeBooth(self.config, self.queue, booth, carddb=self.carddb, knowledge=CardKnowledge())
                    if self.narration_mode == "llm" else None)

        # Cross-match memory, match records and automatic recaps (local only).
        self.history = self.recorder = None
        self.last_recap = None
        if getattr(self.config, "history_enabled", True):
            try:
                from .history import HistoryStore
                from .matchlog import MatchRecorder
                data = config_mod.data_dir(self.config)
                self.history = HistoryStore(data / "history.sqlite")
                self.recorder = MatchRecorder(data, history=self.history, on_game_saved=self._game_saved)
            except Exception as exc:
                logger.warning("match history unavailable: %s", exc)
        if self.llm:
            self.llm.hole_cards = self.hole_cards
            if self.recorder is not None:
                self.llm.history_facts = self.recorder.history_facts
        self.overlay = None
        if self.llm is not None and not self.config.llm_base_url:
            self._warn("Connect the generative booth: try five hosted matches, use a Patreon key, "
                       "or connect your own provider.")

        self.speaker = None          # built in start()
        self.config_path = None      # set by main(); where the window saves settings
        self._started_with = replace(self.config)  # settings the running objects were built from
        self._pending_restart: dict = {}           # saved settings that apply on the next launch
        self.pump = None
        self.watcher = None

        self._watch_thread = None
        self._speech_thread = None
        self._stop_event = threading.Event()

        self._state_lock = threading.Lock()
        self._state = AppState.STARTING
        self._match_id = None
        self._last_utterance = None
        self._said_names: dict[str, str] = {}  # player name -> how the booth says it
        self._last_event_time = 0.0
        self._last_speech_time = 0.0
        self._last_play_snap_id = None

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
                    voice=self.booth["pbp_voice"],
                )
            self.speaker = speaker
            if self.booth_mode == "dual":
                for engine in getattr(speaker, "engines", [getattr(speaker, "engine", None)]):
                    preload = getattr(engine, "preload_voices", None)
                    if callable(preload):
                        preload([v for v in (self.booth["pbp_voice"], self.booth["analyst_voice"]) if v])
            self.pump = SpeechPump(queue=self.queue, speaker=speaker, handoff_gap_s=self.handoff_gap_s)

        self.pump.on_delivery = self._delivered
        self.pump.validate = self._valid_for_delivery
        self.pump.prepare = self._on_air
        if self.llm:
            self.llm.start()

        if getattr(self.config, "overlay_port", 0) and self.overlay is None:
            from .overlay import OverlayServer
            try:
                self.overlay = OverlayServer(self.config.overlay_port)
                self.overlay.start()
            except OSError as exc:
                self.overlay = None
                self._warn(f"OBS overlay could not start on port {self.config.overlay_port}: {exc}")
        self._check_detailed_logs()

        if self.route == "relay_server":
            from .relay import RelayLogSource
            self.watcher = RelayLogSource(self.config.relay_bind or "0.0.0.0:8765",
                                          secret=self.config.relay_secret or "")
            self.watcher.start()
        elif self.route == "dual_file":
            self.watcher = MultiLogWatcher([p for p in (self.config.log_player1, self.config.log_player2) if p],
                                           anchor=False)
        else:
            self.watcher = LogWatcher(path=self.config.log_path, anchor=self.config.anchor)

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

    @property
    def handoff_gap_s(self) -> float:
        """Pause between a call and its analyst reply. Printed text has no audio
        to pace, and a pause would only let a fast replay race the printer."""
        return 0.0 if self.dry_run else self.config.co_caster_delay_ms / 1000.0

    def _valid_for_delivery(self, utt) -> bool:
        """Last check before a line starts (never used to cut one mid-sentence)."""
        # Taking the analyst off air mid-match also drops their unspoken lines.
        if utt.role == "color_analyst" and self.booth_mode != "dual":
            return False
        if not self.llm:
            return True
        # A late play call describes a board that has moved on (a stack long
        # resolved); drop it and let the booth catch up. Its replies go with it.
        if (utt.kind == "llm_commentary" and utt.role == "play_by_play" and utt.salience < ev.SALIENCE_MUST_SPEAK
                and self.llm.now() - utt.ts_created > self.config.llm_call_max_age):
            return False
        return self.llm.valid_for_delivery(utt)

    def _on_air(self, utt):
        """The line as spoken: names the booth can't say become roles, at the chosen speed."""
        # Every engine gets the range Kokoro accepts, even with excitement on top.
        return replace(utt, text=names.scrub(utt.text, self._said_names),
                       rate=max(0.5, min(utt.rate * self.config.speech_speed, 2.0)))

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            logger.warning(message)

    def _check_detailed_logs(self) -> None:
        """Tell the player up front when Arena isn't writing game events."""
        if self.route not in ("single", "auto"):
            return
        from .doctor import DETAILED_LOGS_FIX, detailed_logs_status, resolve_log_path
        path = resolve_log_path(self.config)
        if path is None or not path.is_file():
            self._warn("MTG Arena's Player.log wasn't found yet. Start Arena, or pass --log-path.")
        elif detailed_logs_status(path) is False:
            self._warn("Arena's detailed logs are off, so there's nothing to commentate. " + DETAILED_LOGS_FIX)

    def _delivered(self, result, utt) -> None:
        """Speech-pump delivery hook shared by the booth, recorder and overlay."""
        if self.llm:
            self.llm.delivered(result, utt)
        if self.recorder is not None:
            self.recorder.spoken(result, utt)
        if self.overlay is not None and result.ok:
            name = self.booth.get("analyst_name" if utt.role == "color_analyst" else "pbp_name")
            self.overlay.caption(utt.role, name, utt.text)

    def _game_saved(self, match, game) -> None:
        if not getattr(self.config, "auto_recap", True):
            return
        from .recap import write_recap
        result = write_recap(match, config_mod.data_dir(self.config) / "recaps", booth=self.booth)
        self.last_recap = result["markdown"]
        self._last_recap_lines = result["lines"]
        logger.info("Recap written: %s (arenaonair recap --audio for a spoken version)", result["markdown"])

    def speak_last_recap(self) -> int:
        """Voice the latest recap between games; returns lines queued."""
        lines = getattr(self, "_last_recap_lines", None)
        if not lines or not self._between_games():
            return 0
        return self._queue_between_games("recap", lines)

    def preview_voices(self) -> int:
        """Let the booth introduce itself in its current voices, between games only."""
        if not self._between_games():
            return 0
        pbp = self.booth.get("pbp_name")
        lines = [{"role": "play_by_play",
                  "text": f"This is {pbp}, on the call." if pbp else "Checking the play-by-play voice."}]
        if self.booth_mode == "dual":
            analyst = self.booth.get("analyst_name")
            lines.append({"role": "color_analyst", "text": f"And I'm {analyst}, with the color commentary."
                          if analyst else "And this is the color analyst."})
        return self._queue_between_games("voices", lines, kind="voice_check")

    def _between_games(self) -> bool:
        if self.recorder is None:
            return self._match_id is None
        game = getattr(self.recorder, "_game", None)
        return game is None or bool(game.get("closed"))

    def _queue_between_games(self, prefix: str, lines, kind: str = "recap") -> int:
        """Queue booth lines in their own scope; returns lines queued.

        The next game snapshot makes that scope stale instantly, so these
        lines never talk over live play.
        """
        self._recap_seq = getattr(self, "_recap_seq", 0) + 1
        scope = f"{prefix}-{self._recap_seq}"
        self.queue.set_active_match(scope)
        self._recap_active = True
        anchor = None
        for i, line in enumerate(lines):
            analyst = line["role"] == "color_analyst" and self.booth_mode == "dual"
            utt = Utterance(uid=f"{scope}-{i}", match_id=scope, kind=kind, text=line["text"],
                            salience=ev.SALIENCE_LOW, ts_created=time.monotonic() + i * 1e-3,
                            voice=self.booth["analyst_voice" if analyst else "pbp_voice"],
                            role="color_analyst" if analyst else "play_by_play", anchor_uid=anchor)
            self.queue.enqueue(utt)
            anchor = utt.uid
        return len(lines)

    def _private_view(self, snap):
        """Hide the local hand everywhere downstream when hole cards are off air."""
        if self.hole_cards or not snap.seat_knowledge:
            return snap
        return replace(snap, seat_knowledge={
            seat: replace(k, hand_visible=False, hand_fresh_asof=None) for seat, k in snap.seat_knowledge.items()})

    def set_voice(self, voice: str) -> None:
        """Dynamically switch the broadcast voice on the fly."""
        self.config.tts_voice = str(voice).strip()
        self.booth["pbp_voice"] = str(voice).strip()
        if self.speaker is not None and hasattr(self.speaker, "set_voice"):
            self.speaker.set_voice(str(voice).strip())

    def set_booth_voices(self, pbp_voice: str | None = None, analyst_voice: str | None = None) -> bool:
        """Switch booth voices live and remember them for the next launch.

        The next line queued uses the new voice. A caster named after the old
        voice takes the new voice's name, so "Adam" never introduces himself
        in Onyx's voice. Returns True once saved to ``config_path``.
        """
        changes = {}
        for role, voice in (("pbp", pbp_voice), ("analyst", analyst_voice if self.booth_mode == "dual" else None)):
            voice = str(voice or "").strip()
            old = self.booth.get(f"{role}_voice")
            if not voice or voice == old:
                continue
            changes[f"{role}_voice"] = self.booth[f"{role}_voice"] = voice
            name = self.booth.get(f"{role}_name")
            if name and name == config_mod.caster_name(old):
                changes[f"{role}_name"] = self.booth[f"{role}_name"] = config_mod.caster_name(voice)
        if not changes:
            return False
        for key, value in changes.items():
            setattr(self.config, key, value)
        if "pbp_voice" in changes and self.speaker is not None and hasattr(self.speaker, "set_voice"):
            self.speaker.set_voice(changes["pbp_voice"])
        self._warm_voices([v for k, v in changes.items() if k.endswith("_voice")])
        logger.info("booth voices: pbp=%s analyst=%s", self.booth.get("pbp_voice"), self.booth.get("analyst_voice"))
        return self._save_broadcast(changes)

    def set_broadcast_mode(self, mode: str) -> bool:
        """Put the color analyst on air ("dual") or take them off ("solo"), live.

        The play-by-play caster keeps their voice and name. Returns True once
        saved to ``config_path``.
        """
        if mode not in ("solo", "dual") or mode == self.booth_mode:
            return False
        self.config.broadcast_mode = mode
        booth = config_mod.resolve_booth(self.config)
        booth.update({k: self.booth[k] for k in ("pbp_voice", "pbp_name") if self.booth.get(k)})
        self.booth.update(booth)  # in place: the generative booth shares this dict
        self.booth_mode = mode
        if mode == "dual":
            self._warm_voices([v for v in (booth["pbp_voice"], booth["analyst_voice"]) if v])
        logger.info("booth mode: %s (pbp=%s analyst=%s)", mode, booth["pbp_voice"], booth["analyst_voice"])
        return self._save_broadcast({"mode": mode})

    def set_commentary_focus(self, focus: str) -> bool:
        """Shift airtime between calling plays and analysing the game, live.

        Returns True once saved to ``config_path``.
        """
        if focus not in config_mod.COMMENTARY_FOCUSES or focus == self.config.commentary_focus:
            return False
        return self.apply_setting("commentary_focus", focus)["saved"]

    def apply_setting(self, key: str, value) -> dict:
        """Change one window setting: apply it now when the app can, and save it.

        None or "" restores the default. Returns ``{"saved", "restart"}``;
        ``restart`` means some saved setting waits for the next launch.
        Raises ValueError for a bad value or log sources that can't combine.
        """
        from .settings import BY_KEY
        spec = BY_KEY[key]
        if key == "overlay_port" and value not in (None, "") and 0 < int(value) < 1024:
            raise ValueError("Use port 0 (overlay off) or a port from 1024 up.")
        value = config_mod.coerce(key, value)
        effective = config_mod.default(key) if value is None else value
        try:
            config_mod.resolve_route(replace(self.config, **{**self._pending_restart, key: effective}))
        except config_mod.ConfigConflict:
            raise ValueError("Use one log source: Player.log, the shared player logs, "
                             "or the relay. Clear the others first.") from None
        extra = {}
        if spec.restart:
            self._pending_restart[key] = effective
        else:
            extra = self._apply_live(key, effective) or {}
        logger.info("setting %s = %r%s", key, effective, " (next launch)" if spec.restart else "")
        updates = {spec.section: {spec.name: value}}
        for section, values in extra.items():
            updates.setdefault(section, {}).update(values)
        return {"saved": self._save_settings(updates), "restart": bool(self.restart_needed())}

    def restart_needed(self) -> list[str]:
        """Saved settings that differ from what this launch is running with."""
        return sorted(k for k, v in self._pending_restart.items() if getattr(self._started_with, k) != v)

    def _apply_live(self, key: str, value):
        setattr(self.config, key, value)
        if key == "persona":
            self.persona = personas.get(value)
            self.booth["style"] = self.persona.style if self.persona else None
            self.booth["persona"] = self.persona.name if self.persona else None
            # Without a saved voice pair a persona brings its own: keep today's voices next launch too.
            preset = self.config.booth_preset or self.booth.get("preset")
            return {"broadcast": {"preset": preset}} if preset else None
        elif key in ("pbp_name", "analyst_name"):
            voice = self.booth.get(key.replace("_name", "_voice"))
            self.booth[key] = value or config_mod.caster_name(voice)
        elif key == "booth_preset" and value:
            pbp, analyst, pbp_name, analyst_name = config_mod.BOOTH_PRESETS[value]
            voices = {"pbp_voice": pbp, "pbp_name": pbp_name, "analyst_voice": analyst, "analyst_name": analyst_name}
            for k, v in voices.items():
                setattr(self.config, k, v)
                if self.booth_mode == "dual" or k.startswith("pbp"):
                    self.booth[k] = v
            if self.speaker is not None and hasattr(self.speaker, "set_voice"):
                self.speaker.set_voice(pbp)
            self._warm_voices([pbp, analyst])
            return {"broadcast": dict.fromkeys(voices)}  # the preset governs again
        elif key in ("hole_cards", "spectator", "stream_enabled", "stream_delay_s"):
            self.hole_cards = config_mod.hole_cards_enabled(self.config)
            if self.llm:
                self.llm.hole_cards = self.hole_cards
        elif key == "overlay_port":
            if self.overlay is not None:
                self.overlay.close()
                self.overlay = None
            if value:
                from .overlay import OverlayServer
                try:
                    self.overlay = OverlayServer(value)
                    self.overlay.start()
                except OSError as exc:
                    self.overlay = None
                    raise ValueError(f"The overlay couldn't use port {value}: {exc}") from None
        elif key == "co_caster_delay_ms" and self.pump is not None:
            self.pump.handoff_gap_s = self.handoff_gap_s
        elif key == "analyst_lines_per_turn":
            self.analyst.lines_per_turn = 1 if value is None else value
        return None

    def _save_broadcast(self, changes: dict) -> bool:
        return self._save_settings({"broadcast": changes})

    def _save_settings(self, updates: dict) -> bool:
        if self.config_path is None:
            return False
        try:
            config_mod.save_settings(self.config_path, updates)
        except (OSError, ValueError) as exc:
            logger.warning("could not save settings to %s: %s", self.config_path, exc)
            return False
        return True

    def _warm_voices(self, voices) -> None:
        """Load newly picked voices off the speech thread so the next line isn't late."""
        engines = getattr(self.speaker, "engines", None) or [getattr(self.speaker, "engine", None)]
        loaders = [e.preload_voices for e in engines if callable(getattr(e, "preload_voices", None))]
        if loaders and voices:
            threading.Thread(target=lambda: [load(voices) for load in loaders],
                             name="arenaonair-voice-warmup", daemon=True).start()

    def stop(self) -> None:
        """Signal both threads to wind down; join them; shut the speaker."""
        if self.pump is not None:
            self.pump.stop()
        self._stop_event.set()

        if self.llm:
            self.llm.stop()

        for thread in (self._watch_thread, self._speech_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)

        if self.watcher is not None:
            self.watcher.close()
        if self.speaker is not None:
            try:
                self.speaker.shutdown()
            except Exception:
                logger.debug("speaker shutdown raised", exc_info=True)

        if self.carddb is not None:
            try:
                self.carddb.close()
            except Exception:
                logger.debug("carddb close raised", exc_info=True)
        if self.recorder is not None:
            self.recorder.finish_match()
        if self.history is not None:
            self.history.close()
        if self.overlay is not None:
            self.overlay.close()

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
        from .connection import TRIAL_URL
        trial_first_batch = self.config.llm_base_url.rstrip('/') == TRIAL_URL

        interval = max(0.005, float(self.config.poll_interval))

        while not self._stop_event.is_set():
            try:
                lines = watcher.poll()
            except Exception:
                logger.exception("watcher poll raised; stopping watch loop")
                return
            if self.fusion is not None:
                for sid, healthy in dict(getattr(watcher, "source_health", {})).items():
                    if not healthy:
                        self.fusion.mark_source_lost(sid)
                    self._source_health[sid] = healthy
                # Also invalidate queued analysis during a quiet disconnect.
                current = self.fusion.publish()
                if current is not None:
                    self._invalidate_analysis(current)

            if not lines:
                idle_polls += 1
                if self.once_mode and idle_polls >= IDLE_POLLS_BEFORE_DONE:
                    logger.info("--once: watcher idle %d polls; done",
                                idle_polls)
                    deadline = time.monotonic() + _DRAIN_WAIT_SECONDS
                    while (len(self.queue) > 0 or (self.llm and not self.llm.idle)) and time.monotonic() < deadline \
                            and not self._stop_event.is_set():
                        time.sleep(_DRAIN_POLL_SLEEP)
                    return
                if self._stop_event.wait(interval):
                    return
                continue

            idle_polls = 0
            # Prime state from the existing log without spending a trial match on
            # old traffic. Only subsequent appended activity can request a line.
            self._trial_priming = trial_first_batch or (self.once_mode and self.config.llm_base_url.rstrip('/') == TRIAL_URL)
            for item in lines:
              try:
                if self.fusion is None:
                    ts, raw = item
                    prev_snap, backlog = self._feed_line(prev_snap, backlog, ts, raw)
                else:
                    if len(item) == 4:
                        sid, gen, ts, raw = item
                    else:
                        sid, ts, raw = item
                        gen = getattr(watcher, "generations", {}).get(sid, 0)
                    self._feed_source_line(sid, gen, ts, raw)
                if self._stop_event.is_set():
                    return
              except Exception:
                logger.exception("watcher line feed raised (raw input omitted)")
                raise

            trial_first_batch = False
            self._trial_priming = False

            # In --once mode, pace batch reading to allow the test/smoke speaker
            # to voice each batch in order. In live mode, NEVER block here so
            # the watcher stays strictly real-time with the game log.
            if self.once_mode:
                deadline = time.monotonic() + _DRAIN_WAIT_SECONDS
                while (len(self.queue) > 0 or (self.llm and not self.llm.idle)) and time.monotonic() < deadline \
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

            visible = [snap.local_seat] if snap.local_seat is not None and self.hole_cards else []
            snap = with_knowledge(snap, visible, time.monotonic())
            self._invalidate_analysis(snap)
            self._consume_snapshot(prev_snap, snap, backlog)
            prev_snap, backlog = snap, []

        return prev_snap, backlog

    def _consume_snapshot(self, prev_snap, snap, backlog):
        self._diagnostic_game = {
            "game_id": snap.game_id, "stage": snap.game_stage,
            "gre_state_id": snap.gre_state_id, "turn": snap.turn_info.turn_number,
            "chain_valid": snap.chain_valid,
        }
        snap_match_id = getattr(getattr(snap, "match_meta", None),
                                "match_id", None)
        snap_match_id = str(snap_match_id) if snap_match_id else None

        previous_match_id = self._match_id
        said = names.replacements(snap.match_meta.player_names, snap.local_seat)
        # A snapshot that doesn't know the local seat never relabels the listening player.
        self._said_names = ({**said, **self._said_names} if snap.local_seat is None
                            else {**self._said_names, **said})

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

        if getattr(self, "_recap_active", False):
            # Live play resumed: any unspoken recap lines go stale now.
            self._recap_active = False
            self.queue.set_active_match(snap_match_id or self._match_id)
        events = self._events_for(snap, backlog, prev_snap)
        if self.llm:
            self.llm.update_read(self.story.read(snap))
        snap_id = getattr(snap, "snapshot_id", None)

        # Fast tempo and pruning discipline:
        # During live play, when the player takes an action (cast, attack,
        # block, land drop, counter), check if previous speech was not
        # spoken in time.
        tempo = "normal"
        if not self.llm and not self.once_mode and snap_id != self._last_play_snap_id:
            has_player_action = any(
                e.kind in ("cast", "attack_declared", "block_declared",
                           "land_drop", "counter")
                for e in events
            )
            if has_player_action:
                unspoken_plays = self.queue.play_count(self._match_id)
                is_busy = bool(self.pump and getattr(self.pump, "current", None) is not None)

                if unspoken_plays > 0 or is_busy:
                    tempo = "fast"
                    pruned = self.queue.prune_plays(
                        self._match_id, preserve_public_replies=True,
                        in_flight_uid=getattr(getattr(self.pump, "current", None), "uid", None))
                    if pruned > 0:
                        logger.info("Fast tempo: pruned %d un-spoken play(s)", pruned)

                self._last_event_time = time.monotonic()
                self._last_play_snap_id = snap_id

        if self.overlay is not None:
            self.overlay.update(snap, hole_cards=self.hole_cards)
        for event in events:
            if self.recorder is not None:
                self.recorder.observe(event, snap)
            self._handle_event(event, snap,
                               previous_match_id=previous_match_id,
                               tempo=tempo)
            if not self.llm and event.kind in (ev.GAME_START, ev.TURN_START):
                self._speak_history_intro(snap)

        if snap_match_id is not None \
                and snap_match_id != self._match_id:
            self._match_id = snap_match_id
            self.queue.set_active_match(snap_match_id)
            with self._state_lock:
                if self._state == AppState.WATCHING:
                    self._state = AppState.IN_MATCH


    def _invalidate_analysis(self, snap):
        if self.llm:
            self.llm.observe(snap)
            current = getattr(self.pump, "current", None)
            if current is not None and not self.llm.valid_for_delivery(current):
                self.pump.speaker.cancel()
            return
        # Current-state analysis expires when its evidence changes. A reply
        # reacting to a completed public play remains valid across GRE ticks.
        signature = (snap.match_meta.match_id, snap.game_id, snap.gre_state_id,
                     tuple(sorted((seat, k.hand_visible) for seat, k in snap.seat_knowledge.items())))
        if getattr(self, "_analysis_signature", signature) != signature:
            old = self._analysis_signature
            self.queue.prune_replies(state_dependent_only=(old[:2] == signature[:2]))
            if old[:2] != signature[:2] or not set(old[3]) <= set(signature[3]):
                self.differ._armed_traps.clear()
        self._analysis_signature = signature

    def _feed_source_line(self, sid, generation, ts, raw):
        if self._source_generations.get(sid) != generation:
            self._source_generations[sid] = generation
            self._source_sequences[sid] = 0
            self._source_backlogs[sid] = []
        for msg in parse_line_all(ts, raw):
            self._source_sequences[sid] += 1
            self._source_backlogs.setdefault(sid, []).append(msg)
            self.fusion.ingest(TaggedMessage(SourceTag(sid, generation, self._source_sequences[sid], ts), msg))
            snap = self.fusion.publish()
            if snap is None:
                continue
            snap = self._private_view(snap)
            self._invalidate_analysis(snap)
            key = (snap.match_meta.match_id, snap.game_id, snap.gre_state_id)
            if self.fusion.public_advanced and key not in self._public_seen:
                self._public_seen.add(key)
                messages = self._source_backlogs.get(self.fusion.public_source, [])
                self._consume_snapshot(self._source_prev, snap, messages)
                self._source_prev = snap
                self._source_backlogs = {k: [] for k in self._source_backlogs}
            elif snap.chain_valid:
                # Enrichment/actions can create new strategic observations,
                # but must never re-diff public events or momentum.
                self.differ._window_msgs = [msg] if sid == self.fusion.public_source else []
                enriched_events = self.differ.detect_trap_armed(self._source_prev, snap)
                enriched_events.extend(self.story._fold_clash_of_outs(snap, msg.ts))
                for event in enriched_events:
                    self._handle_event(event, snap)
                self._source_prev = snap

    def _speak_history_intro(self, snap) -> None:
        """Template booth: one memory line per match once the pairing is known."""
        if self.recorder is None:
            return
        text = self.recorder.history_intro(snap)
        if not text:
            return
        dual = self.booth_mode == "dual"
        self._seq_history = getattr(self, "_seq_history", 0) + 1
        utt = Utterance(uid=f"{snap.match_meta.match_id}-history-{self._seq_history}",
                        match_id=str(snap.match_meta.match_id), kind="history_note", text=text,
                        salience=ev.SALIENCE_HIGH, ts_created=time.monotonic(),
                        voice=self.booth["analyst_voice" if dual else "pbp_voice"],
                        role="color_analyst" if dual else "play_by_play")
        if self.queue.enqueue(utt):
            self._last_utterance = text

    def _events_for(self, snap, backlog: list, prev_snap) -> list:
        """Diff + story events for one newly published snapshot."""
        events = list(self.differ.diff(prev_snap, snap, backlog))
        events.extend(self.story.update(snap))
        return events

    def _handle_event(self, event, snap, previous_match_id=None, tempo="normal") -> None:
        """Gate -> render -> enqueue one event (with end-of-game sealing)."""
        if getattr(self, '_trial_priming', False):
            return
        if self.llm and event.kind == ev.TURN_START:
            self.llm.turn_started(snap)  # a game-read moment, whatever the verbosity
        if not passes_gate(event, self.config.verbosity):
            return
        if self.llm:
            self.queue.set_active_match(snap.match_meta.match_id)
            self.llm.submit(event, snap)
            current = getattr(self.pump, "current", None)
            if current is not None and (not self.llm.valid_for_delivery(current) or
                    event.salience >= 3 and current.salience < 3):
                self.pump.speaker.cancel()
            return
        if event.salience < ev.SALIENCE_HIGH and event.kind in FOCUS_SKIPS.get(self.config.commentary_focus, ()):
            return
        if event.kind == ev.CAST and not event.payload.get("name"):
            logger.warning("Card name unavailable for public cast: grp_id=%s instance_id=%s",
                           event.payload.get("grp_id"), event.payload.get("instance_id"))

        unspoken_plays = self.queue.play_count(self._match_id)
        is_busy = bool(self.pump and getattr(self.pump, "current", None) is not None)
        pacing = compute_pacing(
            event, snap,
            story=self.story,
            queue_backlog=unspoken_plays,
            is_busy=is_busy,
        )
        effective_tempo = tempo if tempo != "normal" else pacing.tempo

        # Cadence regulation: enforce breathing room between routine calls during calm/normal play
        if not self.once_mode and event.salience < ev.SALIENCE_HIGH and pacing.cadence_gap > 0.0:
            now = time.monotonic()
            if self._last_speech_time > 0 and (now - self._last_speech_time) < pacing.cadence_gap:
                return

        utt = self.narrator.render(
            event, snap,
            tempo=effective_tempo,
            excitement=personas.cap_excitement(self.persona, pacing.excitement),
            rate=pacing.speech_rate * (self.persona.rate if self.persona else 1.0),
        )
        if utt is None:
            return
        utt = replace(utt, voice=self.booth["pbp_voice"])

        if event.kind in ("game_end", "match_end"):
            # Closing line first:
            # 1. Purge any pending play-by-play utterances for this match so
            #    stale plays never continue after game/match finishes.
            seal_target = previous_match_id \
                if previous_match_id is not None else utt.match_id
            removed = self.queue.prune_plays(seal_target)
            if removed > 0:
                logger.info("%s purged %d pending play(s)", event.kind, removed)

            # 2. Cancel in-flight low-salience speech if pump is currently voicing
            if self.pump and getattr(self.pump, "current", None) is not None:
                in_flight = getattr(self.pump, "current", None)
                if in_flight and in_flight.salience < SALIENCE_MUST_SPEAK:
                    cancellable = getattr(self.pump.speaker, "cancel", None)
                    if callable(cancellable):
                        logger.info("Preempting %s for %s", in_flight.uid, event.kind)
                        cancellable()

            self.queue.enqueue(utt)
            self._last_utterance = utt.text
            self._last_speech_time = time.monotonic()
            self._await_delivery(utt)
            if event.kind == "game_end":
                # Game boundary only: purge obsolete game commentary but keep
                # the queue able to narrate subsequent games of the SAME match
                # (Bo3) and the final match-end announcement under this id.
                removed = self.queue.close_game(seal_target)
                logger.info("%s closed game %s (removed %d obsolete utterance(s))",
                            event.kind, seal_target, removed)
            else:
                # Permanent match closure: seal so late re-enqueues cannot
                # resurrect speech for a finished match.
                removed = self.queue.flush(seal_target)
                logger.info("%s sealed match %s (flushed %d)",
                            event.kind, seal_target, removed)
            return

        # Preempt in-flight mundane speech if an electric event arrives
        if pacing.excitement == "electric" and self.pump and getattr(self.pump, "current", None) is not None:
            in_flight = getattr(self.pump, "current", None)
            if in_flight and in_flight.salience < ev.SALIENCE_HIGH:
                cancellable = getattr(self.pump.speaker, "cancel", None)
                if callable(cancellable):
                    logger.info("Preempting mundane speech %s for electric event %s", in_flight.uid, event.kind)
                    cancellable()

        if self.queue.enqueue(utt):
            self._last_utterance = utt.text
            self._last_speech_time = time.monotonic()
            if self.booth_mode == "dual":
                companion = self.analyst.companion(event, snap, utt, voice=self.booth["analyst_voice"])
                if companion is not None:
                    self.queue.enqueue(companion)

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
            "broadcast_mode": self.booth_mode,
            "route": self.route,
            "sources": dict(self._source_health),
            "enriched": bool(self.fusion and self.fusion.last_publish_was_enriched),
            "narration_mode": self.narration_mode,
            "trial": getattr(self.llm.client, "trial_status", None) if self.llm else None,
            "model": self.llm.status() if self.llm else {"state": "legacy"},
            "persona": self.persona.name if self.persona else None,
            "focus": self.config.commentary_focus,
            "coaching": self.config.coaching,
            "restart_needed": self.restart_needed(),
            "hole_cards": self.hole_cards,
            "overlay": self.overlay.url if self.overlay else None,
            "warnings": list(self.warnings),
            "last_recap": self.last_recap,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arenaonair",
        description="Radio-style play-by-play narration for MTG Arena.",
        epilog="Other commands: arenaonair setup | doctor | recap | build-carddb | install-app  (each takes --help)",
    )
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="TOML config file (default "
                             "~/.arenaonair/config.toml)")
    parser.add_argument("--log-path", metavar="PATH", default=None,
                        help="Explicit Player.log location")
    parser.add_argument("--verbosity", choices=tuple(sorted(VERBOSITY_GATE)),
                        default=None,
                        help="quiet | balanced | detailed")
    parser.add_argument("--voice", metavar="VOICE", default=None,
                        help="TTS voice name (e.g. af_heart, am_adam, bm_george)")
    parser.add_argument("--speed", type=float, metavar="X", default=None,
                        help="How fast the casters talk, 0.5-2.0 (default 1.6; 1.0 is the voice's natural pace)")
    parser.add_argument("--list-voices", action="store_true",
                        help="List all available Kokoro default voices and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print utterances to stdout instead of speaking")
    parser.add_argument("--once", action="store_true",
                        help="Process until the watcher goes idle twice "
                             "consecutively, then exit (CI smoke mode)")
    ui_flags = parser.add_mutually_exclusive_group()
    ui_flags.add_argument("--ui", dest="ui", action="store_true", default=None,
                          help="Open the status window with a Copy bug report button")
    ui_flags.add_argument("--no-ui", dest="ui", action="store_false",
                          help="Keep the broadcast in the terminal")
    parser.add_argument("--narration-mode", choices=("auto", "llm", "legacy"), default=None)
    parser.add_argument("--llm-base-url", default=None, help="Chat-completions API base including /v1")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--llm-key-file", default=None, help="Restricted credential file; or use ARENAONAIR_API_KEY")
    # Dual-booth broadcast flags (dual-expansions.md S3.6/S8.7):
    parser.add_argument("--broadcast-mode", choices=("solo", "dual"),
                        default=None,
                        help="Booth mode; dual enables PBP + color analyst "
                             "co-casters (works with a single log)")
    parser.add_argument("--booth-preset", metavar="PRESET",
                        choices=tuple(sorted(config_mod.BOOTH_PRESETS)),
                        default=None,
                        help="Co-caster voice preset (sports_desk, "
                             "mixed_duo, premier_pro_tour, "
                             "academic_tactical)")
    parser.add_argument("--focus", choices=config_mod.COMMENTARY_FOCUSES, default=None,
                        help="Balance of play calls and game analysis (default balanced)")
    parser.add_argument("--coaching", action=argparse.BooleanOptionalAction, default=None,
                        help="Let the AI booth suggest plays for you (default off)")
    parser.add_argument("--pbp-voice", metavar="VOICE", default=None,
                        help="Override play-by-play voice")
    parser.add_argument("--analyst-voice", metavar="VOICE", default=None,
                        help="Override color analyst voice")
    # Multi-source ingestion flags:
    parser.add_argument("--log-player1", metavar="PATH", default=None,
                        help="Player 1 log path (selects the multi-source "
                             "file route; a second slot is optional)")
    parser.add_argument("--log-player2", metavar="PATH", default=None,
                        help="Player 2 log path (optional enrichment slot)")
    parser.add_argument("--relay-listen", metavar="HOST:PORT", default=None,
                        help="Start as a tournament relay receiver "
                             "(e.g. 0.0.0.0:8765)")
    parser.add_argument("--persona", choices=tuple(personas.PERSONAS), default=None,
                        help="Booth style: " + ", ".join(p.name for p in personas.PERSONAS.values()))
    parser.add_argument("--overlay-port", type=int, metavar="PORT", default=None,
                        help="Serve the OBS browser-source overlay on 127.0.0.1:PORT")
    parser.add_argument("--stream-delay", type=float, metavar="SECONDS", default=None,
                        help="Your OBS stream delay; marks this session as streamed")
    parser.add_argument("--spectator", action="store_true", default=None,
                        help="With shared logs (--log-player1/2, --relay-listen): the listener is not "
                             "one of the players, so hands may go on air")
    parser.add_argument("--hole-cards", choices=("auto", "on", "off"), default=None,
                        help="Whether the booth may talk about your hand (auto: yes unless "
                             "streaming with under 30 s delay)")
    return parser


#: ``arenaonair <command>`` helpers; anything else is the broadcast itself.
SUBCOMMANDS = {
    "doctor": ("doctor", "doctor_main"),
    "setup": ("doctor", "setup_main"),
    "recap": ("recap", "main"),
    "build-carddb": ("build_carddb", "main"),
    "install-app": ("desktop", "main"),
}


def main(argv=None) -> int:
    """CLI entry point. Returns 0 on success."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        import importlib
        module, func = SUBCOMMANDS[argv[0]]
        return getattr(importlib.import_module(f".{module}", __package__), func)(argv[1:]) or 0
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The voice engine checks Hugging Face for model updates at startup; those
    # request lines and the unauthenticated-access notice aren't app problems.
    for noisy in ("httpx", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    args = _build_arg_parser().parse_args(argv)

    if args.list_voices:
        from .platform.tts import list_kokoro_voices
        voices = list_kokoro_voices()
        print("Available Kokoro Default Voices:")
        print("=" * 65)
        for v_name, v_desc in sorted(voices.items()):
            print(f"  {v_name:<12} : {v_desc}")
        print("=" * 65)
        print("Usage: python -m arenaonair.app --voice am_adam")
        return 0

    overrides: dict = {k: getattr(args, k) for k in (
        "narration_mode", "llm_base_url", "llm_model", "llm_key_file") if getattr(args, k) is not None}
    if args.log_path is not None:
        overrides["log_path"] = args.log_path
    if args.verbosity is not None:
        overrides["verbosity"] = args.verbosity
    if args.voice is not None:
        overrides["tts_voice"] = args.voice
    if args.speed is not None:
        overrides["speech_speed"] = args.speed
    if args.broadcast_mode is not None:
        overrides["broadcast_mode"] = args.broadcast_mode
    if args.booth_preset is not None:
        overrides["booth_preset"] = args.booth_preset
    if args.focus is not None:
        overrides["commentary_focus"] = args.focus
    if args.coaching is not None:
        overrides["coaching"] = args.coaching
    if args.pbp_voice is not None:
        overrides["pbp_voice"] = args.pbp_voice
    if args.analyst_voice is not None:
        overrides["analyst_voice"] = args.analyst_voice
    if args.log_player1 is not None:
        overrides["log_player1"] = args.log_player1
    if args.log_player2 is not None:
        overrides["log_player2"] = args.log_player2
    if args.relay_listen is not None:
        overrides["relay_bind"] = args.relay_listen
    if args.persona is not None:
        overrides["persona"] = args.persona
    if args.overlay_port is not None:
        overrides["overlay_port"] = args.overlay_port
    if args.stream_delay is not None:
        overrides["stream_enabled"] = True
        overrides["stream_delay_s"] = args.stream_delay
    if args.hole_cards is not None:
        overrides["hole_cards"] = args.hole_cards
    if args.spectator:
        overrides["spectator"] = True

    cfg = config_mod.load(args.config, **overrides)

    # Route validation (S8.7): reject explicitly conflicting source routes
    # with an actionable error before any thread starts.
    try:
        route_info = config_mod.resolve_route(cfg)
    except config_mod.ConfigConflict as exc:
        parser = _build_arg_parser()
        parser.error(str(exc))
        return 2  # pragma: no cover - parser.error exits

    booth_info = config_mod.resolve_booth(cfg)
    logger.info("route=%s booth=%s preset=%s pbp=%s analyst=%s",
                route_info["route"], booth_info["mode"],
                booth_info.get("preset"), booth_info.get("pbp_voice"),
                booth_info.get("analyst_voice"))

    app = ArenaOnAirApp(cfg,
                        dry_run=args.dry_run,
                        once_mode=args.once)
    app.config_path = Path(args.config).expanduser() if args.config else config_mod.DEFAULT_CONFIG_PATH
    from .diagnostics import BugReportLogs
    llm = getattr(app, "llm", None)
    report_logs = BugReportLogs(secrets=(cfg.relay_secret, llm.client.key if llm else None))
    logging.getLogger().addHandler(report_logs)
    relaunch = False
    try:
        import importlib.util
        want_ui = args.ui is True or (args.ui is None and not args.once and sys.stdout.isatty()
                                     and importlib.util.find_spec("PySide6") is not None)
        if args.ui is True and importlib.util.find_spec("PySide6") is None:
            logger.error("The status window requires the ui extra: pip install -e '.[ui]'")
            return 1
        app.start()
        if want_ui:
            from .ui import run_dashboard
            relaunch = bool(run_dashboard(app, report_logs))
            return 0
        while app._watch_thread is not None \
                and app._watch_thread.is_alive():
            app._watch_thread.join(timeout=0.5)
        if args.once:
            # --once contract: the transcript must be COMPLETE before exit.
            # The watcher's own drain window can elapse on slow runners while
            # the speech thread is still working through the backlog; give
            # the pump bounded extra time to finish everything enqueued.
            deadline = time.monotonic() + 60.0
            while (len(app.queue) > 0 or (app.llm and not app.llm.idle) or getattr(app.pump, "current", None)
                    is not None) and time.monotonic() < deadline:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        logging.getLogger().removeHandler(report_logs)
        report_logs.close()
        if relaunch:
            _relaunch()

    return 0


def _relaunch() -> None:
    """Start again with the same command so saved settings apply.

    exec keeps the process (and so the macOS Dock tile) rather than starting a
    second copy. Launch flags come back too, and still override saved settings.
    """
    logger.info("relaunching to apply saved settings")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
