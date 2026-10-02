# Implementation prompt: LLM-written ArenaOnAir booth

Copy the prompt below into a Codex session on `10.0.0.10`.

---

Implement the next ArenaOnAir phase: original, conversational commentary written
by an LLM for both announcers. Work through implementation, tests, and real-model
verification. Do not stop at a plan, and do not solve this by expanding scripted
phrase pools. Follow applicable repository instructions and preserve existing
uncommitted work. Locate the ArenaOnAir checkout on this server before editing.

## What I want

The current booth sounds like a play-by-play announcer followed by a repetitive
approval machine. In a supplied match segment there were 41 PBP calls and 19
analyst replies; 16 replies were generic praise and none contributed a specific
card, player, or number. The announcers rarely actually talked to each other.

Replace that writing approach with an LLM that understands the available game
facts and the conversation so far. It should write fresh, concise calls,
explanations, questions, answers, and callbacks. Both speakers should sound like
they are watching the same match and listening to each other. The model should
choose when a second voice adds value; no mandatory alternation or response to
every event. Silence is better than “nothing to add,” empty agreement, or another
unsupported claim of optimal play.

Creativity belongs in language, personality, and conversational connections.
It must not invent cards, actions, targets, outcomes, hidden information, player
intentions, or numerical claims. Analytical uncertainty should be expressed
naturally. This is commentary, not instructions coaching a player.

## Backend and authorization

I have LiteLLM running on this server (`10.0.0.10`), reachable locally. The public
gateway is `https://api.mtgacoach.com`. Use the local gateway for development and
verification, and make the base URL configurable so my Mac can use the public
gateway. Do not hardcode a localhost address into the Mac's runtime configuration.

Use the existing GLM 5.3 deployment. The adjacent MTGA Coach client currently
uses the alias `glm-5.3-flash`; verify the live model list and supported request
parameters rather than assuming the alias, port, or historical docs are current.
The running container names observed previously included `litellm`, `litellm-db`,
and `glm53-flash-2x-rtxpro6000`.

Inspect the local LiteLLM configuration and MTGA Coach gateway/client docs as
needed. A known configuration location is
`/home/joshu/docker-stack/litellm/config.yaml`; the adjacent checkout has
`gateway/README.md` and `src/arenamcp/backends/proxy.py`. Some gateway README
instructions still refer to older NAS locations and old model restrictions.

You are authorized to create a dedicated ArenaOnAir virtual API key scoped to
the selected model and run bounded development/replay requests. Keep credentials
out of source control, tool output, prompts, logs, and copied bug reports. Store
the application credential with restricted permissions or use an environment
variable; do not make the application depend on the LiteLLM master key. Do not
restart or reconfigure the shared inference/gateway services for this task.

The existing MTGA Coach client reports two integration details worth verifying:

- The public gateway needs an explicit User-Agent; default Python urllib requests
  have previously received Cloudflare 403 responses.
- This GLM deployment previously worked with
  `chat_template_kwargs={"thinking": true, "reasoning_effort": "low"}`.
  Turning thinking off reportedly leaked deliberation into visible content.
  Read only the final assistant content, never `reasoning_content`, and verify
  the live endpoint's behavior with a small probe. Do not assume these settings
  are universally supported by other models.

## Repository state to inspect first

Read `docs/live-booth-review.md`, `docs/llm-booth.md`, and `dual-expansions.md`
when present. The original PRD/DESIGN restriction against an LLM in narration
describes the old v1; this request explicitly supersedes that restriction.

A previous agent attempted a template-based analyst redesign and left it
unfinished. Do not blindly wire it up or revert entire files from Git HEAD:

- `templates.py` removed the old agreement pool and introduced fact-gated tuples,
  handoff questions/answers, and callbacks. These are not the desired generative
  solution; do not mistake different fixed sentences for original dialogue.
- `narrator.py` had a duplicated region and two `DialogueSequencer` definitions.
  The Mac session removed the duplicated region, reducing it from 2,378 to 1,420
  lines. That narrow cleanup passed all 40 `test_narrator.py` tests. Check whether
  this cleanup reached the server checkout; do not apply line-number deletions.
- The remaining draft is not verified. Problems include references to an undefined
  `carddb_is_tutor`, name/seat dedupe conflating separate copies of a card,
  generated candidates being counted as spoken, mana value treated as mana paid,
  and callbacks that could attribute unrelated life changes to a tutor.
- `app.py` still uses the old companion path; tests in `test_dual_booth.py` contain
  expectations for removed agreement behavior and old pool shapes.
- The earlier 565-test pass predates this unfinished redesign. Do not present it
  as verification of the current tree.

Preserve the existing launch fixes, separate TTS voice routing, single/dual-log
ingestion and conservative fusion, game-boundary cleanup, missing-card lookup
fallback, mulligan/topdeck stage fix, and Copy bug report window.

## Implementation requirements

