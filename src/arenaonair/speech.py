"""Speech subsystem: salience-prioritized queue, session-scoped staleness,
delivery-confirmed speaking, and per-OS TTS engine chains (DESIGN §3.7).

Reliability contract items enforced here (DESIGN §6):

1. No silent drops — every failed delivery logs WARNING with reason + full text.
2. No stale-match speech — staleness is scoped to match_id only; a match-end
   flush guarantees a new match never inherits the previous match's queue.
3. No position-bound staleness — turn numbers never gate delivery anywhere in
   this module (design principle 4).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .interfaces import Speaker
from .models import DeliveryResult, Utterance

logger = logging.getLogger(__name__)


def default_now() -> float:
    """Receiver-clock default (time.time) for reply-expiry comparisons."""
    return time.time()

# Salience scale mirrors events.py; duplicated numerically to keep speech.py
# decoupled from the differ's constant table.
SALIENCE_FILLER = 0
SALIENCE_LOW = 1
SALIENCE_HIGH = 2
SALIENCE_MUST_SPEAK = 3

#: Event kinds exempt from play-by-play tempo pruning.
PRESERVED_KINDS = frozenset({
    "match_start",
    "match_end",
    "game_start",
    "game_end",
})

DEFAULT_CHAINS: dict[str, tuple[str, ...]] = {
    "windows": ("kokoro", "sapi"),
    "darwin": ("kokoro", "say"),
    "linux": ("kokoro", "piper", "espeakng"),
}


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------

class SpeechQueue:
    """Thread-safe priority queue of utterances.

    Ordering: salience descending, then ts ascending (oldest first within a
    tie). Dedupe by uid — re-enqueuing an already-queued utterance is a no-op.

    Staleness is session-scoped ONLY (design principle 4): an utterance is
    stale iff its match_id differs from the currently active match, or its
    match was flushed. Turn numbers are deliberately absent from this class.

    Dual-booth dialogue gating (dual-expansions.md S8.5): an utterance with
    ``anchor_uid`` set is ineligible for delivery until its anchor records a
    DeliveryResult(ok=True) via :meth:`note_anchor_result`. Boundary
    announcements (PRESERVED_KINDS) are never gated.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Utterance] = []
        self._uids: set[str] = set()
        self._flushed_matches: set[str | None] = set()
        self._active_match: str | None = None
        # anchor uid -> DeliveryResult of the most recent delivery attempt
        self._anchor_results: dict[str, DeliveryResult] = {}
        # Injectable clock for expiry checks (defaults to time.time; tests
        # swap in a fake clock). Receiver-clock semantics: expires_ts values
        # are compared against the same clock that stamped ts_created.
        self.now_fn: Callable[[], float] = default_now

    # -- population -------------------------------------------------------

    def enqueue(self, utt: Utterance) -> bool:
        """Add an utterance. True if stored; False if deduped or already flushed.

        Cross-match utterances ARE accepted here — staleness is evaluated at
        pop time against the then-current active match, so speech rendered
        around a match transition queues first and filters later.
        """
        with self._lock:
            if utt.uid in self._uids:
                return False
            if utt.match_id in self._flushed_matches:
                logger.warning(
                    "enqueue rejected utterance uid=%s: match %r already flushed",
                    utt.uid, utt.match_id,
                )
                return False
            self._items.append(utt)
            self._uids.add(utt.uid)
            return True

    def set_active_match(self, match_id: str | None) -> None:
        """Record the current match; implicitly stales every other match."""
        with self._lock:
            self._active_match = match_id

    @property
    def active_match(self) -> str | None:
        with self._lock:
            return self._active_match

    # -- anchor gating (dual-booth) ----------------------------------------

    def note_anchor_result(self, result: DeliveryResult) -> None:
        """Record a delivery outcome for an anchor uid.

        ok=True unblocks every queued reply anchored to that uid; ok=False
        marks the anchor dead so dependent replies get dropped at pop time.
        """
        with self._lock:
            self._anchor_results[result.uid] = result

    def anchor_ok(self, anchor_uid: str | None) -> bool:
        """True iff the anchor delivered successfully (or is ungated)."""
        if not anchor_uid:
            return True
        with self._lock:
            result = self._anchor_results.get(anchor_uid)
        return bool(result is not None and result.ok)

    def drop_dead_replies(self) -> int:
        """Remove queued replies whose anchor failed/was cancelled/pruned.

        Called opportunistically by the pump before each pop. Replies whose
        anchor has NO recorded result stay queued (still waiting). Returns
        the number removed.
        """
        with self._lock:
            survivors = []
            removed = 0
            for u in self._items:
                anchor_uid = getattr(u, "anchor_uid", None)
                if anchor_uid:
                    result = self._anchor_results.get(anchor_uid)
                    if result is not None and not result.ok:
                        logger.debug(
                            "dropping analyst reply uid=%s: anchor %s "
                            "did not deliver (%s)",
                            u.uid, anchor_uid, result.reason or "failed",
                        )
                        removed += 1
                        continue
                survivors.append(u)
            if removed > 0:
                self._items = survivors
                self._uids = {u.uid for u in survivors}
            return removed

    def forget_anchor(self, anchor_uid: str) -> bool:
        """Drop the queued reply(ies) anchored to ``anchor_uid`` outright.

        Used when the anchor itself was pruned/flushed before any delivery
        result existed. Returns True if at least one reply was removed.
        """
        with self._lock:
            survivors = []
            removed = False
            for u in self._items:
                if getattr(u, "anchor_uid", None) == anchor_uid:
                    logger.debug(
                        "dropping orphaned analyst reply uid=%s: anchor %s "
                        "left the queue without delivering",
                        u.uid, anchor_uid,
                    )
                    removed = True
                    continue
                survivors.append(u)
            if removed:
                self._items = survivors
                self._uids = {u.uid for u in survivors}
            return removed

    def flush(self, match_id: str | None) -> int:
        """Match-end discipline: drop every queued utterance of that match.

        Returns the number removed. The match is remembered as flushed so a
        late re-enqueue of its speech cannot resurrect it.
        """
        with self._lock:
            survivors = [u for u in self._items if u.match_id != match_id]
            removed = len(self._items) - len(survivors)
            self._items = survivors
            self._uids = {u.uid for u in survivors}
            self._flushed_matches.add(match_id)
            return removed

    def close_game(self, match_id: str | None) -> int:
        """Game-boundary discipline: drop obsolete pending speech for the
        finished game WITHOUT sealing the match.

        Unlike :meth:`flush` (permanent match closure), the queue stays able
        to narrate subsequent games of the same match (Bo3+) and the final
        match-end announcement under the same match_id. Everything except the
        match-end announcement is obsolete once a game ends, so it is removed.

        Returns the number removed.
        """
        with self._lock:
            survivors = []
            removed = 0
            for u in self._items:
                if u.match_id == match_id and u.kind != "match_end":
                    removed += 1
                else:
                    survivors.append(u)
            if removed > 0:
                self._items = survivors
                self._uids = {u.uid for u in survivors}
            return removed

    def prune_plays(self, match_id: str | None = None) -> int:
        """Fast-tempo / play-boundary discipline: drop un-spoken play-by-play speech.

        Leaves boundary events (match_start/end, game_start/end) intact.
        Returns the number of dropped utterances.
        """
        with self._lock:
            target_match = match_id if match_id is not None else self._active_match
            survivors = []
            removed = 0
            for u in self._items:
                if (target_match is None or u.match_id == target_match) and u.kind not in PRESERVED_KINDS:
                    removed += 1
                else:
                    survivors.append(u)
            if removed > 0:
                self._items = survivors
                self._uids = {u.uid for u in survivors}
            return removed

    def play_count(self, match_id: str | None = None) -> int:
        """Return the count of pending play-by-play utterances."""
        with self._lock:
            target_match = match_id if match_id is not None else self._active_match
            return sum(
                1 for u in self._items
                if (target_match is None or u.match_id == target_match)
                and u.kind not in PRESERVED_KINDS
                and not self._is_stale_locked(u)
            )

    def pending(self) -> list[Utterance]:
        """Snapshot of queued utterances in storage order (read-only).

        Includes gated/stale items — callers filter further as needed. Used
        by the pump's pacing logic to detect queued dialogue replies.
        """
        with self._lock:
            return list(self._items)

    # -- consumption ------------------------------------------------------

    def pop_best(self) -> Utterance | None:
        """Remove and return the highest-priority non-stale utterance."""
        with self._lock:
            idx = self._best_index_locked()
            if idx is None:
                return None
            return self._items.pop(idx)

    def peek_best(self) -> Utterance | None:
        with self._lock:
            idx = self._best_index_locked()
            return None if idx is None else self._items[idx]

    def drain(self) -> list[Utterance]:
        """Empty the queue, returning utterances in priority order."""
        out: list[Utterance] = []
        while True:
            utt = self.pop_best()
            if utt is None:
                return out
            out.append(utt)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _sort_key(u: Utterance) -> tuple[int, float]:
        return (-u.salience, u.ts_created)

    def _best_index_locked(self) -> int | None:
        best_idx: int | None = None
        best_key: tuple[int, float] | None = None
        for i, u in enumerate(self._items):
            if self._is_stale_locked(u):
                continue
            key = self._sort_key(u)
            if best_key is None or key < best_key:
                best_key, best_idx = key, i
        return best_idx

    def _is_stale_locked(self, u: Utterance) -> bool:
        if u.match_id in self._flushed_matches:
            return True
        if u.match_id != self._active_match:
            return True
        # Reply-expiry: an overdue dependent reply silently ages out. Boundary
        # announcements (PRESERVED_KINDS) are NEVER dropped by this logic.
        expires = getattr(u, "expires_ts", None)
        if expires is not None and u.kind not in PRESERVED_KINDS:
            try:
                if self.now_fn() > float(expires):
                    logger.debug(
                        "utterance uid=%s expired (expires_ts=%.3f); "
                        "ineligible for delivery",
                        u.uid, float(expires),
                    )
                    return True
            except (TypeError, ValueError):
                pass
        # Anchor gating: a dependent reply waits until its anchor delivered ok.
        anchor_uid = getattr(u, "anchor_uid", None)
        if anchor_uid and u.kind not in PRESERVED_KINDS:
            result = self._anchor_results.get(anchor_uid)
            if result is None or not result.ok:
                return True
        return False


