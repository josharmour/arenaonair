"""Tests for arenaonair.config: precedence chain + tolerance."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from arenaonair.config import Config, load, save_defaults


def _write_toml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_default_config_fields(self):
        cfg = Config()
        assert cfg.log_path is None
        assert cfg.verbosity == "balanced"
        assert cfg.tts_platform is None
        assert cfg.tts_chains is None
        assert cfg.carddb_path is None
        assert cfg.window == 8
        assert cfg.poll_interval == 0.5
        assert cfg.anchor is True
        assert cfg.story_thresholds is None

    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = load(tmp_path / "does_not_exist.toml")
        assert cfg == Config()


class TestTomlLayer:
    def test_toml_overrides_defaults(self, tmp_path):
        path = _write_toml(tmp_path / "cfg.toml", 'verbosity = "quiet"\n'
                                                 'window = 12\n'
                                                 'poll_interval = 0.25\n'
                                                 'anchor = false\n')
        cfg = load(path)
        assert cfg.verbosity == "quiet"
        assert cfg.window == 12
        assert cfg.poll_interval == 0.25
        assert cfg.anchor is False

    def test_kwargs_override_toml(self, tmp_path):
        path = _write_toml(tmp_path / "cfg.toml", 'verbosity = "quiet"\n'
                                                 'window = 12\n')
        cfg = load(path, verbosity="detailed", window=4)
        assert cfg.verbosity == "detailed"
        assert cfg.window == 4

    def test_full_roundtrip(self, tmp_path):
        path = _write_toml(
            tmp_path / "cfg.toml",
            'log_path = "/tmp/player.log"\n'
            'verbosity = "detailed"\n'
            'tts_platform = "linux"\n'
            'carddb_path = "/tmp/cards.sqlite"\n'
            'window = 6\n'
            'poll_interval = 0.1\n'
            'anchor = false\n',
        )
        cfg = load(path)
        assert cfg.log_path == "/tmp/player.log"
        assert cfg.verbosity == "detailed"
        assert cfg.tts_platform == "linux"
        assert cfg.carddb_path == "/tmp/cards.sqlite"
        assert cfg.window == 6
        assert cfg.poll_interval == 0.1
        assert cfg.anchor is False

    def test_nested_tables_map_to_dicts(self, tmp_path):
        path = _write_toml(
            tmp_path / "cfg.toml",
            "[tts_chains.linux]\nchain = [\"espeakng\"]\n",
        )
        cfg = load(path)
        assert isinstance(cfg.tts_chains, dict)
        assert cfg.tts_chains["linux"]["chain"] == ["espeakng"]


class TestMalformedTolerance:
    def test_malformed_toml_falls_back_to_defaults(self, tmp_path):
        path = _write_toml(tmp_path / "broken.toml",
                           "this is ][ not toml {{{\n")
        cfg = load(path)
        assert cfg == Config()

    def test_malformed_toml_still_accepts_overrides(self, tmp_path):
        path = _write_toml(tmp_path / "broken.toml", "@@@ broken @@@\n")
        cfg = load(path, verbosity="quiet")
        assert cfg.verbosity == "quiet"

    def test_unknown_keys_ignored(self, tmp_path):
        path = _write_toml(tmp_path / "cfg.toml", 'bogus_key = 42\n')
        cfg = load(path)
        assert cfg == Config()

    def test_bad_values_coerced_or_dropped(self, tmp_path):
        path = _write_toml(tmp_path / "cfg.toml",
                           'window = "not-an-int"\n'
                           'verbosity = "shouting"\n')
        cfg = load(path)
        # bad window dropped -> default; bad verbosity -> balanced
        assert cfg.window == 8
        assert cfg.verbosity == "balanced"


class TestSaveDefaults:
    def test_save_defaults_roundtrip(self, tmp_path):
        target = tmp_path / "sub" / "starter.toml"
        written = save_defaults(target)
        assert written == target
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        # Starter file must parse as TOML (comments/keys only).
        assert isinstance(data, dict)

    def test_saved_defaults_load_cleanly(self, tmp_path):
        target = tmp_path / "starter.toml"
        save_defaults(target)
        cfg = load(target)
        # Everything commented out -> pure defaults.
        assert cfg == Config()

    def test_save_defaults_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c.toml"
        save_defaults(target)
        assert target.is_file()


class TestDefaultPathProbe:
    def test_none_probes_default_without_explosion(self, monkeypatch,
                                                   tmp_path):
        # Point the module's default at a nonexistent path: must not raise.
        import arenaonair.config as cmod
        monkeypatch.setattr(cmod, "DEFAULT_CONFIG_PATH",
                            tmp_path / "nope.toml")
        cfg = cmod.load(None)
        assert isinstance(cfg, Config)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