1. **One shared generative booth.** Use a shared context for both roles, with
   distinct names/voices and complementary purposes. Let the model write short
   multi-turn exchanges when useful. An analyst may initiate an observation and
   the PBP may respond. Avoid isolated stateless calls for each speaker. In the
   new LLM mode, both speakers' prose must come from the model, not phrase pools
   with occasional model paraphrasing. Make it straightforward to enable this
   mode and use it by default once configured for my setup. If legacy narration
   remains for compatibility, expose it explicitly and never silently substitute
   canned chatter when the model fails.

2. **A structured factual input.** Supply recent observed events, relevant current
   state, resolved card identities and available authoritative card rules, game
   phase/turn, and provenance/uncertainty. Include only facts the existing knowledge
   model actually supports. Hidden-zone information requires the existing visibility
   and freshness rules; a missing log does not grant knowledge of the other hand.
   Do not send raw Player.log or unrelated account/configuration data. Treat player
   names and other log-derived strings as untrusted data, not instructions.

   This must work fully with **one log**. A second compatible log enriches known
   facts; it is not necessary for LLM commentary, a second voice, or exchanges.
   Retain graceful degradation when a source disappears or becomes incompatible.

3. **Card rules and causal discipline.** Retrieve supported rules text from
   available card data when practical; do not rely on the model recalling every
   card correctly. Missing rules remain unknown. Mana value does not prove actual
   mana payment. Not every tutor loses life, and a later life change alone does
   not establish causation. A cast is not a successful resolution. The model can
   discuss conditional consequences but cannot announce them as already observed.

4. **Real spoken memory.** Record successful speech delivery with role, text,
   event references, and relevant facts. Give the model that history plus a bounded
   factual summary for longer matches. Distinguish generated, queued, interrupted,
   failed, and spoken text. Callbacks and agreements must refer to things listeners
   actually heard. Reset game-specific memory at boundaries. Preserve the meaning
   of discussed points, not merely a list of recent sentence strings.

5. **Live scheduling.** Generate asynchronously, outside log ingestion and audio
   delivery. Coalesce closely related events and use actual play identities where
   available to avoid repeated cast/resolution discussions. Bound pending work,
   context, response length, and age. Prefer useful short contributions over long
   monologues that delay current combat. Do not impose equal airtime quotas.

   Scope each request to the match/game and relevant facts. Discard stale responses
   and invalidated queued commentary. Avoid expiring every useful reply merely
   because an irrelevant GRE tick advanced. Keep urgent announcements prioritized.
   Dependent exchange turns must be gated on successful anchor delivery; cancelled,
   failed, pruned, or expired anchors cannot leave orphan answers behind. Closing
   the app must stop work cleanly and prevent late replies from reappearing.

6. **Output handling and failure behavior.** Use a structured model response with
   allowed speaker roles, short utterances, and references to supplied events/facts.
   Permit an empty response. Validate structure, references, bounds, and obvious
   invalid content before speech. Never speak raw JSON, reasoning, instructions,
   error messages, or malformed output. Valid JSON and valid references do not
   prove the prose is factually correct: add meaningful factual evaluation too.
   Use bounded timeouts/backoff; do not build a backlog during an outage. On a
   missed deadline, silence is preferable to stale or scripted analyst filler.
   Make model failure visible in app status and diagnostics.

7. **Observability.** Extend the existing status window and Copy bug report output
   with safe model status, request latency, event-to-speech age, and counts/reasons
   for stale or rejected outputs. Retain role/text delivery logs. Exclude credentials,
   reasoning traces, raw payloads, and private-game data from copied reports. Keep
   the existing Mac Kokoro voices and TTS pipeline; GLM writes text, not audio.

## Validation and completion

Add meaningful tests with deterministic fake model responses for single-source
operation, enriched/degraded sources, shared conversation history, silence when
appropriate, invalid output, timeouts, stale responses, game transitions, distinct
copies of the same card, cast/resolution grouping, cancellation/orphan handling,
and correct voice routing. Update obsolete agreement/phrase-pool tests to the new
contract without discarding still-relevant queue, pacing, and voice regressions.
Run the relevant existing suite and resolve failures introduced by this phase.

Also run a bounded replay using the **real GLM endpoint**, including card-name,
mulligan, repeated-play, life-change, and combat situations. Inspect the generated
and successfully delivered transcript. Show that both speakers contribute useful
information and sometimes converse, without canned praise or fabricated results.
Measure actual model latency and stale-output behavior. Mock tests alone are not
evidence of model quality. Do not play test audio through shared server hardware;
use a recording/fake speaker and leave a clear Mac launch command for audible use.

Update the docs with configuration, the public gateway setup for my Mac, the
actual final architecture, and any remaining limitations. Finish with a concise
account of what changed, checks that passed, real-model observations, and exactly
how I launch the new booth. Do not claim the feature complete if the live model
path has not been exercised successfully.
