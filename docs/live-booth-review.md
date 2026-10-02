# Live booth review — September 21, 2026

September 22 direction: use an LLM to write original commentary and conversation
for both speakers, instead of implementing more scripted phrase pools. See
[the generative booth direction](llm-booth.md). The factuality, delivery-history,
and scheduling findings below still apply.

The supplied transcript from 23:33:34 through 23:40:04 contains 41 PBP calls and
19 analyst replies. It ends during combat, so this review covers that segment,
not an unrecorded match ending. Earlier sessions in the paste did not log their
spoken lines and cannot be assessed as complete transcripts.

The main problem is that the analyst adds approval, not analysis. Sixteen of the
19 replies are generic praise; the other three are generic exclamations. None
names a card, player, damage amount, or specific resulting board change.

| Analyst phrase family | Occurrences |
| --- | ---: |
| “Nodding along … nothing worth second-guessing” | 2 |
| “Exactly the read I had … textbook execution” | 3 |
| “Sharp sequencing … full value extracted” | 2 |
| “The percentage play … no notes” | 1 |
| “Agreed on every count … as advertised” | 4 |
| “Clean call … the numbers back it” | 4 |
| Generic surprise/race/swing exclamations | 3 |

The implementation explains this pattern: `classify_analyst_category()` defaults
to `agree`, and `DialogueSequencer` qualifies almost every high-salience anchor.
Rendering another sentence from a small synonym pool does not create another
observation. Neither does a delivery dependency alone create a conversation.
The PBP has often only named a card, leaving no actual claim for “Agreed” to
respond to. “Full value,” “percentage play,” and “textbook execution” additionally
assert evaluations that the current rules engine has not established.

The PBP is more informative when it retains names and numbers, but uneven:

- “Iannyk179 casts Llanowar Elves” and “Iannyk179's life ticker reads 22” give
  concrete information. “Our player swings,” “Damage lands,” and “Swing turn”
  omit the actor, consequence, or both. Compression should preserve the facts.
- Llanowar Elves is called at 23:34:07 and its resolution at 23:34:42, after a
  Chatterfang call. Maelstrom Pulse is called at 23:38:12 and its resolution at
  23:39:00. These are spoken-call gaps, not a measurement of actual resolution
  latency. They warrant tracing queue age and merging related events; they do
  not by themselves prove when the spells resolved in the game.
- One point of damage is followed by “This race is officially on fire.” Hype
  should follow a verified change in danger, not the combat-damage event kind.
- “One points” is a grammar bug. “We have a full standoff gives way to the game
  settles back to even” combines phrases that cannot fit the surrounding
  grammar. Arc narration also reverses to a standoff again soon afterward.
- Cast and resolution each receive approval for Fanatic of Rhonas, Goldvein
  Hydra, and Maelstrom Pulse. That doubles airtime without doubling information.
- An unknown spell was the Grim Tutor printing missing from the external cache.
  The opening-hand topdeck call was a stage-gating bug, not an actual hand-state
  assessment. Those two implementation fixes are included in the current work.

## Recommended order of improvement

1. **Replace approval with evidence.** An analyst contribution must contain one
   new, verified fact or explain a rule relevant to the observed play. Otherwise
   stay silent. Delete “no notes,” “nothing to add,” and unsupported claims of
   optimal play; do not just expand their synonym pool.
2. **Give the booth actual exchanges.** Prepare both sides around a supported
   question or observation. Let the PBP hand over explicitly, then return to the
   consequence when it happens. Avoid forced handoffs on routine actions.
3. **Budget airtime.** Target one substantive intervention per turn by default,
   with exceptions for genuinely major developments. Use actual successful
   deliveries to enforce repetition/cooldown limits. Do not reserve airtime for
   a long generic reaction while current combat facts are waiting.
4. **Group one play into one discussion.** Cast, resolution, and resulting
   effects need a shared event identity. Merge pending calls when useful; do not
   automatically attach another analyst reply to every stage of the same play.
5. **Keep short calls informative.** Prefer player + named card or damage +
   resulting life to anonymous hype. Attribute attackers from verified public
   objects; if identity is unknown, say what is known without calling them
   “our player.” Treat fabricated numbers or uncertain attribution as a failure.
6. **Make callbacks factual.** Remember a real earlier point, then revisit it
   when an observable event supports the connection. Retain the distinction
   between the moment of the event and a later broadcast recap.
7. **Stabilize tone and grammar.** Use danger thresholds for excitement and
   sustained evidence before announcing an arc reversal. Build number agreement
   and complete transition clauses explicitly rather than concatenating prose.

An illustrative handoff for the identified tutor, using its public rules:

> PBP: “Grim Tutor—what's the tradeoff here?”
>
> Analyst: “A specific card from the library, at the cost of three life.”
>
> PBP, after the life change is observed: “That leaves Iannyk at 22.”

The last line requires the observed life total; it must not be predicted or
filled in from the example. Nothing in the exchange claims to know the searched
card. For cards or consequences the system cannot verify, omit the analysis.

## Acceptance checks for that redesign

- A cast with no supported analytical addition produces no filler reply.
- A cast and its resolution cannot earn two generic replies about one play.
- Ordinary one-damage events do not automatically receive maximum excitement.
- Delivered analyst lines introduce concrete information and avoid repeating the
  same point, even when wording differs. Track actual spoken history, not just
  generated-and-pruned candidates.
- Exchanges reference the same event and preserve anchor delivery, cancellation,
  private-state expiry, game boundaries, and urgent PBP priority.
- Measure event-to-dispatch age and actual role airtime on a full match. An
  additional voice is useful only if its contribution is worth the delay.

These are review recommendations; the broader dialogue/content redesign is not
claimed as implemented by the current bug-report-button and data-correctness work.
