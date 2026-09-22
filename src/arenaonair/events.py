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

# Proactive strategic / anticipatory kinds
HAND_ONLINE = "hand_online"                # card in hand becomes castable as mana milestone is reached
TUTOR_ANTICIPATION = "tutor_anticipation"  # tutor held/cast, referencing candidate targets in deck
OUTS_ANTICIPATION = "outs_anticipation"    # trailing/lethal pressure, calculating answers in deck
ARCHETYPE_DETECTED = "archetype_detected"  # opponent deck fingerprinted from early plays/companion
GRAVEYARD_RECURSION = "graveyard_recursion"  # card cast or returned from the graveyard

# Tactical shoutcaster kinds
COUNTER_WAR = "counter_war"                # rapid chain of counterspells/responses battling on stack
COMBAT_TRICK = "combat_trick"              # instant-speed buff/trick deployed during combat
CHUMP_BLOCK = "chump_block"                # sacrificial block to absorb heavy attacker
TOPDECK_MODE = "topdeck_mode"              # active player at zero cards in hand drawing off the top
HAND_SCULPTING = "hand_sculpting"          # multiple cantrips/filtering spells cast to set up future turns
UNFAIR_PLAY = "unfair_play"                # high-CMC bomb cheated into play turns ahead of schedule

# Omniscient-strategic kinds -- prerequisite-gated detectors that stay honest
# under partial knowledge ([O1]/[O2]). Each fires ONLY when its own specific
# SeatKnowledge prerequisites are met; unsupported states emit nothing.
TRAP_ARMED = "trap_armed"                  # visible fresh hand holds a reactive card w/ known mana behind it
TRAP_SPRUNG = "trap_sprung"                # public cast walked into a live armed trap within its window
BLUFF_DETECTED = "bluff_detected"          # verified priority delay + qualified visible-hand facts only
CLASH_OF_OUTS = "clash_of_outs"            # exact-accounting answer-count pressure readout

PLAY_BY_PLAY_KINDS = frozenset({
    MATCH_START, MATCH_END, GAME_START, GAME_END, LAND_DROP, CAST, RESOLVE,
    COUNTER, ATTACK_DECLARED, BLOCK_DECLARED, COMBAT_DAMAGE, LIFE_CHANGE,
    BOARD_SHIFT, TURN_START, GRAVEYARD_RECURSION,
    COUNTER_WAR, COMBAT_TRICK, CHUMP_BLOCK, UNFAIR_PLAY,
    TRAP_SPRUNG, CLASH_OF_OUTS,
})

NARRATIVE_KINDS = frozenset({
    NARRATIVE_ARC, NARRATIVE_RESOURCE, NARRATIVE_CALLBACK, NARRATIVE_SPECULATION,
    HAND_ONLINE, TUTOR_ANTICIPATION, OUTS_ANTICIPATION, ARCHETYPE_DETECTED,
    TOPDECK_MODE, HAND_SCULPTING,
    TRAP_ARMED, BLUFF_DETECTED,
})

ALL_KINDS = PLAY_BY_PLAY_KINDS | NARRATIVE_KINDS

#: Anchor milestone events exempt from aggressive pruning or short-pool abbreviations
PRESERVED_KINDS = frozenset({
    MATCH_START,
    MATCH_END,
    GAME_START,
    GAME_END,
})

# Salience scale
SALIENCE_FILLER = 0        # detailed-verbosity only
SALIENCE_LOW = 1           # balanced-verbosity routine events
SALIENCE_HIGH = 2          # combat, counters, meaningful life swings
SALIENCE_MUST_SPEAK = 3    # match start/end, game end, big swings
