"""Abstract interfaces binding the ArenaOnAir pipeline stages together.

Implementations live in their own modules; these protocols are the seam used by
tests (fake speakers, fake watchers) and by app.py wiring.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from .models import DeliveryResult, Event, GameState, GreMessage, Utterance


@runtime_checkable
class LogSource(Protocol):
    """Produces raw log lines in order. Implemented by watcher.py."""

    def lines(self) -> Iterable[tuple[float, str]]:
        """Yield (ts, raw_line) as they arrive. Blocking iterator."""
        ...


@runtime_checkable
class Parser(Protocol):
    def parse_line(self, ts: float, raw: str) -> GreMessage | None:
        """Pure: one log line in → zero or one message out. Never raises."""
        ...


@runtime_checkable
class StateBuilder(Protocol):
    def apply(self, state: GameState | None, msg: GreMessage) -> GameState | None:
        """Return the next snapshot (immutable) or None to skip the message."""
        ...


@runtime_checkable
class Differ(Protocol):
    def diff(self, prev: GameState | None, cur: GameState,
             msgs_since_prev: list[GreMessage]) -> list[Event]:
        """Snapshot pair + intervening messages → ordered events."""
        ...


@runtime_checkable
class StoryModel(Protocol):
    def update(self, state: GameState) -> list[Event]:
        """Fold a new snapshot into the story; return narrative events."""
        ...


@runtime_checkable
class Narrator(Protocol):
    def render(self, event: Event, state: GameState | None) -> Utterance | None:
        """Event → utterance. Return None to suppress (silence discipline)."""
        ...


@runtime_checkable
class Speaker(Protocol):
    def speak(self, utterance: Utterance) -> DeliveryResult:
        """Blocking speak. Must return an outcome; never raise past caller."""
        ...

    def shutdown(self) -> None: ...
