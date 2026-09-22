"""Template pools for arenaonair.narrator -- the anti-drone corpus.

Design rules (PRD section 5 anti-repetition contract + DESIGN section 3.5):

* Every event kind owns a pool of templates with STRUCTURAL variety --
  lead with the actor / lead with the card / lead with the consequence /
  short aside / color remark / stat-forward sentence shapes -- never mere
  synonym swaps of one shape.
* Slots use str.format_map syntax ({actor}, {card}, ...) and are filled by
  narrator.Narrator from event payloads plus live GameState context.
* Announcer-not-coach: no template may instruct the player ("you should",
  "consider", ...). Casters observe; they do not advise.
* Layman glosses ({gloss}) translate card data into broadcast-friendly
  phrases ("a two-mana removal spell", "a real threat", "their legend").
"""

from __future__ import annotations

import re

from . import events as ev

# ---------------------------------------------------------------------------
# Small helpers shared with narrator.py
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
    12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
    16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
    20: "twenty",
}


def num_word(n):
    """Small integer -> English word ('two'); larger -> decimal string."""
    try:
        n_int = int(n)
    except (TypeError, ValueError):
        return ""
    return _NUMBER_WORDS.get(n_int, str(n_int))


_SYMBOL_RE = re.compile(r"\{([^{}]+)\}")
_VARIABLE_PIPS = set("WXYZ")


def converted_mana(mana_cost):
    """'{2}{B}{B}' -> int converted cost; None when unparseable."""
    if not isinstance(mana_cost, str):
        return None
    try:
        total = 0
        found = False
        for sym in _SYMBOL_RE.findall(mana_cost):
            sym = sym.strip()
            if not sym:
                continue
            if sym.isdigit():
                total += int(sym)
                found = True
            elif sym.upper() in _VARIABLE_PIPS:
                continue              # variable costs contribute nothing firm
            else:
                pips = [part for part in sym.split("/")
                        if part.strip().isalpha()]
                if pips:
                    total += len(pips)   # each hybrid half counts as one pip
                    found = True
        return total if found else None
    except Exception:
        return None


_TYPE_NOUNS = (
    ("planeswalker", "planeswalker"),
    ("enchantment", "enchantment"),
    ("artifact", "artifact"),
    ("instant", "instant"),
    ("sorcery", "sorcery"),
    ("creature", "creature"),
    ("land", "land"),
)

_REMOVAL_HINTS = (
    "destroy", "exile", "murder", "bolt", "burn", "wrath", "verdict",
    "sweep", "judgment", "plague", "blowout", "fireball", "shock",
)


def _looks_like_removal(name_lower, type_lower):
    joined = "{0} {1}".format(name_lower or "", type_lower or "")
    return any(hint in joined for hint in _REMOVAL_HINTS)


def gloss_phrase(name=None, type_line=None, mana_cost=None, power=None):
    """Broadcast-friendly layman gloss for one card.

    Priority mirrors DESIGN section 3.5 examples::

        legendary            -> '<name>, their legend'
        creature power >=5   -> '<name>, a real threat'
        cheap instant/sorcery smelling of removal ->
                                '<name>, a two-mana removal spell'
        other typed cards     -> '<name>, a three-mana creature'
        nothing known         -> name itself / ''
    """
    name_str = str(name).strip() if isinstance(name, str) and name.strip() \
        else ""
    type_lower = str(type_line).lower() if isinstance(type_line, str) else ""
    cmc = converted_mana(mana_cost)

    try:
        power_int = int(power)
    except (TypeError, ValueError):
        power_int = None

    noun = None
    for needle_label, noun_word in _TYPE_NOUNS:
        if needle_label in type_lower:
            noun = noun_word
            break
    legendary = bool(type_lower) and (
        type_lower.startswith("legendary") or ", legendary" in type_lower)

    if legendary:
        base = name_str or noun or ""
        return "{0}, their legend".format(base).strip(", ") if base else ""
    if noun == "creature" and power_int is not None and power_int >= 5:
        base = name_str or noun or ""
        return "{0}, a real threat".format(base).strip(", ") if base else ""
    if cmc is not None and cmc <= 3 and noun in ("instant", "sorcery"):
        cost_word = num_word(cmc)
        tail = (f"a {cost_word}-mana removal spell"
                if _looks_like_removal(name_str.lower(), type_lower)
                else f"a cheap {noun}")
        return f"{name_str}, {tail}" if name_str else tail.capitalize()
    if noun and cmc is not None:
        article_word = num_word(cmc)
        tail = f"a {article_word}-mana {noun}"
        return f"{name_str}, {tail}" if name_str else tail.capitalize()
    return name_str


# ---------------------------------------------------------------------------
# Controlled-vocabulary phrase tables (arc / streak / beat / speculation)
# ---------------------------------------------------------------------------

ARC_PHRASES = {
    # story.ARCS vocabulary -> caster phrasing
    "even":             "",
    "pulling_away":     "",
    "comeback_brewing": "",
    "standoff":         "",
}

STREAK_PHRASES = {
    # narrative_resource streak_kind -> caster phrasing fragment
}

BEAT_PHRASES = {
    # narrative_callback beat_kind -> caster phrasing fragment
}

SPECULATION_PHRASES = {
    # narrative_speculation kind -> caster phrasing fragment
}


# ---------------------------------------------------------------------------
# Template pools -- play-by-play kinds
# ---------------------------------------------------------------------------
#
# Shape taxonomy per pool (abbreviations used in trailing comments):
#   ACTOR-FIRST / CARD-FIRST / CONSEQUENCE-FIRST / ASIDE / COLOR / STAT-FWD
#
# Every pool below has >= 6 structurally distinct shapes. Short variants
# (escalating brevity) live in TEMPLATE_SHORT_POOLS with the same key.

