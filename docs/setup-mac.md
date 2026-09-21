# ArenaOnAir — macOS Setup & Test Guide (Antigravity handoff)

**Goal:** get ArenaOnAir running with Kokoro neural voice on the Mac M5 Max, verify it
end-to-end against a real MTGA match log, and report results.

**Repo:** `/mnt/repos/arenaonair` on the Linux box (blackwell) — mount it on the Mac via
SMB (existing `repos` share) or clone it. All commands below assume you are **inside the
repo root**.

**What ArenaOnAir is:** a play-by-play radio announcer for MTG Arena. It tails the MTGA
`Player.log`, reconstructs game state from GRE messages, and speaks commentary like a
tournament caster. Announcer, never coach — it never tells the player what to do.

---

## 1. Environment setup

Python ≥ 3.10 required (3.12+ ideal). Use `uv` (install via `brew install uv` if missing).

```bash
cd <mounted-or-cloned-repo-root>

# venv on LOCAL disk (SMB mounts can't symlink venvs — do NOT create .venv inside the mount)
uv venv ~/.venvs/arenaonair --python 3.12
uv pip install --python ~/.venvs/arenaonair/bin/python -e ".[dev]"
uv pip install --python ~/.venvs/arenaonair/bin/python kokoro soundfile
```

Notes:
- `-e .` installs the `arenaonair` package editable; `[dev]` adds pytest.
- `kokoro` pulls torch — big download (~2 GB), one time.
- If `uv pip install -e .` complains about the SMB mount being slow, be patient; it works.

### Card-name database (grpId → card names)

GRE messages identify cards by numeric id. We resolve them from a local SQLite cache
built from public Scryfall data (~2 MB result).

Fastest: copy the prebuilt cache from blackwell:

```bash
mkdir -p ~/.cache/arenaonair
scp joshu@10.0.0.10:~/.cache/arenaonair/cards.sqlite ~/.cache/arenaonair/
```

Or rebuild from scratch (~5 min, downloads ~250 MB from Scryfall):

```bash
~/.venvs/arenaonair/bin/python tools/build_carddb.py
```

Verify: `sqlite3 ~/.cache/arenaonair/cards.sqlite "SELECT COUNT(*) FROM cards;"` → ~20k rows.

---

## 2. Sanity checks (in order)

```bash
PY=~/.venvs/arenaonair/bin/python

# 1. Contract imports
$PY -c "import arenaonair.models, arenaonair.events, arenaonair.interfaces; print('contract OK')"

# 2. Full test suite (expect 333+ passed)
$PY -m pytest tests/ -q

# 3. Replay harness on the bundled real-match fixture (drone score must be 0)
$PY tools/replay_harness.py --fixture fixtures/matches/match_01.jsonl --determinism

# 4. CLI help
$PY -m arenaonair.app --help
```

All four must pass before moving on. If tests fail on macOS specifically, that's a bug —
report it (the suite is designed to be 3-OS green; CI runs windows/macos/ubuntu).

---

## 3. Voice check (Kokoro)

```bash
$PY - <<'EOF'
from arenaonair.speech import build_speaker_chain
sp = build_speaker_chain()
print("engine selected:", sp.engine.name)   # want: kokoro
from arenaonair.models import Utterance
u = Utterance(uid="t1", match_id="m", kind="match_start",
              text="Welcome to Arena on Air.", salience=3, ts_created=0.0)
r = sp.speak(u)
print("delivered:", r.ok, r.reason)
sp.shutdown()
EOF
```

You should **hear** "Welcome to Arena on Air." in the Kokoro `af_heart` voice.
First run initializes models (~15 s) and auto-downloads a spaCy model if missing.

If kokoro isn't selected: check `python -c "import kokoro"` for import errors.
Fallback chain is kokoro → macOS `say` — `say` working at all is acceptable but kokoro
is the target quality bar.

Known-good reference: on Linux (headless), synthesis produces 24 kHz float32 PCM,
~0.9 s CPU time per line; playback there fails only because the box has no audio device.
On the Mac, playback goes through `afplay` with temp wavs — should just work.

---

## 4. Live run against a real match log

### Option A — MTGA installed on this Mac

Zero config; the platform adapter finds the log automatically:

```bash
$PY -m arenaonair.app --dry-run     # transcript to stdout instead of speaking
$PY -m arenaonair.app               # live voice narration
```

Expected console behavior: status transitions watching → in_match; utterances printed
(one per line) as plays happen; silence when nothing is happening.

### Option B — no MTGA on this Mac: replay a recorded match as if live

The repo bundles three real matches as JSONL (`fixtures/matches/match_0{1,2,3}.jsonl`,
records are `{"kind": ..., "obj": <envelope>}`). Convert one to a synthetic Player.log and
tail it slowly while the app watches:

```bash
$PY - <<'EOF'
import json, time
out = open("/tmp/Player.log", "w")
for rec in map(json.loads, open("fixtures/matches/match_01.jsonl")):
    out.write("[UnityCrossThreadLogger]\n")
    out.write(json.dumps(rec["obj"]) + "\n")
    out.flush()
    time.sleep(0.05)          # ~24 s total replay; adjust to taste
EOF
```

Run that in one terminal, then in another:

```bash
$PY -m arenaonair.app --log-path /tmp/Player.log --dry-run
# then with voice:
$PY -m arenaonair.app --log-path /tmp/Player.log
```

You should hear the match opener ("We're underway…" or similar — phrasings rotate by
design), play-by-play through the game, and a closing line at game end.

### What good output sounds like (samples from the real match_01 replay)

- "Tasha, Unholy Archmage, their legend hits the stack courtesy of Creole."
- "armour digs deep and unleashes The Notary Hobbits."
- "The pendulum swings back to Creole."
- "The dust settles on that one."

---

## 5. Acceptance checklist

- [ ] Full test suite green on macOS (333+ passed)
- [ ] Replay harness exit 0 with drone score 0 on match_01 and match_03
- [ ] Kokoro selected as engine; voice audible and pleasant (not robotic)
- [ ] Live/replay run: opener spoken at match start, play-by-play during play,
      closing line at game end, silence between matches
- [ ] No repeated identical sentences in quick succession (variety engine working)
- [ ] No advice-style phrasing ever ("you should attack" = bug)

## 6. Reporting back

Reply with: test counts, harness scores, which engine spoke, a short transcript excerpt,
and anything that needed fixing (with file paths). If something's broken, include the
traceback and the exact command that produced it.
