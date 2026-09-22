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

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
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
_VALID_INGESTION_MODES = ("single", "dual_file", "relay_server")

#: Booth presets (dual-expansions.md S3.2): preset name ->
#: (pbp_voice, analyst_voice, pbp_name, analyst_name).
BOOTH_PRESETS: dict[str, tuple[str, str, str, str]] = {
    "sports_desk": ("am_adam", "am_onyx", "Adam", "Onyx"),
    "mixed_duo": ("af_heart", "am_adam", "Heart", "Adam"),
    "premier_pro_tour": ("am_michael", "bm_george", "Michael", "George"),
    "academic_tactical": ("bm_lewis", "bf_emma", "Lewis", "Emma"),
}


@dataclass
class Config:
    """Runtime knobs for one ArenaOnAir app instance."""

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
    broadcast_mode: str = "solo"        # solo | dual
    booth_preset: str | None = None     # key of BOOTH_PRESETS or None
    pbp_voice: str | None = None        # explicit role override (wins over preset)
    analyst_voice: str | None = None
    pbp_name: str | None = None
    analyst_name: str | None = None
    co_caster_delay_ms: int = 180       # pause between PBP call and Analyst reply
    # [ingestion] section:
    ingestion_mode: str = "single"      # single | dual_file | relay_server
    log_player1: str | None = None      # dual_file slot paths (either alone is valid)
    log_player2: str | None = None
    relay_bind: str | None = None       # relay_server bind address
    relay_secret: str | None = None     # optional auth token for relay clients


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
            if key == "verbosity":
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
            values.update(_sanitize(data))
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

    values.update(_sanitize(overrides))
    return Config(**values)


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
    has_relay = bool(cfg.relay_bind)

    if has_relay and (has_legacy or has_numbered):
        raise ConfigConflict(
            "--relay-listen cannot be combined with file-source flags "
            "(--log-path / --log-player1 / --log-player2)")
    if has_legacy and has_numbered:
        raise ConfigConflict(
            "--log-path cannot be combined with --log-player1/--log-player2")

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
    preset = cfg.booth_preset if cfg.booth_preset in BOOTH_PRESETS else None
    preset_row = BOOTH_PRESETS.get(preset) if preset else None

    if mode == "solo":
        # Legacy behavior: single voice, single caster.
        return {
            "mode": "solo",
            "pbp_voice": cfg.tts_voice,
            "analyst_voice": None,
            "pbp_name": None,
            "analyst_name": None,
            "preset": None,
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
    elif alias_pbp is not None and preset_row is None:
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
    }


def save_defaults(path: str | os.PathLike[str]) -> Path:
    """Write a commented starter TOML to ``path``; returns the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_STARTER_TOML, encoding="utf-8")
    return target


__all__ = [
    "Config", "load", "save_defaults", "DEFAULT_CONFIG_PATH",
    "BOOTH_PRESETS", "ConfigConflict", "resolve_route", "resolve_booth",
]