TEMPLATE_POOLS = {

    ev.MATCH_START: [
        # ACTOR-FIRST
        "We're underway -- {format_name}, {actor} versus {opp}.",
        # FORMAT-FIRST
        "Welcome aboard -- it's {format_name}: {actor} against {opp}.",
        # ASIDE
        "Cards are in the air between {actor} and {opp}.",
        # COLOR
        "Two pilots strap in tonight -- {actor} facing down {opp}.",
        # STAT-FWD
        "Fresh match detected -- {format_name} bracket, buckle up.",
        # CONSEQUENCE-FIRST
        "And we are live! This pairing promises fireworks.",
    ],

    ev.MATCH_END: [
        # RESULT-FIRST
        "That's game -- well played all around.",
        # WINNER-FIRST (winner slot optional; falls back gracefully)
        "And there it is -- ballgame.",
        # ASIDE
        "The curtain falls on this one.",
        # COLOR
        "That wraps our broadcast -- thanks for riding along.",
        # STAT-FWD
        "Final whistle -- what a ride that was.",
        # CONSEQUENCE-FIRST
        "Signing off from this one -- see you next queue.",
    ],

    ev.GAME_START: [
        # VERB-FIRST
        "Game on! Shuffling up now.",
        # ASIDE
        "New game underway -- deep breaths everyone.",
        # COLOR
        "Opening hands hit the table -- here we go again.",
        # STAT-FWD
        "Seven cards each -- let's dance.",
        # CONSEQUENCE-FIRST
        "Mulligans settled -- battle stations.",
        # ACTOR-NEUTRAL VARIANT
        "A brand-new game kicks off.",
    ],

    ev.GAME_END: [
        # RESULT-FIRST (reason-aware when {reason} present)
        "That's game -- well fought.",
        # WINNER-FIRST
        "And that's the match point -- game over.",
        # ASIDE
        "The dust settles on that one.",
        # COLOR
        "What a finish -- hats off to both sides.",
        # STAT-FWD ({reason} interpolates when known)
        "Game over{reason_clause}.",
        # CONSEQUENCE-FIRST
        "The final blow lands -- this one's in the books.",
    ],

    ev.LAND_DROP: [
        # ACTOR-FIRST (classic play-by-play)
        "{actor} plays a land{ordinal_clause}.",
        # CARD-FIRST
        "{card} enters the battlefield for {actor}.",
        # STAT-FWD (resource count forward)
        "{actor} is up to {land_count} lands now.",
        # ASIDE (short color beat)
        "Another land for {actor} -- resources keep flowing.",
        # COLOR (development framing)
        "{actor} develops the board with land number {land_num}.",
        # CONSEQUENCE-FIRST (tempo framing)
        "Mana keeps coming for {actor} -- that's {land_count} sources.",
    ],

    ev.CAST: [
        # CARD-FIRST (named spell)
        "{actor} casts {card}.",
        # GLOSS-COLOR (layman gloss forward)
        "{actor} reaches for {gloss}.",
        # ASIDE (anticipation)
        "{card} -- onto the stack it goes.",
        # STAT-FWD (cost-forward when known)
        "{actor} spends {mana_words} mana on {card}.",
        # CONSEQUENCE-FIRST (threat framing)
        "Here comes trouble -- {actor} fires off {card}.",
        # COLOR (booth chatter)
        "Oh, {card} from {actor}? Spicy.",
    ],

    ev.RESOLVE: [
        # CONSEQUENCE-FIRST (resolution confirmation)
        "{card} resolves{for_actor_clause}.",
        # CARD-FIRST + destination color
        "{card} lands{dest_clause}.",
        # ASIDE (short confirmation)
        "It resolves -- no answer from the other side.",
        # STAT-FWD (effect-now framing)
        "{actor} gets the effect -- {card} is live.",
        # COLOR (unanswered framing)
        "Nothing meets it -- {card} sneaks through{for_actor_clause}.",
        # ACTOR-FIRST payoff framing
        "{actor}'s {card} connects as planned.",
    ],

    ev.COUNTER: [
        # CONSEQUENCE-FIRST (spell dies)
        "{card} is countered!",
        # ACTOR-FIRST (counter-player spotlight)
        "{counter_actor} says not today -- {card} fizzles.",
        # ASIDE (short sting)
            "Denied! {card} never hits the table.",
            # COLOR (interaction drama)
            "Interaction alert -- {counter_actor} stops {card} cold.",
            # STAT-FWD (stack economics)
            "Trade confirmed: one-for-one, {card} down the drain.",
            # CONSEQUENCE-FIRST tempo framing
            "Tempo swing! {actor}'s {card} eats a counter.",
    ],

    ev.ATTACK_DECLARED: [
            # STAT-FWD (power forward, classic caster line)
            "Attackers coming in -- {count_word} creatures, "
            "{power_word} power on the table.",
            # ACTOR-FIRST
            "{actor} swings out with {count_word} attackers.",
            # CONSEQUENCE-FIRST (pressure framing)
            "Red alert for {opp} -- damage incoming from {actor}.",
            # ASIDE (lean combat call)
            "Combat! Bodies are heading across the line.",
            # COLOR (drama-forward)
            "Here comes the pain train -- all aboard!",
            # CARD-FIRST (lead attacker spotlight)
            "{lead_card} leads the charge for team {actor}.",
    ],

    ev.BLOCK_DECLARED: [
            # STAT-FWD (trade forecast)
            "{count_word} blockers meet the rush for {opp}.",
            # CONSEQUENCE-FIRST (trade/no-trade framing)
            "Blocks are in -- expect some trades up front.",
            # ASIDE (lean defensive call)
            "Defense set -- walls are up for {opp}.",
            # COLOR (chess framing)
            "{opp} arranges the defense like a chess grandmaster.",
            # ACTOR-OPP FIRST (attacker perspective denied)
            "{actor}'s attack runs into a wall of blockers.",
            # CARD-FIRST (single-blocker spotlight)
            "{lead_card} steps in front of the traffic for {opp}.",
    ],

    ev.COMBAT_DAMAGE: [
            # STAT-FWD (number forward)
            "{amount_word} damage crashes into {target_actor}.",
            # CONSEQUENCE-FIRST (life-total consequence)
            "{target_actor} takes the hit{life_after_clause}.",
            # ASIDE (lean damage call)
            "Ouch -- that stings for {target_actor}.",
            # COLOR (clock framing)
            "The clock ticks louder for {target_actor}.",
            # ACTOR-FIRST (aggressor credit)
            "{actor}'s assault connects for {amount_word}.",
            # CARD-FIRST (source spotlight when known)
            "{source_card} does the dirty work this time.",
    ],

    ev.LIFE_CHANGE: [
            # STAT-FWD (before/after ladder readout)
            "{actor}: {from_life} {direction_prep} to {to_life}.",
            # CONSEQUENCE-FIRST (swing magnitude forward)
            "{delta_word}-point swing {against_or_for} {actor}{danger_clause}.",
            # ASIDE (lean gain/loss call)
            "Life moves for {actor} -- now sitting at {to_life}.",
            # COLOR (danger-zone drama when low, calm otherwise)
            "{danger_color}",
            # ACTOR-FIRST recovery framing
            "{actor} {claws_or_drops} {to_life}.",
            # CARD-NEUTRAL trend framing
            "The life totals drift apart -- {actor} at {to_life}.",
    ],

    ev.BOARD_SHIFT: [
            # STAT-FWD (count delta forward)
            "{actor}'s board moves from {creatures_before} to "
            "{creatures_after} creatures.",
            # CONSEQUENCE-FIRST (parity framing)
            "Board presence swings toward parity{parity_clause}.",
            # ASIDE (lean count call)
            "{creatures_after} creatures and counting for {actor}.",
            # COLOR (momentum framing)
            "The board tilts -- momentum favors {actor}.",
            # POWER-STAT variant
            "Power on the ground shifts to {power_after_total} "
            "for team {actor}.",
            # CONSEQUENCE-SECONDARY threat escalation framing
            "{actor}'s army grows -- that's getting harder to answer.",
    ],

    ev.TURN_START: [
        # ACTOR-FIRST turn handoff
        "{actor} takes the reins -- turn {turn_word} begins.",
        # ASIDE (lean handoff)
        "Over to {actor}.",
        # STAT-FWD (turn counter forward)
        "Turn {turn_word}: {actor} in the hot seat.",
        # COLOR (rhythm framing)
        "The pendulum swings back to {actor}.",
        # CONSEQUENCE-FIRST (what their turn means)
        "{actor}'s move now -- the plot thickens.",
        # OPPONENT-RELATIONAL framing
        "Handoff complete -- {opp} watches {actor} work.",
    ],

    ev.GRAVEYARD_RECURSION: [
        # ACTOR-FIRST
        "{actor} pulls {card_name} right out of the graveyard.",
        # CARD-FIRST
        "{card_name} comes back from the graveyard for {actor}.",
        # VALUE FRAMING
        "Recursion value: {card_name} rises from the yard for {actor}.",
        # COLOR
        "Not dead yet -- {card_name} returning to play for {actor}.",
        # ASIDE
        "{actor} recurs {card_name} from the bin.",
        # DRAMA
        "Second chapter for {card_name} as {actor} brings it back.",
    ],
    ev.COUNTER_WAR: [
        # CONSEQUENCE-FIRST
        "A full-blown counter-war erupts on the stack over {card_name}!",
        # ACTOR-FIRST
        "{actor} answers the counter as the stack reaches {depth} spells deep.",
        # DRAMA-FORWARD
        "Spells fly back and forth with both sides battling for {card_name}.",
        # TENSION
        "Neither player yields -- the stack is blazing with {depth} responses.",
        # ASIDE
        "Stack battle royale underway; {card_name} hangs in the balance.",
        # COLOR
        "Total counter frenzy on the wire over {card_name}!",
        # STAT-FORWARD
        "{depth} spells clash on the stack in an all-out counter war.",
    ],
    ev.COMBAT_TRICK: [
        # ACTOR-FIRST
        "{actor} unleashes a mid-combat surprise with {card}!",
        # ACTION-FORWARD
        "Combat trick deployed -- {card} flips the math entirely.",
        # RED-ZONE
        "Right into the red zone comes {card} from {actor}.",
        # CONSEQUENCE-FIRST
        "Sudden twist in combat as {card} hits the stack.",
        # ASIDE
        "Sneaky instant from {actor}: {card} alters the battlefield.",
        # DRAMA
        "Surprise maneuver! {actor} turns combat on its head with {card}.",
        # CARD-FIRST
        "{card} arrives during combat to rewrite the engagement.",
    ],
    ev.CHUMP_BLOCK: [
        # ACTOR-FIRST
        "{actor} throws {blocker_name} under the bus to absorb {attacker_name}.",
        # CLASSIC-CALL
        "Classic chump block -- {blocker_name} buys precious time against {attacker_name}.",
        # SACRIFICE
        "Sacrificial defense from {actor}: {blocker_name} steps in front of {attacker_name}.",
        # CONSEQUENCE-FIRST
        "{blocker_name} takes one for the team, absorbing the blow from {attacker_name}.",
        # DRAMA
        "Desperate times call for chumps -- {actor} feeds {blocker_name} to {attacker_name}.",
        # BODIES
        "Bodies on the line as {blocker_name} steps up against {attacker_name}.",
        # COLOR
        "A chump block from {actor} spares their life total for now.",
    ],
    ev.UNFAIR_PLAY: [
        # ACTOR-FIRST
        "{actor} cheats out {card_name} way ahead of schedule on turn {turn}!",
        # FORMAT-CALL
        "Unfair Magic in full effect -- {card_name} crashes down on turn {turn}.",
        # DISBELIEF
        "How is {card_name} already in play? That is a massive turn {turn} bomb.",
        # DANGER-FORWARD
        "Alarm bells ringing for {opp} as {actor} lands {card_name} on turn {turn}.",
        # MANA-FORWARD
        "Bypassing the mana curve entirely: {actor} delivers {card_name} on turn {turn}.",
        # COLOR
        "Early monster alert -- {card_name} enters the fray turns before it should.",
        # CARD-FIRST
        "{card_name} hits the battlefield lightyears ahead of the curve.",
    ],

    # --- Omniscient-strategic detector kinds (payload contracts honored) -----

    ev.TRAP_ARMED: [
        # THREAT-FIRST ({seat}/{threat_name} tolerated via getattr-style slots)
        "Trap status: armed{threat_clause}{seat_clause}.",
        # WARN-FORWARD
        "Heads-up broadcast -- a reactive piece waits in that hand{threat_clause}.",
        # ASIDE
        "Something is cocked and loaded over there{threat_clause}.",
        # COLOR (drama)
        "Quiet hand, loud intentions -- that grip smells like a trap.",
        # STAT-FWD (mana-behind framing)
        "Mana behind it and a receiver ready -- the ambush is staged.",
        # CONSEQUENCE-FIRST
        "Anyone casting into that hand had better be ready to lose a spell.",
    ],

    ev.TRAP_SPRUNG: [
        # VICTIM-FIRST ({victim_name} tolerated)
        "There it is -- the trap springs shut{victim_clause}!",
        # CONSEQUENCE-FIRST
        "Walked right into it -- that cast pays the toll.",
        # ASIDE (lean sting)
        "Snap! Ambush executed exactly as advertised.",
        # COLOR (payoff framing)
        "The armed trap cashes in -- patience rewarded in full.",
        # DRAMA
        "Oh no, right into the teeth of it -- brutal spot.",
        # PAYOFF-STAT
        "Predicted it, delivered it -- that hand was loaded all along.",
    ],

    ev.BLUFF_DETECTED: [
        # READ-FORWARD ({visible_hand_summary} tolerated)
        "Bluff called! That hesitation gave it away{hand_clause}.",
        # ASIDE
        "The pause says everything -- nothing to back it up over there.",
        # COLOR (psychology framing)
        "Poker lesson live on air: that was pure theater.",
        # STAT-FWD
        "Priority stalled, hand exposed -- the bluff unravels on camera.",
        # CONSEQUENCE-FIRST
        "No teeth behind that stare -- the threat was hollow all along.",
        # DRAMA
        "Caught them acting -- the booth sees all, folks.",
    ],

    ev.CLASH_OF_OUTS: [
        # PRESSURE-FORWARD ({pressure_desc} tolerated; never assumes numbers)
        "Outs versus answers -- {pressure_desc}, and every draw matters.",
        # ASIDE (lean tension call)
        "The math tightens{pressure_clause} -- one card decides this.",
        # COLOR (accounting framing)
        "Ledger check{pressure_clause}: somebody's outs run dry soon.",
        # CONSEQUENCE-FIRST
        "Draw steps just became life or death{pressure_clause}.",
        # DRAMA
        "This is the clash we waited for{pressure_clause} -- razor thin!",
        # STAT-FWD (generic pressure framing)
        "Answer accounting live{pressure_clause} -- margins this slim decide games.",
    ],
}


