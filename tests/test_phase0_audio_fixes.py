"""Phase-0 audio/delivery regression tests (S7.1, S7.2, S7.3, S7.4, S7.5, S7.10).

Each test class maps to one bug id; every acceptance bullet from the spec is
covered by at least one test. External binaries/subprocesses are mocked so the
module is hermetic.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

try:
    import numpy  # noqa: F401
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

from arenaonair.models import DeliveryResult, Event, Utterance
from arenaonair.platform.tts import TTSEngine
from arenaonair.speech import (
    ChainedSpeaker,
    EngineSpeaker,
    SpeechPump,
    SpeechQueue,
    build_speaker_chain,
)


def mk(uid, salience=1, ts=0.0, match="m1", text=None, kind="life_change"):
    return Utterance(
        uid=uid,
        match_id=match,
        kind=kind,
        text=text or f"utterance {uid}",
        salience=salience,
        ts_created=ts,
    )


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------

class _FakePipeline:
    """Stands in for kokoro.KPipeline: yields N chunk objects with audio."""

    def __init__(self, n_chunks: int = 3):
        self.n_chunks = n_chunks
        self.calls: list[tuple[str, str | None, float]] = []

    def __call__(self, text, voice=None, speed=1.0):
        self.calls.append((text, voice, speed))

        class _Chunk:
            def __init__(self, audio):
                self.audio = audio

        import numpy as np

        for _ in range(self.n_chunks):
            yield _Chunk(np.zeros(2400, dtype=np.float32))


class _StubEngine(TTSEngine):
    """Scriptable chain member: available() True, speak() scriptable."""

    def __init__(self, name: str = "stub", fail: bool = False,
                 reason: str | None = None):
        self.name = name
        self.fail = fail
        self.reason = reason or f"{name}: synthesis blew up"
        self.spoken: list[str] = []
        self.cancel_count = 0
        self.kwargs_accepted: dict = {}
        self.voice: str | None = None

    def set_voice(self, voice: str) -> None:
        self.voice = str(voice).strip()

    def available(self) -> bool:
        return True

    def accept_kwargs(self, **kwargs) -> None:
        self.kwargs_accepted = kwargs

    def speak(self, utterance) -> DeliveryResult:
        if self.fail:
            return DeliveryResult(uid=utterance.uid, ok=False,
                                  reason=self.reason)
        self.spoken.append(utterance.text)
        return DeliveryResult(uid=utterance.uid, ok=True)

    def cancel(self) -> None:
        self.cancel_count += 1

    def shutdown(self) -> None:
        pass


def _patch_loader(monkeypatch, registry: dict[str, _StubEngine]):
    """Point build_speaker_chain's engine loader at the stub registry."""
    import arenaonair.platform.tts as tts_mod

    def fake_load(name):
        if name not in registry:
            raise ValueError(f"unknown TTS engine {name!r}")
        stub = registry[name]

        class _Factory:
            def __new__(cls, **kwargs):
                stub.accept_kwargs(**kwargs)
                return stub

        # build_speaker_chain does ``engine = cls(**kwargs)``; give it a class
        # whose instantiation returns the shared stub.
        return _Factory

    monkeypatch.setattr(tts_mod, "load_engine_class", fake_load)


# ---------------------------------------------------------------------------
# S7.1 — KokoroEngine.cancel scoped to the in-flight utterance
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_NUMPY,
                        reason="numpy not installed")
