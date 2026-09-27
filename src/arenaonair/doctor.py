"""First-run checks and setup: ``arenaonair doctor`` and ``arenaonair setup``.

The doctor never touches the network unless ``--online`` is passed. Each
check returns a status (ok / warn / fail) and, when not ok, the one action
that fixes it.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import sys

OK, WARN, FAIL = "ok", "warn", "fail"

DETAILED_ON = b"DETAILED LOGS: ENABLED"
DETAILED_OFF = b"DETAILED LOGS: DISABLED"
DETAILED_LOGS_FIX = ("In MTG Arena open Options > Account, tick 'Detailed Logs (Plugin Support)', "
                     "then restart Arena.")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def detailed_logs_status(path: str | os.PathLike | None) -> bool | None:
    """True/False from Arena's startup marker in Player.log; None if unknown.

    Arena rewrites Player.log at each launch and prints the marker once near
    the top, so the last marker in the file reflects the running client.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read(64 * 1024 * 1024)
    except OSError:
        return None
    on, off = data.rfind(DETAILED_ON), data.rfind(DETAILED_OFF)
    if on < 0 and off < 0:
        # No marker (older client or truncated log): GRE traffic proves it on.
        return True if b"GreToClientEvent" in data else None
    return on > off


def resolve_log_path(cfg) -> Path | None:
    if cfg.log_path:
        return Path(cfg.log_path).expanduser()
    try:
        from .platform.logpath import default_player_log_path
        return default_player_log_path()
    except Exception:
        return None


def run_checks(cfg, *, online: bool = False) -> list[Check]:
    from .carddb import DEFAULT_DB_PATH, CardDb
    checks: list[Check] = []

    py = sys.version_info
    checks.append(Check("Python", OK if py >= (3, 11) else FAIL, f"{py.major}.{py.minor}.{py.micro}",
                        "" if py >= (3, 11) else "Install Python 3.11 or newer."))

    route = "relay" if cfg.relay_bind else "dual logs" if (cfg.log_player1 or cfg.log_player2) else "single log"
    if route == "single log":
        log = resolve_log_path(cfg)
        if log is None or not log.is_file():
            checks.append(Check("Arena log", FAIL, "Player.log not found",
                                "Launch MTG Arena once, or pass --log-path to its Player.log."))
        else:
            checks.append(Check("Arena log", OK, str(log).replace(str(Path.home()), "~")))
            detailed = detailed_logs_status(log)
            if detailed is False:
                checks.append(Check("Detailed logs", FAIL, "Arena is not writing game events", DETAILED_LOGS_FIX))
            elif detailed is None:
                checks.append(Check("Detailed logs", WARN, "Couldn't tell yet (start Arena, then re-run)",
                                    DETAILED_LOGS_FIX))
            else:
                checks.append(Check("Detailed logs", OK, "enabled"))
    else:
        checks.append(Check("Arena log", OK, f"{route} route configured"))

    carddb_path = Path(cfg.carddb_path).expanduser() if cfg.carddb_path else DEFAULT_DB_PATH
    arena_db = False
    try:
        db = CardDb(carddb_path)
        arena_db = bool(getattr(db, "_arena_conn", None))
        db.close()
    except Exception:
        pass
    if carddb_path.is_file():
        checks.append(Check("Card names", OK, "local card database present"))
        if not _has_rules(carddb_path):
            checks.append(Check("Card rules", WARN, "card database predates rules text: the analyst "
                                "can only describe card types", "Rebuild it: arenaonair build-carddb"))
    elif arena_db:
        checks.append(Check("Card names", WARN, "using Arena's installed card data only",
                            "For card rules and full coverage run: arenaonair build-carddb"))
    else:
        checks.append(Check("Card names", FAIL, "no card data: casts will be unnamed",
                            "Run: arenaonair build-carddb   (one-time ~250 MB download from Scryfall)"))

    if importlib.util.find_spec("kokoro") is not None:
        checks.append(Check("Voices", OK, "Kokoro neural voices installed"))
    else:
        fallback = {"darwin": "say", "win32": "Windows speech"}.get(sys.platform) or (
            "espeak-ng" if shutil.which("espeak-ng") else None)
        checks.append(Check("Voices", WARN if fallback else FAIL,
                            f"Kokoro not installed; falling back to {fallback}" if fallback else "no speech engine",
                            "From the source folder, rerun run.sh / run.ps1, or install "
                            "with your environment's Python: python -m pip install -e \".[tts,ui]\""))

    if cfg.llm_base_url:
        key = os.environ.get("ARENAONAIR_API_KEY", "").strip()
        detail, status, fix = "key from ARENAONAIR_API_KEY", OK, ""
        if not key:
            kf = Path(cfg.llm_key_file).expanduser() if cfg.llm_key_file else None
            if kf is None:
                from .connection import HOSTED_URL, TRIAL_URL
                if cfg.llm_base_url in (HOSTED_URL, TRIAL_URL):
                    status, detail, fix = FAIL, "no API key", "Open Connect / subscription in the app."
                else:
                    detail = "provider without authentication"
            elif not kf.is_file():
                status, detail, fix = FAIL, "key file missing", "Open Connect / subscription in the app."
            elif os.name != "nt" and kf.stat().st_mode & 0o077:
                status, detail, fix = FAIL, "key file readable by others", f"chmod 600 {kf}"
            else:
                detail = "key file present"
        checks.append(Check("Generative booth", status, f"{cfg.llm_model} via {cfg.llm_base_url} ({detail})", fix))
        if online and status == OK:
            checks.append(_probe_model(cfg))
    else:
        checks.append(Check("Generative booth", WARN if cfg.narration_mode == "legacy" else FAIL,
                            "Scripted diagnostics" if cfg.narration_mode == "legacy" else "Connection required",
                            "Connect in the desktop app: five hosted matches, Patreon, or your own provider."))

    if cfg.overlay_port:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", cfg.overlay_port))
                checks.append(Check("OBS overlay", OK, f"http://127.0.0.1:{cfg.overlay_port}/"))
            except OSError:
                checks.append(Check("OBS overlay", FAIL, f"port {cfg.overlay_port} is in use",
                                    "Pick another overlay_port in config.toml."))
    return checks