# ---------------------------------------------------------------------------
# Short variants -- escalating brevity (2nd+ occurrence inside a streak)
# ---------------------------------------------------------------------------

TEMPLATE_SHORT_POOLS = {
    ev.MATCH_START: [
        "Underway: {actor} vs {opp}.",
        "New match -- {format_name}.",
        "We're live.",
        "Match found.",
        "Rolling now.",
        "Here we go.",
    ],
    ev.MATCH_END: [
        "Ballgame.",
        "That's game.",
        "Match over.",
        "Wrap it up.",
        "Done and dusted.",
        "See you next queue.",
    ],
    ev.GAME_START: [
        "Game on.",
        "Shuffle up.",
        "Next game underway.",
        "Fresh seven.",
        "Here we go again.",
        "Round starts.",
    ],
    ev.GAME_END: [
        "Game over.",
        "That's game.",
        "In the books.",
        "Finished.",
        "What a finish.",
        "Dust settled.",
    ],
    ev.LAND_DROP: [
        "Land for {actor}.",
        "Another land drop.",
        "{land_count} lands now.",
        "Developing.",
        "Mana up.",
        "Land number {land_num}.",
    ],
    ev.CAST: [
        "{card}.",
        "{actor} casts {card}.",
        "Spell on the stack.",
        "{gloss}.",
        "{card} incoming.",
        "Casting {card}.",
    ],
    ev.RESOLVE: [
        "Resolves.",
        "{card} lands.",
        "No answer.",
        "Through.",
        "{card} sticks.",
        "Effect goes live.",
    ],
    ev.COUNTER: [
        "Countered!",
        "{card} fizzles.",
        "Denied!",
        "Stopped cold.",
        "One-for-one trade.",
        "Not today.",
    ],
    ev.ATTACK_DECLARED: [
        "Attackers incoming.",
        "{actor} swings.",
        "Combat!",
        "{count_word} across the line.",
        "Here comes the rush.",
        "Swing turn.",
    ],
    ev.BLOCK_DECLARED: [
        "Blocks declared.",
        "Defense set.",
        "{count_word} blockers.",
        "Walls up.",
        "Trades coming.",
        "Blocked up front.",
    ],
    ev.COMBAT_DAMAGE: [
        "{amount_word} damage.",
        "{target_actor} takes {amount_word}.",
        "Ouch.",
        "Clock ticking.",
        "Damage lands.",
        "{amount_word} through.",
    ],
    ev.LIFE_CHANGE: [
        "{actor} at {to_life}.",
        "{delta_word} life swing.",
        "Life moves: {to_life}.",
        "{to_life} and falling." if False else "{to_life} for {actor}.",
        "{down_or_up} to {to_life}.",
        "{delta_word}-point move.",
    ],
    ev.BOARD_SHIFT: [
        "{creatures_after} creatures for {actor}.",
        "Board shifts.",
        "{actor}'s board grows.",
        "Counts move for {actor}.",
        "{power_after_total} power now.",
        "Army building.",
    ],
    ev.TURN_START: [
        "{actor}'s turn.",
        "Over to {actor}.",
        "Turn {turn_word}.",
        "{actor} up.",
        "Pendulum swings.",
        "Handoff.",
    ],
    ev.GRAVEYARD_RECURSION: [
        "{card_name} back from the yard.",
        "{actor} recurs {card_name}.",
        "{card_name} returned.",
        "From the bin: {card_name}.",
        "{card_name} returns.",
        "{actor} retrieves {card_name}.",
    ],
    ev.COUNTER_WAR: [
        "Counter-war underway!",
        "Stack fight!",
        "Fighting on the stack.",
        "{depth} spells deep.",
        "Counters flying!",
        "Battle on the stack.",
    ],
    ev.COMBAT_TRICK: [
        "Combat trick: {card}.",
        "Trick in combat!",
        "{card} mid-combat.",
        "Surprise instant.",
        "Combat ambush with {card}.",
        "Combat math shifted.",
    ],
    ev.CHUMP_BLOCK: [
        "Chump block.",
        "{blocker_name} chumps.",
        "Sacrificial block.",
        "Buying time with {blocker_name}.",
        "Chumped by {blocker_name}.",
        "Feeding {blocker_name} to the beast.",
    ],
    ev.UNFAIR_PLAY: [
        "Unfair {card_name} on turn {turn}!",
        "Cheating out {card_name}.",
        "Turn {turn} bomb!",
        "Ahead of schedule: {card_name}.",
        "Massive early drop: {card_name}.",
        "Mana curve shattered.",
    ],
    ev.TRAP_SPRUNG: [
        "Trap sprung!",
        "Right into it.",
        "Ambush lands.",
        "Sprung{victim_clause}!",
        "Patience pays.",
        "Loaded hand cashes in.",
    ],
    ev.CLASH_OF_OUTS: [
        "Outs clash{pressure_clause}.",
        "Math tightening.",
        "Every draw matters.",
        "Razor thin{pressure_clause}.",
        "Answer watch live.",
        "Margins decide this.",
    ],
}

