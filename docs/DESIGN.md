# MTGA Announcer — Design Document

**Version:** 0.1 (draft)
**Date:** 2026-09-19
**Companion to:** `docs/PRD.md`
**Source of inspiration:** `~/repos/mtgacoach` (read for lessons; **no code copied**)

---

## 1. Design principles

1. **Logs are the only input.** No BepInEx plugin, no GRE socket, no action submission. The
   MTGA Player.log contains enough GRE detail to reconstruct public game state — mtgacoach's
   `gamestate.py` proves this daily.
2. **No LLM in the critical path.** v1 narration is template-rendered. An LLM color pass is a
   post-v1 optional layer behind the same interface. This removes backend-health fragility,
   render latency, and the entire class of "LLM returned garbage" failure modes.
3. **Speech delivery is confirmed, never assumed.** Every utterance has an identity and a
   delivery outcome; unconfirmed = logged loudly, never silently dropped.
4. **Staleness is session-scoped.** The single most expensive bug family in mtgacoach
   conversation mode was position-bound staleness gates cancelling valid speech (commits
   7d5f3b6, d545d53). Here: an utterance is stale only if its match/session ended. Turn-number
   gating is forbidden.
5. **Silence is a feature.** The announcer earns trust by not talking. Every narration rule
   must have a suppression story (cooldown, dedupe, verbosity gate).

## 2. Architecture overview

```
┌─────────────┐   raw lines   ┌──────────────┐  GRE msgs  ┌───────────────┐
│ LogWatcher  │──────────────▶│ GRE Parser   │───────────▶│ GameState     │
│ (watchdog + │               │ (pure fn)    │            │ Builder       │
│  poll)      │               └──────────────┘            │ (zones, seats,│
└─────────────┘                                           │  turn, life)  │
                                                          └──────┬────────┘
                                                                 │ snapshot N+1 vs N
                                                                 ▼
                     ┌──────────────┐   events    ┌─────────────────────┐
                     │ SpeechQueue  │◀──utterances│ EventDiffer         │
                     │ + Arbiter    │             │ (debounce, dedupe,  │
                     └──────┬───────┘             │  verbosity gates)   │
                            │ text                          └─────────────────────┘
                            ▼
                     ┌──────────────┐
                     │ Speaker      │  Kokoro → SAPI/espeak fallback chain
                     └──────────────┘
```

Five modules, one process, one thread each for watch/speak; the differ runs on the watcher
thread after each state update.

## 3. Module design

### 3.1 `watcher.py` — log tailing

Adapted concept from mtgacoach `watcher.py` (which is solid): watchdog `on_modified` +
periodic poll fallback + startup anchor scan to find the current match without replaying the
whole log. New requirements:

- Emits raw new lines to the parser; owns nothing game-related.
- Handles log rotation/truncation (MTGA rewrites Player.log on client restart): detect size
  shrink → reset position, re-anchor.
- Health self-report: if no file events for N seconds while MTGA process is running, surface
  `watcher_stalled` status (never crash).

### 3.2 `gre_parser.py` — line → message

Pure functions: line in, structured message out (or None). Isolated so MTGA format changes are
caught by fixture tests. Port the *parsing rules* conceptually from mtgacoach `gamestate.py`
(`update_from_message`, `_update_game_object`, `_update_zone`, `_update_player`) but write them
fresh against recorded fixtures — the mtgacoach versions are entangled with decision-tracking,
engine-busy marking, and played-card bookkeeping we don't need.

### 3.3 `state_builder.py` — message → game state

Maintains: zones (battlefield/hand[local]/graveyard/stack/exile), players (seat, life,
hand-size), turn info (number, active player, phase/step), match identity (match_id,
format_name, player names), local-seat detection.

Key simplifications vs mtgacoach:
- No legal actions, no decision context, no pending combat steps for submission — combat is
  *observed* from zone/zone-change and GRE combat events, not from pending-action plumbing.
- No card oracle text needed for v1 templates — names + types + power/toughness suffice.
  Card-type lookup from a small bundled static table (name → type line / mana cost), not the
  full mtgadb.

### 3.4 `differ.py` — snapshot diff → events (the new core)

Input: previous snapshot, current snapshot, plus raw GRE events since last poll.
Output: ordered list of `Event` dataclasses:

```python
@dataclass(frozen=True)
class Event:
    kind: str          # "land_drop" | "cast" | "resolve" | "counter" | "attack_declared"
                       # | "block_declared" | "life_change" | "board_shift" | "match_start"
                       # | "match_end" | "turn_start" | ...
    seat: int          # actor's seat (local or opponent)
    payload: dict      # kind-specific: card names, counts, damage amounts, life totals
    ts: float          # monotonic timestamp from log line arrival
    salience: int      # 0=filler … 3=must-speak (drives verbosity + preemption)
```

Detection rules (each a pure function, individually unit-tested):

| Rule | Signal |
|---|---|
| land_drop | new battlefield object, type land, owner seat |
| cast | object appears on stack with source seat |
| resolve | stack object removed + battlefield/graveyard appearance of same instance |
| counter | stack object removed + graveyard appearance + counter GRE event |
| attack_declared | GRE combat event / attackers-set signal with creature list |
| block_declared | GRE combat event / blockers signal |
| life_change | player life delta; salience scales with magnitude & danger zone (<5) |
| board_shift | creature-count or power-total delta crossing threshold per seat |
| match_start / match_end | match_id appear / disappear + result payload |

