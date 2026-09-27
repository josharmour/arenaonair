# LLM booth validation — September 22, 2026

The implementation runs end to end against the real local `glm-5.3-flash`
gateway, with model-written text for both voices and a recording speaker.
**This is a guarded generative implementation, not a claim that the requested
factual-quality bar is fully met.** Real transcript review exposed errors that
an LLM auditor alone accepted. Local guards now reject the specific unsupported
resolution, life-causation, attack-exclusivity, uncited rules, unobserved targets,
spelled-number errors, and one-time/next-creature broadening cases
found during this work. Other semantic errors can still pass; continued review
is needed before relying on it unattended.

## Automated checks

The full suite is run with `PYTHONPATH=src:. pytest --disable-warnings`.
The full run passed **581 tests**, with two optional tests skipped and one
existing pytest fixture deprecation warning. After the final reference, numeric,
and completeness guards, **85 focused model/application tests passed**. These
focused tests overlap the full suite; they are not an additional 85 distinct tests.
`git diff --check`, module compilation, CLI help, and configured model-mode startup
also passed. A source scan found no occurrence of the application credential.

The 9 failures in the initial 565-test working tree were obsolete analyst-pool
expectations and missing companions in the unfinished draft. Those contracts
were replaced with model-response/delivery tests while retaining queue, voice,
ingestion, and pacing coverage. Legacy PBP variety was preserved.

Test coverage includes real single-log parsing into the shared booth, optional
private enrichment and source loss, freshness, delivered-only memory, bounded
pending work, distinct copies/recasts, repeated combat snapshots, duplicate play
stages, timeouts/backoff, shutdown, game boundaries, invalidated queued evidence,
transitive orphan cancellation, voice routing, schema/number/content rejection,
and a single repair attempt that cannot enter heard memory before delivery.

## Backend verification

- Local gateway: `http://127.0.0.1:8444/v1`; selected advertised alias:
  `glm-5.3-flash`.
- Dedicated model-scoped virtual key created outside the repository with mode
  0600. The app does not use the LiteLLM master key. Shared services were unchanged.
- Public `https://api.mtgacoach.com/v1/models`: HTTP 200 using the dedicated key
  and `User-Agent: ArenaOnAir/0.1`.
- Live writing with JSON-schema roles/reference enums succeeded. Only final
  assistant content was processed; reasoning was never copied or spoken.
- Medium-effort auditing exceeded the live completion/time budget on recorded
  contexts. Runtime uses low effort with explicit claim checks and one repair.
- A targeted real audit rejected “Tenacious Pup boosts itself” and incorrect
  tutor ordering, and accepted their correct counterparts. Low-effort times for
  those four probes were 1.329, 0.569, 0.563, and 0.430 seconds respectively.
  These are individual probes, not a broad evaluator accuracy score.

The card cache was rebuilt with rules for 19,978 Arena IDs. The specific printing
93987 was still absent on this server, which has no discovered native Arena DB.
The installed-Arena fallback remains available on the Mac, and an exact-name
match can attach cached rules to a natively resolved printing. Missing identities
or rules remain unknown; successful canonical Grim Tutor lookup in a synthetic
case does not prove that every printing resolves here.

## Real replays

All runs used a recording speaker. No audio was played on server hardware. Local
JSON artifacts have permissions 0600 and contain candidate/delivery transcripts
and safe timing/counters, not keys or reasoning. They are intentionally outside
the repository because recorded commentary can contain private facts.

Synthetic evaluation (`/tmp/arenaonair-validated-synthetic.json`) exercised ten
model batches: pregame/mulligans, a supplied mulligan count, tutors with and without
life loss, stack departure, an unrelated life change, two distinct Elves, combat,
and a repeated observation. Eight turns reached the recording speaker. Total
writer/auditor/repair latency ranged from 0.978 to 3.569 seconds, median 2.565.
Four batches remained rejected after repair; a repeated observation was suppressed.
The analyst sometimes initiated a line. Its conditional Grim Tutor explanation
kept searching, putting the card into hand, shuffling, and losing life in order.
The ordinary one-damage line reported only damage and the observed resulting total.

A real deadline probe (`/tmp/arenaonair-real-deadline.json`) used a 0.1-second age
budget. The actual model returned after 0.519 seconds; the response was rejected
as stale, with zero speech. Deterministic tests separately cover changes during
requests and speech. A timeout in a recorded run exercised backoff and skipped
pending actions rather than accumulating a backlog.