# ---------------------------------------------------------------------------
# Narrative-kind pools (story.py emissions)
# ---------------------------------------------------------------------------

NARRATIVE_POOLS = {
    ev.NARRATIVE_ARC: [
            # ARC-TRANSITION FORWARD
            "{arc_transition_clause}.",
            # LEADER-FIRST
            "{leader_actor} seizes control of the story{arc_tail}.",
            # ASIDE
            "{arc_color}",
            # CONSEQUENCE-FIRST
            "{arc_consequence_clause}",
            # COLOR (drama-forward)
            "Plot twist! {arc_detail}",
            # STAT-FWD (from/to arc slugs)
            "{from_arc_pretty} gives way to {to_arc_pretty}.",
    ],
    ev.NARRATIVE_RESOURCE: [
            # STREAK-STAT FORWARD
            "{streak_clause}",
            # ACTOR-FIRST
            "{actor}{streak_tail}",
            # ASIDE
            "{streak_aside}",
            # COLOR (fortune framing)
            "{streak_color}",
            # CONSEQUENCE-FIRST
            "{streak_consequence_clause}",
            # LENGTH-EMPHASIS variant
            "{length_word} straight{streak_noun_clause} for {actor}.",
    ],
    ev.NARRATIVE_CALLBACK: [
            # CALLBACK FORWARD
            "{callback_clause}",
            # DISTANCE-STAT FORWARD
            "{distance_word} turns later, it still matters{callback_tail}",
            # ASIDE
            "{callback_aside}",
            # COLOR (memory framing)
            "{callback_color}",
            # CONSEQUENCE-FIRST
            "{callback_consequence_clause}",
            # REF-ACTOR variant
            "{ref_actor}'s earlier play still echoing{callback_tail2}",
    ],
    ev.NARRATIVE_SPECULATION: [
            # SPECULATION FORWARD (clearly framed anticipation)
            "{speculation_clause}",
            # CONDITIONAL-FIRST
            "{spec_conditional_clause}",
            # ASIDE
            "{speculation_aside}",
            # COLOR (anticipation drama)
            "{speculation_color}",
            # LEADER-FIRST
            "{leader_actor} eyes a opening here{spec_tail}",
            # STAT-FWD race framing
            "{spec_stat_clause}",
    ],
    ev.HAND_ONLINE: [
        # ACTOR-FIRST
        "{actor} hits {land_count} lands -- {card_name} is officially live in hand.",
        # MILESTONE
        "Curve milestone reached: {card_name} unlocked for {actor}.",
        # MANA-FORWARD
        "{card_name} now playable for {actor} with {land_count} mana online.",
        # COLOR
        "Green light on {card_name} in {actor}'s hand.",
        # TENSION
        "{actor} reaches {land_count} lands -- {card_name} waiting in the wings.",
        # ASIDE
        "{card_name} in hand just found the mana it needs.",
    ],
    ev.TUTOR_ANTICIPATION: [
        # STACK-FORWARD
        "{card_name} on the stack -- {target_count} copies of {target_name} remain in the library.",
        # SEARCH-FORWARD
        "{actor} searches the library with {card_name}; looking for {target_name}.",
        # INTENT
        "Tutor time: {card_name} digging deep into {actor}'s deck.",
        # ACTION-FORWARD
        "{card_name} cast -- {actor} reaching into the deck for {target_name}.",
        # STAT-FORWARD
        "Tutor on the wire -- {actor} has {target_count} {target_name} left to choose from.",
        # COLOR
        "{actor} calls upon {card_name} to locate an answer.",
    ],
    ev.OUTS_ANTICIPATION: [
        # PRESSURE-FORWARD
        "{actor} under lethal pressure, digging for {target_count} {target_name} left in the deck.",
        # CRITICAL-LIFE
        "Life total in critical territory -- {actor} drawing to {target_count} {target_name}.",
        # WALL-AGAINST-BACK
        "Back against the wall for {actor}: {target_count} copies of {target_name} remain as outs.",
        # URGENCY
        "{actor} needs to hit an answer here -- {target_count} {target_name} still in the library.",
        # SWEEPER-WATCH
        "Answer watch: {actor} searching for {target_count} remaining {target_name}.",
        # PIVOTAL
        "Pivotal draw step for {actor} with {target_count} {target_name} left to survive.",
    ],
    ev.ARCHETYPE_DETECTED: [
        # SIGNATURE-FORWARD
        "Early {signature_card} from {actor} -- looks like {archetype_name} today.",
        # CONFIRMED
        "Archetype confirmed: {actor} is on {archetype_name}.",
        # TELL
        "{signature_card} gives it away -- {actor} running {archetype_name}.",
        # ACROSS-THE-TABLE
        "That's {archetype_name} across the table for {actor}.",
        # IDENTITY
        "{actor}'s deck identity revealed: {archetype_name} in action.",
        # GAMEPLAN
        "The gameplan is clear -- {actor} piloting {archetype_name}.",
    ],
    ev.TOPDECK_MODE: [
        # ACTOR-FIRST
        "{actor} is completely empty-handed -- officially in topdeck mode.",
        # TOP-OF-DECK
        "Living off the top of the deck now for {actor}.",
        # DO-OR-DIE
        "Zero cards in hand for {actor}; every draw step is do-or-die.",
        # TANK-EMPTY
        "The tank is empty -- {actor} relying purely on topdeck luck from here.",
        # COLOR
        "Ripping off the top! {actor} enters topdeck territory.",
        # ASIDE
        "No safety net remaining -- {actor} playing off the top of the library.",
        # STAT-FORWARD
        "{actor} hits zero cards -- topdeck mode activated.",
    ],
    ev.HAND_SCULPTING: [
        # ACTOR-FIRST
        "{actor} is deep in the tank, sculpting the hand with multiple cantrips.",
        # VELOCITY
        "Filtering through the deck -- {actor} chains cantrips to set up future turns.",
        # PROGRESS
        "Hand sculpting in progress as {actor} cycles through cards this turn.",
        # INTENT
        "Digging for pieces: {actor} lines up resources with back-to-back cantrips.",
        # METAPHOR
        "Blueprint under construction -- {actor} polishes the hand looking for answers.",
        # VELOCITY-ALT
        "Velocity turn for {actor}, cycling through the library to sculpt the grip.",
        # ASIDE
        "{actor} sculpts the hand with rapid-fire card selection.",
    ],
    ev.TRAP_ARMED: [
        # THREAT-FIRST ({seat}/{threat_name} tolerated via getattr-style slots)
        "Trap status: armed{threat_clause}{seat_clause}.",
        # WARN-FORWARD
        "Heads-up broadcast -- a reactive piece waits in that hand{threat_clause}.",
        # ASIDE
        "Something is cocked and loaded over there{threat_clause}.",
        # COLOR (drama)
        "Quiet hand, loud intentions -- that grip smells like a trap.",
        # STAT-FWD (mana-behind framing)
        "Mana behind it and a receiver ready -- the ambush is staged.",
        # CONSEQUENCE-FIRST
        "Anyone casting into that hand had better be ready to lose a spell.",
    ],
    ev.BLUFF_DETECTED: [
        # READ-FORWARD ({visible_hand_summary} tolerated)
        "Bluff called! That hesitation gave it away{hand_clause}.",
        # ASIDE
        "The pause says everything -- nothing to back it up over there.",
        # COLOR (psychology framing)
        "Poker lesson live on air: that was pure theater.",
        # STAT-FWD
        "Priority stalled, hand exposed -- the bluff unravels on camera.",
        # CONSEQUENCE-FIRST
        "No teeth behind that stare -- the threat was hollow all along.",
        # DRAMA
        "Caught them acting -- the booth sees all, folks.",
    ],
}


