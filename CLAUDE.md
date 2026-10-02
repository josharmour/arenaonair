# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- The repo lives on an SMB mount (`/Volumes/...`). Never create a `.venv` inside it — the venv is `~/.venvs/arenaonair` (uv, Python 3.12). Use `~/.venvs/arenaonair/bin/python` for everything.
  - Setup: `uv venv ~/.venvs/arenaonair --python 3.12 && uv pip install --python ~/.venvs/arenaonair/bin/python -e ".[dev]"`
- Python >= 3.11. Extras: `dev`, `tts` (kokoro/numpy/sounddevice), `ui` (PySide6), `watchdog`.
- Root-level `arenaonair/__init__.py` is an intentional unpackaged shim so `python -m arenaonair.app` works from a checkout; real code is in `src/arenaonair/`.
- No formatter or linter is configured; match surrounding style.

## Commands

- Tests: `~/.venvs/arenaonair/bin/python -m pytest` (run from repo root — `tests/test_card_knowledge.py` imports `tools.*`).
- Replay harness (a CI job; exit 0 iff all thresholds pass):
  `python tools/replay_harness.py --fixture fixtures/matches/match_02.jsonl --determinism`
- Every user-facing setting is in the window's Settings tab, built from `settings.py`; add new settings there so `ArenaOnAirApp.apply_setting` saves them to the right TOML table.
- Subcommands: `arenaonair setup | doctor | recap | build-carddb | install-app` (dispatched in `app.main`).
  `install-app` (`desktop.py`) writes `~/Applications/ArenaOnAir.app` on macOS; its launcher must `exec` Python so the Dock tile stays the bundle's.
- Run app: `./run.sh [--dry-run] [--once] [--log-path PATH] [--broadcast-mode dual] [--narration-mode llm|auto]`
- Card data is generated outside the repo in `~/.cache/arenaonair/`:
  - `cards.sqlite`: `arenaonair build-carddb` (module moved to `src/arenaonair/build_carddb.py`; `tools/build_carddb.py` is a shim)
  - `knowledge.sqlite`: `PYTHONPATH=src:. python -m tools.build_card_knowledge`

## CI and tests

- CI runs on ubuntu, windows and macOS (py3.12), plus ubuntu on py3.14, and installs `.[dev]` only. Tests must:
  - skip cleanly when optional deps are missing (`websockets`, `numpy`, `PySide6` via `importorskip`)
  - skip cleanly when `~/.cache/arenaonair/cards.sqlite` is missing
  - be independent of the OS and path separator (Windows file locking; audio player differs by OS: `afplay`/`aplay`/sapi)
- `tests/conftest.py` points `ARENAONAIR_DATA_DIR` at a temp dir so tests never write to `~/.arenaonair`. Tests set other `ARENAONAIR_*` env vars via monkeypatch.

## Invariants (docs/DESIGN.md §6, docs/PRD.md)

- **Announcer by default:** commentary suggests plays only when `config.coaching` is on (AI booth, opt-in via the window or `--coaching`), and never from information the listening player can't see.
- The GRE parser never raises. Replays are deterministic. No template repeats within an 8-utterance window.
- No silent drops: every failed utterance logs a WARNING. Turn numbers never gate delivery.
- Every feature works on all three OSes.
- No network card lookups at runtime; card data comes only from the local sqlite caches.
- LLM booth (`docs/llm-booth.md`):
  - Never invent cards or hidden info.
  - Use only the final `content`, never `reasoning_content`.
  - Never speak raw JSON or errors; model failures stay silent and surface only in status.
- Hands on air are gated by `config.hole_cards_enabled`: shared-log routes (dual_file/relay) never reveal hands unless `spectator` is set, since a listening player would hear the opponent's hand.
- The API key lives at `~/.config/arenaonair/llm.key`. Never put it in source, config files in the repo, CLI args or prompts.

## Working tree

An unfinished LLM-booth redesign is in progress in the working tree. Don't revert whole files to HEAD or apply line-number-based deletions; make targeted edits.

## Commits

Use `type(scope): summary`, e.g. `fix(ci): ...`, `feat(app): ...`, `diag(app): ...`, `docs: ...`.
