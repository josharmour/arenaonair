"""Core frozen data types shared by every ArenaOnAir module.

This is the interface contract: modules exchange these types and nothing else.
All types are frozen/hashable where practical so snapshots can be shared across
threads without locks (copy-on-write at the builder level).

Conventions:
- Seats are ints (1-based systemSeatIds straight from GRE).
- Timestamps are floats: seconds since epoch taken from log line arrival time
  when live, or parsed from envelope ``timestamp`` ms fields in replays.
- Payloads are plain dicts with string keys; unknown keys are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# --------------------------------------------------------------------------
# Parser output
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GreMessage:
    """One decoded message extracted from a Player.log line.

    kind is a dotted classification string, e.g.:
      "room_state.playing", "room_state.completed",
      "gre.GameStateMessage", "gre.ConnectResp", "gre.UIMessage".
    Unknown/unparsed lines never become GreMessage instances.
    """

    kind: str
    payload: Mapping[str, Any]   # the useful inner object (already unwrapped)
    ts: float                     # seconds since epoch
    raw_len: int = 0              # length of originating log line (diagnostics)


# --------------------------------------------------------------------------
# Game state snapshot
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CardRef:
    """Public-facing view of a game object (card/ability/token on a zone)."""

    instance_id: int
    grp_id: int | None            # card database id; None when unknown
    name: str | None              # resolved card name if known
    type_line: str | None         # e.g. "Creature — Beast"
    card_types: tuple[str, ...]   # normalized lower-case types: land/creature/…
    power: int | None = None
    toughness: int | None = None
    loyalty: int | None = None
    controller_seat: int | None = None
    owner_seat: int | None = None


@dataclass(frozen=True)
class ZoneView:
    zone_id: int
    zone_type: str                              # e.g. "battlefield", "hand"
    owner_seat: int | None
    object_ids: tuple[int, ...]                 # ordered as reported


@dataclass(frozen=True)
class PlayerView:
    seat: int
    life: int | None
    starting_life: int | None = None
    max_hand_size: int | None = None
    hand_size: int | None = None                # filled from hand zone when visible


@dataclass(frozen=True)
class TurnInfo:
    turn_number: int | None                     # combined turn count if derivable
    active_player: int | None
    phase: str | None                           # normalized step name if known


@dataclass(frozen=True)
class MatchMeta:
    match_id: str | None
    format_name: str | None                     # e.g. "Brawl_Ladder"
    player_names: Mapping[int, str] = field(default_factory=dict)  # seat -> name


@dataclass(frozen=True)
class GameState:
    """Immutable snapshot after applying one GRE message."""

    snapshot_id: int                            # monotonically increasing
    prev_snapshot_id: int | None                # GRE prevGameStateId linkage
    zones: Mapping[str, ZoneView]               # key "{zone_type}:{owner_seat|pub}"
    objects: Mapping[int, CardRef]              # by instance_id
    players: Mapping[int, PlayerView]           # by seat
    turn_info: TurnInfo
    match_meta: MatchMeta
    local_seat: int | None = None               # seat of the person running the client
    player_deck: tuple[int, ...] = ()           # grpIds of local player's submitted deck
    commander_cards: tuple[int, ...] = ()       # grpIds of local player's commander(s)
    # Dual-source omniscience additions (dual-expansions.md S8.1/S8.4):
    player_decks: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    commander_cards_by_seat: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    seat_knowledge: Mapping[int, "SeatKnowledge"] = field(default_factory=dict)

    @property
    def is_omniscient(self) -> bool:
        """Derived conservative flag: every seat's hand visible AND library
        accounted exactly. Detectors must gate on specific SeatKnowledge
        fields instead of this blanket boolean."""
        if not self.seat_knowledge:
            return False
        return all(
            k.hand_visible and k.library_uncertainty == "exact"
            for k in self.seat_knowledge.values()
        )


@dataclass(frozen=True)
class SeatKnowledge:
    """Per-seat knowledge-completeness tracking (S8.1)."""

    seat: int
    hand_visible: bool = False              # current hand identities known
    hand_fresh_asof: float | None = None    # receiver ts backing hand_visible
    deck_submitted: bool = False            # valid submitted decklist this game
    library_accounted: bool = False         # reconciled with observed library size
    library_uncertainty: str = "unknown"    # exact | estimated | unknown


# --------------------------------------------------------------------------
# Differ / story output
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """A discrete narratable occurrence."""

    kind: str                                   # see events.py constants
    seat: int | None                            # actor's seat; None = ambient
    payload: Mapping[str, Any]
    ts: float
    salience: int                               # 0=filler .. 3=must-speak


# --------------------------------------------------------------------------
# Narrator / speech output
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Utterance:
    uid: str                                    # unique identity for delivery tracking
    match_id: str | None                        # session scoping for staleness
    kind: str                                   # originating event kind
    text: str                                   # final spoken sentence(s)
    salience: int
    ts_created: float
    tempo: str = "normal"                       # deliberate | normal | fast | frenzy
    excitement: str = "normal"                  # calm | normal | tense | electric
    rate: float = 1.0                           # TTS playback speed multiplier
    voice: str | None = None                    # optional per-utterance voice override
    # Dual-booth dialogue additions (dual-expansions.md S3.2/S8.5):
    role: str = "play_by_play"                  # play_by_play | color_analyst
    dialogue_id: str | None = None              # groups anchor+reply pair
    anchor_uid: str | None = None               # reply eligible only after THIS uid delivers ok
    expires_ts: float | None = None             # reply eligibility deadline (receiver clock)


@dataclass(frozen=True)
class DeliveryResult:
    uid: str
    ok: bool
    reason: str = ""                            # required when ok is False
