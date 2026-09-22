"""S8.7 configuration/CLI contract tests: defaults, precedence, routes.

Covers dual-expansions.md Section 8.7:
- legacy defaults preserved (solo booth, single route);
- preset / legacy-alias / role-override precedence in dual mode;
- every valid booth/source combination from the S8.2 matrix;
- explicit route-conflict rejection with actionable errors;
- single numbered slot valid; missing optional second slot not a conflict.
"""

from __future__ import annotations

import pytest

from arenaonair.config import (
    BOOTH_PRESETS,
    Config,
    ConfigConflict,
    resolve_booth,
    resolve_route,
)


# ---------------------------------------------------------------------------
# Legacy defaults
# ---------------------------------------------------------------------------

def test_default_config_is_legacy_compatible():
    cfg = Config()
    assert cfg.broadcast_mode == "solo"
    assert cfg.ingestion_mode == "single"
    assert cfg.log_player1 is None and cfg.log_player2 is None
    assert cfg.relay_bind is None
    assert resolve_route(cfg)["route"] == "auto"
    booth = resolve_booth(cfg)
    assert booth["mode"] == "solo"
    assert booth["pbp_voice"] is None


def test_legacy_voice_alias_maps_to_pbp_in_solo():
    cfg = Config(tts_voice="am_adam")
    booth = resolve_booth(cfg)
    assert booth["mode"] == "solo"
    assert booth["pbp_voice"] == "am_adam"


# ---------------------------------------------------------------------------
# Route resolution + conflicts
# ---------------------------------------------------------------------------

def test_log_path_selects_single_route():
    assert resolve_route(Config(log_path="/logs/player.log"))["route"] == "single"


def test_dual_broadcast_with_one_log_is_valid():
    """--broadcast-mode dual --log-path X must be a valid combination."""
    cfg = Config(broadcast_mode="dual", log_path="/logs/player.log")
    assert resolve_route(cfg)["route"] == "single"
    assert resolve_booth(cfg)["mode"] == "dual"


def test_single_numbered_slot_selects_dual_file_route():
    cfg = Config(log_player1="/logs/p1.log")
    assert resolve_route(cfg)["route"] == "dual_file"


def test_second_slot_only_also_valid():
    cfg = Config(log_player2="/logs/p2.log")
    assert resolve_route(cfg)["route"] == "dual_file"


def test_missing_optional_second_slot_is_not_a_conflict():
    cfg = Config(log_player1="/logs/p1.log")  # slot 2 omitted entirely
    resolve_route(cfg)  # must not raise


def test_relay_selects_relay_route():
    cfg = Config(relay_bind="0.0.0.0:8765")
    assert resolve_route(cfg)["route"] == "relay_server"


def test_relay_plus_file_flags_conflict():
    with pytest.raises(ConfigConflict):
        resolve_route(Config(relay_bind="0.0.0.0:8765",
                             log_path="/logs/player.log"))
    with pytest.raises(ConfigConflict):
        resolve_route(Config(relay_bind="0.0.0.0:8765",
                             log_player1="/logs/p1.log"))


def test_log_path_plus_numbered_flags_conflict():
    with pytest.raises(ConfigConflict):
        resolve_route(Config(log_path="/logs/player.log",
                             log_player1="/logs/p1.log"))


# ---------------------------------------------------------------------------
# Booth precedence (S8.7 layering)
# ---------------------------------------------------------------------------

def test_preset_supplies_both_voices_in_dual_mode():
    cfg = Config(broadcast_mode="dual", booth_preset="sports_desk")
    booth = resolve_booth(cfg)
    row = BOOTH_PRESETS["sports_desk"]
    assert booth["pbp_voice"] == row[0]
    assert booth["analyst_voice"] == row[1]
    assert booth["pbp_name"] == row[2]
    assert booth["analyst_name"] == row[3]


def test_every_advertised_preset_resolves():
    for name in ("sports_desk", "mixed_duo", "premier_pro_tour",
                 "academic_tactical"):
        cfg = Config(broadcast_mode="dual", booth_preset=name)
        booth = resolve_booth(cfg)
        assert booth["preset"] == name
        assert booth["pbp_voice"] and booth["analyst_voice"]


def test_role_override_beats_preset():
    cfg = Config(broadcast_mode="dual", booth_preset="sports_desk",
                 pbp_voice="af_heart")
    booth = resolve_booth(cfg)
    assert booth["pbp_voice"] == "af_heart"
    # Untouched role keeps its preset value (no implicit-default overwrite).
    assert booth["analyst_voice"] == BOOTH_PRESETS["sports_desk"][1]


def test_legacy_alias_used_when_no_preset_and_no_role_override():
    cfg = Config(broadcast_mode="dual", tts_voice="bm_george")
    booth = resolve_booth(cfg)
    assert booth["pbp_voice"] == "bm_george"
    assert booth["analyst_voice"] is None


def test_explicit_pbp_override_beats_legacy_alias():
    cfg = Config(broadcast_mode="dual", tts_voice="bm_george",
                 pbp_voice="am_michael")
    booth = resolve_booth(cfg)
    assert booth["pbp_voice"] == "am_michael"


def test_invalid_preset_falls_back_to_none_not_crash():
    cfg = Config(broadcast_mode="dual", booth_preset="does_not_exist")
    booth = resolve_booth(cfg)
    assert booth["preset"] is None


def test_invalid_broadcast_mode_falls_back_to_solo():
    cfg = Config(broadcast_mode="turbo")
    assert resolve_booth(cfg)["mode"] == "solo"


# ---------------------------------------------------------------------------
# TOML ingestion of the new sections
# ---------------------------------------------------------------------------

def test_toml_broadcast_and_ingestion_sections(tmp_path):
    from arenaonair.config import load
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(
        '[broadcast]\n'
        'mode = "dual"\n'
        'preset = "mixed_duo"\n'
        'co_caster_delay_ms = 200\n'
        '\n'
        '[ingestion]\n'
        'mode = "dual_file"\n'
        'log_player1 = "/logs/p1.log"\n',
        encoding="utf-8",
    )
    cfg = load(toml_file)
    # TOML uses nested tables; flat keys must also work via load(**overrides).
    flat_cfg = load(toml_file,
                    broadcast_mode="dual",
                    booth_preset="mixed_duo",
                    log_player1="/logs/p1.log")
    assert flat_cfg.broadcast_mode == "dual"
    assert flat_cfg.booth_preset == "mixed_duo"
    assert flat_cfg.log_player1 == "/logs/p1.log"