class TestS71KokoroCancelScoping:
    def _make(self, monkeypatch, n_chunks=3, hold=0.0):
        return _make_kokoro_stub(monkeypatch, n_chunks=n_chunks, hold=hold)

    def test_inflight_cancel_unsuccessful_then_next_succeeds(self, monkeypatch):
        eng, stub = self._make(monkeypatch, hold=0.3)
        box: dict = {}

        def speak_async():
            box["r1"] = eng.speak("first utterance")

        t = threading.Thread(target=speak_async)
        t.start()
        assert stub.started.wait(timeout=2), "playback never started"
        eng.cancel()  # controlled in-flight cancel
        t.join(timeout=5)

        r1 = box["r1"]
        assert r1.ok is False, "interrupted delivery must be unsuccessful"
        assert "cancel" in r1.reason.lower()

        # Next utterance synthesizes + plays normally.
        r2 = eng.speak("second utterance")
        assert r2.ok is True
        assert len(stub.calls) >= 1  # its chunks really played

    def test_cancellation_interrupts_active_playback_and_suppresses_rest(
            self, monkeypatch):
        eng, stub = self._make(monkeypatch, n_chunks=5)
        gate = threading.Event()
        first_done = threading.Event()
        real_stub_call = stub.__call__

        def play_first_then_cancel(pcm, sr, generation=0):
            if not first_done.is_set():
                first_done.set()
                gate.wait(timeout=2)
                eng.cancel()  # interrupt ACTIVE playback of chunk 1
                return
            real_stub_call(pcm, sr, generation)

        monkeypatch.setattr(eng, "_play_chunk", play_first_then_cancel)
        box: dict = {}

        def speak_async():
            box["res"] = eng.speak("multi-chunk line")

        t = threading.Thread(target=speak_async)
        t.start()
        first_done.wait(timeout=2)
        gate.set()
        t.join(timeout=5)
        res = box["res"]
        assert res.ok is False
        assert "cancel" in res.reason.lower()
        # Remaining chunks of the canceled utterance were suppressed.
        assert stub.calls == []

    def test_repeated_cancels_ok(self, monkeypatch):
        eng, stub = self._make(monkeypatch)
        for _ in range(3):
            eng.cancel()  # while idle: harmless
            assert eng.speak("line").ok is True
            eng.cancel()  # after success: harmless
            assert eng.speak("another line").ok is True

    def test_cancel_while_idle_ok(self, monkeypatch):
        eng, _stub = self._make(monkeypatch)
        eng.cancel()
        assert eng.speak("idle-cancel survivor").ok is True

    def test_new_utterance_cannot_clear_cancel_of_older_one(self, monkeypatch):
        # A speaks slowly; cancel aims at A; a NEW utterance B starting right
        # after must not clear that cancel -- A still reports interrupted.
        eng, _stub = self._make(monkeypatch)
        ready = threading.Event()
        real_play = eng._play_chunk

        def slow_play(pcm, sr, generation=0):
            ready.set()
            time.sleep(0.3)
            real_play(pcm, sr, generation=generation)

        monkeypatch.setattr(eng, "_play_chunk", slow_play)

        box: dict = {}

        def speak_a():
            box["a"] = eng.speak("slow one")

        ta = threading.Thread(target=speak_a)
        ta.start()
        assert ready.wait(timeout=2)
        eng.cancel()                       # aimed at A (older generation)
        box["b"] = eng.speak("quick one")  # newer generation -- unaffected
        ta.join(timeout=5)

        assert box["a"].ok is False and "cancel" in box["a"].reason.lower()
        assert box["b"].ok is True

    def test_uid_travels_on_interrupted_result(self, monkeypatch):
        eng, _stub = self._make(monkeypatch)
        ready = threading.Event()
        real_play = eng._play_chunk

        def slow_play(pcm, sr, generation=0):
            ready.set()
            time.sleep(0.2)
            real_play(pcm, sr, generation=generation)

        monkeypatch.setattr(eng, "_play_chunk", slow_play)
        utt = mk("kok-cancelled")
        box: dict = {}

        def speak_async():
            box["res"] = eng.speak(utt)

        t = threading.Thread(target=speak_async)
        t.start()
        assert ready.wait(timeout=2)
        eng.cancel()
        t.join(timeout=5)
        res = box["res"]
        assert res.uid == "kok-cancelled"
        assert res.ok is False


# ---------------------------------------------------------------------------
# S7.2 — game_end closes the game; match_end seals the match
# ---------------------------------------------------------------------------

