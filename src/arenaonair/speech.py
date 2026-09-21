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
from dataclasses import dataclass, field

from .interfaces import Speaker
from .models import DeliveryResult, Utterance

logger = logging.getLogger(__name__)

# Salience scale mirrors events.py; duplicated numerically to keep speech.py
# decoupled from the differ's constant table.
SALIENCE_FILLER = 0
SALIENCE_LOW = 1
SALIENCE_HIGH = 2
SALIENCE_MUST_SPEAK = 3

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
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Utterance] = []
        self._uids: set[str] = set()
        self._flushed_matches: set[str | None] = set()
        self._active_match: str | None = None

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
        return u.match_id != self._active_match


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
    """

    queue: SpeechQueue
    speaker: Speaker
    results: list[DeliveryResult] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stopped: bool = False

    # -- public API -------------------------------------------------------

    def run_once(self) -> DeliveryResult | None:
        """One dequeue→speak→confirm cycle. Returns the result or None."""
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
        import time

        while not self.stopped:
            spoke = self.run_once()
            if spoke is None:
                time.sleep(poll_interval)

    def stop(self) -> None:
        self.stopped = True

    # -- bookkeeping ------------------------------------------------------

    def record(self, result: DeliveryResult, utt: Utterance) -> None:
        """Confirm delivery; loud WARNING on any failure (never silent)."""
        with self.lock:
            self.results.append(result)
        if not result.ok:
            logger.warning(
                "speech delivery FAILED uid=%s kind=%s reason=%s text=%r",
                result.uid,
                utt.kind,
                result.reason or "(no reason given)",
                utt.text,
            )


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


def build_speaker_chain(
    platform: str | None = None,
    config: dict | None = None,
) -> Speaker:
    """Build the first AVAILABLE engine for this platform as a Speaker.

    Chains (config["chains"][platform] overrides):
      windows: kokoro → sapi     darwin: kokoro → say     linux: kokoro → piper → espeakng

    Availability probe is a cheap constructor + available() check. Raises
    RuntimeError when nothing on the chain is available.
    """
    cfg = config or {}
    plat = detect_platform(platform)
    chains_cfg = cfg.get("chains") or {}
    chain_names: tuple[str, ...] = tuple(chains_cfg.get(plat) or DEFAULT_CHAINS[plat])

    from .platform.tts import load_engine_class

    tried: list[str] = []
    for name in chain_names:
        cls = load_engine_class(name)
        engines_cfg = cfg.get("engines") or {}
        kwargs = engines_cfg.get(name) or {} if isinstance(engines_cfg, dict) else {}
        try:
            engine = cls(**kwargs)
            ok = bool(engine.available())
        except Exception as exc:
            logger.debug("engine %s probe failed: %s", name, exc)
            ok = False
        tried.append(f"{name}({'ok' if ok else 'unavailable'})")
        if ok:
            logger.info("TTS engine selected: %s (chain=%s)", name, chain_names)
            return EngineSpeaker(engine)

    raise RuntimeError(f"no available TTS engine for platform {plat!r}; tried {tried}")


class EngineSpeaker:
    """Adapts a TTSEngine to the interfaces.Speaker protocol."""

    def __init__(self, engine) -> None:
        from .platform.tts import TTSEngine

        if not isinstance(engine, TTSEngine):
            raise TypeError(f"expected TTSEngine subclass, got {type(engine)!r}")
        self.engine = engine

    def speak(self, utterance: Utterance) -> DeliveryResult:
        return self.engine.speak(utterance)

    def cancel(self) -> None:
        self.engine.cancel()

    def shutdown(self) -> None:
        self.engine.shutdown()



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
