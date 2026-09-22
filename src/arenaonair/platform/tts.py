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
import os
import subprocess  # noqa: F401  (re-exported convenience for subclasses)
import sys
from typing import Union

from ..models import DeliveryResult, Utterance

SpeakSource = Union[str, Utterance]

#: Comprehensive catalog of default Kokoro-82M voices
KOKORO_VOICES: dict[str, str] = {
    # American Female (af)
    "af_heart": "American Female - Heart (Default warm broadcast caster)",
    "af_alloy": "American Female - Alloy",
    "af_aoede": "American Female - Aoede",
    "af_bella": "American Female - Bella",
    "af_jessica": "American Female - Jessica",
    "af_kore": "American Female - Kore",
    "af_nicole": "American Female - Nicole",
    "af_nova": "American Female - Nova",
    "af_river": "American Female - River",
    "af_sarah": "American Female - Sarah",
    "af_sky": "American Female - Sky",
    # American Male (am)
    "am_adam": "American Male - Adam (Crisp sports shoutcaster)",
    "am_echo": "American Male - Echo",
    "am_eric": "American Male - Eric",
    "am_fenrir": "American Male - Fenrir",
    "am_liam": "American Male - Liam",
    "am_michael": "American Male - Michael",
    "am_onyx": "American Male - Onyx (Deep radio broadcast)",
    "am_puck": "American Male - Puck",
    "am_santa": "American Male - Santa",
    # British Female (bf)
    "bf_alice": "British Female - Alice",
    "bf_emma": "British Female - Emma",
    "bf_isabella": "British Female - Isabella",
    "bf_lily": "British Female - Lily",
    # British Male (bm)
    "bm_daniel": "British Male - Daniel",
    "bm_fable": "British Male - Fable",
    "bm_george": "British Male - George",
    "bm_lewis": "British Male - Lewis",
}


def list_kokoro_voices() -> dict[str, str]:
    """Return dictionary of all default Kokoro voices and descriptions."""
    return dict(KOKORO_VOICES)


def coerce(source: SpeakSource) -> tuple[str, str, float, str | None]:
    """(text, uid, rate, voice) out of either an Utterance or a bare string."""
    if isinstance(source, Utterance):
        return (
            source.text,
            source.uid,
            float(getattr(source, "rate", 1.0) or 1.0),
            getattr(source, "voice", None),
        )
    return str(source), "", 1.0, None


class TTSEngine:
    """Base class wrapping a concrete synthesizer with delivery-result semantics."""

    name = "tts"

    def available(self) -> bool:
        raise NotImplementedError

    def speak(self, source: SpeakSource) -> DeliveryResult:
        text, uid, rate, voice = coerce(source)
        try:
            try:
                self.synthesize(text, rate=rate, voice=voice)
            except TypeError:
                try:
                    self.synthesize(text, rate=rate)
                except TypeError:
                    self.synthesize(text)
        except Exception as exc:  # engines never raise past the caller
            return DeliveryResult(
                uid=uid,
                ok=False,
                reason=f"{self.name}: synthesis failed: {exc}",
            )
        return DeliveryResult(uid=uid, ok=True)

    def synthesize(self, text: str, rate: float = 1.0, voice: str | None = None) -> None:
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

    Playback: synthesis yields float32 PCM at 24 kHz; playback dispatches per
    OS — Windows via sounddevice WASAPI, macOS via afplay on a temp wav,
    Linux via aplay (ALSA) with a PulseAudio-friendly temp-wav fallback.
    ``device='cpu'`` by default: Kokoro-82M is small enough for CPU narration
    and avoids grabbing VRAM on GPU-saturated hosts (override via kwargs).
    """

    name = "kokoro"
    SAMPLE_RATE = 24000

    def __init__(self, voice: str = "af_heart", speed: float = 1.0,
                 device: str | None = None, lang_code: str | None = None,
                 player_bin: str | None = None) -> None:
        self.voice = voice
        self.speed = speed
        self.device = device or "cpu"
        self.lang_code = lang_code or ("z" if sys.platform.startswith("win")
                                       else "a")
        self.player_bin = player_bin  # optional explicit playback binary
        self._pipeline = None
        self._cancel_flag = False

    def available(self) -> bool:
        return importlib.util.find_spec("kokoro") is not None

    def set_voice(self, voice: str) -> None:
        """Update active voice name (e.g. 'af_heart', 'am_adam', 'bm_george')."""
        self.voice = str(voice).strip()

    # -- synthesis ---------------------------------------------------------

    def _get_pipeline(self):
        if self._pipeline is None:
            from kokoro import KPipeline  # heavy import deferred

            self._pipeline = KPipeline(lang_code=self.lang_code,
                                       device=self.device)
        return self._pipeline

    def _synthesize_pcm(self, text: str, rate: float = 1.0, voice: str | None = None):
        """Yield (numpy_float32_array, sample_rate) chunks for ``text``."""
        import numpy as np

        pipe = self._get_pipeline()
        effective_speed = max(0.5, min(2.0, self.speed * rate))
        active_voice = voice or self.voice
        for chunk in pipe(text, voice=active_voice, speed=effective_speed):
            if self._cancel_flag:
                return
            audio = getattr(chunk, "audio", None)
            if audio is None:
                continue
            yield np.asarray(audio, dtype=np.float32), self.SAMPLE_RATE

    # -- playback per OS ----------------------------------------------------

    def _play_chunk(self, pcm, sr: int) -> None:
        """Blocking playback of one PCM chunk; honors cancel flag."""
        if self._cancel_flag:
            return
        if sys.platform == "win32":
            self._play_sounddevice(pcm, sr)
        elif sys.platform == "darwin":
            self._play_afplay(pcm, sr)
        else:
            self._play_aplay(pcm, sr)

    @staticmethod
    def _pcm_to_wav_bytes(pcm, sr: int) -> bytes:
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes((pcm.clip(-1.0, 1.0) * 32767.0)
                           .astype("<i2").tobytes())
        return buf.getvalue()

    def _play_sounddevice(self, pcm, sr: int) -> None:
        import sounddevice as sd  # optional dep on Windows

        sd.play(pcm.reshape(-1, 1), samplerate=sr, blocking=True)

    def _play_afplay(self, pcm, sr: int) -> None:
        import subprocess
        import tempfile

        wav = self._pcm_to_wav_bytes(pcm, sr)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(wav)
            path = tf.name
        try:
            subprocess.run([self.player_bin or "afplay", path],
                           check=False,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _play_aplay(self, pcm, sr: int) -> None:
        import shutil
        import subprocess
        import tempfile

        player = self.player_bin or shutil.which("aplay")
        if not player:
            raise RuntimeError("kokoro: no playback binary (aplay) found")
        wav = self._pcm_to_wav_bytes(pcm, sr)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(wav)
            path = tf.name
        try:
            proc = subprocess.run(
                [player, "-q", path],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"kokoro: playback exited {proc.returncode} "
                    f"(no audio device?)")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- TTSEngine surface ---------------------------------------------------

    def synthesize(self, text: str, rate: float = 1.0, voice: str | None = None) -> None:
        chunks_played = 0
        for pcm, sr in self._synthesize_pcm(text, rate=rate, voice=voice):
            self._play_chunk(pcm, sr)
            chunks_played += 1
        if chunks_played == 0 and not self._cancel_flag:
            raise RuntimeError("kokoro produced no audio")

    def cancel(self) -> None:
        self._cancel_flag = True

    def shutdown(self) -> None:
        self._pipeline = None
        self._cancel_flag = False


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
