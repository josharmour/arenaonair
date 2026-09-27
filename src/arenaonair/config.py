"""ArenaOnAir configuration: layered defaults < TOML file < explicit overrides.

Layering precedence (lowest binds first):

1. Built-in dataclass defaults.
2. Keys found in a TOML config file (--config PATH, else
   ``~/.arenaonair/config.toml`` when it exists).
3. Explicit keyword overrides passed to :func:`load`.

A missing file is normal (first run); a malformed file must never crash the
app -- it logs an INFO note and falls back to defaults for that layer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".arenaonair" / "config.toml"

_VALID_VERBOSITY = ("quiet", "balanced", "detailed")

_STARTER_TOML = """\
# ArenaOnAir configuration (~/.arenaonair/config.toml)
# All keys are optional; uncomment/edit what you need.
# Verbosity gates which event saliences get spoken:
#   quiet    -> salience >= 2 only
#   balanced -> salience >= 1
#   detailed -> everything

#verbosity = "balanced"

# Explicit Player.log location; omit to use the platform default.
#log_path = "/home/me/.config/Wizards/Wizards Studio/Player.log"

# Force a TTS platform chain ("windows"/"darwin"/"linux"); omit to autodetect.
#tts_platform = "linux"

# Per-platform engine chains override (first available engine wins).
#[tts_chains.linux]
#chain = ["kokoro", "piper", "espeakng"]

# Card database sqlite path; omit for ~/.cache/arenaonair/cards.sqlite.
#carddb_path = "~/.cache/arenaonair/cards.sqlite"

# Anti-drone rolling window for template variety.
#window = 8

# Log-tail polling cadence in seconds.
#poll_interval = 0.5

# Anchor startup playback at the last live GRE marker instead of replaying
# boot noise from byte zero.
#anchor = true