# --------------------------------------------------------------------------
# Pump / arbiter
# --------------------------------------------------------------------------

@dataclass
class SpeechPump:
    """Arbiter between queue and speaker.

    run_once(): pop the best non-stale utterance, speak it, record the
    DeliveryResult. Failures log WARNING (reason + full text) and the pump
    continues — reliability contract #1.

    Preemption: while a lower-salience utterance is mid-speak, a MUST_SPEAK
    arrival cancels the in-flight synthesis (when the speaker supports it)
    and delivers the new one immediately.

    Dual-booth dialogue (dual-expansions.md S8.5): dependent replies
    (anchor_uid set) are only popped once their anchor recorded ok; failed/
    cancelled anchors cause the reply to be dropped at DEBUG. Co-caster
    handoff pacing: after an anchor whose reply is queued, the next gap uses
    ``handoff_gap_s`` (tight, 0.180s default); unrelated plays breathe at
    ``play_gap_min_s``..``play_gap_max_s``.
    """

    queue: SpeechQueue
    speaker: Speaker
    results: list[DeliveryResult] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stopped: bool = False

    # -- co-caster pacing knobs (D3) ----------------------------------------
    #: Tight gap between an anchor and its queued analyst reply (seconds).
    handoff_gap_s: float = 0.18
    #: Breathing-room bounds between unrelated distinct plays (seconds);
    #: consumed by :meth:`next_gap_s` and enforced in the delivery loop when
    #: ``enforce_play_gaps`` is on (default off preserves the historical
    #: pump timing; upstream cadence logic remains the breathing-room owner).
    play_gap_min_s: float = 0.8
    play_gap_max_s: float = 1.5
    enforce_play_gaps: bool = False

    # -- public API -------------------------------------------------------

    def run_once(self) -> DeliveryResult | None:
        """One dequeue→speak→confirm cycle. Returns the result or None."""
        self.queue.drop_dead_replies()
        utt = self.queue.pop_best()
        if utt is None:
            return None
        return self.deliver(utt)

    def deliver(self, utt: Utterance) -> DeliveryResult:
        with self.lock:
            self.current = utt
        mark = getattr(self.speaker, "mark_current", None)
        if callable(mark):
            mark(utt)
        try:
            result = self.speaker.speak(utt)
        except Exception as exc:  # Speaker promises not to raise; belt+braces
            result = DeliveryResult(
                uid=utt.uid, ok=False, reason=f"speaker raised: {exc}",
            )
        finally:
            with self.lock:
                self.current = None
        self.record(result, utt)
        return result

    def _has_queued_reply_for(self, anchor_uid: str | None) -> bool:
        if not anchor_uid:
            return False
        return any(getattr(u, "anchor_uid", None) == anchor_uid
                   for u in self.queue.pending())

    def next_gap_s(self, just_delivered: Utterance | None = None) -> float:
        """Gap to observe before the NEXT delivery (D3 pacing).

        - Anchor whose dialogue reply is still queued -> tight co-caster
          handoff (``handoff_gap_s``).
        - Otherwise unrelated distinct plays breathe within
          ``play_gap_min_s``..``play_gap_max_s`` (urgency of the pending
          backlog pulls the gap toward the minimum bound).
        """
        if just_delivered is not None \
                and self._has_queued_reply_for(getattr(just_delivered, "uid", None)):
            return self.handoff_gap_s
        pending = self.queue.pending()
        if pending:
            top_salience = max(
                int(getattr(u, "salience", 0) or 0) for u in pending)
            span = max(0.0, self.play_gap_max_s - self.play_gap_min_s)
            urgency = max(0.0, min(1.0, top_salience / 3.0))
            return self.play_gap_max_s - span * urgency
        return self.play_gap_min_s

    def wait_gap(self, gap_s: float) -> None:
        """Sleep ``gap_s`` seconds honoring an injectable clock when present."""
        if gap_s <= 0:
            return
        clock = getattr(self.queue, "now_fn", None)
        if clock is not None:
            target = clock() + gap_s
            while clock() < target and not self.stopped:
                time.sleep(min(0.005, max(0.001, target - clock())))
            return
        time.sleep(gap_s)

    def preempt_if_idle_with(self, utt: Utterance) -> DeliveryResult | None:
        """Preemption entry point for a MUST_SPEAK arrival.

        If a lower-salience utterance is mid-speak and the speaker supports
        cancel(), interrupt it first; then deliver ``utt`` ahead of whatever
        remains queued.
        """
        cancellable = getattr(self.speaker, "cancel", None)
        if callable(cancellable):
            with self.lock:
                in_flight = getattr(self, "current", None)
            if in_flight is not None and in_flight.salience < SALIENCE_MUST_SPEAK:
                logger.info(
                    "preempting uid=%s (salience %d) for MUST_SPEAK uid=%s",
                    in_flight.uid, in_flight.salience, utt.uid,
                )
                cancellable()
        return self.deliver(utt)

    def run_forever(self, poll_interval: float = 0.05) -> None:
        """Thread body; run_once in a loop until stop()."""
        while not self.stopped:
            spoke = self.run_once()
            if spoke is None:
                time.sleep(poll_interval)
                continue
            # D3 pacing: co-caster handoff after an anchor with a queued
            # reply is ALWAYS tight; unrelated play gaps only when enabled
            # (default off keeps historical pump timing for cadence tests).
            just_utt = spoke_utt_of(self, spoke)
            if just_utt is not None \
                    and self._has_queued_reply_for(getattr(just_utt, "uid", None)):
                self.wait_gap(self.handoff_gap_s)
            elif self.enforce_play_gaps:
                self.wait_gap(self.next_gap_s(just_utt))

    def stop(self) -> None:
        self.stopped = True

    # -- bookkeeping ------------------------------------------------------

    def record(self, result: DeliveryResult, utt: Utterance) -> None:
        """Confirm delivery; loud WARNING on any failure (never silent)."""
        with self.lock:
            self.results.append(result)
            ledger = getattr(self, "_last_utt_by_uid", None)
            if ledger is None:
                ledger = {}
                self._last_utt_by_uid = ledger
            ledger[result.uid] = utt
        # Anchor bookkeeping: unblock (or doom) dependent replies.
        self.queue.note_anchor_result(result)
        if not result.ok:
            logger.warning(
                "speech delivery FAILED uid=%s kind=%s reason=%s text=%r",
                result.uid,
                utt.kind,
                result.reason or "(no reason given)",
                utt.text,
            )


