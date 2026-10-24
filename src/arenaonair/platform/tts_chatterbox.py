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
import time

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
        dtype: str | None = None,
    ) -> None:
        # KokoroEngine.__init__ only sets plain attributes + locks; it never
        # imports kokoro. voice doubles as the default reference audio.
        # device=None means AUTO here: pass "auto" through (the parent would
        # otherwise coerce None to "cpu" before _get_model ever sees it).
        super().__init__(voice=voice or "", speed=1.0,
                         device=device or "auto", player_bin=player_bin)
        self.exaggeration = float(exaggeration)
        self.cfg_weight = float(cfg_weight)
        self.temperature = float(temperature)
        self.reference_audio = reference_audio or None
        self.dtype = str(dtype).lower() if dtype else None

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        import importlib.util

        return (importlib.util.find_spec("chatterbox") is not None
                and importlib.util.find_spec("torch") is not None)

    # -- model -------------------------------------------------------------

    def _get_model(self):
        """Lazy ChatterboxTTS load; device auto-selects CUDA when present.

        A CUDA load that OOMs (GPU shared with the LLM server, for example)
        falls back to CPU once and remembers it — a shared GPU must never
        take the booth down.
        """
        with self._pipeline_lock:
            if self._pipeline is None:
                import torch
                from chatterbox.tts import ChatterboxTTS

                device = self.device
                if device in (None, "", "auto"):
                    if torch.cuda.is_available():
                        device = "cuda"
                    elif getattr(torch.backends, "mps", None) is not None \
                            and torch.backends.mps.is_available():
                        device = "mps"  # Apple Silicon
                    else:
                        device = "cpu"
                logger.info("loading chatterbox model on %s", device)
                try:
                    self._pipeline = ChatterboxTTS.from_pretrained(device=device)
                except torch.OutOfMemoryError:
                    if not device.startswith("cuda"):
                        raise
                    logger.warning(
                        "chatterbox: CUDA OOM loading model (%s busy?); "
                        "falling back to CPU", device)
                    device = "cpu"
                    self._pipeline = ChatterboxTTS.from_pretrained(device=device)
                self.device = device
                self._apply_dtype()
            return self._pipeline

    def _apply_dtype(self) -> None:
        """Optionally cast the model to a lower precision (speed on MPS/CUDA).

        ``[tts_engines.chatterbox] dtype = "bfloat16"`` halves compute and
        memory; quality is very close but UNVERIFIED by ear — opt-in until
        listened to. Unknown names are ignored with a warning.
        """
        if not self.dtype or self._pipeline is None:
            return
        import torch

        torch_dtype = getattr(torch, self.dtype, None)
        if torch_dtype is None or not torch_dtype.is_floating_point:
            logger.warning("chatterbox: unknown dtype %r; staying float32",
                           self.dtype)
            return
        casted = []
        for part in ("t3", "s3gen", "ve"):
            module = getattr(self._pipeline, part, None)
            if module is not None:
                module.to(dtype=torch_dtype)
                casted.append(part)
        logger.info("chatterbox: cast %s to %s", "+".join(casted) or "nothing",
                    self.dtype)

    def warmup(self) -> bool:
        """Load the model and run one throwaway synthesis.

        Called on a daemon thread at app start: pays the one-time model load
        plus kernel/vocoder warm-up up front so the first game-end call costs
        only synthesis. Never raises; returns True when warm.
        """
        try:
            started = time.monotonic()
            model = self._get_model()
            wav = model.generate(
                "Ready.",
                audio_prompt_path=self._resolve_reference(None),
                exaggeration=self.exaggeration,
                cfg_weight=self.cfg_weight,
                temperature=self.temperature,
            )
            del wav
            logger.info("chatterbox warm on %s in %.1fs", self.device,
                        time.monotonic() - started)
            return True
        except Exception as exc:
            logger.warning(
                "chatterbox warmup failed (%s); the first hype line may be late",
                exc)
            return False

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
        if hasattr(wav, "detach"):  # torch.Tensor
            wav = wav.detach().cpu().numpy()
        pcm = np.asarray(wav, dtype=np.float32).reshape(-1)
        if pcm.size == 0:
            raise RuntimeError("chatterbox produced no audio")
        if self._is_cancelled(generation):
            raise InterruptedError("chatterbox: synthesis cancelled before playback")
        self._play_chunk(pcm, sr, generation=generation)

    def shutdown(self) -> None:
        self._pipeline = None
