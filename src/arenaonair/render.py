"""Render a two-voice commentary script to a WAV file with Kokoro (no playback).

Used by recaps and ``tools/render_commentary.py``. Requires the ``tts`` extra.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import wave


def kokoro_available() -> bool:
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("kokoro", "numpy"))


def render(transcript, out, *, speed=1.0, gap=.4, engine=None):
    import numpy as np
    from .platform.tts import KokoroEngine

    if not transcript:
        raise ValueError('Replay has no delivered commentary to render')
    if not .5 <= speed <= 2 or not 0 <= gap <= 5:
        raise ValueError('Invalid speed or gap')
    for turn in transcript:
        if not isinstance(turn.get('text'), str) or not turn['text'].strip() or not turn.get('voice'):
            raise ValueError('Every turn needs text and an explicit voice')
    engine = engine or KokoroEngine(device='cpu', lang_code='a')
    voices = list(dict.fromkeys(t['voice'] for t in transcript))
    if engine.preload_voices(voices) != len(voices):
        raise RuntimeError('Could not load all selected booth voices')
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cues, frames, sample_rate = [], 0, 24000
    silence = np.zeros(round(gap * sample_rate), dtype='<i2').tobytes()
    with tempfile.TemporaryDirectory(dir=out.parent, prefix='commentary-') as temporary:
        stage = Path(temporary) / 'audio.wav'
        with wave.open(str(stage), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            for i, turn in enumerate(transcript):
                if i:
                    wav.writeframes(silence)
                    frames += len(silence) // 2
                start = frames
                for pcm, sr in engine._synthesize_pcm(turn['text'], rate=speed, voice=turn['voice']):
                    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
                    if sr != sample_rate or not np.isfinite(pcm).all():
                        raise ValueError('Invalid synthesized audio')
                    wav.writeframes((np.clip(pcm, -1, 1) * 32767).astype('<i2').tobytes())
                    frames += len(pcm)
                if frames == start:
                    raise RuntimeError('Synthesis produced an empty turn')
                cues.append(dict(role=turn['role'], voice=turn['voice'], text=turn['text'],
                                 start=round(start/sample_rate, 3), end=round(frames/sample_rate, 3)))
                print(f'Rendered {i+1}/{len(transcript)} turns ({frames/sample_rate:.1f}s)', flush=True)
        os.chmod(stage, 0o600)
        stage.replace(out)
    return dict(sample_rate=sample_rate, channels=1, duration_seconds=frames/sample_rate, turns=cues)