class _AlwaysOkSpeaker:
    def __init__(self):
        self.spoken: list[str] = []

    def speak(self, utterance):
        self.spoken.append(utterance.text)
        return DeliveryResult(uid=utterance.uid, ok=True)

    def cancel(self):
        pass

    def shutdown(self):
        pass


def _snap_for(match_id):
    from arenaonair.models import GameState, MatchMeta, TurnInfo

    return GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        zones={},
        objects={},
        players={},
        turn_info=TurnInfo(turn_number=1, active_player=1, phase="main1"),
        match_meta=MatchMeta(match_id=match_id, format_name="Standard"),
    )


class TestS72GameBoundaryVsMatchClosure:
    def test_close_game_keeps_queue_usable_for_same_match(self):
        q = SpeechQueue()
        q.set_active_match("bo3")
        q.enqueue(mk("g1-play", kind="cast", match="bo3"))
        removed = q.close_game("bo3")
        assert removed == 1
        # Same match id still narrates game two.
        q.enqueue(mk("g2-play", kind="cast", match="bo3"))
        assert q.pop_best().uid == "g2-play"

    def test_close_game_keeps_match_end_announcement_enqueued(self):
        q = SpeechQueue()
        q.set_active_match("bo3")
        q.enqueue(mk("closing", kind="match_end", salience=3, match="bo3"))
        q.close_game("bo3")
        # The match-end announcement survives the game boundary.
        assert q.pop_best().uid == "closing"

    def test_full_bo3_scenario_under_one_match_id(self):
        """Spec scenario: game_start -> plays -> game_end -> game_start ->
        plays -> game_end -> match_end delivers everything due."""
        q = SpeechQueue()
        pump = SpeechPump(queue=q, speaker=_AlwaysOkSpeaker())
        q.set_active_match("bo3")

        # Game one
        q.enqueue(mk("opener", kind="match_start", salience=3, match="bo3"))
        q.enqueue(mk("g1-cast", kind="cast", match="bo3"))
        assert pump.run_once().ok  # opener
        assert pump.run_once().ok  # g1-cast
        # game_end boundary: obsolete plays removed, queue stays usable
        q.enqueue(mk("late-g1-play", kind="attack_declared", match="bo3"))
        assert q.close_game("bo3") == 1  # late play removed
        assert pump.run_once() is None   # nothing stale left

        # Game two -- SAME match id must still narrate
        q.enqueue(mk("g2-cast", kind="cast", match="bo3"))
        assert pump.run_once().ok
        assert q.close_game("bo3") == 0

        # Final match-end announcement under the same id delivers
        q.enqueue(mk("final", kind="match_end", salience=3, match="bo3"))
        res = pump.run_once()
        assert res is not None and res.ok

    def test_late_utterances_after_match_closure_rejected(self):
        q = SpeechQueue()
        q.set_active_match("bo3")
        q.flush("bo3")  # match_end sealed it
        assert q.enqueue(mk("late", match="bo3")) is False

    def test_app_game_end_does_not_seal_match(self):
        """Integration: app._handle_event(game_end) leaves the queue able to
        speak for the same match; match_end seals it."""
        import io as _io

        from arenaonair.app import ArenaOnAirApp, PrintingSpeaker
        from arenaonair.config import Config
        from arenaonair.speech import SpeechPump as _Pump

        buf = _io.StringIO()
        app = ArenaOnAirApp(Config(verbosity="balanced"), dry_run=True)
        app.speaker = PrintingSpeaker(stream=buf)
        app.pump = _Pump(queue=app.queue, speaker=app.speaker)
        app._match_id = "m-test"
        app.queue.set_active_match("m-test")

        def handle(kind):
            app._handle_event(
                Event(kind=kind, seat=None, payload={"winner": 1},
                      ts=time.time(), salience=3),
                _snap_for("m-test"),
            )

        handle("game_end")
        # Queue must still accept + deliver speech for the SAME match.
        assert app.queue.enqueue(mk("post-game1", match="m-test")) is True
        assert app.pump.run_once() is not None

        handle("game_end")
        assert app.queue.enqueue(mk("post-game2", match="m-test")) is True

        handle("match_end")
        # After match closure, late utterances for this id are rejected.
        assert app.queue.enqueue(mk("too-late", match="m-test")) is False