# ---------------------------------------------------------------------------
# Phrase tables backing the narrative-kind slots
# ---------------------------------------------------------------------------

ARC_PHRASES = {
    # story.ARCS vocabulary -> caster phrasing fragments
    "even":             "the game settles back to even",
    "pulling_away":     "someone is pulling away",
    "comeback_brewing": "a comeback is brewing",
    "standoff":         "we have a full standoff",
}

ARC_TRANSITIONS = {
    # (from_arc, to_arc) -> clause; missing pairs fall back to generic
    ("even", "pulling_away"):       "the even game just broke wide open",
    ("even", "comeback_brewing"):   "this even game hides a comeback",
    ("even", "standoff"):           "the game congeals into a standoff",
    ("standoff", "even"):           "the standoff finally breaks",
    ("standoff", "pulling_away"):   "the standoff tips -- one side pulls ahead",
    ("standoff", "comeback_brewing"):
        "behind on paper, but the fight is far from over",
    ("pulling_away", "even"):       "the runaway lead shrinks back to even",
    ("pulling_away", "standoff"):   "the chase stalls into a standoff",
    ("pulling_away", "comeback_brewing"):
        "the comeback engine starts humming",
    ("comeback_brewing", "even"):   "the comeback lands -- dead even again",
    ("comeback_brewing", "pulling_away"):
        "the comeback stalls -- the leader surges again",
    ("comeback_brewing", "standoff"):
        "the rally freezes into a standoff",
}

