"""Chatterbox engine (Resemble AI, MIT) — the "hype" voice.

Chatterbox is a zero-shot TTS with an explicit ``exaggeration`` control
(~0.25 neutral newsreader → 0.7+ sports-hype). This engine lets the booth
route HIGH-salience moments (game/match end, electric swings) through it
while Kokoro keeps latency-sensitive play-by-play (see speech.py routing).

Design notes:

- Subclasses :class:`KokoroEngine` purely to REUSE its OS playback dispatch
  (aplay/afplay/sounddevice), PCM→WAV conversion, and generation-scoped
  cancellation bookkeeping. All synthesis/model code is overridden; no
  kokoro import ever happens on this path.
- The model load is deferred to first synthesis (heavy import + checkpoint
  download on first run); ``available()`` stays a cheap find_spec probe so a
  missing install simply means the chain skips this engine.
- ``voice`` means a REFERENCE AUDIO path (or alias registered in
  ``REFERENCE_ALIASES``), not a Kokoro voice id. ``_utterance_for_engine``
  already strips ``af_/am_/bm_/bf_`` ids for non-kokoro engines, so a Kokoro
  booth voice never leaks in as a bogus path.
- Cancellation: Chatterbox's ``generate()`` is a blocking single-shot call we
  cannot interrupt mid-flight, so a cancel keeps the synthesized audio from
  ever PLAYING (checked before playback, same contract as Kokoro's
  "a cancel only counts once it keeps a chunk from playing").
"""

from __future__ import annotations

import logging
import os

from .tts import KokoroEngine

logger = logging.getLogger(__name__)

#: Friendly alias -> reference wav path. Paths may contain ``~``. A missing
#: file falls back to the model's built-in default voice with a WARNING,
#: never a hard failure (a bad alias must not take down the booth).
REFERENCE_ALIASES: dict[str, str] = {
    # "hype": "~/arenaonair/voices/hype_ref.wav",
}


class ChatterboxEngine(KokoroEngine):
    """High-energy TTS via the optional ``chatterbox-tts`` package."""

    name = "chatterbox"

    def __init__(
        self,
        exaggeration: float = 0.65,
        cfg_weight: float = 0.4,
        temperature: float = 0.8,
        device: str | None = None,
        reference_audio: str | None = None,
        voice: str | None = None,
        player_bin: str | None = None,
    ) -> None:
        # KokoroEngine.__init__ only sets plain attributes + locks; it never
        # imports kokoro. voice doubles as the default reference audio.
        super().__init__(voice=voice or "", speed=1.0, device=device,
                         player_bin=player_bin)
        self.exaggeration = float(exaggeration)
        self.cfg_weight = float(cfg_weight)
        self.temperature = float(temperature)
        self.reference_audio = reference_audio or None

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        import importlib.util

        return (importlib.util.find_spec("chatterbox") is not None
                and importlib.util.find_spec("torch") is not None)

    # -- model -------------------------------------------------------------

    def _get_model(self):
        """Lazy ChatterboxTTS load; device auto-selects CUDA when present."""
        with self._pipeline_lock:
            if self._pipeline is None:
                import torch
                from chatterbox.tts import ChatterboxTTS

                device = self.device
                if device in (None, "", "auto"):
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                logger.info("loading chatterbox model on %s", device)
                self._pipeline = ChatterboxTTS.from_pretrained(device=device)
            return self._pipeline

    def _resolve_reference(self, voice: str | None) -> str | None:
        """Reference wav path for this utterance, or None for model default."""
        candidate = voice or self.voice or self.reference_audio
        if not candidate:
            return None
        candidate = str(candidate).strip()
        aliased = REFERENCE_ALIASES.get(candidate)
        if aliased:
            candidate = aliased
        path = os.path.expanduser(candidate)
        if os.path.isfile(path):
            return path
        logger.warning(
            "chatterbox reference audio %r not found; using model default voice",
            candidate,
        )
        return None

    # -- synthesis ---------------------------------------------------------

    def synthesize(self, text: str, rate: float = 1.0, voice: str | None = None) -> None:
        """Synthesize + play one utterance; raise on failure or cancellation."""
        import numpy as np

        generation = self._begin_utterance()
        model = self._get_model()
        wav = model.generate(
            text,
            audio_prompt_path=self._resolve_reference(voice),
            exaggeration=self.exaggeration,
            cfg_weight=self.cfg_weight,
            temperature=self.temperature,
        )
        sr = int(getattr(model, "sr", 24000))
        pcm = np.asarray(wav.squeeze().cpu().numpy(), dtype=np.float32)
        if pcm.size == 0:
            raise RuntimeError("chatterbox produced no audio")
        if self._is_cancelled(generation):
            raise InterruptedError("chatterbox: synthesis cancelled before playback")
        self._play_chunk(pcm, sr, generation=generation)

    def shutdown(self) -> None:
        self._pipeline = None