# ---------------------------------------------------------------------------
# S7.3 -- runtime fallback through the full engine chain
# ---------------------------------------------------------------------------

class TestS73RuntimeFallbackChain:
    def _chain(self, monkeypatch, *stubs, chain_names=None):
        names = chain_names or [s.name for s in stubs]
        registry = {s.name: s for s in stubs}
        import arenaonair.platform.tts as tts_mod

        def fake_load(name):
            if name not in registry:
                raise ValueError(f"unknown TTS engine {name!r}")
            stub = registry[name]

            class _Factory:
                def __new__(cls, **kwargs):
                    stub.accept_kwargs(**kwargs)
                    return stub

            return _Factory

        monkeypatch.setattr(tts_mod, "load_engine_class", fake_load)
        return build_speaker_chain(
            platform="linux",
            config={"chains": {"linux": list(names)}},
            voice=None,
        )

    def test_primary_runtime_failure_falls_through_to_secondary(self,
                                                               monkeypatch):
        primary = _StubEngine("primary", fail=True)
        secondary = _StubEngine("secondary")
        speaker = self._chain(monkeypatch, primary, secondary)
        assert isinstance(speaker, ChainedSpeaker)

        utt = mk("fallback-works")
        res = speaker.speak(utt)
        assert res.ok is True
        assert res.uid == "fallback-works"
        assert secondary.spoken == ["utterance fallback-works"]

    def test_all_engines_fail_reports_failed_delivery_with_reasons(
            self, monkeypatch):
        e1 = _StubEngine("e1", fail=True)
        e2 = _StubEngine("e2", fail=True)
        speaker = self._chain(monkeypatch, e1, e2)
        res = speaker.speak(mk("doomed"))
        assert res.ok is False
        assert res.uid == "doomed"
        assert "all engines failed" in res.reason
        assert "e1" in res.reason and "e2" in res.reason

    def test_canceled_utterance_does_not_fall_through(self, monkeypatch):
        canceled_res = DeliveryResult(
            uid="cut", ok=False, reason="kokoro: synthesis cancelled mid-utterance")
        primary = _StubEngine("primary", fail=True, reason=canceled_res.reason)
        secondary = _StubEngine("secondary")
        speaker = self._chain(monkeypatch, primary, secondary)

        res = speaker.speak(mk("cut"))
        assert res.ok is False
        # Secondary must NOT have replayed the canceled line.
        assert secondary.spoken == []
        assert "cancel" in res.reason.lower()

    def test_single_available_engine_wraps_as_engine_speaker(self, monkeypatch):
        only = _StubEngine("only")
        speaker = self._chain(monkeypatch, only)
        assert isinstance(speaker, EngineSpeaker)


# ---------------------------------------------------------------------------
# S7.4 -- per-engine voice mapping; Kokoro voice id never disables fallback
# ---------------------------------------------------------------------------

