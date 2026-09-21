# ArenaOnAir — Design Document

**Version:** 0.2 (draft)
**Date:** 2026-09-20
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
6. **Cross-platform is a constraint, not a port.** Windows, macOS, and Linux are supported
   identically from the first commit. Every platform difference (log paths, TTS engines,
   tray/notification APIs) hides behind an interface; no `sys.platform` special cases outside
   a single `platform/` adapter layer. CI runs the full test suite on all three OSes at every
   wave — a wave isn't done until the matrix is green.
7. **Variety is engineered, not sprinkled.** Repetition is the death of an announcer. Templates
   remain the mechanism (deterministic, testable), but selection is context-aware and
   history-weighted so the same event never yields the same sentence twice in quick succession,
   and repeated low-salience events compress instead of droning (PRD anti-repetition contract).

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
                     │ Speaker      │  per-OS fallback chains:
                     └──────────────┘  Win: Kokoro → SAPI · Mac: Kokoro → say
                                       Linux: Kokoro → Piper/espeak-ng
```

Seven modules plus a thin `platform/` adapter layer, one process, one thread each for
watch/speak; the differ and story model run on the watcher thread after each state update.

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
- Resolves the Player.log location through the platform adapter (`platform/logpath.py`),
  never inline: standard `%APPDATA%` path on Windows, `~/Library/Application Support/
  com.wizards.mtga/Logs/...` glob on macOS (dated subdirectories), config-overridable default
  on Linux. Config override wins on every OS.

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

Template engine with an explicit **variety engine** in front of it (PRD anti-repetition
contract):

- **Template pools with structural variety.** Every event kind has a pool of ≥ 6 templates
  differing in sentence *shape* — lead with actor / lead with card / lead with consequence /
  short aside / color remark — not synonyms of one shape.
- **History-weighted selection.** A rolling window (~8 utterances) of recently rendered
  templates per kind weights selection away from recent picks; exact repeats are never
  consecutive and can't recur inside the window (QR6).
- **Context-aware slots.** Templates consume game context — turn count, life totals, board
  size, streak lengths, current arc — so repeated kinds still render differently as the game
  evolves ("develops his fifth land" early vs "twelve lands deep and still digging" late).
- **Escalating brevity.** Repeated low-salience events compress across repetitions:
  full sentence → short aside → silent acknowledgment; resets when the streak breaks or
  salience rises.
- **Determinism for tests:** seeded RNG keyed by match_id; a fixture replay renders an
  identical transcript every run (keeps reliability contract #6 intact).

Plain-language glosses for laymen stay: mana cost → "two-mana removal spell", power ≥ 5 →
"a real threat". No LLM call anywhere in this module; an optional `LLMGlosser` implementing
the same interface is a post-v1 A/B hook.

### 3.6 `story.py` — game dynamics → narrative events (the caster layer)

Consumes the same immutable snapshots as the differ and maintains a lightweight, fully
deterministic story model:

- **Momentum index** per seat: decayed score from damage dealt, creatures deployed vs
  removed, spells resolved unanswered, life pressure applied.
- **Arc classification:** even game / pulling away / comeback brewing / standoff / race —
  transitions emit `narrative_arc` events ("the standoff finally breaks").
- **Resource ledger:** land-streak detection (flood vs screw), hand-size pressure,
  spells-per-turn pace over a sliding window.
- **Beat memory:** ring buffer of notable past events (answered threats, missed land drops,
  big trades) keyed by turn — enables callback lines ("that planeswalker they answered two
  turns ago would sure be nice right now").

Derived statistics crossing thresholds emit ordinary `Event`s (`narrative_arc`,
`narrative_resource`, `narrative_callback`, …) into the same differ output stream — narrative
beats inherit debouncing, suppression, salience gating, and the variety engine like any other
event. Threshold crossings only; never a timer-driven chatter loop.

All thresholds live in config; every rule is a pure function unit-tested like differ rules.
Speculation is generated only from public state and always phrased as observation.

### 3.7 `speech.py` — queue + arbiter + TTS

- `SpeechQueue`: priority by salience; drops stale utterances **by session/match scope only**
  (principle 4).
- Delivery confirmation: `Speaker.speak()` returns an outcome; failures log at WARNING with the
  full text so nothing disappears silently.
- Per-platform TTS fallback chains behind one narrow interface so engines are swappable and
  testable with a fake:
  - Windows: Kokoro → SAPI
  - macOS: Kokoro → `say`
  - Linux: Kokoro → Piper/espeak-ng
  
  Chain order is config-overridable per platform.

### 3.8 `app.py` — wiring + minimal UI

Single entrypoint (`python -m arenaonair` or console script). Status surface: tray icon or
tiny window with state label (watching / in-match / speaking / error), mute toggle, verbosity
selector. Tray/notification APIs differ per OS — those calls live in the platform adapter;
PySide6 covers all three OSes if a window is wanted; v1 can ship CLI + tray to keep deps
minimal.

## 4. Data flow & threading

- Watcher thread: tail → parse → build state → diff → update story model → enqueue utterances
  (play-by-play + narrative events).
- Speech thread: dequeue → speak → confirm → mark delivered.
- Shared state guarded by one lock; snapshots immutable (copy-on-write) so the differ and
  story model never race a partial update — mtgacoach's published-snapshot pattern
  (`get_published_snapshot`) is the right idea and is reimplemented cleanly here.

## 5. Configuration

Single TOML file (`~/.arenaonair/config.toml`): log path override, verbosity default,
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
7. **No drone.** Over any fixture replay: no two consecutive utterances share a sentence
   shape; no template recurs within an 8-utterance window; repeated low-salience stretches
   compress (QR6). Test: drone-score assertion in the replay harness.
8. **Platform parity.** Every feature works on all three OSes with no behavior gaps beyond
   TTS engine availability (which the fallback chain absorbs). Test: full suite green on the
   three-OS CI matrix at every wave; platform-adapter unit tests per OS.

## 7. Testing strategy

- Unit: every differ rule against synthetic snapshots; parser against fixture lines; story
  model rules (arc transitions, streaks, momentum) against synthetic snapshot sequences;
  variety engine (window exclusivity, brevity escalation, seeded determinism).
- Integration: recorded full-match logs through watcher→narrator with fake speaker; assert
  transcript properties (coverage of notable plays, silence during idle stretches, drone score
  within QR6 bounds).
- Platform: platform-adapter tests per OS (log-path resolution, TTS chain construction);
  full-suite CI matrix on Windows/macOS/Linux runners at every wave.
- Manual: live match on each OS where practical (Windows primary MTGA host; macOS/Linux via
  synced log fixtures); latency + coverage scoring per PRD §8.

## 8. Tech stack

- Python ≥ 3.10, stdlib-first; `pathlib`/`os` abstractions only — no POSIX-only or
  Windows-only APIs outside `platform/`.
- Deps: `watchdog` (log tailing), TTS engine(s) as available per platform; PySide6 optional
  for UI (cross-platform by nature).
- Packaging: single package `src/arenaonair/` (+ `src/arenaonair/platform/` adapters),
  pyproject/hatchling, console script entry.
- CI: three-OS matrix (windows-latest / macos-latest / ubuntu-latest) running the full suite
  at every wave.
- No MCP server, no LLM client, no BepInEx component in v1.

## 9. Build order (implementation waves)

1. **Wave A — ground truth:** platform adapter (log paths) + watcher + parser + state builder
   against recorded fixtures; replay harness prints reconstructed state timeline. CI matrix
   green from this wave onward.
2. **Wave B — events:** differ rules + debouncer + suppression; unit tests per rule.
3. **Wave C — voice:** narrator templates + variety engine + story model (narrative events) +
   speech queue/arbiter + per-platform TTS fallback chains; delivery-contract tests (§6) and
   drone-score tests (§6.7).
4. **Wave D — product:** config, status UI/tray via platform adapter, packaging; live-match
   validation per PRD §8 on each OS where practical.

Each wave ends with the full test suite green on all three OSes and a replay-harness demo
against a real match log.

## 10. Explicitly rejected alternatives

- **Copying mtgacoach modules wholesale:** rejected per product decision — we want a clean,
  debt-free codebase for A/B comparison; lessons are carried as design rules instead.
- **BepInEx ground truth:** unnecessary without action submission; logs suffice for narration.
- **LLM-first narration:** latency + backend fragility not worth it for v1; templates are
  deterministic and testable; LLM glossing remains an A/B experiment hook.
- **Windows-first port later:** rejected — retro-fitting cross-platform onto a Windows-shaped
  codebase is how path separators and TTS calls end up scattered everywhere; the platform
  adapter exists from commit one.
- **Timer-driven narrative chatter:** rejected — narrative beats fire only on threshold
  crossings like any other event, keeping silence discipline intact.