def spoke_utt_of(pump: "SpeechPump", result: DeliveryResult) -> Utterance | None:
    """Recover the utterance just delivered from the pump's uid ledger."""
    ledger = getattr(pump, "_last_utt_by_uid", {})
    return ledger.get(result.uid)


def pending_dialogue_reply(queue: SpeechQueue, anchor_uid: str | None) -> bool:
    """True iff a queued reply anchored to ``anchor_uid`` still waits."""
    if not anchor_uid:
        return False
    return any(getattr(u, "anchor_uid", None) == anchor_uid
               for u in queue.pending())


# --------------------------------------------------------------------------
# Engine chain construction
# --------------------------------------------------------------------------

def detect_platform(platform: str | None = None) -> str:
    if platform is not None:
        return platform
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _apply_voice_kwargs(
    name: str,
    cls: type,
    kwargs: dict,
    effective_voice: str | None,
) -> dict:
    """Translate a configured voice id into per-engine constructor kwargs.

    A Kokoro voice id (``af_heart``, ``am_adam``, ...) must never disable a
    system fallback engine: engines whose constructors do not accept a
    ``voice`` parameter simply get no voice kwarg (they keep their own
    sensible defaults) instead of dying with a TypeError that the probe
    misreports as "engine unavailable".

    Mapping rules:
      - kokoro: pass the voice through verbatim (it IS a Kokoro voice id).
      - say (macOS): pass through verbatim — macOS voice names are free-form
        strings and ``say -v`` accepts any installed voice name.
      - sapi / piper / espeakng: no ``voice`` constructor param; the voice is
        intentionally not mapped (SAPI/piper pick their own default; piper's
        voice is the model file name, not a Kokoro voice id).
      - unknown engines: pass ``voice`` only when the constructor accepts it.
    """
    if not effective_voice:
        return kwargs
    if name == "kokoro":
        kwargs.setdefault("voice", effective_voice)
        return kwargs
    if name == "say":
        kwargs.setdefault("voice", effective_voice)
        return kwargs
    # Engines without a voice constructor param: leave them alone.
    try:
        import inspect

        params = inspect.signature(cls.__init__).parameters
        if "voice" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            kwargs.setdefault("voice", effective_voice)
    except (TypeError, ValueError):  # pragma: no cover - exotic constructors
        pass
    return kwargs