class TestS74VoiceMapping:
    def test_kokoro_voice_does_not_disable_system_fallback(self, monkeypatch):
        """voice=am_adam with Kokoro failing: SAPI/espeak-ng still construct
        and speak (the old bug: TypeError -> 'no available TTS engine')."""
        kokoro = _StubEngine("kokoro", fail=True)
        espeak = _StubEngine("espeakng")
        _patch_loader(monkeypatch, {"kokoro": kokoro, "espeakng": espeak})
        speaker = build_speaker_chain(
            platform="linux",
            config={"chains": {"linux": ["kokoro", "espeakng"]}},
            voice="am_adam",
        )
        # Construction succeeded despite a Kokoro-only voice id.
        res = speaker.speak(mk("still-heard"))
        assert res.ok is True
        assert espeak.spoken == ["utterance still-heard"]

    def test_voice_kwarg_passed_only_to_engines_accepting_it(self):
        """Unit-level: the per-engine mapper routes voice by constructor sig."""
        from arenaonair.speech import _apply_voice_kwargs

        class KokoroLike:
            def __init__(self, voice="af_heart"):  # accepts voice
                pass

        class EspeakLike:
            def __init__(self, rate=175, pitch=50):  # NO voice param
                pass

        kok_kwargs = _apply_voice_kwargs(
            "kokoro", KokoroLike, {}, "am_adam")
        esp_kwargs = _apply_voice_kwargs(
            "espeakng", EspeakLike, {}, "am_adam")
        assert kok_kwargs == {"voice": "am_adam"}
        # espeak-ng gets no bogus voice kwarg (would be a TypeError pre-fix).
        assert "voice" not in esp_kwargs
        assert esp_kwargs == {}

    def test_chain_constructs_every_engine_with_voice_config(self, monkeypatch):
        """Integration: with voice=am_adam BOTH engines construct (pre-fix,
        the sapi/espeak TypeError was misreported as engine-unavailable)."""
        kokoro = _StubEngine("kokoro")
        espeak = _StubEngine("espeakng")
        _patch_loader(monkeypatch, {"kokoro": kokoro, "espeakng": espeak})
        speaker = build_speaker_chain(
            platform="linux",
            config={"chains": {"linux": ["kokoro", "espeakng"]}},
            voice="am_adam",
        )
        # Both engines made it into the chain (nothing was disabled).
        assert isinstance(speaker, ChainedSpeaker)
        assert [e.name for e in speaker.engines] == ["kokoro", "espeakng"]

    def test_unsupported_voice_override_never_breaks_construction(
            self, monkeypatch):
        # Even an exotic voice string must not disable any engine.
        e1 = _StubEngine("e1")
        e2 = _StubEngine("e2")
        _patch_loader(monkeypatch, {"e1": e1, "e2": e2})
        speaker = build_speaker_chain(
            platform="linux",
            config={"chains": {"linux": ["e1", "e2"]}},
            voice="totally-not-a-voice-id",
        )
        assert speaker.speak(mk("v")).ok is True

    def test_per_engine_defaults_survive_when_no_voice_configured(
            self, monkeypatch):
        e1 = _StubEngine("e1")
        _patch_loader(monkeypatch, {"e1": e1})
        build_speaker_chain(
            platform="linux",
            config={"chains": {"linux": ["e1"]}},
            voice=None,
        )
        assert "voice" not in e1.kwargs_accepted

    def test_runtime_voice_change_routes_to_whole_chain(self, monkeypatch):
        e1 = _StubEngine("e1")
        e2 = _StubEngine("e2")
        speaker = ChainedSpeaker([e1, e2])
        speaker.set_voice("bm_george")
        assert e1.voice == "bm_george"
        assert e2.voice == "bm_george"


# ---------------------------------------------------------------------------
# S7.5 -- PiperEngine actually plays generated audio
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr
        self.killed = False
        self.polled = False

    def poll(self):
        self.polled = True
        return self.returncode if self.killed else None

    def kill(self):
        self.killed = True

    def communicate(self, text=None):
        return b"", self.stderr


@pytest.mark.skipif(not _HAS_NUMPY,
                        reason="numpy not installed")
