# ArenaOnAir — Dual-Expansion Contracts (Phase 1 prerequisite)

Implements the contract requirements of dual-expansions.md §8.1 and §8.5.
These definitions land in `models.py` / `interfaces.py` / `events.py` before any
expansion feature work begins (execution order §8.8 step 1). Frozen-dataclass
additions are backwards-compatible optional fields with defaults.

---

## 1. Source identity & tagged input (§8.1)

### `SourceTag` — identity carried by every ingested message

```python
@dataclass(frozen=True)
class SourceTag:
    source_id: int            # stable id of the ingest channel (0-based slot)
    session_gen: int          # increments on reconnect/re-bootstrap of this channel
    recv_seq: int             # per-source monotonically increasing receive sequence
    recv_ts: float            # RECEIVER monotonic arrival time (never client wall clock)
```

Rules:
- A `source_id` or user-supplied `--seat` NEVER establishes GRE player identity.
  Player seat is resolved only from validated session metadata (match/game
  identity + seat evidence in the stream).
- One raw log line may contain several GRE messages; parsing expands one tagged
  line into `TaggedMessage(tag, msg)` items sharing the tag's identity fields,
  each with its own sub-sequence.
- Non-state room/connect messages establish metadata (match id, player names,
  seat hints) and are routed to the metadata path, never sorted as game ticks.

### `SourceStatus` — per-channel health, distinct axes (§8.2)

```python
@dataclass(frozen=True)
class SourceStatus:
    source_id: int
    transport_ok: bool        # file readable / websocket alive (heartbeats)
    chain_valid: bool         # GRE state chain continuous from a valid baseline
    baseline_ok: bool         # saw a valid bootstrap baseline (ConnectResp-era data)
    last_recv_ts: float       # receiver monotonic time of last message
    last_state_progress: float  # receiver monotonic time of last state advance
```

- Transport health ≠ state-chain validity ≠ knowledge completeness. A quiet
  game is NOT disconnection; absence-of-messages decisions use
  `last_state_progress` + explicit timeout policy, not raw silence.

## 2. Knowledge model (§8.1, §8.3, §8.4)

### `KnowledgeStatus` — per-seat, per-field visibility tracking

```python
@dataclass(frozen=True)
class SeatKnowledge:
    seat: int
    hand_visible: bool            # current hand identities known
    hand_fresh_asof: float | None # receiver ts of the observation backing hand_visible
    deck_submitted: bool          # valid submitted decklist ingested this game
    library_accounted: bool       # composition reconciled with observed library size
    library_uncertainty: str      # 'exact' | 'estimated' | 'unknown'
```

- `GameState.is_omniscient` stays a DERIVED convenience property:
  True only when every seat's `hand_visible` and `library_accounted=='exact'`
  hold simultaneously with fresh observations. Detectors never gate on it;
  each detector checks the specific `SeatKnowledge` fields it consumes.
- Stale/private-data loss ⇒ flip the specific field to unavailable; never keep
  advertising a disconnected player's last-known hand as current truth.

### Publication & alignment (§8.1)

- Each source has a child `StateBuilder`; snapshots carry local `snapshot_id`
  sequence (unchanged) PLUS the source's GRE `gameStateId`/`prevGameStateId`
  linkage and `SourceTag`.
- Single healthy source ⇒ publish its coherent state immediately, always.
- Two initialized sources eligible for enrichment ⇒ bounded pairing window,
  default max wait **150 ms** (receiver-monotonic deadline), matching on
  verified match/game identity + compatible GRE state linkage. Timeout ⇒
  publish healthy single-source view, mark enrichment unavailable for that state.
- Public-event dedup: one event identity contract across sources keyed on
  (verified match/game id, event semantic key). Secondary enrichment of an
  already-published state must not re-run cast/attack/boundary announcements nor
  double-update story momentum.
- Never merge: different matches, different games, or two connections belonging
  to the SAME player (duplicate player identity).

## 3. Dialogue delivery contract (§8.5)

### Utterance additions

```python
role: str = "play_by_play"        # "play_by_play" | "color_analyst"
dialogue_id: str | None = None    # groups anchor+reply pair
anchor_uid: str | None = None     # reply depends on THIS utterance's successful delivery
expires_ts: float | None = None   # reply eligibility deadline (optional)
```

### Sequencing rules

1. A reply is eligible ONLY after its anchor completed playback successfully
   (`DeliveryResult.ok=True`). Anchor failed/canceled/pruned ⇒ drop the reply.
2. Supporting state obsolete or source degraded ⇒ expire pending replies whose
   prerequisites no longer hold; PRESERVED_KINDS boundary announcements are
   never dropped by this expiry.
3. Keep pairs adjacent when ordinary scheduling permits; urgent events may
   preempt; bounded analysis frequency + queued duration prevent two-lines-
   per-play backlog growth during action bursts.
4. Game/match boundaries follow the §7.2-fixed queue lifecycle: game-boundary
   cleanup removes obsolete game commentary including undelivered pairs;
   match closure seals everything.
5. Identical behavior in single-log and enriched modes — source count controls
   available FACTS, never whether sequencing is enabled.

## 4. Detector prerequisite gates (§8.3 summary)

| Detector | Minimum prerequisites |
|---|---|
| TRAP_ARMED | target hand visible+fresh, priority/phase known, mana computable, card legality supported |
| TRAP_SPRUNG | live armed trap + public cast matching its trigger; never implies resolution |
| BLUFF_DETECTED | verified priority delay + supported hand/playability data; internal-only category |
| CLASH_OF_OUTS | per-seat library_accounted=='exact' + supported draw model + answer legality |

Unsupported prerequisite ⇒ detector suppressed for that decision; ordinary
commentary continues. Single-source mode retains public tactics + local private
analysis; only cross-player-private assertions are unavailable.

## 5. Validation evidence requirement

Cross-client GRE assumptions (shared gameStateId semantics, instance-id scope,
deck-message equivalence) must be validated against PAIRED REAL LOGS before
relied upon. Paired real logs are not currently in `fixtures/`; until obtained,
synthetic dual fixtures carry an explicit limitation note and no claim of
real dual-source correctness (plan §4 Agent-7 task 1).