def build_speaker_chain(
    platform: str | None = None,
    config: dict | None = None,
    voice: str | None = None,
) -> Speaker:
    """Build the ordered AVAILABLE-engine chain for this platform as a Speaker.

    Chains (config["chains"][platform] overrides):
      windows: kokoro → sapi     darwin: kokoro → say     linux: kokoro → piper → espeakng

    Availability probe is a cheap constructor + available() check. Every
    engine that probes available is wrapped into a :class:`ChainedSpeaker`
    so a RUNTIME synthesis/playback failure falls through to the next engine
    (probe success is no guarantee the engine still works later). Raises
    RuntimeError when nothing on the chain is available.
    """
    cfg = config or {}
    plat = detect_platform(platform)
    chains_cfg = cfg.get("chains") or {}
    chain_names: tuple[str, ...] = tuple(chains_cfg.get(plat) or DEFAULT_CHAINS[plat])

    from .platform.tts import load_engine_class

    effective_voice = voice or cfg.get("tts_voice") or cfg.get("voice")
    tried: list[str] = []
    available_engines: list = []
    for name in chain_names:
        cls = load_engine_class(name)
        engines_cfg = cfg.get("engines") or {}
        kwargs = dict(engines_cfg.get(name) or {} if isinstance(engines_cfg, dict) else {})
        kwargs = _apply_voice_kwargs(name, cls, kwargs, effective_voice)
        try:
            engine = cls(**kwargs)
            ok = bool(engine.available())
        except Exception as exc:
            logger.debug("engine %s probe failed: %s", name, exc)
            ok = False
            engine = None
        tried.append(f"{name}({'ok' if ok else 'unavailable'})")
        if ok:
            available_engines.append(engine)

    if not available_engines:
        raise RuntimeError(f"no available TTS engine for platform {plat!r}; tried {tried}")

    logger.info(
        "TTS engine chain: %s (primary=%s)",
        [e.name for e in available_engines],
        available_engines[0].name,
    )
    if len(available_engines) == 1:
        return EngineSpeaker(available_engines[0])
    return ChainedSpeaker(available_engines)