def _has_rules(path: Path) -> bool:
    import sqlite3
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            return any(row[1] == "oracle_text" for row in conn.execute("PRAGMA table_info(cards)"))
    except sqlite3.Error:
        return False


def _probe_model(cfg) -> Check:
    try:
        from .llm_booth import JsonClient
        client = JsonClient(cfg)
        from .connection import TRIAL_URL, TRIAL_ROOT, request_json
        if client.base == TRIAL_URL:
            status = request_json(TRIAL_ROOT + "/status", client.key)
            return Check("Model connection", OK, f"Hosted GLM 5.3: {status['matches_remaining']} free matches remaining")
        client._request("Reply with the JSON object {\"ok\": true}.", {"ping": True})
        return Check("Model reachable", OK, "test request succeeded")
    except Exception as exc:
        return Check("Model reachable", FAIL, f"test request failed ({exc})",
                     "Check the base URL, model name and key.")


def _print(checks: list[Check]) -> int:
    marks = {OK: "[ok]", WARN: "[! ]", FAIL: "[x ]"}  # ASCII: Windows consoles
    for c in checks:
        print(f" {marks[c.status]} {c.name:<17} {c.detail}")
        if c.fix:
            print(f"      {'':<17} -> {c.fix}")
    failed = sum(c.status == FAIL for c in checks)
    print("\nReady to broadcast." if not failed else f"\n{failed} problem(s) to fix before a match.")
    return 1 if failed else 0


def doctor_main(argv=None) -> int:
    from . import config as config_mod
    parser = argparse.ArgumentParser(prog="arenaonair doctor", description="Check this machine is ready.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--online", action="store_true", help="Also send one test request to the model")
    args = parser.parse_args(argv)
    return _print(run_checks(config_mod.load(args.config), online=args.online))


# ---------------------------------------------------------------------------
# setup wizard
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "", *, choices=None, required=False, input_fn=input) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input_fn(f"{prompt}{suffix}: ").strip() or default
        if not answer and required:
            continue
        if choices is None or answer in choices:
            return answer
        print(f"  Choose one of: {', '.join(choices)}")


