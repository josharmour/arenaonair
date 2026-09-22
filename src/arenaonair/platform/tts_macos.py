"""macOS TTS engines: Kokoro (shared) → the built-in ``say`` binary."""

from __future__ import annotations

import shutil
import subprocess

from .tts import TTSEngine


class SayEngine(TTSEngine):
    """macOS ``say`` synthesis; text piped over stdin to dodge argv limits."""

    name = "say"

    def __init__(self, voice: str | None = None, rate: int | None = None) -> None:
        self.voice = voice
        self.rate = rate
        self._proc: subprocess.Popen | None = None

    def available(self) -> bool:
        return shutil.which("say") is not None

    def set_voice(self, voice: str) -> None:
        """Update active macOS voice name."""
        self.voice = str(voice).strip()

    def synthesize(self, text: str, rate: float = 1.0, voice: str | None = None) -> None:
        cmd = ["say"]
        active_voice = voice or self.voice
        if active_voice:
            cmd += ["-v", active_voice]
        base_rate = self.rate if self.rate is not None else 185
        effective_rate = int(base_rate * rate)
        cmd += ["-r", str(effective_rate)]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._proc = proc
        _, err = proc.communicate(text)
        self._proc = None
        if proc.returncode != 0:
            raise RuntimeError(f"say exited {proc.returncode}: {(err or '').strip()}")

    def cancel(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
