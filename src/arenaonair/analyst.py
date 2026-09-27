"""Template-path color analyst: one factual line or silence.

The live-booth review (docs/live-booth-review.md) found scripted analyst
replies were approval, not analysis. This analyst only speaks when it has a
concrete fact the listening player may not have:

* the OPPONENT's card, the first time it's cast this game: its rules text
  from the local card database (or its type and cost when rules are absent);
* a big life swing: who lost how much this turn and where the totals stand.

At most one line per game turn (``analyst_lines_per_turn``). Every line is
anchored to the play-by-play call it follows, so it's dropped if that call
never plays.
"""
from __future__ import annotations

import re
import time
from dataclasses import replace

from . import events as ev
from . import templates as tpl
from .models import Utterance

_COLORS = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green", "C": "colorless",
           "S": "snow", "X": "X", "T": "tap", "Q": "untap", "E": "energy"}
_SYMBOL = re.compile(r"\{([^{}]+)\}")
_REMINDER = re.compile(r"\s*\([^()]*\)")
MAX_RULE_WORDS = 34
BIG_SWING = 5

_CARD_INTROS = (
    "Quick look at {name}: {text}",
    "For anyone new to {name}: {text}",
    "{name}, from {owner}. {text}",
    "Here's what {name} does. {text}",
)
_GLOSS_INTROS = (
    "{owner} brings out {gloss}.",
    "That's {gloss}, on {owner}'s side.",
)


def _symbol(match) -> str:
    sym = match.group(1).upper()
    if sym.isdigit():
        return tpl.num_word(int(sym))
    parts = [_COLORS.get(p, p) for p in sym.split("/")]
    return " or ".join(parts)


def speakable_rules(oracle: str | None) -> str | None:
    """Oracle text in words a TTS voice reads well, or None if too long."""
    if not oracle:
        return None
    text = _REMINDER.sub("", oracle)
    text = _SYMBOL.sub(lambda m: " " + _symbol(m) + " ", text)
    text = re.sub(r" {2,}", " ", text).replace(" :", ":").replace(" .", ".").replace(" ,", ",")
    text = re.sub(r"\s*\n+\s*", ". ", text).replace("..", ".").strip()
    text = re.sub(r"^tap: ", "Tap it: ", text, flags=re.I).replace(". tap: ", ". Tap it: ")
    if not text or len(text.split()) > MAX_RULE_WORDS:
        return None
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


class TemplateAnalyst:
    def __init__(self, *, card_lookup=None, lines_per_turn: int = 1, clock=time.monotonic):
        self.card_lookup = card_lookup
        self.lines_per_turn = lines_per_turn
        self.clock = clock
        self._scope = None
        self._explained: set = set()
        self._per_turn: dict = {}
        self._seq = 0

    def _reset_if_new_game(self, snap) -> None:
        scope = (snap.match_meta.match_id, snap.game_id)
        if scope != self._scope:
            self._scope = scope
            self._explained.clear()
            self._per_turn.clear()

    def companion(self, event, snap, anchor: Utterance, *, voice=None) -> Utterance | None:
        """The analyst line following ``anchor``, or None (silence)."""
        try:
            self._reset_if_new_game(snap)
            turn = snap.turn_info.turn_number
            if self._per_turn.get(turn, 0) >= self.lines_per_turn and event.salience < ev.SALIENCE_MUST_SPEAK:
                return None
            text = None
            if event.kind == ev.CAST:
                text = self._card_line(event, snap)
            elif event.kind == ev.LIFE_CHANGE:
                text = self._swing_line(event, snap)
            if not text:
                return None
            self._per_turn[turn] = self._per_turn.get(turn, 0) + 1
            self._seq += 1
            return replace(anchor, uid=f"{anchor.uid}-analyst", kind="analyst_fact", text=text,
                           voice=voice, role="color_analyst", anchor_uid=anchor.uid,
                           dialogue_id=anchor.uid, ts_created=anchor.ts_created + 1e-3,
                           expires_ts=self.clock() + 20.0, excitement="normal")
        except Exception:
            return None

    def _names(self, snap):
        return dict(snap.match_meta.player_names)

    def _card_line(self, event, snap):
        local = snap.local_seat
        if local is None or event.seat is None or event.seat == local:
            return None          # the player knows their own cards
        grp = event.payload.get("grp_id")
        name = event.payload.get("name")
        if not grp or not name or name in self._explained or self.card_lookup is None:
            return None
        info = self.card_lookup(grp)
        if info is None:
            return None
        self._explained.add(name)
        owner = self._names(snap).get(event.seat) or "the opponent"
        pick = (self._seq + len(self._explained)) % len(_CARD_INTROS)
        rules = speakable_rules(getattr(info, "oracle_text", ""))
        if rules:
            return _CARD_INTROS[pick].format(name=name, text=rules, owner=owner)
        gloss = tpl.gloss_phrase(name, getattr(info, "type_line", None), getattr(info, "mana_cost", None))
        if not gloss or gloss == name:
            return None
        return _GLOSS_INTROS[pick % len(_GLOSS_INTROS)].format(gloss=gloss, owner=owner)

    def _swing_line(self, event, snap):
        delta = event.payload.get("delta")
        if not isinstance(delta, int) or delta > -BIG_SWING:
            return None
        names = self._names(snap)
        seat = event.seat
        other = next((s for s in sorted(snap.players) if s != seat), None)
        me, them = names.get(seat), names.get(other)
        life_me = snap.players[seat].life if seat in snap.players else None
        life_them = snap.players[other].life if other in snap.players else None
        if not (me and them and isinstance(life_me, int) and isinstance(life_them, int)):
            return None
        lost = tpl.num_word(-delta)
        if life_me < life_them:
            standing = f"{me} now trails {life_me} to {life_them}"
        elif life_me > life_them:
            standing = f"{me} still leads, {life_me} to {life_them}"
        else:
            standing = f"we're level at {life_me} apiece"
        return f"That's {lost} life gone for {me} in one hit, and {standing}."