def _yes(prompt: str, default: bool, input_fn=input) -> bool:
    return _ask(prompt + " (y/n)", "y" if default else "n", choices=("y", "n", "yes", "no"),
                input_fn=input_fn).startswith("y")


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_config_toml(answers: dict) -> str:
    lines = ["# Written by 'arenaonair setup'. Edit freely; re-run setup to start over.", ""]
    lines.append(f"persona = {_toml_str(answers['persona'])}")
    if answers.get("log_path"):
        lines.append(f"log_path = {_toml_str(answers['log_path'])}")
    lines += ["", "[broadcast]", f"mode = {_toml_str(answers['mode'])}"]
    if answers.get("base_url"):
        lines += ["", "[llm]", f"base_url = {_toml_str(answers['base_url'])}",
                  f"model = {_toml_str(answers['model'])}", f"profile = {_toml_str(answers['profile'])}",
                  f"key_file = {_toml_str(answers['key_file'])}"]
    lines += ["", "[stream]", f"enabled = {'true' if answers.get('stream') else 'false'}"]
    if answers.get("stream"):
        lines += [f"delay_s = {int(answers.get('delay_s', 0))}", f"overlay_port = {int(answers.get('port', 8787))}"]
    return "\n".join(lines) + "\n"


def setup_main(argv=None, *, input_fn=input, getpass_fn=None) -> int:
    from . import config as config_mod
    from .personas import PERSONAS
    import getpass
    getpass_fn = getpass_fn or getpass.getpass
    parser = argparse.ArgumentParser(prog="arenaonair setup", description="Interactive first-run setup.")
    parser.add_argument("--config", default=str(config_mod.DEFAULT_CONFIG_PATH))
    args = parser.parse_args(argv)
    target = Path(args.config).expanduser()

    print("ArenaOnAir setup: a few questions, then you're on air.\n")
    print("Booth personas:")
    for p in PERSONAS.values():
        print(f"  {p.name:<11} {p.label}")
    answers = {"persona": _ask("Persona", "classic", choices=tuple(PERSONAS), input_fn=input_fn)}
    answers["mode"] = "dual" if _yes("Two commentators (play-by-play + analyst)?", True, input_fn) else "solo"
    existing = config_mod.load(target)
    if existing.log_path:
        answers["log_path"] = existing.log_path
    elif resolve_log_path(existing) is None:
        path = _ask("Arena Player.log path (leave blank to auto-detect later)", "", input_fn=input_fn)
        if path:
            answers["log_path"] = path

    from . import connection
    print("\nThe generative booth needs a model connection.")
    print("Hosted use sends selected game facts and booth dialogue to our server; voices run locally.")
    choice = _ask("Connection: trial (5 free matches), premium (Patreon key), custom, or later (desktop dialog)",
                  "trial", choices=("trial", "premium", "custom", "later"), input_fn=input_fn)
    if choice == "trial":
        try:
            values, status = connection.start_trial(target)
            answers.update(values)
            print(f"{status['matches_remaining']} free matches remaining. Subscribe: {connection.SUBSCRIBE_URL}")
        except connection.ConnectionError as exc:
            print(f"{exc} You can reconnect from the desktop dialog.")
    elif choice in ("premium", "custom"):
        print(f"Subscribe or get your Patreon key: {connection.SUBSCRIBE_URL}")
        answers["base_url"] = connection.HOSTED_URL if choice == "premium" else connection.endpoint(
            _ask("API endpoint (including /v1)", required=True, input_fn=input_fn))
        answers["model"] = connection.MODEL if choice == "premium" else _ask(
            "Model name (as your provider lists it)", required=True, input_fn=input_fn)
        answers["profile"] = "glm" if "glm" in answers["model"].lower() else "generic"
        key = getpass_fn("API key (hidden; blank only for a provider without authentication): ").strip()
        key_file = Path.home() / ".config" / "arenaonair" / "llm.key"
        answers["key_file"] = str(connection.write_key(key_file, key)) if key else ""
        if choice == "premium":
            try:
                if connection.MODEL not in connection.models(connection.HOSTED_URL, key):
                    print("GLM 5.3 is not available to this key. Reconnect from the desktop dialog.")
            except connection.ConnectionError as exc:
                print(str(exc))

    if _yes("Do you stream with OBS?", False, input_fn):
        answers["stream"] = True
        answers["port"] = int(_ask("Overlay port", "8787", input_fn=input_fn))
        answers["delay_s"] = int(_ask("Your OBS stream delay in seconds (0 if none)", "0", input_fn=input_fn))
        if answers["delay_s"] < config_mod.MIN_HOLE_CARD_STREAM_DELAY_S:
            print(f"  Hole cards stay off air unless your stream delay is at least "
                  f"{int(config_mod.MIN_HOLE_CARD_STREAM_DELAY_S)} s (opponents can watch).")

    if target.exists():
        backup = target.with_suffix(".toml.bak")
        target.replace(backup)
        print(f"Previous config saved as {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_config_toml(answers), encoding="utf-8")
    print(f"\nWrote {target}\n")
    return _print(run_checks(config_mod.load(target)))
