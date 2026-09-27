"""Keep tests away from the real ~/.arenaonair: no user config, history or recaps.

A developer's own config.toml can select the live model booth; tests must
never read it (or send requests to a real gateway).
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_user_files(tmp_path_factory, monkeypatch):
    from arenaonair import config
    home = tmp_path_factory.mktemp("arenaonair-home")
    monkeypatch.setenv("ARENAONAIR_DATA_DIR", str(home / "data"))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", home / "config.toml")
    for var in ("ARENAONAIR_BASE_URL", "ARENAONAIR_MODEL", "ARENAONAIR_KEY_FILE", "ARENAONAIR_API_KEY"):
        monkeypatch.delenv(var, raising=False)
