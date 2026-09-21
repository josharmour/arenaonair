"""Event kind constants and salience scale.

Shared vocabulary between differ, story model, narrator, and tests.
Adding a kind is additive; renaming/removing one requires bumping the
interface contract (models.py + this file) in the same commit.
"""

# Play-by-play kinds
MATCH_START = "match_start"
MATCH_END = "match_end"
GAME_START = "game_start"          # individual game within a match (Bo3/Bo5)
GAME_END = "game_end"
LAND_DROP = "land_drop"
CAST = "cast"
RESOLVE = "resolve"
COUNTER = "counter"
ATTACK_DECLARED = "attack_declared"
BLOCK_DECLARED = "block_declared"
COMBAT_DAMAGE = "combat_damage"
LIFE_CHANGE = "life_change"
BOARD_SHIFT = "board_shift"
TURN_START = "turn_start"

# Story / narrative kinds (emitted by story.py)
NARRATIVE_ARC = "narrative_arc"            # arc transition (standoff breaks, comeback…)
NARRATIVE_RESOURCE = "narrative_resource"  # flood/screw streaks, hand pressure
NARRATIVE_CALLBACK = "narrative_callback"  # reference to an earlier beat
NARRATIVE_SPECULATION = "narrative_speculation"  # framed anticipation

PLAY_BY_PLAY_KINDS = frozenset({
    MATCH_START, MATCH_END, GAME_START, GAME_END, LAND_DROP, CAST, RESOLVE,
    COUNTER, ATTACK_DECLARED, BLOCK_DECLARED, COMBAT_DAMAGE, LIFE_CHANGE,
    BOARD_SHIFT, TURN_START,
})

NARRATIVE_KINDS = frozenset({
    NARRATIVE_ARC, NARRATIVE_RESOURCE, NARRATIVE_CALLBACK, NARRATIVE_SPECULATION,
})

ALL_KINDS = PLAY_BY_PLAY_KINDS | NARRATIVE_KINDS

# Salience scale
SALIENCE_FILLER = 0        # detailed-verbosity only
SALIENCE_LOW = 1           # balanced-verbosity routine events
SALIENCE_HIGH = 2          # combat, counters, meaningful life swings
SALIENCE_MUST_SPEAK = 3    # match start/end, game end, big swings
