"""Chatterbox hype engine: registration, routing, and synthesis contract.

The engine itself is optional (chatterbox-tts not installed on CI), so every
test either probes cheap surfaces (registry/chains/availability) or drives
synthesis through a stubbed model — never a real checkpoint download.
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from arenaonair import speech as speech_mod
from arenaonair.models import Utterance
from arenaonair.platform import tts as tts_mod
from arenaonair.platform.tts import TTSEngine
from arenaonair.platform.tts_chatterbox import ChatterboxEngine
from arenaonair.speech import (
    DEFAULT_CHAINS,
    HYPE_KINDS,
    HYPE_MIN_SALIENCE,
    ChainedSpeaker,
    build_speaker_chain,
)


def utt(kind="land_drop", salience=1, excitement="normal", uid="u1",
        voice=None, text="He plays a land."):
    return Utterance(
        uid=uid, match_id="m1", kind=kind, text=text, salience=salience,
        ts_created=0.0, excitement=excitement, voice=voice,
    )


class RecordingEngine(TTSEngine):
    """Scriptable chain member: records speak() calls, optionally fails."""

    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.spoken: list[str] = []

    def available(self) -> bool:
        return True

    def synthesize(self, text: str, rate: float = 1.0, voice=None) -> None:
        if self.fail:
            raise RuntimeError(f"{self.name} exploded")
        self.spoken.append(text)


def make_chain(routing=None, hype_fail=False):
    kokoro = RecordingEngine("kokoro")
    hype = RecordingEngine("chatterbox", fail=hype_fail)
    fallback = RecordingEngine("piper")
    return ChainedSpeaker([kokoro, hype, fallback], routing=routing), \
        kokoro, hype, fallback


class TestRegistration:
    def test_registry_resolves_chatterbox(self):
        assert tts_mod.load_engine_class("chatterbox") is ChatterboxEngine

    def test_default_chains_include_chatterbox_behind_kokoro(self):
        for plat, chain in DEFAULT_CHAINS.items():
            assert "chatterbox" in chain, plat
            assert chain.index("kokoro") < chain.index("chatterbox"), plat

    def test_available_false_without_package(self, monkeypatch):
        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, *a, **k: None)
        assert ChatterboxEngine().available() is False

    def test_available_requires_torch_too(self, monkeypatch):
        real = importlib.util.find_spec

        def fake(name, *a, **k):
            return None if name == "torch" else real(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake)
        assert ChatterboxEngine().available() is False


class TestHypeRouting:
    def test_high_salience_tries_hype_first(self):
        speaker, kokoro, hype, _ = make_chain()
        assert speaker.speak(utt(salience=HYPE_MIN_SALIENCE)).ok
        assert hype.spoken == ["He plays a land."] and kokoro.spoken == []

    def test_low_salience_keeps_chain_order(self):
        speaker, kokoro, hype, _ = make_chain()
        assert speaker.speak(utt(salience=speech_mod.SALIENCE_LOW)).ok
        assert kokoro.spoken == ["He plays a land."] and hype.spoken == []

    @pytest.mark.parametrize("excitement", ["electric", "tense"])
    def test_excited_levels_route_when_opted_in(self, excitement):
        speaker, kokoro, hype, _ = make_chain(
            routing={"excitement": ("electric", "tense")})
        assert speaker.speak(utt(salience=0, excitement=excitement)).ok
        assert hype.spoken and not kokoro.spoken

    def test_excitement_routing_off_by_default(self):
        # CPU-speed synthesis lags mid-play lines; excitement routing is opt-in.
        speaker, kokoro, hype, _ = make_chain()
        assert speaker.speak(utt(salience=0, excitement="electric")).ok
        assert kokoro.spoken and not hype.spoken

    def test_default_threshold_is_must_speak(self):
        assert HYPE_MIN_SALIENCE == speech_mod.SALIENCE_MUST_SPEAK

    def test_calm_excitement_does_not_route(self):
        speaker, kokoro, hype, _ = make_chain()
        assert speaker.speak(utt(salience=0, excitement="calm")).ok
        assert kokoro.spoken and not hype.spoken

    @pytest.mark.parametrize("kind", sorted(HYPE_KINDS))
    def test_hype_kinds_route_regardless_of_salience(self, kind):
        speaker, kokoro, hype, _ = make_chain()
        assert speaker.speak(utt(kind=kind, salience=0)).ok
        assert hype.spoken and not kokoro.spoken

    def test_hype_failure_falls_through_to_kokoro(self):
        speaker, kokoro, hype, _ = make_chain(hype_fail=True)
        assert speaker.speak(utt(salience=3)).ok
        assert kokoro.spoken == ["He plays a land."]

    def test_routing_override_lowers_threshold(self):
        speaker, kokoro, hype, _ = make_chain(routing={"min_salience": 1})
        assert speaker.speak(utt(salience=1)).ok  # now qualifies
        assert hype.spoken and not kokoro.spoken

    def test_routing_override_can_disable_kind_routing(self):
        speaker, kokoro, hype, _ = make_chain(routing={"kinds": []})
        assert speaker.speak(utt(kind="game_end", salience=0)).ok
        assert kokoro.spoken and not hype.spoken

    def test_no_hype_engine_is_a_noop(self):
        kokoro = RecordingEngine("kokoro")
        piper = RecordingEngine("piper")
        speaker = ChainedSpeaker([kokoro, piper])
        assert speaker.speak(utt(salience=3)).ok
        assert kokoro.spoken and speaker._hype_engine() is None

    def test_active_engine_tracks_real_index_after_routing(self):
        speaker, _, hype, _ = make_chain()
        speaker.speak(utt(salience=3))
        assert speaker.engine is hype  # engines[1], not ordered-list index 0


class FakeModel:
    sr = 24000

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, text, audio_prompt_path=None, **kwargs):
        self.calls.append({"text": text,
                           "audio_prompt_path": audio_prompt_path, **kwargs})
        return np.zeros((1, 2400), dtype=np.float32)  # 0.1s of silence


class StubbedChatterbox(ChatterboxEngine):
    """ChatterboxEngine with model + playback swapped for recording doubles."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fake_model = FakeModel()
        self.played: tuple | None = None

    def _get_model(self):
        return self.fake_model

    def _play_chunk(self, pcm, sr: int, generation: int = 0) -> None:
        self.played = (pcm.copy(), sr)


