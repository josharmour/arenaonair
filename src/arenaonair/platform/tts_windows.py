"""Windows TTS engines: Kokoro (shared) → SAPI via PowerShell."""

from __future__ import annotations

import shutil
import subprocess

from .tts import TTSEngine


class SapiEngine(TTSEngine):
    """Windows SAPI speech synthesized through a PowerShell one-liner.

    Windows-only by construction: ``available()`` is False everywhere else.
    """

    name = "sapi"

    def __init__(self, rate: int = 0) -> None:
        self.rate = rate
        self._proc: subprocess.Popen | None = None

    def available(self) -> bool:
        if not sys.platform.startswith("win"):
            return False
        return shutil.which("powershell.exe") is not None

    def synthesize(self, text: str) -> None:
        ps_script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate={int(self.rate)}; "
            "$s.Speak([Console]::In.ReadToEnd())"
        )
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script]
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
            raise RuntimeError(f"powershell exited {proc.returncode}: {(err or '').strip()}")

    def cancel(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()


import sys  # noqa: E402  (kept at bottom so the module docstring stays first)
