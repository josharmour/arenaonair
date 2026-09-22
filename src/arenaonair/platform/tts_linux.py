"""Linux TTS engines: Piper (neural, optional) → espeak-ng (always-there fallback)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading

from .tts import TTSEngine


class PiperEngine(TTSEngine):
    """Piper neural TTS. Model dir overridable via ``PIPER_MODEL_DIR`` env.

    S7.5: piper renders a real WAV temp file which is then played through the
    same playback mechanism Kokoro uses on Linux (``aplay``); delivery only
    succeeds when playback COMPLETED with exit status 0.
    """

    name = "piper"

    def __init__(self, model_dir: str | None = None, voice: str | None = None,
                 player_bin: str | None = None) -> None:
        self.model_dir = model_dir or os.environ.get("PIPER_MODEL_DIR")
        self.voice = voice
        self.player_bin = player_bin  # optional explicit playback binary
        self._proc: subprocess.Popen | None = None
        self._play_proc: subprocess.Popen | None = None
        self._cancel_lock = threading.Lock()
        self._cancelled = False

    def available(self) -> bool:
        if shutil.which("piper") is None:
            return False
        return bool(self.model_dir)

    # -- synthesis + playback ------------------------------------------------

    def _synth_to_wav(self, text: str, rate: float) -> str:
        """Render ``text`` to a temp WAV via piper; returns the path."""
        model = os.path.join(self.model_dir or "", f"{self.voice or 'default'}.onnx")
        if not os.path.isfile(model):
            raise RuntimeError(f"piper: model not found: {model}")
        length_scale = max(0.5, min(2.0, 1.0 / rate))
        fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="piper-")
        os.close(fd)
        cmd = [
            "piper",
            "--model", model,
            "--output_file", wav_path,
            "--length_scale", f"{length_scale:.2f}",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._cancel_lock:
                if self._cancelled:
                    proc.kill()
                    raise InterruptedError("piper: synthesis cancelled")
                self._proc = proc
            _, err = proc.communicate(text)
            self._proc = None
            if proc.returncode != 0:
                raise RuntimeError(
                    f"piper exited {proc.returncode}: {(err or '').strip()}")
        except BaseException:
            self._cleanup_wav(wav_path)
            raise
        return wav_path

    def _play_wav(self, wav_path: str) -> None:
        """Block until the WAV finished playing; raise on playback failure."""
        player = self.player_bin or shutil.which("aplay")
        if not player:
            raise RuntimeError("piper: no playback binary (aplay) found")
        proc = subprocess.Popen(
            [player, "-q", wav_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with self._cancel_lock:
            if self._cancelled and proc.poll() is None:
                proc.kill()
            else:
                self._play_proc = proc
        _, err = proc.communicate()
        self._play_proc = None
        if proc.returncode != 0:
            detail = (err or b"").decode(errors="replace").strip()
            raise RuntimeError(
                f"piper: playback exited {proc.returncode}"
                f"{': ' + detail if detail else ''}")

    @staticmethod
    def _cleanup_wav(wav_path: str) -> None:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

    def synthesize(self, text: str, rate: float = 1.0) -> None:
        """Synthesize to WAV, play it, confirm completion; clean up always."""
        with self._cancel_lock:
            self._cancelled = False
        wav_path = self._synth_to_wav(text, rate)
        try:
            self._play_wav(wav_path)
        finally:
            self._cleanup_wav(wav_path)

    def cancel(self) -> None:
        with self._cancel_lock:
            self._cancelled = True
            procs = (self._proc, self._play_proc)
        for proc in procs:
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