#[story_thresholds]
#comeback_swing = 10
"""


_DEFAULT_RELAY_BIND = "0.0.0.0:8765"

_VALID_BROADCAST_MODES = ("solo", "dual")
#: How airtime splits between calling plays and analysing the game.
COMMENTARY_FOCUSES = ("calls", "balanced", "analysis")
_VALID_INGESTION_MODES = ("single", "dual_file", "relay_server")

#: Booth presets (dual-expansions.md S3.2): preset name ->
#: (pbp_voice, analyst_voice, pbp_name, analyst_name).
BOOTH_PRESETS: dict[str, tuple[str, str, str, str]] = {
    "sports_desk": ("am_adam", "am_onyx", "Adam", "Onyx"),
    "mixed_duo": ("af_heart", "am_adam", "Heart", "Adam"),
    "premier_pro_tour": ("am_michael", "bm_george", "Michael", "George"),
    "academic_tactical": ("bm_lewis", "bf_emma", "Lewis", "Emma"),
    "esports_arena": ("am_puck", "af_nova", "Puck", "Nova"),
    "test_match": ("bm_daniel", "bm_fable", "Daniel", "Fable"),
}


def caster_name(voice: str | None) -> str | None:
    """On-air name that goes with a Kokoro voice id (``am_adam`` -> ``Adam``)."""
    if not voice:
        return None
    _, _, name = str(voice).strip().partition("_")
    return name.capitalize() or None


#: Hole cards stay off the air while streaming unless the stream is delayed
#: at least this long (opponents can otherwise watch the stream).
MIN_HOLE_CARD_STREAM_DELAY_S = 30.0


@dataclass
class Config:
    """Runtime knobs for one ArenaOnAir app instance."""

    narration_mode: str = "auto"       # auto selects llm when base_url is configured
    llm_base_url: str = ""
    llm_model: str = "glm-5.3-flash"
    llm_key_file: str = ""
    llm_profile: str = "glm"            # glm or generic (no GLM template parameters)
    llm_timeout: float = 12.0
    llm_max_age: float = 24.0
    llm_coalesce: float = 0.35
    llm_call_max_age: float = 10.0      # a play-by-play line must start this soon after its play
    log_path: str | None = None
    verbosity: str = "balanced"
    tts_platform: str | None = None
    tts_chains: dict | None = None
    carddb_path: str | None = None
    window: int = 8
    poll_interval: float = 0.5
    anchor: bool = True
    story_thresholds: dict | None = None
    tts_voice: str | None = None
    # [broadcast] dual-booth section (dual-expansions.md S3.6/S8.7):
    broadcast_mode: str = "dual"        # solo | dual
    booth_preset: str | None = None     # key of BOOTH_PRESETS or None
    pbp_voice: str | None = None        # explicit role override (wins over preset)
    analyst_voice: str | None = None
    pbp_name: str | None = None
    analyst_name: str | None = None
    co_caster_delay_ms: int = 180       # pause between PBP call and Analyst reply
    speech_speed: float = 1.6           # how fast the casters talk; 1.0 is the voice's natural pace
    # [ingestion] section:
    ingestion_mode: str = "single"      # single | dual_file | relay_server
    log_player1: str | None = None      # dual_file slot paths (either alone is valid)
    log_player2: str | None = None
    relay_bind: str | None = None       # relay_server bind address
    relay_secret: str | None = None     # optional auth token for relay clients
    # Booth persona (personas.py): tone for the LLM, voices/pace for both paths.
    persona: str | None = None
    analyst_lines_per_turn: int | None = None  # analyst lines per turn; None: set by the focus
    commentary_focus: str = "balanced"  # calls | balanced | analysis ([broadcast] focus)
    coaching: bool = False              # AI booth may suggest plays to the listening player
    # [stream] section: streamer overlay + hole-card gating.
    stream_enabled: bool = False
    stream_delay_s: float = 0.0         # declared OBS stream delay (not enforced by us)
    overlay_port: int = 0               # 0 = overlay off; else 127.0.0.1:PORT
    hole_cards: str = "auto"            # auto | on | off
    spectator: bool = False             # listener is not a player (shared-log routes)
    # [history] section: local cross-match memory + recaps.
    history_enabled: bool = True
    data_dir: str | None = None         # default ~/.arenaonair
    auto_recap: bool = True             # write a text recap at each game end


def _coerce_str(value):
    return value if isinstance(value, str) else None


def _sanitize(kwargs: dict) -> dict:
    """Drop/coerce incoming keys into types Config actually accepts."""
    clean: dict = {}
    valid_names = {f.name for f in fields(Config)}
    for key, value in kwargs.items():
        if key not in valid_names or value is None:
            continue
        try:
            if key in ("narration_mode", "llm_profile"):
                allowed = ("auto", "llm", "legacy") if key == "narration_mode" else ("glm", "generic")
                if value not in allowed:
                    raise ConfigConflict(f"Invalid {key}")
                clean[key] = value
            elif key in ("llm_base_url", "llm_model", "llm_key_file"):
                clean[key] = _coerce_str(value)
            elif key in ("llm_timeout", "llm_max_age", "llm_coalesce", "llm_call_max_age"):
                bounds = {"llm_timeout": (0.1, 20), "llm_max_age": (0.1, 45), "llm_coalesce": (0, 2),
                          "llm_call_max_age": (1, 45)}
                lo, hi = bounds[key]
                clean[key] = max(lo, min(float(value), hi))
            elif key == "verbosity":
                v = str(value).lower()
                clean[key] = v if v in _VALID_VERBOSITY else "balanced"
            elif key == "window":
                clean[key] = max(2, int(value))
            elif key == "poll_interval":
                v = float(value)
                clean[key] = v if v > 0 else 0.5
            elif key == "anchor":
                clean[key] = bool(value)
            elif key == "log_path":
                clean[key] = _coerce_str(value)
            elif key == "tts_platform":
                clean[key] = _coerce_str(value)
            elif key == "tts_voice":
                clean[key] = _coerce_str(value)
            elif key == "carddb_path":
                clean[key] = _coerce_str(value)
            elif key == "tts_chains":
                clean[key] = dict(value) if isinstance(value, dict) else None
            elif key == "story_thresholds":
                clean[key] = dict(value) if isinstance(value, dict) else None
            elif key == "broadcast_mode":
                v = str(value).lower()
                clean[key] = v if v in _VALID_BROADCAST_MODES else "solo"
            elif key == "booth_preset":
                v = str(value).strip()
                clean[key] = v if v in BOOTH_PRESETS else None
            elif key in ("pbp_voice", "analyst_voice", "pbp_name",
                         "analyst_name"):
                clean[key] = _coerce_str(value)
            elif key == "co_caster_delay_ms":
                try:
                    v = int(value)
                except (TypeError, ValueError):
                    v = 180
                clean[key] = max(0, min(v, 2000))
            elif key == "ingestion_mode":
                v = str(value).lower()
                clean[key] = v if v in _VALID_INGESTION_MODES else "single"
            elif key in ("log_player1", "log_player2", "relay_bind",
                         "relay_secret"):
                clean[key] = _coerce_str(value)
            elif key == "persona":
                from .personas import PERSONAS
                v = str(value).strip().lower()
                clean[key] = v if v in PERSONAS else None
            elif key == "analyst_lines_per_turn":
                clean[key] = max(0, min(int(value), 4))
            elif key == "commentary_focus":
                v = str(value).strip().lower()
                clean[key] = v if v in COMMENTARY_FOCUSES else "balanced"
            elif key in ("stream_enabled", "history_enabled", "auto_recap", "spectator", "coaching"):
                clean[key] = bool(value)
            elif key == "stream_delay_s":
                clean[key] = max(0.0, min(float(value), 900.0))
            elif key == "speech_speed":
                clean[key] = max(0.5, min(float(value), 2.0))
            elif key == "overlay_port":
                v = int(value)
                clean[key] = v if v == 0 or 1024 <= v <= 65535 else 0
            elif key == "hole_cards":
                v = str(value).lower()
                clean[key] = v if v in ("auto", "on", "off") else "auto"
            elif key == "data_dir":
                clean[key] = _coerce_str(value)
        except (TypeError, ValueError):
            logger.info("ignoring bad config value %s=%r", key, value)
    return clean


def load(path: str | os.PathLike[str] | None = None,
         **overrides) -> Config:
    """Build a Config through the full precedence chain.

    ``path=None`` probes :data:`DEFAULT_CONFIG_PATH` and silently proceeds
    with defaults when absent or unreadable.
    """
    values: dict = {}

    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        with open(target, "rb") as fh:
            data = tomllib.load(fh)
        if isinstance(data, dict):
            _apply_layer(values, data)
        else:
            logger.info("config file %s is not a TOML table; ignoring", target)
    except FileNotFoundError:
        if path is not None:
            logger.info("config file %s not found; using defaults", target)
    except tomllib.TOMLDecodeError as exc:
        logger.info(
            "malformed config file %s (%s); falling back to defaults",
            target,
            exc,
        )
        values.clear()
    except OSError as exc:
        logger.info("unreadable config file %s (%s); using defaults",
                    target, exc)

    _apply_layer(values, {key: os.environ[env] for key, env in (
        ("llm_base_url", "ARENAONAIR_BASE_URL"), ("llm_model", "ARENAONAIR_MODEL"),
        ("llm_key_file", "ARENAONAIR_KEY_FILE")) if os.environ.get(env)})
    _apply_layer(values, overrides)
    return Config(**values)


def _apply_layer(values: dict, data: dict) -> None:
    """Resolve each layer before combining it with the next one."""
    flat = {k: v for k, v in data.items() if k not in ("broadcast", "ingestion", "llm", "stream", "history")}
    for section, aliases in (
        ("llm", {"base_url": "llm_base_url", "model": "llm_model", "key_file": "llm_key_file",
                 "profile": "llm_profile", "timeout": "llm_timeout", "max_age": "llm_max_age", "coalesce": "llm_coalesce",
                 "call_max_age": "llm_call_max_age"}),
        ("broadcast", {"mode": "broadcast_mode", "preset": "booth_preset", "focus": "commentary_focus",
                       "speed": "speech_speed"}),
        ("ingestion", {"mode": "ingestion_mode"}),
        ("stream", {"enabled": "stream_enabled", "delay_s": "stream_delay_s"}),
        ("history", {"enabled": "history_enabled"}),
    ):
        table = data.get(section, {})
        if isinstance(table, dict):
            flat.update({aliases.get(k, k): v for k, v in table.items()})
    clean = _sanitize(flat)
    # An explicit route in a higher layer replaces the lower layer's route.
    if any(clean.get(k) for k in ("log_path", "log_player1", "log_player2", "relay_bind")):
        for k in ("log_path", "log_player1", "log_player2", "relay_bind", "ingestion_mode"):
            values.pop(k, None)
    if clean.get("persona") and "booth_preset" not in clean:
        from .personas import PERSONAS
        clean["booth_preset"] = PERSONAS[clean["persona"]].preset
    if "booth_preset" in clean:
        row = BOOTH_PRESETS.get(clean["booth_preset"])
        if row:
            for key, value in zip(("pbp_voice", "analyst_voice", "pbp_name", "analyst_name"), row):
                values[key] = value
    if clean.get("tts_voice"):
        values["pbp_voice"] = clean["tts_voice"]
    values.update(clean)


class ConfigConflict(ValueError):
    """Explicitly conflicting source-route or booth configuration."""


def resolve_route(cfg: Config) -> dict:
    """Validate source-route selection and derive the effective route.

    Rules (dual-expansions.md S8.7):
    - ``log_path`` is the legacy single-file route.
    - Either numbered file flag selects the dual_file-capable route, even
      with only one slot configured.
    - ``relay_bind`` selects the relay route.
    - Explicitly conflicting combinations raise :class:`ConfigConflict`;
      a missing/unreadable/disconnected SECOND source is never a conflict.
    """
    has_legacy = bool(cfg.log_path)
    has_numbered = bool(cfg.log_player1 or cfg.log_player2)
    has_relay = bool(cfg.relay_bind) or cfg.ingestion_mode == "relay_server"

    if has_relay and (has_legacy or has_numbered):
        raise ConfigConflict(
            "--relay-listen cannot be combined with file-source flags "
            "(--log-path / --log-player1 / --log-player2)")
    if has_legacy and has_numbered:
        raise ConfigConflict(
            "--log-path cannot be combined with --log-player1/--log-player2")

    if cfg.ingestion_mode == "dual_file" and not has_numbered:
        raise ConfigConflict("dual_file requires at least one numbered log path")
    if has_relay:
        route = "relay_server"
    elif has_numbered:
        route = "dual_file"
    elif has_legacy:
        route = "single"
    else:
        route = "auto"          # platform-default Player.log discovery
    return {"route": route}


def resolve_booth(cfg: Config) -> dict:
    """Resolve effective booth voices/names honoring S8.7 precedence.

    Within each layer: explicit preset first, legacy ``tts_voice`` alias
    next (acts as PBP voice in dual mode), explicit role overrides last.
    Absent optional role fields never overwrite preset values with
    implicit defaults.
    """
    mode = cfg.broadcast_mode if cfg.broadcast_mode in _VALID_BROADCAST_MODES \
        else "solo"
    from .personas import get as _persona
    persona = _persona(getattr(cfg, "persona", None))
    if persona is not None and cfg.booth_preset is None:
        cfg = replace(cfg, booth_preset=persona.preset)
    preset = (cfg.booth_preset if cfg.booth_preset in BOOTH_PRESETS else
              "sports_desk" if cfg.booth_preset is None and mode == "dual" else None)
    preset_row = BOOTH_PRESETS.get(preset) if preset else None

    if mode == "solo":
        # Legacy behavior: single voice, single caster.
        return {
            "mode": "solo",
            "pbp_voice": cfg.pbp_voice or cfg.tts_voice or (BOOTH_PRESETS[persona.preset][0] if persona else None),
            "analyst_voice": None,
            "pbp_name": BOOTH_PRESETS[persona.preset][2] if persona else None,
            "analyst_name": None,
            "preset": None,
            "persona": persona.name if persona else None,
        }

    pbp_voice = analyst_voice = pbp_name = analyst_name = None
    if preset_row is not None:
        pbp_voice, analyst_voice, pbp_name, analyst_name = preset_row
    # Legacy alias: --voice / tts_voice acts as the PBP voice in dual mode
    # (only when no explicit role override supersedes it below).
    alias_pbp = cfg.tts_voice
    # Explicit role overrides win over preset and alias.
    if cfg.pbp_voice is not None:
        pbp_voice = cfg.pbp_voice
    elif alias_pbp is not None:
        pbp_voice = alias_pbp
    if cfg.analyst_voice is not None:
        analyst_voice = cfg.analyst_voice
    if cfg.pbp_name is not None:
        pbp_name = cfg.pbp_name
    if cfg.analyst_name is not None:
        analyst_name = cfg.analyst_name
    return {
        "mode": "dual",
        "pbp_voice": pbp_voice,
        "analyst_voice": analyst_voice,
        "pbp_name": pbp_name,
        "analyst_name": analyst_name,
        "preset": preset,
        "persona": persona.name if persona else None,
    }


def hole_cards_enabled(cfg: Config) -> bool:
    """Whether the broadcast may reveal private hands.

    With a shared-log route (two logs or the relay) the booth can see BOTH
    hands, so a listening player would hear their opponent's cards. Those
    routes keep every hand off air unless the listener declares they are a
    spectator -- even ``hole_cards = "on"`` does not override that.
    """
    try:
        shared = resolve_route(cfg)["route"] in ("dual_file", "relay_server")
    except ConfigConflict:
        shared = True
    if shared and not getattr(cfg, "spectator", False):
        return False
    mode = getattr(cfg, "hole_cards", "auto")
    if mode in ("on", "off"):
        return mode == "on"
    if not getattr(cfg, "stream_enabled", False):
        return True
    return float(getattr(cfg, "stream_delay_s", 0.0)) >= MIN_HOLE_CARD_STREAM_DELAY_S


def data_dir(cfg: Config | None = None) -> Path:
    """Local directory for match records, history and recaps."""
    raw = (getattr(cfg, "data_dir", None) if cfg is not None else None) or os.environ.get("ARENAONAIR_DATA_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".arenaonair"


_TOML_TABLE = re.compile(r"^\s*\[")


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))


def _set_toml_key(lines: list[str], section: str | None, key: str, value) -> None:
    """Set (or with None, remove) one scalar key in place; comments stay put."""
    if section is None:
        start, end = 0, next((i for i, line in enumerate(lines) if _TOML_TABLE.match(line)), len(lines))
    else:
        header = next((i for i, line in enumerate(lines)
                       if re.match(rf"^\s*\[\s*{re.escape(section)}\s*\]\s*(#.*)?$", line)), None)
        if header is None:
            if value is None:
                return
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            header = len(lines) - 1
        start = header + 1
        end = next((i for i in range(start, len(lines)) if _TOML_TABLE.match(lines[i])), len(lines))
    found = next((i for i in range(start, end) if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i])), None)
    if value is None:
        if found is not None:
            del lines[found]
        return
    entry = f"{key} = {_toml_value(value)}"
    if found is not None:
        lines[found] = entry
        return
    insert = end
    while insert > start and not lines[insert - 1].strip():
        insert -= 1
    lines.insert(insert, entry)


def save_settings(path: str | os.PathLike[str], updates: dict) -> Path:
    """Write settings as ``{table or None (top level): {key: value or None}}``.

    None removes a key, so the default applies again. Comments, ordering and
    every other key stay as written. The result is re-parsed before it replaces
    the file, so an unusual layout (an inline table, say) raises ValueError and
    leaves it untouched.
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    lines = text.splitlines()
    for section, values in updates.items():
        for key, value in values.items():
            _set_toml_key(lines, section, key, value)
    updated = "\n".join(lines) + "\n"
    data = tomllib.loads(updated)
    for section, values in updates.items():
        table = data if section is None else data.get(section)
        if not isinstance(table, dict) and any(v is not None for v in values.values()):
            raise ValueError(f"could not update [{section}] in {target}")
        for key, value in values.items():
            if (table or {}).get(key) != value:
                raise ValueError(f"could not update {key} in {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(updated, encoding="utf-8")
    if text:
        shutil.copymode(target, temp)  # the file may hold a relay secret
    os.replace(temp, target)
    return target


def save_broadcast_settings(path: str | os.PathLike[str], values: dict[str, str]) -> Path:
    """Set keys in the ``[broadcast]`` table (see :func:`save_settings`)."""
    return save_settings(path, {"broadcast": values})


def coerce(key: str, value):
    """One setting as Config stores it. None or "" clears it back to its default."""
    if value is None or value == "":
        return None
    clean = _sanitize({key: value})
    if key not in clean:
        raise ValueError(f"invalid value for {key}: {value!r}")
    return clean[key]


def default(key: str):
    """Built-in default of one Config field."""
    return next(f.default for f in fields(Config) if f.name == key)


def save_defaults(path: str | os.PathLike[str]) -> Path:
    """Write a commented starter TOML to ``path``; returns the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_STARTER_TOML, encoding="utf-8")
    return target


__all__ = [
    "Config", "load", "save_defaults", "DEFAULT_CONFIG_PATH",
    "BOOTH_PRESETS", "ConfigConflict", "resolve_route", "resolve_booth",
    "hole_cards_enabled", "data_dir", "MIN_HOLE_CARD_STREAM_DELAY_S",
    "caster_name", "save_broadcast_settings", "save_settings", "coerce", "default", "COMMENTARY_FOCUSES",
]
