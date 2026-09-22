"""Linux TTS engines: Piper (neural, optional) → espeak-ng (always-there fallback)."""

from __future__ import annotations

import os
import shutil
import subprocess

from .tts import TTSEngine


class PiperEngine(TTSEngine):
    """Piper neural TTS. Model dir overridable via ``PIPER_MODEL_DIR`` env."""

    name = "piper"

    def __init__(self, model_dir: str | None = None, voice: str | None = None) -> None:
        self.model_dir = model_dir or os.environ.get("PIPER_MODEL_DIR")
        self.voice = voice
        self._proc: subprocess.Popen | None = None

    def available(self) -> bool:
        if shutil.which("piper") is None:
            return False
        return bool(self.model_dir)

    def synthesize(self, text: str, rate: float = 1.0) -> None:
        if not self.model_dir:
            raise RuntimeError("piper: no model dir configured")
        model = os.path.join(self.model_dir, f"{self.voice or 'default'}.onnx")
        if not os.path.isfile(model):
            raise RuntimeError(f"piper: model not found: {model}")
        length_scale = max(0.5, min(2.0, 1.0 / rate))
        cmd = ["piper", "--model", model, "--output_file", "/dev/null",
               "--length_scale", f"{length_scale:.2f}"]
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
            raise RuntimeError(f"piper exited {proc.returncode}: {(err or '').strip()}")

    def cancel(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()


class EspeakNgEngine(TTSEngine):
    """espeak-ng: the last link of the Linux chain; ships in every distro repo."""

    name = "espeakng"

    def __init__(self, rate: int = 175, pitch: int = 50) -> None:
        self.rate = rate
        self.pitch = pitch
        self._proc: subprocess.Popen | None = None

    def available(self) -> bool:
        return shutil.which("espeak-ng") is not None

    def synthesize(self, text: str, rate: float = 1.0) -> None:
        cmd = [
            "espeak-ng",
            "-s", str(int(self.rate * rate)),
            "-p", str(int(self.pitch)),
            "--stdin",
        ]
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
            raise RuntimeError(f"espeak-ng exited {proc.returncode}: {(err or '').strip()}")

    def cancel(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
