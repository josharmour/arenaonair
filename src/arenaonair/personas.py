"""Booth personas: a named broadcast style shared by both narration paths.

A persona picks the co-caster voice preset, a delivery pace for the template
path, and a tone description for the generative booth. The tone text is
fixed here (never user-supplied) because it is sent to the model as trusted
style guidance; it can shape word choice but never relaxes any booth rule.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    name: str
    label: str
    preset: str                  # key of config.BOOTH_PRESETS
    rate: float                  # template-path speech-rate multiplier
    max_excitement: str          # calm | normal | tense | electric
    style: str                   # tone guidance for the generative booth


PERSONAS: dict[str, Persona] = {
    "classic": Persona(
        "classic", "Classic radio booth", "sports_desk", 1.0, "electric",
        "Classic radio sports booth: warm, clear, lightly energetic. The play-by-play "
        "voice paints the action; the analyst adds one crisp explanation."),
    "esports": Persona(
        "esports", "Esports hype desk", "esports_arena", 1.08, "electric",
        "High-energy esports shoutcasting: punchy short sentences, vivid verbs, rising "
        "excitement on big swings, playful banter between the two casters. Hype only "
        "what the facts show; routine plays stay brief."),
    "test_match": Persona(
        "test_match", "Test-match understatement", "test_match", 0.94, "tense",
        "Dry, understated British cricket commentary: unhurried, wry, gently ironic, "
        "fond of a well-placed pause. Big moments get a raised eyebrow, not shouting."),
    "pro_tour": Persona(
        "pro_tour", "Pro Tour analysts", "premier_pro_tour", 1.0, "tense",
        "Measured Pro Tour coverage by veteran analysts: precise card language, calm "
        "authority, explains why a card or board state matters. Never evaluates a "
        "decision as right or wrong."),
}

DEFAULT_PERSONA = "classic"

_EXCITEMENT_ORDER = ("calm", "normal", "tense", "electric")


def get(name: str | None) -> Persona | None:
    return PERSONAS.get(name) if name else None


def cap_excitement(persona: Persona | None, excitement: str) -> str:
    """Clamp template-path excitement to the persona's ceiling."""
    if persona is None or excitement not in _EXCITEMENT_ORDER:
        return excitement
    ceiling = _EXCITEMENT_ORDER.index(persona.max_excitement)
    return _EXCITEMENT_ORDER[min(_EXCITEMENT_ORDER.index(excitement), ceiling)]
