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
            elif key == "carddb_path":
                clean[key] = _coerce_str(value)
            elif key == "tts_chains":
                clean[key] = dict(value) if isinstance(value, dict) else None
            elif key == "story_thresholds":
                clean[key] = dict(value) if isinstance(value, dict) else None
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


def save_defaults(path: str | os.PathLike[str]) -> Path:
    """Write a commented starter TOML to ``path``; returns the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_STARTER_TOML, encoding="utf-8")
    return target


__all__ = ["Config", "load", "save_defaults", "DEFAULT_CONFIG_PATH"]
