"""Tests for game progression, pacing, cadence, excitement, and voice switching.

Covers:
- Excitement tiers (calm, normal, tense, electric)
- Tempo selection (deliberate, normal, fast, frenzy)
- Speech rate dynamic scaling (0.98x to 1.25x)
- Cadence gap regulation
- Lethal threat detection on board
- Voice listing and dynamic switching
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from arenaonair import events as ev
from arenaonair.models import (
    CardRef,
    Event,
    GameState,
    MatchMeta,
    PlayerView,
    TurnInfo,
    Utterance,
    ZoneView,
)
from arenaonair.narrator import Narrator
from arenaonair.pacing import PacingContext, compute_pacing
from arenaonair.platform.tts import KOKORO_VOICES, list_kokoro_voices, coerce, KokoroEngine
from arenaonair.speech import EngineSpeaker


def _card(iid: int, name: str, types=("creature",), power=None, toughness=None,
          ctrl: int = 1) -> CardRef:
    return CardRef(
        instance_id=iid,
        grp_id=None,
        name=name,
        type_line=" ".join(types).title(),
        card_types=tuple(types),
        power=power,
        toughness=toughness,
        controller_seat=ctrl,
        owner_seat=ctrl,
    )


def _make_state(turn: int = 1,
                lives=(20, 20),
                creatures: tuple[CardRef, ...] = (),
                active: int = 1) -> GameState:
    objs = {c.instance_id: c for c in creatures}
    bf_ids = tuple(c.instance_id for c in creatures)
    zones = {
        "battlefield:pub": ZoneView(
            zone_id=1,
            zone_type="ZoneType_Battlefield",
            owner_seat=None,
            object_ids=bf_ids,
        ),
    }
    players = {
        1: PlayerView(seat=1, life=lives[0], starting_life=20, max_hand_size=7),
        2: PlayerView(seat=2, life=lives[1], starting_life=20, max_hand_size=7),
    }
    return GameState(
        snapshot_id=1,
        prev_snapshot_id=None,
        match_meta=MatchMeta(match_id="test-match", format_name="Standard"),
        turn_info=TurnInfo(turn_number=turn, active_player=active, phase="main1"),
        players=players,
        zones=zones,
        objects=objs,
        local_seat=1,
    )


class TestPacingAndExcitement:

    def test_calm_early_game_land_drop(self):
        state = _make_state(turn=1, lives=(20, 20))
        event = Event(kind=ev.LAND_DROP, seat=1, payload={"name": "Plains"}, ts=1.0, salience=ev.SALIENCE_FILLER)

        pacing = compute_pacing(event, state)
        assert pacing.game_stage == "early"
        assert pacing.excitement == "calm"
        assert pacing.tempo == "deliberate"
        assert pacing.speech_rate == 0.98
        assert pacing.cadence_gap == 1.5

    def test_normal_mid_game_play(self):
        state = _make_state(turn=4, lives=(18, 16))
        event = Event(kind=ev.CAST, seat=1, payload={"name": "Grizzly Bears"}, ts=2.0, salience=ev.SALIENCE_LOW)

        pacing = compute_pacing(event, state)
        assert pacing.game_stage == "mid"
        assert pacing.excitement == "normal"
        assert pacing.tempo == "normal"
        assert pacing.speech_rate == 1.05
        assert pacing.cadence_gap == 0.8

    def test_tense_low_life_or_combat_trick(self):
        # Tense from low life (<= 10)
        state = _make_state(turn=5, lives=(9, 14))
        event = Event(kind=ev.CAST, seat=1, payload={"name": "Shock"}, ts=3.0, salience=ev.SALIENCE_LOW)

        pacing = compute_pacing(event, state)
        assert pacing.excitement == "tense"
        assert pacing.tempo == "fast"
        assert pacing.speech_rate == 1.15
        assert pacing.cadence_gap == 0.2

        # Tense from tactical trick even with high life
        state_healthy = _make_state(turn=3, lives=(20, 20))
        event_trick = Event(kind=ev.COMBAT_TRICK, seat=1, payload={"name": "Giant Growth"}, ts=3.5, salience=ev.SALIENCE_HIGH)
        pacing_trick = compute_pacing(event_trick, state_healthy)
        assert pacing_trick.excitement == "tense"

    def test_electric_counter_war_and_game_end(self):
        state = _make_state(turn=5, lives=(15, 15))
        event_war = Event(kind=ev.COUNTER_WAR, seat=None, payload={"depth": 3}, ts=4.0, salience=ev.SALIENCE_HIGH)

        pacing = compute_pacing(event_war, state)
        assert pacing.excitement == "electric"
        assert pacing.tempo == "frenzy"
        assert pacing.speech_rate == 1.25
        assert pacing.cadence_gap == 0.0

        event_end = Event(kind=ev.GAME_END, seat=None, payload={"reason": "win"}, ts=5.0, salience=ev.SALIENCE_MUST_SPEAK)
        pacing_end = compute_pacing(event_end, state)
        assert pacing_end.excitement == "electric"
        assert pacing_end.tempo == "frenzy"

    def test_electric_lethal_attack_on_board(self):
        # Seat 1 has 12 power, Seat 2 has 10 life -> lethal threatened!
        creature = _card(101, "Colossus", power=12, toughness=12, ctrl=1)
        state = _make_state(turn=6, lives=(20, 10), creatures=(creature,))
        event = Event(kind=ev.ATTACK_DECLARED, seat=1, payload={"total_power": 12}, ts=6.0, salience=ev.SALIENCE_HIGH)

        pacing = compute_pacing(event, state)
        assert pacing.lethal_threat is True
        assert pacing.excitement == "electric"
        assert pacing.speech_rate == 1.25

    def test_queue_backlog_escalates_tempo(self):
        state = _make_state(turn=4, lives=(18, 16))
        event = Event(kind=ev.CAST, seat=1, payload={"name": "Spell"}, ts=7.0, salience=ev.SALIENCE_LOW)

        # 0 backlog -> normal tempo
        p_normal = compute_pacing(event, state, queue_backlog=0, is_busy=False)
        assert p_normal.tempo == "normal"

        # 2 backlogged items -> fast tempo
        p_fast = compute_pacing(event, state, queue_backlog=2, is_busy=False)
        assert p_fast.tempo == "fast"


class TestNarratorWithPacingAndExcitement:

    def test_narrator_renders_utterance_with_pacing_metadata(self):
        narrator = Narrator()
        state = _make_state(turn=5, lives=(8, 12))
        event = Event(kind=ev.CAST, seat=1, payload={"name": "Lightning Bolt"}, ts=8.0, salience=ev.SALIENCE_HIGH)

        utt = narrator.render(event, state, tempo="fast", excitement="tense", rate=1.15)
        assert utt is not None
        assert utt.tempo == "fast"
        assert utt.excitement == "tense"
        assert utt.rate == 1.15
        assert utt.text != ""

    def test_electric_excitement_adds_exclamation_punctuation(self):
        narrator = Narrator()
        state = _make_state(turn=7, lives=(4, 15))
        event = Event(kind=ev.COMBAT_DAMAGE, seat=1, payload={"amount": 4, "target_seat": 1}, ts=9.0, salience=ev.SALIENCE_HIGH)

        utt = narrator.render(event, state, tempo="frenzy", excitement="electric", rate=1.25)
        assert utt is not None
        assert utt.text.endswith("!")


class TestVoiceConfigurationAndSwitching:

    def test_kokoro_voices_catalog(self):
        voices = list_kokoro_voices()
        assert len(voices) >= 24
        assert "af_heart" in voices
        assert "am_adam" in voices
        assert "bm_george" in voices
        assert "bf_emma" in voices

    def test_coerce_extracts_rate_and_voice(self):
        utt = Utterance(
            uid="u1",
            match_id="m1",
            kind="cast",
            text="Testing voice",
            salience=2,
            ts_created=1.0,
            rate=1.25,
            voice="am_adam",
        )
        text, uid, rate, voice = coerce(utt)
        assert text == "Testing voice"
        assert uid == "u1"
        assert rate == 1.25
        assert voice == "am_adam"

    def test_engine_and_speaker_set_voice(self):
        from arenaonair.platform.tts import TTSEngine
        mock_engine = MagicMock(spec=TTSEngine)
        mock_engine.voice = "af_heart"
        mock_engine.set_voice = lambda v: setattr(mock_engine, "voice", v)

        speaker = EngineSpeaker(mock_engine)
        speaker.set_voice("am_adam")
        assert mock_engine.voice == "am_adam"

    def test_kokoro_engine_set_voice(self):
        engine = KokoroEngine(voice="af_heart")
        assert engine.voice == "af_heart"
        engine.set_voice("am_onyx")
        assert engine.voice == "am_onyx"
