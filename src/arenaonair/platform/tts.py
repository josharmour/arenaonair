"""Shared TTS plumbing + the Kokoro engine (common to every OS).

Concrete per-OS engines live in ``tts_windows`` / ``tts_macos`` / ``tts_linux``
(DESIGN.md §2 constraint #6). Every engine exposes:

    available() -> bool     cheap probe; no side effects beyond an import/which
    speak(text_or_utterance) -> DeliveryResult   blocking; never raises
    cancel()                best-effort interrupt of an in-flight synthesis

Engines accept either a plain string or an :class:`Utterance`; the uid travels
with the result so delivery stays confirmable end to end.
"""

from __future__ import annotations

import importlib.util
import subprocess  # noqa: F401  (re-exported convenience for subclasses)
import sys
from typing import Union

from ..models import DeliveryResult, Utterance

SpeakSource = Union[str, Utterance]


def coerce(source: SpeakSource) -> tuple[str, str]:
    """(text, uid) out of either an Utterance or a bare string."""
    if isinstance(source, Utterance):
        return source.text, source.uid
    return str(source), ""


class TTSEngine:
    """Base class wrapping a concrete synthesizer with delivery-result semantics."""

    name = "tts"

    def available(self) -> bool:
        raise NotImplementedError

    def speak(self, source: SpeakSource) -> DeliveryResult:
        text, uid = coerce(source)
        try:
            self.synthesize(text)
        except Exception as exc:  # engines never raise past the caller
            return DeliveryResult(
                uid=uid,
                ok=False,
                reason=f"{self.name}: synthesis failed: {exc}",
            )
        return DeliveryResult(uid=uid, ok=True)

    def synthesize(self, text: str) -> None:
        """Blocking synthesis of one utterance; raise on any failure."""
        raise NotImplementedError

    def cancel(self) -> None:
        """Best-effort interrupt of the current synthesis (default noop)."""

    def shutdown(self) -> None:
        """Release any held resources."""


class KokoroEngine(TTSEngine):
    """Neural TTS via the optional ``kokoro`` pip package.

    Optional by design (DESIGN §3.7): missing package simply means the chain
    falls through to the next engine.
    """

    name = "kokoro"

    def __init__(self, voice: str = "af_heart", speed: float = 1.0) -> None:
        self.voice = voice
        self.speed = speed
        self._pipeline = None

    def available(self) -> bool:
        return importlib.util.find_spec("kokoro") is not None

    def synthesize(self, text: str) -> None:
        if self._pipeline is None:
            from kokoro import KPipeline  # heavy import deferred

            lang = "z" if sys.platform.startswith("win") else "a"
            self._pipeline = KPipeline(lang_code=lang)
        # KPipeline yields generator chunks; consuming them drives synthesis.
        chunks = list(self._pipeline(text, voice=self.voice, speed=self.speed))
        if not chunks:
            raise RuntimeError("kokoro produced no audio")

    def shutdown(self) -> None:
        self._pipeline = None


def load_engine_class(name: str) -> type[TTSEngine]:
    """Resolve an engine short name to its class via the registry."""
    try:
        module_name, cls_name = ENGINE_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown TTS engine {name!r}") from exc
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)


ENGINE_REGISTRY: dict[str, tuple[str, str]] = {
    "kokoro": ("arenaonair.platform.tts", "KokoroEngine"),
    "sapi": ("arenaonair.platform.tts_windows", "SapiEngine"),
    "say": ("arenaonair.platform.tts_macos", "SayEngine"),
    "piper": ("arenaonair.platform.tts_linux", "PiperEngine"),
    "espeakng": ("arenaonair.platform.tts_linux", "EspeakNgEngine"),
}
