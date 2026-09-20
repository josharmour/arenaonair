# MTGA Announcer — Product Requirements Document

**Version:** 0.1 (draft)
**Date:** 2026-09-19
**Status:** Approved for design; implementation not started
**Sibling project:** `~/repos/mtgacoach` (full coaching app — this project is narration-only, built from scratch for later A/B comparison)

---

## 1. Problem statement

MTG Arena gives no commentary. Watching a match — your own replayed, a friend's on a second
monitor, or a stream — means reading board states yourself. The mtgacoach project proved the
hard parts (log tailing, GRE parsing, game-state reconstruction, TTS) work reliably, but its
conversation mode is embedded in a 66k-line coaching app whose primary job is telling the
player what to do. A pure play-by-play announcer is a much smaller product with a different
contract: **the listener is an audience member, never a player being instructed.**

## 2. Product vision

A small, standalone desktop app that watches an MTG Arena match in real time and narrates it
like a radio broadcast: what was played, what resolved, who's attacking, how the board and
life totals are shifting — in plain language a layman can follow. It never says what to play.

## 3. Non-goals (explicit)

| Not in v1 | Why |
|---|---|
| Advice, recommendations, "you should attack" | Core identity: announcer, not coach. Hard product rule. |
| Autopilot / action submission | Removes the entire BepInEx plugin + GRE bridge dependency; logs alone are sufficient ground truth for narration. |
| Win-probability estimates | Requires MageZero inference; adds backend fragility for marginal listener value. Revisit post-v1. |
| Draft support | Different event stream; separate product later if ever. |
| Overlay / HUD | Audio-first product; visual UI is a status panel only. |
| Interactive Q&A ("why not attack?") | v1 is one-way broadcast. Q&A is a v2 candidate (mtgacoach already has this pattern if we want inspiration). |

## 4. Target user

Josh (primary): plays or watches MTGA matches and wants ambient commentary — e.g., booth-style
narration while playing on another screen, or for spectating. Secondary: anyone who wants to
follow an MTGA match without parsing the board themselves.

## 5. User experience (v1)

| Moment | Announcer behavior |
|---|---|
| App starts | Status line: "Watching for a match…" (log tail active). |
| Match detected | Opener: "We're underway — <format>, <player> versus <opponent>." |
| Land played | Brief acknowledgment ("Josh develops his fifth land"), throttled so it doesn't drone. |
| Spell cast → resolves | Named: "Cast Shriekwing Implication… it resolves." Counterspells called out explicitly. |
| Combat declared | "Attackers coming in — three creatures, seven power on the table." Blockers when visible. |
| Life total changes | Called out when meaningful (big swing, low-life danger zone), not every 1-point tick. |
| Board shift | "That's board presence back to even." / "Opponent's up to four creatures now." |
| Card color/tidbit | Occasional flavor for laymen ("a two-mana removal spell") — template-based, no LLM required. |
| Match ends | Result line: "That's game — <winner> takes it." Then back to watching. |
| Nothing happening | **Silence.** No filler narration of priority passes or draw steps by default. |

### Verbosity settings
- **Quiet** — match open/close + combat + life swings only.
- **Balanced** (default) — the table above.
- **Detailed** — adds draw-step mentions, land drops every turn, graveyard highlights.

### Controls (minimal)
- Start/stop watching.
- Mute / verbosity selector.
- Status indicator: watching / in-match / speaking / error.
- That's the whole UI.

## 6. Functional requirements

- **FR1** Tail the MTGA Player.log (watchdog + poll fallback) and reconstruct full game state
  from GRE messages without any BepInEx plugin.
- **FR2** Detect match boundaries (start/end) reliably; never re-announce a just-finished match.
- **FR3** Diff consecutive game-state snapshots into discrete play-by-play events
  (plays, resolves, counters, attacks/blocks, damage/life changes, zone movements).
- **FR4** Render events as spoken sentences via template engine (no LLM in the critical path).
- **FR5** Speak through local TTS with a speech queue; new urgent events may preempt stale ones.
- **FR6** Enforce silence discipline: cooldowns, repetition suppression, verbosity gating.
- **FR7** Run on Windows (primary MTGA host) and macOS/Linux dev machines.
- **FR8** Operate fully offline/local except card-name data (bundled or cached).

## 7. Quality requirements

- **QR1 Latency:** event spoken ≤ 3 s after it appears in the log (p95), template path only.
- **QR2 Reliability of silence:** zero unprompted speech when no match is active; zero
  narration of stale/finished matches.
- **QR3 Robustness:** malformed/partial GRE messages never crash the watcher; state
  reconstruction degrades gracefully (skip event rather than desync).
- **QR4 Footprint:** single Python process; no GPU requirement; < 300 MB RAM with TTS loaded.
- **QR5 Testability:** every event-detection rule unit-testable against recorded log fixtures;
  replay harness can score a full match transcript deterministically.

## 8. Success metrics

- A full unattended match produces commentary with zero silent-failure incidents (the class of
  bug that plagued mtgacoach conversation mode: rendered speech silently cancelled by staleness
  gates).
- Narration latency p95 ≤ 3 s measured by replay harness timestamps.
- Listener test (Josh): ≥ 80% of notable plays named correctly; no instruction-style phrasing
  ever spoken.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Hidden information limits narration richness | Accept: public-info narration only (battlefield/stack/graveyard/life). Phrase opponent-hand inferences as speculation or omit. |
| Log bursts cause triple-narration of one play (cast→resolve→ETB) | Event debouncer merges correlated events within a window into one sentence (design doc §4.4). |
| GRE format changes break parsing | Parser is isolated behind a state-builder interface; fixtures from live logs catch regressions; degrade to "something happened" silence rather than wrong narration. |
| TTS engine availability varies by OS | Same layered fallback approach as mtgacoach tts.py (Kokoro → SAPI/espeak), but behind one narrow `Speaker` interface so engines are swappable. |
| Repeating mtgacoach's silent-speech bugs | Design doc §6 makes delivery confirmation + session-scope staleness first-class requirements with replay-harness tests; this is the single most important lesson carried over. |

## 10. Future (post-v1)

- Optional LLM color-commentary pass for richer phrasing (A/B against templates — this is the
  planned comparison vs mtgacoach's conversation mode).
- Spectator mode narrating a match on a second machine via shared log sync.
- Interactive Q&A layer.