STREAK_PHRASES = {
    # streak_kind -> (clause_template, noun)
    "flood":          ("extra lands {length_word} turns running -- "
                       "that's a proper flood", "flood turn"),
    "screw":          ("{length_word} straight land drops missed -- "
                       "that's land screw", "missed land drop"),
    "hand_pressure":  ("the hand shrank by {length_word} cards -- "
                       "real pressure showing", "card of hand pressure"),
}

BEAT_PHRASES = {
    # beat_kind -> clause fragment
}

SPECULATION_PHRASES = {
    # speculation kind -> clause fragment
}


def arc_phrase(arc):
    """Arc slug -> caster phrase ('standoff' -> 'a full standoff')."""
    return ARC_PHRASES.get(str(arc), "")


def arc_transition_phrase(from_arc, to_arc):
    """Pair of arc slugs -> transition clause; generic fallback."""
    key = (str(from_arc), str(to_arc))
    if key in ARC_TRANSITIONS:
        return ARC_TRANSITIONS[key]
    frm = ARC_PHRASES.get(str(from_arc), str(from_arc))
    to = ARC_PHRASES.get(str(to_arc), str(to_arc))
    if frm and to:
        return f"{frm} gives way to {to}"
    return to or frm or ""


def streak_phrase(streak_kind, length):
    """(streak_kind, length) -> caster clause for narrative_resource."""
    entry = STREAK_PHRASES.get(str(streak_kind))
    if entry is None:
        return ""
    clause_template, _noun = entry
    try:
        return clause_template.format(length_word=num_word(length))
    except Exception:
        return ""