class EngineSpeaker:
    """Adapts a TTSEngine to the interfaces.Speaker protocol."""

    def __init__(self, engine) -> None:
        from .platform.tts import TTSEngine

        if not isinstance(engine, TTSEngine):
            raise TypeError(f"expected TTSEngine subclass, got {type(engine)!r}")
        self.engine = engine

    def speak(self, utterance: Utterance) -> DeliveryResult:
        # Dual-booth: a per-utterance voice override must reach the engine
        # even when the engine lacks a per-call voice parameter — temporarily
        # stamp the engine default, then restore.
        override = getattr(utterance, "voice", None)
        if not override:
            return self.engine.speak(utterance)
        with _engine_voice_override(self.engine, override):
            return self.engine.speak(utterance)

    def set_voice(self, voice: str) -> None:
        if hasattr(self.engine, "set_voice"):
            self.engine.set_voice(voice)
        elif hasattr(self.engine, "voice"):
            self.engine.voice = voice

    def cancel(self) -> None:
        self.engine.cancel()

    def shutdown(self) -> None:
        self.engine.shutdown()


class _engine_voice_override:
    """Context manager: temporarily stamp ``voice`` as an engine's default.

    Engines that accept a per-call voice kwarg already honor ``utt.voice``
    via ``coerce()``; this covers the rest of the chain (piper/espeakng-style
    engines whose default voice attribute is their only knob). Restores the
    previous value even on failure.
    """

    def __init__(self, engine, voice: str) -> None:
        self._engine = engine
        self._voice = str(voice).strip()
        self._prev = None
        self._had_prev = False

    def __enter__(self):
        if hasattr(self._engine, "voice"):
            self._prev = self._engine.voice
            self._had_prev = True
            self._engine.voice = self._voice
        elif hasattr(self._engine, "set_voice"):
            try:
                self._prev = None
                self._had_prev = False
                self._engine.set_voice(self._voice)
            except Exception:
                pass
        return self._engine

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._had_prev:
                self._engine.voice = self._prev
        except Exception:
            pass
        return False