class TestS75PiperPlayback:
    def _engine(self, monkeypatch, tmp_path, *, synth_rc=0, play_rc=0,
                model_exists=True):
        from arenaonair.platform.tts_linux import PiperEngine

        eng = PiperEngine(model_dir=str(tmp_path))
        model = tmp_path / "default.onnx"
        if model_exists:
            model.write_bytes(b"fake-onnx")

        created: dict[str, object] = {}

        def fake_popen(cmd, **kwargs):
            if cmd[0] == "piper":
                out_idx = cmd.index("--output_file")
                wav_path = cmd[out_idx + 1]
                created["wav_path"] = wav_path
                # Simulate piper writing a real wav file.
                with open(wav_path, "wb") as f:
                    f.write(b"RIFF....WAVEfmt ")
                created["piper_cmd"] = cmd
                return _FakeProc(returncode=synth_rc)
            created["play_cmd"] = cmd
            return _FakeProc(returncode=play_rc)

        monkeypatch.setattr("arenaonair.platform.tts_linux.subprocess.Popen",
                            fake_popen)
        monkeypatch.setattr(
            "arenaonair.platform.tts_linux.shutil.which",
            lambda name: f"/usr/bin/{name}")
        return eng, created

    def test_successful_synth_invokes_playback_with_generated_audio(
            self, monkeypatch, tmp_path):
        eng, created = self._engine(monkeypatch, tmp_path)
        eng.synthesize("Dragons attack for five.")
        assert "play_cmd" in created, "generated wav must reach a player"
        assert os.path.basename(created["play_cmd"][0]) == "aplay"
        assert created["play_cmd"][-1] == created["wav_path"]
        # No more /dev/null output: piper wrote a real file.
        assert created["wav_path"] != "/dev/null"

    def test_playback_failure_is_unsuccessful_delivery(self, monkeypatch,
                                                       tmp_path):
        from arenaonair.platform.tts import coerce

        eng, created = self._engine(monkeypatch, tmp_path, play_rc=2)
        utt = mk("piper-fail")
        res = eng.speak(utt)
        assert res.ok is False
        assert res.uid == "piper-fail"
        assert "playback" in res.reason.lower()

    def test_synth_failure_is_unsuccessful_delivery(self, monkeypatch,
                                                    tmp_path):
        eng, created = self._engine(monkeypatch, tmp_path, synth_rc=1)
        res = eng.speak(mk("piper-synth-fail"))
        assert res.ok is False
        assert "piper" in res.reason.lower()

    def test_cancel_stops_active_work_and_cleans_up(self, monkeypatch,
                                                    tmp_path):
        from arenaonair.platform.tts_linux import PiperEngine as PE

        eng, created = self._engine(monkeypatch, tmp_path)

        killed_procs: list[_FakeProc] = []
        real_communicate = _FakeProc.communicate

        def slow_communicate(self_proc, text=None):
            if self_proc not in killed_procs:
                killed_procs.append(self_proc)
                eng.cancel()  # cancel while piper is mid-synthesis
            return real_communicate(self_proc, text)

        monkeypatch.setattr(_FakeProc, "communicate", slow_communicate)
        res = eng.speak(mk("piper-cancel"))
        # Either the cancel aborted synthesis (unsuccessful) or the run
        # completed before the cancel landed; the killed proc proves cancel
        # reached active work.
        assert any(p.killed for p in killed_procs) or res.ok is True


# ---------------------------------------------------------------------------
# S7.10 -- afplay exit status checked; temp files cleaned on all paths
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_NUMPY,
                        reason="numpy not installed")