def beat_phrase(beat_kind):
    """beat_kind -> caster clause fragment for narrative_callback."""
    return BEAT_PHRASES.get(str(beat_kind), "")


def speculation_phrase(spec_kind):
    """speculation kind -> caster clause fragment."""
    return SPECULATION_PHRASES.get(str(spec_kind), "")


# ---------------------------------------------------------------------------
# Pool introspection helpers (used by tests + harness)
# ---------------------------------------------------------------------------

def pool_for(kind):
    """Full-length template pool for one event kind (list of strings)."""
    return list(TEMPLATE_POOLS.get(kind, []))


def short_pool_for(kind):
    """Short-variant pool for one event kind (list of strings)."""
    return list(TEMPLATE_SHORT_POOLS.get(kind, []))


def pool_sizes():
    """{kind: (full_pool_size, short_pool_size)} across all known kinds."""
    out = {}
    for kind in sorted(set(TEMPLATE_POOLS) | set(TEMPLATE_SHORT_POOLS)):
        out[kind] = (len(TEMPLATE_POOLS.get(kind, [])),
                     len(TEMPLATE_SHORT_POOLS.get(kind, [])))
    return out


def all_templates():
    """Every template string in every pool (full + short + narrative)."""
    seen = []
    for pool in (TEMPLATE_POOLS, TEMPLATE_SHORT_POOLS, NARRATIVE_POOLS):
        for templates in pool.values():
            seen.extend(t for t in templates if isinstance(t, str))
    return seen


def validate_pools(min_full=6, min_short=6):
    """Assert structural minimums; returns list of violation strings."""
    problems = []
    for kind, templates in TEMPLATE_POOLS.items():
        if len(templates) < min_full:
            problems.append(f"{kind}: full pool {len(templates)} < {min_full}")
        for tpl in templates:
            if not isinstance(tpl, str) or "{" not in tpl and len(tpl) < 8:
                problems.append(f"{kind}: weak template {tpl!r}")
        if len(set(templates)) != len(templates):
            problems.append(f"{kind}: duplicate templates")
        for tpl in templates:
            if shape_signature(tpl) == "":
                problems.append(f"{kind}: empty shape signature")
                break
            break
    for kind, templates in TEMPLATE_SHORT_POOLS.items():
        if len(templates) < min_short:
            problems.append(f"{kind}: short pool {len(templates)} < {min_short}")
        if len(set(templates)) != len(templates):
            problems.append(f"{kind}: duplicate short templates")
    return problems