class ChainedSpeaker:
    """Speaker that walks an ordered engine list at speak time.

    build_speaker_chain() wraps every AVAILABLE-at-probe engine into this
    speaker so a runtime failure (engine dies, audio device vanishes, model
    load blows up) falls through to the next engine instead of dropping the
    utterance. Matches DESIGN §3.7's "layered fallback ... behind one narrow
    interface".

    Cancellation is NOT an engine failure: when the active engine reports the
    utterance was intentionally interrupted (canceled), the chain stops
    immediately and reports the delivery unsuccessful — a canceled line is
    never replayed or resumed by a fallback engine.
    """

    #: Reason substrings that mark a failed result as an intentional cancel
    #: rather than an engine fault (set by the engines themselves).
    CANCEL_MARKERS = ("cancelled", "canceled", "interrupted")

    def __init__(self, engines: list) -> None:
        from .platform.tts import TTSEngine

        if not engines or not all(isinstance(e, TTSEngine) for e in engines):
            raise TypeError("ChainedSpeaker expects non-empty TTSEngine list")
        self.engines = engines
        self._active = 0

    @property
    def engine(self):
        """Currently active engine (for diagnostics/tests)."""
        return self.engines[self._active]

    def set_voice(self, voice: str) -> None:
        for eng in self.engines:
            if hasattr(eng, "set_voice"):
                eng.set_voice(voice)
            elif hasattr(eng, "voice"):
                eng.voice = voice

    @classmethod
    def _is_cancellation(cls, reason: str | None) -> bool:
        if not reason:
            return False
        lowered = reason.lower()
        return any(marker in lowered for marker in cls.CANCEL_MARKERS)

    def speak(self, utterance: Utterance) -> DeliveryResult:
        reasons: list[str] = []
        override = getattr(utterance, "voice", None)
        for idx, eng in enumerate(self.engines):
            if override:
                with _engine_voice_override(eng, override):
                    result = eng.speak(utterance)
            else:
                result = eng.speak(utterance)
            if result.ok:
                self._active = idx
                return result
            reasons.append(f"{eng.name}: {result.reason}")
            if self._is_cancellation(result.reason):
                # Intentional interruption: do NOT fall through — a fallback
                # engine replaying a canceled line would defeat the cancel.
                logger.info(
                    "TTS engine %s reports cancellation for uid=%s; "
                    "not falling through chain",
                    eng.name, utterance.uid)
                return DeliveryResult(
                    uid=utterance.uid,
                    ok=False,
                    reason=result.reason or "cancelled",
                )
            logger.warning(
                "TTS engine %s failed (%s); falling through chain",
                eng.name, result.reason)
        return DeliveryResult(
            uid=utterance.uid,
            ok=False,
            reason="all engines failed: " + " | ".join(reasons),
        )

    def cancel(self) -> None:
        # Best effort: cancel every engine; engines not currently speaking
        # treat it as a noop (their cancel is scoped to in-flight work only).
        for eng in self.engines:
            eng.cancel()

    def shutdown(self) -> None:
        for eng in self.engines:
            eng.shutdown()



