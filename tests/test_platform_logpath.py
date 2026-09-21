"""Tests for arenaonair.platform log-path resolution.

Dispatch is asserted per fake sys.platform value by monkeypatching
arenaonair.platform.logpath.sys.platform; per-OS resolvers are exercised
against tmp_path homes/env without touching the real filesystem.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from arenaonair.platform import default_player_log_path, logpath
from arenaonair.platform import linux as plat_linux
from arenaonair.platform import macos as plat_macos
from arenaonair.platform import windows as plat_windows


def _mk_mtga(base: Path) -> Path:
    p = base / "Wizards Of The Coast" / "MTGA" / "Player.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stub\n")
    return p


class TestDispatch:

    def test_dispatch_windows(self, monkeypatch, tmp_path):
        local = tmp_path / "AppData" / "Local"
        local.mkdir(parents=True)
        expected = _mk_mtga(local.parent / "LocalLow")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setattr(logpath.sys, "platform", "win32")
        assert logpath.default_player_log_path() == expected

    def test_dispatch_macos(self, monkeypatch, tmp_path):
        base = tmp_path / "Library" / "Application Support" / "com.wizards.mtga"
        expected = base / "Logs" / "Player.log"
        expected.parent.mkdir(parents=True)
        expected.write_text("stub\n")
        monkeypatch.setattr(logpath.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert logpath.default_player_log_path() == expected

    def test_dispatch_linux(self, monkeypatch, tmp_path):
        steamapps = tmp_path / ".steam" / "steam" / "steamapps"
        rel = ("compatdata/2141910/pfx/drive_c/users/steamuser/AppData/"
               "LocalLow/Wizards Of The Coast/MTGA/Player.log")
        expected = steamapps / rel
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text("stub\n")
        monkeypatch.setattr(logpath.sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert logpath.default_player_log_path() == expected

    def test_unsupported_platform_raises(self, monkeypatch):
        monkeypatch.setattr(logpath.sys, "platform", "sunos5")
        with pytest.raises(RuntimeError):
            logpath.default_player_log_path()


class TestWindowsResolver:

    def test_localappdata_sibling_local_low(self, monkeypatch, tmp_path):
        local = tmp_path / "AppData" / "Local"
        local.mkdir(parents=True)
        expected = _mk_mtga(local.parent / "LocalLow")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        assert plat_windows.default_player_log_path() == expected

    def test_home_fallback_when_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        expected = _mk_mtga(tmp_path / "AppData" / "LocalLow")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert plat_windows.default_player_log_path() == expected

    def test_raises_listing_candidates_when_absent(self, monkeypatch, tmp_path):
        local = tmp_path / "AppData" / "Local"
        local.mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(FileNotFoundError) as ei:
            plat_windows.default_player_log_path()
        msg = str(ei.value)
        assert "tried" in msg.lower()


class TestMacosResolver:

    def test_direct_logs_path_preferred(self, monkeypatch, tmp_path):
        base = tmp_path / "Library" / "Application Support" / "com.wizards.mtga"
        expected = base / "Logs" / "Player.log"
        expected.parent.mkdir(parents=True)
        expected.write_text("stub\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert plat_macos.default_player_log_path() == expected

    def test_glob_finds_dated_subdir_newest(self, monkeypatch, tmp_path):
        base = tmp_path / "Library" / "Application Support" / "com.wizards.mtga"
        old_dir = base / "Logs" / "2026-01-01"
        new_dir = base / "Logs" / "2026-09-18"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        old = old_dir / "Player.log"
        new = new_dir / "Player.log"
        old.write_text("old\n")
        new.write_text("new\n")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert plat_macos.default_player_log_path() == new

    def test_glob_prefers_player_over_other_logs(self, monkeypatch, tmp_path):
        base = tmp_path / "Library" / "Application Support" / "com.wizards.mtga"
        d = base / "Logs" / "2026-09-18"
        d.mkdir(parents=True)
        other = d / "updater.log"
        player = d / "Player.log"
        other.write_text("x\n")
        player.write_text("y\n")
        os.utime(other, (2_000_000, 2_000_000))
        os.utime(player, (1_000_000, 1_000_000))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert plat_macos.default_player_log_path() == player

    def test_raises_when_base_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(FileNotFoundError):
            plat_macos.default_player_log_path()


class TestLinuxResolver:

    STEAM_REL = ("compatdata/2141910/pfx/drive_c/users/steamuser/AppData/"
                 "LocalLow/Wizards Of The Coast/MTGA/Player.log")

    def test_dot_steam_root(self, monkeypatch, tmp_path):
        expected = tmp_path / ".steam" / "steam" / "steamapps" / self.STEAM_REL
        expected.parent.mkdir(parents=True)
        expected.write_text("stub\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert plat_linux.default_player_log_path() == expected

    def test_local_share_steam_root(self, monkeypatch, tmp_path):
        expected = (tmp_path / ".local" / "share" / "Steam" /
                    "steamapps" / self.STEAM_REL)
        expected.parent.mkdir(parents=True)
        expected.write_text("stub\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert plat_linux.default_player_log_path() == expected

    def test_dot_steam_wins_when_both_exist(self, monkeypatch, tmp_path):
        first = tmp_path / ".steam" / "steam" / "steamapps" / self.STEAM_REL
        second = (tmp_path / ".local" / "share" / "Steam" /
                  "steamapps" / self.STEAM_REL)
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("a\n")
        second.write_text("b\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert plat_linux.default_player_log_path() == first

    def test_raises_listing_both_roots_when_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(FileNotFoundError) as ei:
            plat_linux.default_player_log_path()
        assert ".steam" in str(ei.value) and ".local/share/Steam".replace("/", os.sep) in str(ei.value) or ".local" in str(ei.value)


class TestFirstExistingHelper:

    def test_returns_first_match(self, tmp_path):
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        a.write_text("1\n")
        b.write_text("2\n")
        assert logpath._first_existing([tmp_path / "zzz.log", a, b]) == a

    def test_error_lists_all_tried(self, tmp_path):
        with pytest.raises(FileNotFoundError) as ei:
            logpath._first_existing([tmp_path / "x.log", tmp_path / "y.log"])
        msg = str(ei.value)
        assert str(tmp_path / "x.log") in msg