Recorded replays use `fixtures/matches/match_03.jsonl`. Successful runs produced
original questions and answers about Green Sun's Zenith and Utopia Sprawl, without
forcing a second speaker for every event. They also exposed factual overreach,
including inferred life-loss causation and incorrect interpretation of a boon.
The final run below was made after the deterministic guards for those findings.

The evaluator deliberately paces recorded batches and lets transport backoff
finish between them. This establishes real model integration and inspectable
quality/latency samples. It does not simulate a full-speed match or measure Kokoro
synthesis, audible handoff, or physical playback success. The existing conservative
fusion still lacks paired real-client recordings proving cross-client GRE alignment.
The synthetic mulligan count is a fixture observation; the live differ does not
claim to detect every mulligan decision.

Configuration and launch instructions are in
[the generative booth guide](llm-booth.md).


## Final quality review

The later recorded review still found errors slipping past the model auditor:
“eighteen” after a three-life loss from twenty, a six-mana claim for a cost of
five plus two green, and a one-time boon described too broadly. The final local
validator normalizes spelled numbers as well as digits, requires rule citations,
and preserves explicit one-time/next-creature scope. Unknown or truncated public
zones are never marked complete. Tests exercise these failures directly.

This evidence is why the feature is described as guarded and experimental. A
schema, a second model call, and a growing set of local checks still do not prove
all arbitrary prose is correct. In particular, frequent rejected batches mean
missing commentary; lowering safeguards simply to increase airtime would conceal
the observed quality problem. The original “no fabricated results” acceptance
bar is **not certified by these runs**.


The pre-knowledge-cache recorded check was
`/tmp/arenaonair-final-numeric-recorded.json`: **8 model batches,
7 recording-speaker deliveries**, with total batch latency
**1.347–4.566 seconds**, median **2.464 seconds**.
Its counters were `{"discussed_play": 3, "duplicate_combat": 2, "factual_rejection": 3, "generated": 7, "initial_factual_rejection": 3, "initial_unsupported_causality": 1, "initial_unsupported_resolution": 1, "initial_unsupported_rule": 1, "queued": 7, "repair_attempts": 6, "spoken": 7, "unsupported_rule": 1}`.
The retained artifacts allow inspection of both rejected candidates and deliveries.

## Local card grounding (September 22)

Built `~/.cache/arenaonair/knowledge.sqlite` from official Scryfall Oracle/tag
exports and the official Commander Spellbook bulk export: 38,906 card identities,
4,556 tags, 236,260 tag assignments and 108,809 complete combo recipes, about
528 MiB. A first local lookup took 10.8 ms; 100 subsequent lookups across five
cards measured median 0.187 ms and maximum 1.281 ms. These measure local retrieval,
not model generation or audio delivery. Source export dates and reference URLs
are included in the evidence passed to both model calls.

The full suite passed **600 tests, 2 skipped** before the last narrow safeguards.
After those changes, **102 focused knowledge/booth/application tests passed**.
Coverage includes no network during lookup, corrupt/missing/stale sources, exact
Arena identity, token/name collisions, conflicting rules, atomic refresh and
failed-refresh preservation, complete combo prerequisites, oversized-record
omission, worker/ingestion isolation, and unsupported meta claims.

Actual `glm-5.3-flash` tests used the recording speaker, not audio:

- `/tmp/arenaonair-knowledge-synthetic.json`: eight general synthetic batches.
- `/tmp/arenaonair-knowledge-recorded.json`: eight batches from `match_03.jsonl`.
- `/tmp/arenaonair-knowledge-cards.json`: four named-card casts, with retrieved
  knowledge included in the restricted artifact. All four received local facts.
- `/tmp/arenaonair-knowledge-final-cards.json`: final two-card check after adding
  stricter combo-partner, resolution and partner-question checks. Demonic Tutor
  was suppressed; Utopia Sprawl delivered two lines. Batch times were 2.666 and
  3.282 seconds respectively.

Manual review of the earlier named-card run found errors the model auditor had
approved: an overbroad “each counter” Scurry Oak explanation and an underspecified
Approach of the Second Sun payoff, among others. Narrow deterministic checks now
require named combo partners and reject additional observed bad phrasing, but
do not constitute a semantic proof of every condition in a recipe. The new data
does **not** certify factual accuracy or remove the limitations above. Rejected
batches stay silent; no unsupported claim is deliberately converted into a
fallback script. EDHREC rank supplies Commander popularity only, not a complete
EDHREC analysis database or Arena metagame statistics.
