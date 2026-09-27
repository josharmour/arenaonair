# Generative booth

The generative booth is ArenaOnAir's default product: original exchanges between
play-by-play and color commentators, written from observed Arena game facts.
Our hosted model is **GLM 5.3**, API model ID `glm-5.3-flash`. Generation and factual
checks can still make mistakes or reject lines, leaving gaps.

## Connect your model

After [installation](../README.md#install), open **Connect / subscription…**.

- **Five free matches:** connect to our server without payment details. The app
  stores a private trial credential; the server stores its hash and a device
  identifier. A match counts on its first successful model response. Reconnects
  and games within one best-of-three match use the same match. Voice previews and
  model discovery do not spend matches. A session is limited to four hours and
  1,200 requests; five match sessions are offered per device trial.
- **Premium:** open [mtgacoach.com/subscribe](https://mtgacoach.com/subscribe),
  subscribe through Patreon, link your Patreon account, and copy the issued key.
  Paste it in the dialog. Existing patrons use their existing key. The endpoint
  `https://api.mtgacoach.com/v1` and model `glm-5.3-flash` are preconfigured.
- **My own provider:** enter the HTTP(S) endpoint, optional port, and model. Use
  **Find local providers** to check loopback ports 11434, 1234, 8000 and 8080, or
  **Load models** for your entered endpoint. Select a loaded model. An API key is
  optional for servers without authentication. Discovery does not install or
  start a model. The provider needs chat completions, JSON-object output and
  strict JSON-schema output; a model list alone does not verify those capabilities.

**Connect and restart** saves the connection and relaunches the booth. The CLI
`setup` command also offers trial, premium, custom and later choices. Trial keys
are stored beside the config in `trial.key`; dialog-entered provider keys are in
`provider.key`. CLI-entered keys use `~/.config/arenaonair/llm.key`. Keys are kept
out of TOML and created with mode 0600 on macOS/Linux. Never share these files.
Keep the trial key when updating or reinstalling; a device identifier alone cannot
recover an existing trial's credential.

Hosted commentary sends selected game facts, card context, allowed hand facts,
relevant match-history facts and recent booth dialogue to our server. A custom
provider receives that context instead. The app does not send the raw Arena log.
Speech synthesis and saved match history are local after initial downloads.
A fully local setup requires a local language model as well as local voices.
Disable hand commentary in Settings when desired.

Custom provider charges and limits belong to that provider. `doctor --online`
checks model connectivity; on a trial it checks access and remaining matches
without generating commentary or spending a match. It does not certify commentary
quality. If a connection fails or the trial ends, the booth stays silent and
shows the error. Use the connection dialog to subscribe or choose another provider.
The scripted writer remains an explicit replay/diagnostics option
(`--narration-mode legacy`), not an automatic fallback.

## Manual configuration

Merge the following into `~/.arenaonair/config.toml`, replacing the example URL,
model, and key path with your own. Do not duplicate existing TOML sections.

```toml
narration_mode = "auto"

[llm]
base_url = "https://your-provider.example/v1"
model = "your-model-name"
key_file = "~/.config/arenaonair/llm.key"
profile = "generic"
timeout = 12.0
max_age = 24.0
coalesce = 0.35
```

Use `profile = "glm"` only for an endpoint accepting the GLM-specific template
parameters described below. `ARENAONAIR_API_KEY` can replace the credential file;
`ARENAONAIR_BASE_URL`, `ARENAONAIR_MODEL`, and `ARENAONAIR_KEY_FILE` override TOML.
CLI flags override those settings. Keep keys out of CLI arguments and source
control. Without a configured URL, `auto` uses the built-in scripted booth.

## Backend and card data

Development validation used a GLM gateway advertising `glm-5.3-flash` on
September 22, 2026. Structured JSON-schema generation and model discovery were
exercised there; this is a historical test result, not a service offered by this project.
Requests carry `User-Agent: ArenaOnAir/0.1`; redirects never forward credentials.
The GLM profile sends `chat_template_kwargs` with `thinking=true`, using `low`
reasoning effort for both writing and evidence auditing. Medium effort was also
probed: it improved targeted audit cases but exceeded live token/time budgets on
recorded context, so the runtime retains low effort and explicit claim checks.
Only final assistant content is read. Reasoning fields are never used or saved.
`profile="generic"` omits these GLM-specific settings; another model still needs
chat completions with JSON-schema output and must be evaluated separately.

Rules are read from the local card cache. Its builder now retains Scryfall oracle
text, including separately labelled faces. Existing caches without rules still
work, with rules explicitly unknown. Installed Arena data remains the fallback
for missing identities; an exact-name cache match can supply rules for that
printing. No network card lookup runs in the ingestion or speech thread.

```bash
python3 -m tools.build_carddb
# Or reuse an existing downloaded Scryfall bulk file:
python3 -m tools.build_carddb --from-file /path/to/default_cards.json
```

## Local card knowledge

The generative booth also reads `~/.cache/arenaonair/knowledge.sqlite` for sourced
card roles and Commander context. Build or refresh it from the checkout:

```bash
PYTHONPATH=src:. python3 -m tools.build_card_knowledge
```

This downloads the official [Scryfall bulk exports](https://api.scryfall.com/bulk-data)
(`oracle_cards` and `oracle_tags`) and the
[Commander Spellbook bulk catalog](https://backend.commanderspellbook.com/schema/).
Scryfall supplies Oracle rules, keywords, community functional tags, EDHREC
popularity rank, and the Commander Game Changer flag. Spellbook supplies named
combo pieces, quantities, starting zones, mana, other prerequisites, steps,
results and format legalities. Source URLs and export/download dates accompany
the model evidence. These are structured facts, not prewritten commentary.

The September 22 snapshot contains 38,906 card identities, 4,556 tags, and
108,809 combo recipes; the SQLite index is about 528 MiB. It is separate from
the Arena printing-ID cache above. Copy both caches to the machine running the
booth, or build there. `--out PATH` changes the output; set
`ARENAONAIR_KNOWLEDGE_DB=PATH` on the app to read a custom location.
`--from-dir PATH` rebuilds saved exports without network access (see tool help).

Refresh weekly using the same command. There is no scheduled task installed and
no automatic download at launch. Builds replace the database atomically; failed
builds preserve the old cache and the running app notices successful replacements.
Sources older than 14 days supply no knowledge facts until refreshed. Missing or
unusable caches fall back to existing local rules/observations.

Live lookup is entirely local and runs on the generation worker, outside the
ingestion lock. Only identities resolved for current events are looked up. No
card, hand, player or log data goes to Scryfall/Spellbook during play. Exact names
and matching Oracle text prevent mixing paper and Arena rebalanced versions;
ambiguous names and conflicting rules produce no enrichment. A bounded evidence
budget selects up to two complete combo recipes per card; oversized recipes are
omitted rather than losing prerequisites.

EDHREC rank is Commander deck popularity, not an Arena win rate or a detailed
EDHREC synergy analysis. This does not mirror EDHREC's articles, commander-specific
recommendations or lift scores. Role tags and catalog recipes explain potential
uses; they never establish a player's intent or that a combo is available in the
current match. Game Changer claims must explicitly refer to Commander. The writer
and factual auditor both receive these constraints; the existing observed-event,
private-fact freshness and delivery checks still apply.

## Runtime

`llm_booth.py` owns one worker and one context for both named roles. Ingestion
submits whitelisted observed event fields and immutable state. The writer receives
public objects, life, turn/phase/stage, available rules, uncertainty, and fresh
visible hands. It never receives raw logs, account configuration, or guessed
library composition. Private hands require a coherent chain, known membership,
resolved identities, and receiver age no greater than five seconds. Losing a
source invalidates dependent work without disabling either voice.

The LLM input treats legacy RESOLVE events as stack departures, not proof of
successful resolution. Counter claims require explicit annotation evidence.
Combat request candidates without confirmed GRE attack/block states are excluded.
Damage without a known source controller does not invent an actor by elimination.
Mana cost does not establish payment; event order does not establish causation.

### Game read and commentary focus

The story model's `read()` turns public state into plain facts: each player's life, life
change over the last four turns, creatures and total creature power (whether that
matches the opponent's life), lands, hand size and own turns in a row without a new land,
plus a shape (`race`, `comeback_brewing`, `pulling_away`, `standoff`, `even`) with its
meaning. Shapes are defined only by numbers the read reports, so the fact-check can verify
them; the scripted arcs are separate and unchanged. The writer sees the latest read as
`state:game_read`. At a turn start, a `game_read` event carrying that read (frozen, so
later board changes don't void the line) opens a moment for one line on where the game
stands: the analyst's second standing job, or play-by-play in a solo booth. Moments start
on the third turn of a game.

`commentary_focus` (`[broadcast] focus`, `--focus`, the window's Focus control) paces it:

| Focus | Game-read moments | Analyst lines per turn | Context only, not a cue |
|---|---|---|---|
| `calls` | when the shape or who leads it changes | 1 | nothing |
| `balanced` (default) | when the read changes, else every 2 turns | 2 | land drops |
| `analysis` | every turn | 3 | land drops, stack departures |

Context-only events join the next exchange's facts without triggering a request. An
explicit `analyst_lines_per_turn` overrides the focus. The writer also receives the
focus as trusted guidance, like the persona style. A local check (`coaching`) rejects
advice or verdicts on decisions ("should attack", "needs to find", "mistake", "the right
play"); conditionals such as "should those attackers connect" stay allowed. Another
(`internal_jargon`) keeps the read's own vocabulary off air ("game read", "the read says",
field names, "officially"): a September 23 replay of match_03 showed the model narrating
its notes ("Game read says even…") until told to speak in broadcast language. The scripted
booth follows the focus too: Mostly calls drops its low-salience narrative lines and
Mostly analysis drops routine land-drop, resolve and turn-start calls.

### Coaching (opt-in)

`coaching` (`[broadcast] coaching`, `--coaching`, the window's Coaching box) is off by
default. When on, the writer receives trusted `coaching` guidance: one suggestion per
exchange for the listening player (`state:local_seat`), framed as a suggestion, reasoned
from cited facts (board, life, game read, card rules, the listener's own hand) and never
from another player's private hand. The prompt's no-advice rules apply only while it is
off; the local `coaching` check stands down; the fact-check receives `coaching_allowed`
and judges the suggestion's factual premises, not the opinion. Shared-log routes already
keep every hand off air unless `spectator` is set, so advice there can't use the
opponent's hand either.

Close events coalesce for 350 ms. Pending input is bounded to 16 events; recent
observations to 20; context to 48,000 serialized characters. Cast and departure
stages share an instance-based play ID, and a later cast of the same object starts
a new play. Distinct copies never deduplicate by name. New writing waits for the
current exchange to finish so its context reflects what listeners actually heard.
Urgent events cancel queued lower-priority exchanges and preempt mundane speech.

The writer returns up to three short turns (60 words total), or silence. The
schema restricts roles and reference IDs. Local checks reject malformed output,
reasoning/instructions, unsupported numeric literals or spelled numbers, internal IDs, unanswered
handoffs, and unsupported causal/exclusivity language. Explicit guards also prevent an
unqualified resolution claim and broadening a next-creature boon to other creatures. A separate GLM audit checks
**each line against only its cited evidence**. A single bounded repair attempt may
correct a rejection before the deadline. A valid JSON shape is not a factuality
certificate; semantic auditing is an additional, imperfect check.

The request and all its speech expire 24 seconds after the oldest relevant event
arrived. A play-by-play line must also *start* within `llm_call_max_age` (10 s, Settings >
AI model) of its play, or it is dropped (logged, with its replies) before speaking: on
September 23 a 30-word Force of Will call started 12 s after the cast and described a
stack that had long resolved. Game-ending calls are exempt; nothing is cut mid-sentence
by this limit. Calls are asked to stay under 18 words, and to shrink to one line or
silence while earlier lines are still waiting. HTTP requests time out after twelve seconds. Transport failures back off
up to 30 seconds and drop pending work. Match/game epochs, cited state facts,
private-context freshness, spoken-history changes, and shutdown invalidate work.
Routine unrelated GRE ticks do not expire a public observation.

Every exchange turn depends on successful playback of the previous one. Queue
expiry, pruning, failure, cancellation, or a missing anchor cascades through all
subsequent turns. The existing speech pump routes each role to its configured
voice. Only successful delivery enters the shared history; failed/interrupted
speech cannot support a callback. Sixteen recent delivered lines and 24 older
points retain their wording, event references, and supporting facts. This is a
bounded evidence-backed memory, not an invented model summary. Game boundaries
reset it. Closing stops the worker and rejects late responses.

The status window and copied report include safe model state, request latency,
event-to-delivery age, pending count, and rejection/drop reasons. Role/text remain
in local delivery logs. Copied reports conservatively omit **all LLM speech**,
because a line can mix public and private facts. They exclude credentials,
reasoning, and payloads; they do not export the evaluation transcripts below.

## Evaluation and limits

Deterministic tests cover single-source input, enriched/degraded hands, private
freshness, shared delivered memory, silence, malformed output, factual rejection,
timeouts/backoff, stale generations, game transitions, object identity, recasts,
cast/departure grouping, transitive orphan cancellation, shutdown, queue bounds,
and role/voice routing. Existing queue, pacing, ingestion, and voice regressions
remain; obsolete agreement/pool-shape tests were replaced with generative tests.

The real-model evaluator uses a **recording speaker**, never server audio:

```bash
PYTHONPATH=src python3 -m tools.replay_llm --out /tmp/booth-synthetic.json
PYTHONPATH=src python3 -m tools.replay_llm \
  --log-path fixtures/matches/match_03.jsonl --max-requests 12 \
  --out /tmp/booth-recorded.json
# Deliberately miss a deadline with a real model request:
PYTHONPATH=src python3 -m tools.replay_llm --max-requests 1 --max-age 0.1 \
  --out /tmp/booth-deadline.json
```

Each run is capped at 30 batches (12 by default), at most four calls per batch
including auditing and repair. Output files have permissions 0600 and contain
candidate/final transcripts, safe status, and latency—not credentials or reasoning.
Synthetic cases cover opening/mulligan stage, observed mulligan count, tutors with
and without life loss, repeated Elves, stack departure, unrelated life change,
combat, and duplicate observations. The synthetic mulligan count is a supplied
observation, not a claim that the live differ detects every mulligan decision.
Recorded replay uses real reconstructed game snapshots and events. Replay is paced
per batch; it does not establish performance under a full-speed match or real TTS.

Early live trials exposed wrong tutor ordering, inferred life-loss causation,
unobserved attack outcomes, and a Tenacious Pup boon applied to the wrong creature.
Those findings informed the validation and auditor tests. Successful later samples
include genuine PBP questions followed by rules explanations from the analyst.
Rejections remain frequent and create gaps. Model-based semantic validation can
still miss errors; this is a guarded generative mode requiring continued transcript
review, not a proven guarantee that every spoken claim is correct. Real Mac audible
handoff timing and full-speed sustained-match behavior have not been measured here.
See `llm-booth-validation.md` for the final checked run and remaining quality findings.