**Debouncer (§PRD risk):** correlated events within a 1.5 s window merge into one utterance:
cast+resolve → "Cast X and it resolves"; cast+counter → "Cast X — countered!"; attack set +
multiple creatures → one sentence listing them.

**Suppression:** per-kind cooldowns (e.g., land_drop ≥ 1 per turn only in Detailed),
repetition signature cache (same card+seat within N turns suppressed), verbosity gate by
salience.

### 3.5 `narrator.py` — events → sentences

Template engine. Each event kind has templates with slot filling and simple variety rotation
(2–3 phrasings per kind to avoid robotic repetition). Plain-language glosses for laymen:
mana cost → "two-mana removal spell", power ≥ 5 → "a real threat". No LLM call anywhere in
this module; an optional `LLMGlosser` implementing the same interface is a post-v1 A/B hook.

### 3.6 `speech.py` — queue + arbiter + TTS

- `SpeechQueue`: priority by salience; drops stale utterances **by session/match scope only**
  (principle 4).
- Delivery confirmation: `Speaker.speak()` returns an outcome; failures log at WARNING with the
  full text so nothing disappears silently.
- TTS fallback chain modeled on mtgacoach `tts.py` lessons: Kokoro (local neural) → platform
  TTS (SAPI on Windows / espeak or `say` elsewhere). Behind one narrow interface so engines
  are swappable and testable with a fake.

### 3.7 `app.py` — wiring + minimal UI

Single entrypoint (`python -m mtga_announcer` or console script). Status surface: tray icon or
tiny window with state label (watching / in-match / speaking / error), mute toggle, verbosity
selector. PySide6 only if a window is wanted; v1 can ship CLI + tray to keep deps minimal.

## 4. Data flow & threading

- Watcher thread: tail → parse → build state → diff → enqueue utterances.
- Speech thread: dequeue → speak → confirm → mark delivered.
- Shared state guarded by one lock; snapshots immutable (copy-on-write) so the differ never
  races a partial update — mtgacoach's published-snapshot pattern (`get_published_snapshot`)
  is the right idea and is reimplemented cleanly here.

## 5. Configuration

Single TOML file (`~/.mtga-announcer/config.toml`): log path override, verbosity default,
TTS engine preference, cooldown tuning. No settings database.

## 6. Reliability contract (the anti-mtgacoach-debt section)

These are hard requirements with named tests:

1. **No silent drops.** Every utterance either plays or logs a WARNING with reason + text.
   Test: fake speaker that fails randomly; assert zero unlogged drops over simulated match.
2. **No stale-match speech.** Utterances carry match_id; queue flushes on match end; a new
   match can never inherit queued speech from the previous one (phantom-opener lesson,
   d545d53). Test: match-end → new-match-start race fixture.
3. **No position-bound staleness.** Turn numbers never gate delivery (7d5f3b6 lesson).
   Test: utterance rendered during turn N delivered during turn N+1 must play.
4. **Burst merging.** One play = at most one utterance. Test: cast→resolve→ETB burst fixture.
5. **Crash-free parsing.** Fuzz: truncated/corrupt GRE lines never raise past the parser.
6. **Replay determinism.** Recorded log fixtures replay to an identical event/utterance list;
   this doubles as the A/B harness vs mtgacoach conversation mode later.

## 7. Testing strategy

- Unit: every differ rule against synthetic snapshots; parser against fixture lines.
- Integration: recorded full-match logs through watcher→narrator with fake speaker; assert
  transcript properties (coverage of notable plays, silence during idle stretches).
- Manual: live match on Windows host; latency + coverage scoring per PRD §8.

## 8. Tech stack

- Python ≥ 3.10, stdlib-first.
- Deps: `watchdog` (log tailing), TTS engine(s) as available; PySide6 optional for UI.
- Packaging: single package `src/mtga_announcer/`, pyproject/hatchling, console script entry.
- No MCP server, no LLM client, no BepInEx component in v1.

## 9. Build order (implementation waves)

1. **Wave A — ground truth:** watcher + parser + state builder against recorded fixtures;
   replay harness prints reconstructed state timeline.
2. **Wave B — events:** differ rules + debouncer + suppression; unit tests per rule.
3. **Wave C — voice:** narrator templates + speech queue/arbiter + TTS fallback chain;
   delivery-contract tests (§6).
4. **Wave D — product:** config, status UI/tray, packaging; live-match validation per PRD §8.

Each wave ends with the full test suite green and a replay-harness demo against a real match log.

## 10. Explicitly rejected alternatives

- **Copying mtgacoach modules wholesale:** rejected per product decision — we want a clean,
  debt-free codebase for A/B comparison; lessons are carried as design rules instead.
- **BepInEx ground truth:** unnecessary without action submission; logs suffice for narration.
- **LLM-first narration:** latency + backend fragility not worth it for v1; templates are
  deterministic and testable; LLM glossing remains an A/B experiment hook.
