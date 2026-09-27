"""Every setting the window can change: where it lives in config.toml and how it applies.

The window builds its Settings tab from :data:`SETTINGS`, and
``ArenaOnAirApp.apply_setting`` uses it to save each change to the right TOML
table. ``restart`` settings are wired into objects built at startup (log
watchers, the model connection); everything else applies to the next line.
Launch-only flags (--dry-run, --once, --list-voices, --ui, --config) and nested
tables (tts_chains, story_thresholds) stay in the CLI and config.toml.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import BOOTH_PRESETS
from .personas import PERSONAS


@dataclass(frozen=True)
class Setting:
    key: str                     # Config field
    group: str                   # heading in the Settings tab
    label: str
    kind: str                    # choice | bool | int | float | text | path | secret
    section: str | None = None   # TOML table; None is the top level
    toml_key: str | None = None  # name inside that table, when not the field name
    choices: tuple = ()          # (value, label) pairs; value None means "not set"
    bounds: tuple = ()           # (minimum, maximum, step) for numbers
    restart: bool = False
    help: str = ""

    @property
    def name(self) -> str:
        return self.toml_key or self.key


_PRESETS = tuple((name, f"{name.replace('_', ' ').capitalize()}: {row[2]} and {row[3]}")
                 for name, row in BOOTH_PRESETS.items())
_PERSONAS = ((None, "Standard"),) + tuple((p.name, p.label) for p in PERSONAS.values())

SETTINGS: tuple[Setting, ...] = (
    # -- booth (the Broadcast tab shows these beside the caster and voice pickers) --
    Setting("commentary_focus", "Booth", "Focus", "choice", section="broadcast", toml_key="focus",
            choices=(("calls", "Mostly calls"), ("balanced", "Balanced"), ("analysis", "Mostly analysis"))),
    Setting("coaching", "Booth", "Coaching", "bool", section="broadcast",
            help="The AI booth may suggest plays for you, using only what you can see."),
    Setting("speech_speed", "Booth", "Speed", "float", section="broadcast", toml_key="speed",
            bounds=(0.5, 2.0, 0.1), help="How fast the casters talk. 1.0 is the voice's natural pace."),
    # -- commentary -----------------------------------------------------------
    Setting("narration_mode", "Commentary", "Writer", "choice",
            choices=(("auto", "Generative booth (connection required)"), ("llm", "AI booth"),
                     ("legacy", "Scripted replay / diagnostics")), restart=True,
            help="The AI booth writes original lines with a language model; the scripted booth uses templates."),
    Setting("persona", "Commentary", "Style", "choice", choices=_PERSONAS,
            help="Tone for the AI booth and delivery pace for the scripted booth."),
    Setting("verbosity", "Commentary", "Plays called", "choice",
            choices=(("quiet", "Quiet: big moments only"), ("balanced", "Balanced"),
                     ("detailed", "Detailed: everything, turn starts too"))),
    Setting("analyst_lines_per_turn", "Commentary", "Analyst lines per turn", "choice", section="broadcast",
            choices=((None, "Set by the focus"), (0, "0"), (1, "1"), (2, "2"), (3, "3"), (4, "4")),
            help="Big moments are exempt."),
    Setting("co_caster_delay_ms", "Commentary", "Pause before the analyst replies (ms)", "int",
            section="broadcast", bounds=(0, 2000, 20)),
    # -- casters --------------------------------------------------------------
    Setting("booth_preset", "Casters", "Voice pair", "choice", section="broadcast", toml_key="preset",
            choices=((None, "Custom (use the voice pickers)"),) + _PRESETS,
            help="Sets both voices and names; the Broadcast tab's pickers can change them after."),
    Setting("pbp_name", "Casters", "Play-by-play name", "text", section="broadcast",
            help="Blank: named after the voice."),
    Setting("analyst_name", "Casters", "Analyst name", "text", section="broadcast",
            help="Blank: named after the voice."),
    # -- your game --------------------------------------------------------------
    Setting("log_path", "Your game", "Arena Player.log", "path", restart=True,
            help="Blank: found automatically."),
    Setting("hole_cards", "Your game", "Your hand on air", "choice", section="stream",
            choices=(("auto", "Auto: off while streaming without enough delay"), ("on", "On"), ("off", "Off"))),
    Setting("spectator", "Your game", "I'm watching, not playing", "bool", section="stream",
            help="With shared logs, lets both hands on air: the listener isn't one of the players."),
    Setting("history_enabled", "Your game", "Remember past matches", "bool", section="history", toml_key="enabled",
            restart=True),
    Setting("auto_recap", "Your game", "Write a recap after each game", "bool", section="history"),
    # -- streaming --------------------------------------------------------------
    Setting("stream_enabled", "Streaming", "I'm streaming", "bool", section="stream", toml_key="enabled"),
    Setting("stream_delay_s", "Streaming", "Stream delay (seconds)", "float", section="stream", toml_key="delay_s",
            bounds=(0, 900, 5), help="Your hand stays off air unless the delay is at least 30 seconds."),
    Setting("overlay_port", "Streaming", "OBS overlay port", "int", section="stream", bounds=(0, 65535, 1),
            help="0 turns the overlay off; otherwise 1024 or higher. Serves http://127.0.0.1:PORT/."),
    # -- AI model ---------------------------------------------------------------
    Setting("llm_base_url", "AI model", "API base URL", "text", section="llm", toml_key="base_url", restart=True,
            help="Including /v1. The key itself stays in its own file."),
    Setting("llm_model", "AI model", "Model", "text", section="llm", toml_key="model", restart=True),
    Setting("llm_key_file", "AI model", "Key file", "path", section="llm", toml_key="key_file", restart=True),
    Setting("llm_profile", "AI model", "Request profile", "choice", section="llm", toml_key="profile",
            choices=(("glm", "GLM"), ("generic", "Generic OpenAI-compatible")), restart=True),
    Setting("llm_timeout", "AI model", "Request timeout (s)", "float", section="llm", toml_key="timeout",
            bounds=(0.1, 20, 0.5), restart=True),
    Setting("llm_max_age", "AI model", "Drop lines older than (s)", "float", section="llm", toml_key="max_age",
            bounds=(0.1, 45, 1)),
    Setting("llm_coalesce", "AI model", "Group plays within (s)", "float", section="llm", toml_key="coalesce",
            bounds=(0, 2, 0.05)),
    Setting("llm_call_max_age", "AI model", "Start a play call within (s)", "float", section="llm",
            toml_key="call_max_age", bounds=(1, 45, 1),
            help="A play-by-play line that can't start this soon after its play is dropped, so the booth "
                 "doesn't describe a stack that's long over. Game-ending calls are exempt."),
    # -- shared logs and relay ----------------------------------------------------
    Setting("log_player1", "Shared logs", "Player 1 log", "path", section="ingestion", restart=True,
            help="Two players' logs; either slot works alone. Replaces Player.log above."),
    Setting("log_player2", "Shared logs", "Player 2 log", "path", section="ingestion", restart=True),
    Setting("relay_bind", "Shared logs", "Relay listen address", "text", section="ingestion", restart=True,
            help="HOST:PORT, e.g. 0.0.0.0:8765, to receive logs from players' machines."),
    Setting("relay_secret", "Shared logs", "Relay secret", "secret", section="ingestion", restart=True),
    # -- advanced -----------------------------------------------------------------
    Setting("tts_platform", "Advanced", "Voice engines", "choice",
            choices=((None, "This computer's"), ("darwin", "macOS"), ("windows", "Windows"), ("linux", "Linux")),
            restart=True),
    Setting("anchor", "Advanced", "Start at the live game, skipping older log", "bool", restart=True),
    Setting("window", "Advanced", "Lines before a phrase can repeat", "int", bounds=(2, 64, 1), restart=True),
    Setting("poll_interval", "Advanced", "Log check interval (s)", "float", bounds=(0.05, 5, 0.05), restart=True),
    Setting("carddb_path", "Advanced", "Card database", "path", restart=True, help="Blank: ~/.cache/arenaonair."),
    Setting("data_dir", "Advanced", "Data folder", "path", restart=True, help="Blank: ~/.arenaonair."),
)

BY_KEY = {s.key: s for s in SETTINGS}
GROUPS = tuple(dict.fromkeys(s.group for s in SETTINGS))
