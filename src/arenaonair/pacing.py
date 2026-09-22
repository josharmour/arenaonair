"""Game progression, cadence, tempo, and excitement analysis.

Evaluates live game progression (turn phase, life pressure, lethal threats,
tactical situations, story arcs, and queue pressure) to compute broadcast
excitement tiers, adaptive speech tempo, dynamic TTS playback rate, and
rhythmic cadence spacing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import events as ev
from .models import Event, GameState


@dataclass(frozen=True)
class PacingContext:
    """Snapshot of broadcast pacing and emotional intensity for one event."""

    game_stage: str       # 'early' (turns 1-3) | 'mid' (turns 4-6) | 'late' (turns 7+)
    lowest_life: int      # min life total across known players
    lethal_threat: bool   # whether any player's board power threatens lethal
    excitement: str       # 'calm' | 'normal' | 'tense' | 'electric'
    tempo: str            # 'deliberate' | 'normal' | 'fast' | 'frenzy'
    speech_rate: float    # TTS speed multiplier (e.g. 0.98, 1.05, 1.15, 1.25)
    cadence_gap: float    # minimum breathing space in seconds between routine calls


def _norm_type(token: Any) -> str:
    s = str(token or "")
    return s.split("_", 1)[-1].lower()


def _extract_lowest_life(state: GameState | None) -> int:
    if state is None or not state.players:
        return 20
    lives = [p.life for p in state.players.values() if isinstance(getattr(p, "life", None), int)]
    return min(lives) if lives else 20


def _detect_lethal_threat(state: GameState | None) -> bool:
    """Check if any seat's total creature power on the battlefield exceeds an opponent's life."""
    if state is None or not state.players or not state.zones or not state.objects:
        return False
    try:
        bf_zone = None
        for z in state.zones.values():
            if _norm_type(getattr(z, "zone_type", "")) == "battlefield":
                bf_zone = z
                break
        if bf_zone is None:
            return False

        # Calculate power per seat
        power_per_seat: dict[int, int] = {}
        for iid in getattr(bf_zone, "object_ids", ()) or ():
            ref = state.objects.get(iid)
            if not ref:
                continue
            types = getattr(ref, "card_types", ()) or ()
            if any(_norm_type(t) == "creature" for t in types):
                seat = getattr(ref, "controller_seat", None) or getattr(ref, "owner_seat", None)
                p = getattr(ref, "power", None)
                if isinstance(p, (int, float)) and not isinstance(p, bool):
                    power_per_seat[seat] = power_per_seat.get(seat, 0) + int(p)

        # Check against opposing player life totals
        seats = [s for s in state.players.keys() if s is not None]
        for attacker_seat, power in power_per_seat.items():
            if power <= 0:
                continue
            for defender_seat in seats:
                if defender_seat != attacker_seat:
                    def_life = getattr(state.players.get(defender_seat), "life", None)
                    if isinstance(def_life, int) and power >= def_life:
                        return True
        return False
    except Exception:
        return False


def _extract_turn(state: GameState | None, story: Any = None) -> int:
    if state and state.turn_info and isinstance(state.turn_info.turn_number, int) and state.turn_info.turn_number > 0:
        return state.turn_info.turn_number
    if story and isinstance(getattr(story, "_turn", None), int) and story._turn > 0:
        return story._turn
    return 1


def compute_pacing(
    event: Event | None,
    state: GameState | None,
    story: Any = None,
    queue_backlog: int = 0,
    is_busy: bool = False,
) -> PacingContext:
    """Evaluate game progression, tension, and backlog to determine broadcast pacing."""
    turn = _extract_turn(state, story)
    if turn <= 3:
        stage = "early"
    elif turn <= 6:
        stage = "mid"
    else:
        stage = "late"

    lowest_life = _extract_lowest_life(state)
    lethal_threat = _detect_lethal_threat(state)
    kind = getattr(event, "kind", "") if event else ""
    payload = getattr(event, "payload", {}) or {}

    # --- Excitement evaluation ---
    # 1. ELECTRIC (Maximum excitement: shoutcaster frenzy, game deciders, lethal swings)
    is_electric = False
    if kind in (ev.MATCH_END, ev.GAME_END):
        is_electric = True
    elif kind in (ev.COUNTER_WAR, ev.OUTS_ANTICIPATION):
        is_electric = True
    elif kind == ev.UNFAIR_PLAY and turn <= 3:
        is_electric = True
    elif lethal_threat and kind in (ev.ATTACK_DECLARED, ev.COMBAT_DAMAGE, ev.COMBAT_TRICK):
        is_electric = True
    elif lowest_life <= 5 and kind in (ev.ATTACK_DECLARED, ev.COMBAT_DAMAGE, ev.COMBAT_TRICK, ev.CHUMP_BLOCK, ev.LIFE_CHANGE):
        is_electric = True

    # 2. TENSE (Heightened excitement: combat tricks, chumps, low life, topdeck, race)
    is_tense = False
    if not is_electric:
        arc = getattr(story, "_arc", None)
        damage_val = payload.get("amount") or payload.get("delta")
        try:
            damage_mag = abs(int(damage_val)) if damage_val is not None else 0
        except (TypeError, ValueError):
            damage_mag = 0

        if lowest_life <= 10 or lethal_threat:
            is_tense = True
        elif kind in (ev.COMBAT_TRICK, ev.CHUMP_BLOCK, ev.TOPDECK_MODE, ev.HAND_SCULPTING, ev.UNFAIR_PLAY):
            is_tense = True
        elif kind == ev.COUNTER:
            is_tense = True
        elif arc in ("race", "comeback_brewing"):
            is_tense = True
        elif damage_mag >= 4:
            is_tense = True
        elif kind == ev.ATTACK_DECLARED:
            atk_power = payload.get("total_power")
            try:
                if atk_power is not None and int(atk_power) >= 4:
                    is_tense = True
            except (TypeError, ValueError):
                pass

    # 3. CALM (Relaxed setup: early turns, high life, routine setup)
    is_calm = False
    if not is_electric and not is_tense:
        if stage == "early" and lowest_life >= 18:
            if kind in (ev.LAND_DROP, ev.TURN_START, ev.GAME_START, ev.MATCH_START):
                is_calm = True
            elif not kind or kind in ev.NARRATIVE_KINDS:
                is_calm = True

    # Classify overall excitement
    if is_electric:
        excitement = "electric"
    elif is_tense:
        excitement = "tense"
    elif is_calm:
        excitement = "calm"
    else:
        excitement = "normal"

    # --- Tempo evaluation ---
    # Fast / frenzy when queue is backed up OR game is in high excitement
    has_backlog = queue_backlog > 0 or is_busy
    if has_backlog:
        if excitement == "electric":
            tempo = "frenzy"
        else:
            tempo = "fast"
    elif excitement == "electric":
        tempo = "frenzy"
    elif excitement == "tense":
        tempo = "fast"
    elif excitement == "calm":
        tempo = "deliberate"
    else:
        tempo = "normal"

    # --- Dynamic Speech Rate (TTS speed multiplier) ---
    rate_map = {
        "calm": 0.98,
        "normal": 1.05,
        "tense": 1.15,
        "electric": 1.25,
    }
    speech_rate = rate_map.get(excitement, 1.05)
    # Extra nudge during frenzy tempo
    if tempo == "frenzy" and speech_rate < 1.25:
        speech_rate = 1.25

    # --- Cadence Gap (breathing room before non-critical commentary) ---
    cadence_map = {
        "calm": 1.5,
        "normal": 0.8,
        "tense": 0.2,
        "electric": 0.0,
    }
    cadence_gap = cadence_map.get(excitement, 0.8)

    return PacingContext(
        game_stage=stage,
        lowest_life=lowest_life,
        lethal_threat=lethal_threat,
        excitement=excitement,
        tempo=tempo,
        speech_rate=speech_rate,
        cadence_gap=cadence_gap,
    )
