"""Player names as the booth says them.

Arena screen names are often handles ("MikeUchiha18", "SBK", "bnhjgsyeduligksr")
that text-to-speech reads out as digits or spells letter by letter. The booth
says a name only when it reads like an ordinary name, and otherwise calls the
player by role: "the opponent", or "our player" for the listening player.
Written output (recap files, the overlay, history) keeps the real names.
"""
from __future__ import annotations

import re
import unicodedata

OPPONENT = "the opponent"
LOCAL = "our player"

_VOWELS = set("aeiouyæøœ")
# Consonant clusters a name can open with ("Str" in Strand, "Schm" in Schmidt).
_ONSETS = set("""bl br ch cl cr dj dm dr dw fl fr gh gl gn gr gw kh kl kn kr ng ph pl pr ps sc sh sk sl
    sm sn sp sq sr st sv sw th tr ts tw vl wh wr zh zw chr phr sch scr shr sph spl spr str thr
    schl schm schn schr schw""".split())
_SENTENCE_START = re.compile(rf"(^|[.!?]\s+)({OPPONENT}|{LOCAL})")


def spoken(name) -> str | None:
    """How the booth says ``name``, or None when it isn't a regular name.

    Rejects digits, symbols, non-Latin scripts, acronyms and letter salad.
    CamelCase splits into words and shouting is calmed: "SorinMarkov" is said
    "Sorin Markov", "SPARKY" is said "Sparky".
    """
    if not isinstance(name, str):
        return None
    name = _key(name)
    if not 2 <= len(name) <= 24:
        return None
    for c in name:
        if c.isalpha():
            if not unicodedata.name(c, "").startswith("LATIN"):
                return None  # the voices speak English
        elif c not in " '-":
            return None
    words = [w for part in re.split(r"[ -]+", name) for w in _camel_words(part) if w]
    if not 1 <= len(words) <= 3:
        return None
    said = []
    for word in words:
        if word.isupper():
            word = word.capitalize()
        if len(word) < 2 or not (word.islower() or word.istitle()) or not _pronounceable(word):
            return None
        said.append(word)
    return " ".join(said)


def replacements(player_names, local_seat=None) -> dict[str, str]:
    """Each name the booth says differently -> what it says instead."""
    out = {}
    for seat, raw in dict(player_names or {}).items():
        if not isinstance(raw, str) or not _key(raw):
            continue
        said = spoken(raw) or (LOCAL if seat == local_seat else OPPONENT)
        if said != _key(raw):
            out[_key(raw)] = said
    return out


def scrub(text: str, subs: dict[str, str]) -> str:
    """``text`` with every player name said the booth's way."""
    if not subs or not isinstance(text, str):
        return text
    text = _replace(text, subs)
    return _SENTENCE_START.sub(lambda m: m[1] + m[2][0].upper() + m[2][1:], text)


def on_air(value, subs: dict[str, str]):
    """``value`` (facts: dicts, lists, strings) with player names said the booth's way."""
    if not subs:
        return value
    if isinstance(value, str):
        return _replace(value, subs)
    if isinstance(value, dict):
        return {k: on_air(v, subs) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(on_air(v, subs) for v in value)
    return value


def _key(name: str) -> str:
    return " ".join("".join(c for c in name if c.isprintable()).split())


def _replace(text: str, subs: dict[str, str]) -> str:
    for raw in sorted(subs, key=len, reverse=True):
        said = subs[raw]
        text = re.sub(rf"(?<!\w){re.escape(raw)}(?!\w)", lambda _m: said, text, flags=re.I)
    return text


def _camel_words(part: str) -> list[str]:
    words, start = [], 0
    for i in range(1, len(part)):
        if part[i - 1].islower() and part[i].isupper():
            words.append(part[start:i])
            start = i
    return words + [part[start:]]


def _pronounceable(word: str) -> bool:
    base = "".join(c for c in unicodedata.normalize("NFD", word.lower()) if not unicodedata.combining(c))
    if not any(c in _VOWELS for c in base) or re.search(r"(.)\1\1", base):
        return False
    runs = re.findall(r"c+|v+", "".join("v" if c in _VOWELS else "c" for c in base))
    if any(len(r) > (4 if r[0] == "c" else 3) for r in runs):
        return False
    onset = len(runs[0]) if runs[0][0] == "c" else 0
    if onset > 1 and base[:onset] not in _ONSETS:
        return False
    return not (runs[-1][0] == "c" and len(runs[-1]) > 3)  # "-rst" is fine, "-nkst" isn't