class TestSynthesis:
    def test_generate_receives_energy_params_and_plays_24k(self):
        eng = StubbedChatterbox(exaggeration=0.7)
        eng.synthesize("What a turn!", rate=1.0)
        call = eng.fake_model.calls[0]
        assert call["text"] == "What a turn!"
        assert call["exaggeration"] == pytest.approx(0.7)
        assert call["cfg_weight"] == pytest.approx(0.4)
        assert call["temperature"] == pytest.approx(0.8)
        pcm, sr = eng.played
        assert sr == 24000 and pcm.dtype == np.float32

    def test_reference_audio_resolves_to_path(self, tmp_path):
        ref = tmp_path / "hype_ref.wav"
        ref.write_bytes(b"RIFF")
        eng = StubbedChatterbox(reference_audio=str(ref))
        eng.synthesize("go", rate=1.0)
        assert eng.fake_model.calls[0]["audio_prompt_path"] == str(ref)

    def test_missing_reference_falls_back_to_model_default(self):
        eng = StubbedChatterbox(voice="/nonexistent/ref.wav")
        eng.synthesize("go", rate=1.0)
        assert eng.fake_model.calls[0]["audio_prompt_path"] is None

    def test_empty_output_raises_engine_fault(self):
        eng = StubbedChatterbox()
        eng.fake_model.generate = lambda *a, **k: np.zeros(
            (1, 0), dtype=np.float32)
        result = eng.speak(utt())
        assert not result.ok and "no audio" in result.reason

    def test_cancel_before_playback_reports_interruption(self):
        eng = StubbedChatterbox()
        # A cancel landing DURING synthesis flags the current generation;
        # the freshly synthesized audio must never reach playback.
        def generate_then_cancel(*a, **k):
            eng.cancel()
            return np.zeros((1, 2400), dtype=np.float32)

        eng.fake_model.generate = generate_then_cancel
        with pytest.raises(InterruptedError):
            eng.synthesize("go", rate=1.0)
        assert eng.played is None

    def test_speak_wraps_faults_never_raises_past_caller(self):
        class BoomModel:
            sr = 24000

            def generate(self, *a, **k):
                raise RuntimeError("oom")

        eng = StubbedChatterbox()
        eng.fake_model = BoomModel()
        result = eng.speak(utt())
        # Engine fault (NOT a cancel marker) so the chain falls through.
        assert not result.ok
        assert "oom" in result.reason
        assert "interrupted" not in result.reason.lower()


class DummyKokoro(TTSEngine):
    name = "kokoro"

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def available(self) -> bool:
        return True

    def synthesize(self, text: str, rate: float = 1.0, voice=None) -> None:
        pass


class DummyChatterbox(DummyKokoro):
    name = "chatterbox"


class TestBuildChainPlumbing:
    def test_engine_kwargs_and_routing_flow_through(self, monkeypatch):
        monkeypatch.setitem(tts_mod.ENGINE_REGISTRY,
                            "kokoro", (__name__, "DummyKokoro"))
        monkeypatch.setitem(tts_mod.ENGINE_REGISTRY,
                            "chatterbox", (__name__, "DummyChatterbox"))
        cfg = {
            "chains": {"linux": ("kokoro", "chatterbox")},
            "engines": {"chatterbox": {"exaggeration": 0.8}},
            "routing": {"min_salience": 3},
            "tts_voice": "af_heart",
            "platform": "linux",
            "_marker": True,
        }
        speaker = build_speaker_chain(platform="linux", config=cfg)
        assert isinstance(speaker, ChainedSpeaker)
        by_name = {e.name: e for e in speaker.engines}
        assert by_name["chatterbox"].kwargs.get("exaggeration") == 0.8
        # A Kokoro voice id must never leak into chatterbox as a kwarg.
        assert "voice" not in by_name["chatterbox"].kwargs
        assert speaker.hype_min_salience == 3

    def test_chatterbox_absent_from_chain_when_probe_fails(self, monkeypatch):
        monkeypatch.setitem(tts_mod.ENGINE_REGISTRY,
                            "kokoro", (__name__, "DummyKokoro"))
        monkeypatch.setitem(tts_mod.ENGINE_REGISTRY,
                            "chatterbox", (__name__, "DummyChatterbox"))

        class UnavailableChatterbox(DummyChatterbox):
            def available(self) -> bool:
                return False

        monkeypatch.setattr(__import__(__name__), "DummyChatterbox",
                            UnavailableChatterbox, raising=False)
        # Re-point the registry at the patched name is unnecessary: the
        # module attribute lookup happens at load_engine_class call time.
        speaker = build_speaker_chain(platform="linux", config={
            "chains": {"linux": ("kokoro", "chatterbox")},
            "platform": "linux",
        })
        # Single available engine -> EngineSpeaker (no .engines list).
        assert [e.name for e in getattr(speaker, "engines",
                                        [speaker.engine])] == ["kokoro"]