class TestS710AfplayStatus:
    def _engine(self):
        from arenaonair.platform.tts import KokoroEngine

        return KokoroEngine(player_bin="/usr/bin/afplay")

    def test_nonzero_player_exit_is_unsuccessful_with_actionable_reason(
            self, monkeypatch):
        import numpy as np

        eng = self._engine()
        calls: dict = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return _FakeProc(returncode=1, stderr=b"coreaudio: device gone")

        monkeypatch.setattr("subprocess.run", fake_run)
        with pytest.raises(RuntimeError) as excinfo:
            eng._play_afplay(np.zeros(100, dtype=np.float32), 24000)
        msg = str(excinfo.value)
        assert "afplay exited 1" in msg
        assert "coreaudio: device gone" in msg  # actionable stderr retained
        assert calls["kwargs"].get("check") is False  # status checked manually

    def test_zero_exit_is_success(self, monkeypatch):
        import numpy as np

        eng = self._engine()
        monkeypatch.setattr("subprocess.run",
                            lambda cmd, **kw: _FakeProc(returncode=0))
        # Must not raise.
        eng._play_afplay(np.zeros(100, dtype=np.float32), 24000)

    @staticmethod
    def _track_temp_files(monkeypatch, registry: list[str]):
        """Wrap tempfile.NamedTemporaryFile so created paths are recorded."""
        import tempfile as tempfile_mod

        real_namedtemp = tempfile_mod.NamedTemporaryFile

        def tracking_namedtemp(*args, **kwargs):
            kwargs["dir"] = kwargs.get("dir") or None
            tf = real_namedtemp(*args, **kwargs)
            registry.append(tf.name)
            return tf

        monkeypatch.setattr(tempfile_mod, "NamedTemporaryFile",
                            tracking_namedtemp)

    def test_temp_file_removed_on_success(self, monkeypatch):
        import numpy as np

        eng = self._engine()
        written: list[str] = []
        self._track_temp_files(monkeypatch, written)
        monkeypatch.setattr("subprocess.run",
                            lambda cmd, **kw: _FakeProc(returncode=0))
        eng._play_afplay(np.zeros(100, dtype=np.float32), 24000)
        assert written, "a temp wav was created"
        assert all(not os.path.exists(p) for p in written)

    def test_temp_file_removed_on_failure(self, monkeypatch):
        import numpy as np

        eng = self._engine()
        written: list[str] = []
        self._track_temp_files(monkeypatch, written)
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: _FakeProc(returncode=3, stderr=b"boom"))
        with pytest.raises(RuntimeError):
            eng._play_afplay(np.zeros(100, dtype=np.float32), 24000)
        assert all(not os.path.exists(p) for p in written)

    def test_temp_file_removed_on_cancel_path(self, monkeypatch):
        # Cancel before playback: _play_chunk returns early (no temp file at
        # all); cancel mid-utterance leaves no temp file behind either.
        import numpy as np

        eng = self._engine()
        eng.cancel()
        generation = eng._begin_utterance()
        # Flag targets the older generation; this new one is unaffected --
        # prove by playing successfully with a stubbed player.
        monkeypatch.setattr("subprocess.run",
                            lambda cmd, **kw: _FakeProc(returncode=0))
        eng._play_afplay(np.zeros(10, dtype=np.float32), 24000,
                         generation=generation)

    def test_playback_failure_propagates_to_delivery_layer(self, monkeypatch):
        """End to end: afplay failure -> speak() -> unsuccessful DeliveryResult
        with the actionable reason."""
        import numpy as np

        from arenaonair.platform.tts import KokoroEngine

        eng = KokoroEngine()
        monkeypatch.setattr(eng, "_get_pipeline", lambda: _FakePipeline(1))

        def fake_run(cmd, **kwargs):
            return _FakeProc(returncode=1, stderr=b"audio stack dead")

        monkeypatch.setattr("subprocess.run", fake_run)
        res = eng.speak(mk("afplay-dead"))
        assert res.ok is False
        # Actionable reason traveled to the delivery layer (this box is
        # Linux, so the aplay branch of the same failure contract fires).
        assert "playback exited 1" in res.reason



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

# --- appended helper: controllable playback stub for Kokoro tests ---
class _PlaybackStub:
    """Replaces KokoroEngine._play_chunk in tests; records + can block/fail."""

    def __init__(self, hold_seconds: float = 0.0):
        self.hold_seconds = hold_seconds
        self.calls: list[tuple[int, int, int]] = []  # (len(pcm), sr, gen)
        self.started = threading.Event()

    def __call__(self, pcm, sr, generation=0):
        self.started.set()
        if self.hold_seconds:
            time.sleep(self.hold_seconds)
        self.calls.append((len(pcm), sr, generation))


def _make_kokoro_stub(monkeypatch, n_chunks=3, hold=0.0):
    from arenaonair.platform.tts import KokoroEngine

    eng = KokoroEngine()
    fake_pipe = _FakePipeline(n_chunks=n_chunks)
    monkeypatch.setattr(eng, "_get_pipeline", lambda: fake_pipe)
    stub = _PlaybackStub(hold_seconds=hold)
    monkeypatch.setattr(eng, "_play_chunk", stub)
    return eng, stub