class FakeSpeaker:
    """Scriptable Speaker double covering every scenario in test_speech.py.

    Failure scripting: fail_next=N (next N calls fail), fail_rate+seed
    (stochastic), fail_uids (per-uid), outcomes={uid: DeliveryResult}
    (explicit, wins over all). Preemption: supports_cancel, cancel_delay,
    on_cancel hook. Timing: speak_delay simulates blocking synthesis;
    raise_on_uid drills the protocol-violation path.

    Observation surfaces: calls (every speak attempt), cancelled_calls,
    delivered_texts, results, last_result, cancel_requested, shut_down.
    reset() restores mutable state between phases.
    """

    def __init__(
        self,
        *,
        outcomes=None,
        fail_next=0,
        fail_rate=0.0,
        seed=0,
        raise_on_uid=None,
        supports_cancel=True,
        on_speak=None,
        on_cancel=None,
        speak_delay=0.0,
        cancel_delay=0.0,
        fail_reason="scripted failure",
        fail_uids=None,
    ):
        import random
        import threading

        self.outcomes = dict(outcomes or {})
        self.fail_next_remaining = int(fail_next)
        self.fail_rate = float(fail_rate)
        self.rng = random.Random(seed)
        self.raise_on_uid = raise_on_uid
        self.supports_cancel_flag = bool(supports_cancel)
        self.on_speak_cb = on_speak
        self.on_cancel_cb = on_cancel
        self.speak_delay_value = float(speak_delay)
        self.cancel_delay_value = float(cancel_delay)
        self.fail_reason_value = fail_reason
        self.fail_uids_set = set(fail_uids or ())
        self._lock = threading.Lock()

        # Observation surfaces
        self.calls = []
        self.cancelled_calls = []
        self.delivered_texts = []
        self.results = []
        self.last_result = None
        self.cancel_requested = 0
        self.shut_down = False
        # The pump stamps the in-flight utterance here so cancel() knows
        # exactly which call it belongs to.
        self._current_hint = None

    # -- Speaker protocol ---------------------------------------------------

    def speak(self, utterance):
        import time

        with self._lock:
            self.calls.append(utterance)
            uid = utterance.uid
            forced = None
            if uid in self.outcomes:
                forced = self.outcomes[uid]
            elif self.raise_on_uid is not None and uid == self.raise_on_uid:
                raise RuntimeError(f"scripted raise for {uid}")
            elif uid in self.fail_uids_set:
                forced = DeliveryResult(uid, False, f"scripted failure for {uid}")
            elif self.fail_next_remaining > 0:
                self.fail_next_remaining -= 1
                forced = DeliveryResult(uid, False, self.fail_reason_value)
            elif self.fail_rate > 0.0 and self.rng.random() < self.fail_rate:
                forced = DeliveryResult(uid, False, "random failure")

        if self.speak_delay_value > 0:
            time.sleep(self.speak_delay_value)

        if forced is not None:
            result = forced
        else:
            result = DeliveryResult(uid=uid, ok=True)

        with self._lock:
            self.results.append(result)
            self.last_result = result
            if result.ok:
                self.delivered_texts.append(utterance.text)
            if self.on_speak_cb is not None:
                self.on_speak_cb(utterance, result)
        return result

    def mark_current(self, utterance):
        """Called by the pump so cancel() knows the in-flight utterance."""
        with self._lock:
            self._current_hint = utterance

    def cancel(self):
        import time

        with self._lock:
            current = self._current_hint
            if current is not None and current not in self.cancelled_calls:
                self.cancelled_calls.append(current)
            self.cancel_requested += 1
            if self.on_cancel_cb is not None:
                self.on_cancel_cb()
            if current is not None:
                # A cancelled synthesis never delivers: flip its ok result.
                for i, r in enumerate(self.results):
                    if r.uid == current.uid and r.ok:
                        self.results[i] = DeliveryResult(
                            r.uid, False, "cancelled by preemption"
                        )
                        break
                if current.text in self.delivered_texts:
                    self.delivered_texts.remove(current.text)

    def shutdown(self):
        with self._lock:
            self.shut_down = True

    def reset(self):
        with self._lock:
            self.calls.clear()
            self.cancelled_calls.clear()
            self.delivered_texts.clear()
            self.results.clear()
            self.last_result = None
            self.cancel_requested = 0
            self._current_hint = None