def shape_signature(text):
    """Coarse sentence-shape fingerprint: first two words + length bucket.

    Used by the drone-detector (QR6): two utterances sharing a signature are
    considered the same SHAPE even when wording differs.
    """
    try:
        words = str(text).split()
        if not words:
            return ""
        head = "_".join(w.lower().strip(",.!?;:") for w in words[:2])
        length_bucket = min(len(words) // 4, 6)
        return f"{head}|{length_bucket}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Dual-booth analyst companion pools (dual-expansions.md S3.x)
#
# Keyed by analyst category (NOT event kind): DialogueSequencer classifies a
# qualifying PBP anchor event into one category deterministically and renders
# ONE companion line from here with role='color_analyst'. Same variety rules
# as every other pool: >= 6 structurally distinct shapes per category,
# announcer-not-coach wording throughout (analyze drama/math; never instruct).
# ---------------------------------------------------------------------------

ANALYST_AGREE_POOL = [
    # READ-ECHO variant
    "Exactly the read I had -- textbook execution right there.",
    # ENDORSEMENT variant
    "Agreed on every count -- that line plays out just as advertised.",
    # PERCENTAGE variant
    "That is the percentage play -- no notes from this booth.",
    # NUMBERS-BACK variant
    "Clean call all around -- the numbers back it every step.",
    # NOD variant
    "Nodding along over here -- nothing worth second-guessing.",
    # VALUE variant
    "Sharp sequencing -- full value extracted from that spot.",
]

ANALYST_DOUBT_POOL = [
    # HEDGE variant
    "Not so sure about that one -- plenty of openings left behind.",
    # SKEPTIC variant
    "Color me skeptical -- that math feels optimistic at best.",
    # TIMING variant
    "Timing looks shaky -- one answer wider and this flips fast.",
    # INSURANCE variant
    "Bold without backup -- I would want insurance before feeling safe.",
    # WINDOW variant
    "Leaves a window open -- opponents live to punish exactly that.",
    # COST variant
    "Questionable price tag -- that is a lot paid for uncertain gain.",
]

ANALYST_TACTICAL_POOL = [
    # RESOURCE-MATH variant
    "Resource ledger says {actor} just banked real advantage there.",
    # TEMPO variant
    "Pure tempo transaction -- {actor} traded permanence for pace.",
    # TRADE variant
    "One-for-one on paper, but {actor} wins the exchange rate.",
    # POSITION variant
    "Position over polish -- {actor} quietly improved the whole board.",
    # THREAT-LEDGER variant
    "Threat ledger updates -- {actor} now holds the bigger stick.",
    # MANA-EFFICIENCY variant
    "Mana efficiency on display -- {actor} spent less to affect more.",
]

ANALYST_EXCLAMATION_POOL = [
    # SWING variant
    "What a swing! The whole complexion of this game just changed.",
    # COUNTER-DRAMA variant
    "Are you kidding me?! Nobody saw that coming from this angle.",
    # LIFE-SWING variant
    "Look at those life totals! This race is officially on fire.",
    # UNREAL variant
    "Absolutely unreal -- that is highlight-reel material right there.",
    # STAKES variant
    "The stakes just skyrocketed -- buckle up for the aftermath!",
    # GASPS variant
    "Wow -- just wow. That line deserves a slow-motion replay.",
]

#: Category name -> pool registry consumed by narrator.DialogueSequencer.
ANALYST_POOLS = {
    "agree": ANALYST_AGREE_POOL,
    "doubt": ANALYST_DOUBT_POOL,
    "tactical": ANALYST_TACTICAL_POOL,
    "exclamation": ANALYST_EXCLAMATION_POOL,
}
# "no template twice within any rolling window of 8" is satisfiable.
# ---------------------------------------------------------------------------

_EXTRA_FULL = {
    ev.MATCH_START: [
        "The booth is set -- {actor} and {opp} about to throw down.",
        "A new challenger scenario loads -- {format_name} rules apply.",
        "Decklists shuffled, nerves steady -- match time.",
    ],
    ev.MATCH_END: [
        "GGs exchanged -- this match goes to the history books.",
        "The arena empties -- match complete.",
        "Victory decided -- we'll see you in the next one.",
    ],
    ev.GAME_START: [
        "Libraries cut, hands drawn -- game faces on.",
        "The battlefield resets -- a clean slate for both sides.",
        "Annxious shuffles settle -- play begins." .replace("Annxious", "Anxious"),
    ],
    ev.GAME_END: [
        "The last point scores -- game set.",
        "One side stands tall -- this game concludes.",
        "The whirlwind stops -- final board state locked in.",
    ],
    ev.LAND_DROP: [
        "{actor} expands the mana base{ordinal_clause}.",
        "{card} tapped gently onto the battlefield for {actor}.",
        "The economy grows -- {actor} adds a land source.",
    ],
    ev.CAST: [
        "{actor} digs deep and unleashes {card}.",
        "From the hand comes {card} -- {actor} acting fast.",
        "{gloss} hits the stack courtesy of {actor}.",
    ],
    ev.RESOLVE: [
        "{card} sails through untouched{for_actor_clause}.",
        "The stack clears -- {card} takes effect.",
        "{actor}'s plan survives -- {card} does its thing.",
    ],
    ev.COUNTER: [
        "{card} gets swatted from the stack by {counter_actor}.",
        "A firm no from {counter_actor} -- {card} undone.",
        "The counter war claims {card} as its victim.",
    ],
    ev.ATTACK_DECLARED: [
        "{count_word} attackers tip toe across for {actor}." .replace("tip toe", "tiptoe"),
        "{actor} commits {count_word} to the red zone.",
        "Sabers rattling -- {actor} sends everyone in.",
    ],
    ev.BLOCK_DECLARED: [
        "{opp} signs off on {count_word} blockers.",
        "The defensive line forms for {opp}.",
        "{opp} chooses caution -- blockers everywhere.",
    ],
    ev.COMBAT_DAMAGE: [
        "{amount_word} points slide through to {target_actor}.",
        "{target_actor} braces -- {amount_word} damage arrives.",
        "First aid needed -- {target_actor} absorbs {amount_word}.",
    ],
    ev.LIFE_CHANGE: [
        "{actor}'s life ticker reads {to_life} now.",
        "{delta_word}-point movement leaves {actor} at {to_life}.",
        "The lifepad updates -- {actor}, {to_life} strong." .replace("lifepad", "life pad"),
    ],
    ev.BOARD_SHIFT: [
        "{actor}'s ranks swell to {creatures_after}.",
        "Creature math updated for {actor}: {creatures_after} on board.",
        "{actor} reshapes the ground game{parity_clause}.",
    ],
    ev.TURN_START: [
        "{actor} steps up -- turn {turn_word} underway.",
        "The spotlight finds {actor} for turn {turn_word}.",
        "{actor} plots while {opp} schemes -- turn {turn_word}.",
    ],
}

for _kind_key, _extras in _EXTRA_FULL.items():
    _existing = TEMPLATE_POOLS.get(_kind_key, [])
    TEMPLATE_POOLS[_kind_key] = _existing + [
        _t for _t in _extras if _t not in _existing]
del _kind_key, _extras, _existing