class TestLaunchChainsToggle:
    """The hype_voice toggle gates whether chatterbox joins the chain."""

    def test_off_strips_chatterbox_from_defaults(self):
        from arenaonair.config import Config

        cfg = Config(hype_voice=False)
        chains = speech_mod.launch_chains(cfg)
        plat = speech_mod.detect_platform()
        assert "chatterbox" not in chains[plat]
        assert chains[plat][0] == "kokoro"

    def test_on_keeps_full_default_chain(self):
        from arenaonair.config import Config

        cfg = Config(hype_voice=True)
        chains = speech_mod.launch_chains(cfg)
        plat = speech_mod.detect_platform()
        assert chains[plat] == speech_mod.DEFAULT_CHAINS[plat]

    def test_off_wins_over_user_chain_override(self):
        from arenaonair.config import Config

        cfg = Config(hype_voice=False,
                     tts_chains={"linux": ("chatterbox", "kokoro")})
        assert speech_mod.launch_chains(cfg)["linux"] == ("kokoro",)

    def test_on_leaves_user_chain_override_untouched(self):
        from arenaonair.config import Config

        cfg = Config(hype_voice=True,
                     tts_chains={"linux": ("chatterbox", "kokoro")})
        assert speech_mod.launch_chains(cfg)["linux"] == ("chatterbox", "kokoro")

    def test_toggle_roundtrips_through_config_coercion(self):
        from arenaonair.config import coerce

        assert coerce("hype_voice", True) is True
        assert coerce("hype_voice", "true") is True
        assert coerce("hype_voice", 0) is False

    def test_hype_voice_defaults_on(self):
        from arenaonair.config import Config

        assert Config().hype_voice is True

    def test_cuda_oom_falls_back_to_cpu_and_remembers(self, monkeypatch):
        import torch

        calls = []

        def fake_from_pretrained(cls, device=None):
            calls.append(device)
            if device.startswith("cuda"):
                raise torch.OutOfMemoryError("GPU busy")
            return FakeModel()

        import chatterbox.tts as ctts
        monkeypatch.setattr(ctts.ChatterboxTTS, "from_pretrained",
                            classmethod(fake_from_pretrained))
        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        eng = ChatterboxEngine()
        eng._get_model()
        assert calls == ["cuda", "cpu"]
        assert eng.device == "cpu"

    def test_warmup_loads_and_generates_once(self):
        eng = StubbedChatterbox()
        assert eng.warmup() is True
        assert len(eng.fake_model.calls) == 1
        assert eng.fake_model.calls[0]["text"] == "Ready."
        # Second warmup is idempotent-cheap: model already resident.
        assert eng.warmup() is True
        assert len(eng.fake_model.calls) == 2

    def test_warmup_never_raises(self):
        eng = StubbedChatterbox()

        def boom(*a, **k):
            raise RuntimeError("cold")

        eng.fake_model.generate = boom
        assert eng.warmup() is False

    def test_dtype_casts_model_parts(self, monkeypatch):
        import torch

        class Part:
            def __init__(self):
                self.dtype = torch.float32

            def to(self, dtype=None):
                self.dtype = dtype

        class PartsModel(FakeModel):
            def __init__(self):
                super().__init__()
                self.t3, self.s3gen, self.ve = Part(), Part(), Part()

        import chatterbox.tts as ctts
        monkeypatch.setattr(ctts.ChatterboxTTS, "from_pretrained",
                            classmethod(lambda cls, device=None: PartsModel()))
        eng = ChatterboxEngine(dtype="bfloat16")
        model = eng._get_model()
        assert model.t3.dtype == torch.bfloat16
        assert model.s3gen.dtype == torch.bfloat16
        assert model.ve.dtype == torch.bfloat16

    def test_unknown_dtype_is_ignored(self, monkeypatch):
        import chatterbox.tts as ctts
        monkeypatch.setattr(ctts.ChatterboxTTS, "from_pretrained",
                            classmethod(lambda cls, device=None: FakeModel()))
        eng = ChatterboxEngine(dtype="nonsense")
        assert eng._get_model() is not None  # no raise, stays float32
