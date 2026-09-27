"""Desktop app: the voice picker, saving booth voices, and install-app launchers."""
import plistlib
import shutil
import struct
import sys
import tomllib
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from arenaonair import desktop
from arenaonair.app import ArenaOnAirApp
from arenaonair.config import Config, caster_name, load, resolve_booth, save_broadcast_settings
from arenaonair.speech import SpeechQueue

USER_CONFIG = """\
# ArenaOnAir on this Mac: the generative (AI) booth, two casters.
narration_mode = "llm"
persona = "classic"

[broadcast]
mode = "dual"
preset = "sports_desk"

[llm]
base_url = "https://example.invalid/v1"
model = "glm-5.3-flash"
"""


def test_saving_voices_keeps_the_rest_of_the_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(USER_CONFIG, encoding="utf-8")
    save_broadcast_settings(path, {"pbp_voice": "bm_george", "analyst_voice": "af_nova"})
    save_broadcast_settings(path, {"pbp_voice": "am_eric"})
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# ArenaOnAir on this Mac")
    assert text.count("pbp_voice") == 1
    data = tomllib.loads(text)
    assert data["broadcast"] == {"mode": "dual", "preset": "sports_desk",
                                 "pbp_voice": "am_eric", "analyst_voice": "af_nova"}
    assert data["llm"]["model"] == "glm-5.3-flash"
    booth = resolve_booth(load(path))
    assert (booth["pbp_voice"], booth["analyst_voice"]) == ("am_eric", "af_nova")
    # Explicit launch flags still beat the saved choice.
    assert resolve_booth(load(path, pbp_voice="am_onyx"))["pbp_voice"] == "am_onyx"


def test_saving_voices_creates_a_config_and_refuses_odd_layouts(tmp_path):
    fresh = tmp_path / "new" / "config.toml"
    save_broadcast_settings(fresh, {"pbp_voice": "af_heart"})
    assert tomllib.loads(fresh.read_text(encoding="utf-8")) == {"broadcast": {"pbp_voice": "af_heart"}}
    inline = tmp_path / "inline.toml"
    inline.write_text('broadcast = { mode = "dual" }\n', encoding="utf-8")
    with pytest.raises(ValueError):
        save_broadcast_settings(inline, {"pbp_voice": "af_heart"})
    assert inline.read_text(encoding="utf-8") == 'broadcast = { mode = "dual" }\n'


def test_solo_booth_uses_a_saved_voice():
    assert resolve_booth(Config(broadcast_mode="solo", pbp_voice="bf_emma"))["pbp_voice"] == "bf_emma"
    assert caster_name("am_adam") == "Adam" and caster_name(None) is None


def _app(tmp_path, **config):
    app = ArenaOnAirApp(Config(carddb_path=str(tmp_path / "none"), **config), dry_run=True)
    app.config_path = tmp_path / "config.toml"
    return app


def test_picking_voices_switches_live_renames_casters_and_saves(tmp_path):
    app = _app(tmp_path)
    try:
        assert (app.booth["pbp_voice"], app.booth["pbp_name"]) == ("am_adam", "Adam")
        assert app.set_booth_voices(pbp_voice="bm_george") is True
        assert (app.booth["pbp_voice"], app.booth["pbp_name"]) == ("bm_george", "George")
        assert app.booth["analyst_voice"] == "am_onyx"
        saved = tomllib.loads(app.config_path.read_text(encoding="utf-8"))["broadcast"]
        assert saved == {"pbp_voice": "bm_george", "pbp_name": "George"}
        assert app.set_booth_voices(pbp_voice="bm_george") is False  # no change, nothing written
    finally:
        app.stop()


def test_custom_caster_names_survive_a_voice_change(tmp_path):
    app = _app(tmp_path, analyst_name="Marshall")
    try:
        app.set_booth_voices(analyst_voice="af_nova")
        assert (app.booth["analyst_voice"], app.booth["analyst_name"]) == ("af_nova", "Marshall")
    finally:
        app.stop()


def test_analyst_toggles_off_and_back_on_live(tmp_path):
    from arenaonair.models import Utterance
    app = _app(tmp_path)
    try:
        app.set_booth_voices(pbp_voice="bm_george", analyst_voice="af_nova")
        reply = Utterance("r", "m", "cast", "Reaction", 1, 1.0, role="color_analyst")
        call = Utterance("c", "m", "cast", "Call", 1, 1.0)
        assert app._valid_for_delivery(reply)
        assert app.set_broadcast_mode("solo") is True
        assert app.status()["broadcast_mode"] == "solo" and app.booth["analyst_voice"] is None
        assert (app.booth["pbp_voice"], app.booth["pbp_name"]) == ("bm_george", "George")
        assert not app._valid_for_delivery(reply) and app._valid_for_delivery(call)
        assert app.preview_voices() == 1
        assert app.set_broadcast_mode("dual") is True
        assert (app.booth["analyst_voice"], app.booth["analyst_name"]) == ("af_nova", "Nova")
        assert app.booth["pbp_voice"] == "bm_george" and app._valid_for_delivery(reply)
        assert app.set_broadcast_mode("dual") is False
        assert tomllib.loads(app.config_path.read_text(encoding="utf-8"))["broadcast"]["mode"] == "dual"
        assert load(app.config_path).broadcast_mode == "dual"
    finally:
        app.stop()


def test_booth_says_hello_between_games_only(tmp_path):
    app = _app(tmp_path)
    try:
        assert app.preview_voices() == 2
        hello, reply = app.queue.pending()
        assert (hello.voice, hello.role) == ("am_adam", "play_by_play") and "Adam" in hello.text
        assert (reply.voice, reply.role, reply.anchor_uid) == ("am_onyx", "color_analyst", hello.uid)
        app.recorder._game = {"closed": False}
        assert app.preview_voices() == 0
    finally:
        app.stop()


def _window(app):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from arenaonair.diagnostics import BugReportLogs
    from arenaonair.ui import BroadcastWindow
    QApplication.instance() or QApplication([])
    return BroadcastWindow(app, BugReportLogs())


def _stub(mode="dual"):
    calls = []
    state = {"mode": mode}
    booth = {"pbp_voice": "am_adam", "analyst_voice": "am_onyx" if mode == "dual" else None,
             "analyst_name": "Onyx" if mode == "dual" else None}

    def set_mode(new):
        calls.append({"mode": new})
        state["mode"] = new
        booth.update(analyst_voice="am_onyx" if new == "dual" else None, analyst_name="Onyx")
        return True
    app = SimpleNamespace(
        booth=booth, queue=SpeechQueue(), speaker=None,
        status=lambda: {"state": "watching", "broadcast_mode": state["mode"], "queued": 0},
        set_booth_voices=lambda **kw: calls.append(kw) or True, set_broadcast_mode=set_mode,
        preview_voices=lambda: 2)
    return app, calls


def test_window_voice_picker_lists_voices_and_applies_a_pick(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, calls = _stub()
    window = _window(app)
    try:
        pbp, analyst = window.voice_boxes["pbp_voice"], window.voice_boxes["analyst_voice"]
        assert (pbp.currentData(), analyst.currentData()) == ("am_adam", "am_onyx")
        assert pbp.currentText().startswith("Adam · American male")
        pbp.setCurrentIndex(pbp.findData("bf_emma"))
        pbp.activated.emit(pbp.currentIndex())
        assert calls == [{"pbp_voice": "bf_emma"}]
        assert "Emma" in window.voice_note.text()
        window.preview_button.click()
        assert "hello" in window.voice_note.text()
    finally:
        window.timer.stop()
        window.close()


def test_window_toggles_between_two_casters_and_solo(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, calls = _stub("solo")
    window = _window(app)
    try:
        analyst = window.voice_boxes["analyst_voice"]
        assert window.mode_buttons["solo"].isChecked()
        assert not analyst.isEnabled() and analyst.currentIndex() == -1
        window.mode_buttons["dual"].click()
        assert calls == [{"mode": "dual"}] and window.mode_buttons["dual"].isChecked()
        assert analyst.isEnabled() and analyst.currentData() == "am_onyx"
        assert "Onyx" in window.voice_note.text()
        window.mode_buttons["solo"].click()
        assert calls[-1] == {"mode": "solo"}
        assert not analyst.isEnabled() and analyst.currentData() == "am_onyx"  # kept for the way back
    finally:
        window.timer.stop()
        window.close()


def test_packaged_icon_is_a_1024_png():
    head = desktop.ICON_PNG.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", head[16:24]) == (1024, 1024)


def test_macos_launcher_execs_python_with_the_window():
    script = desktop.macos_launcher("/opt/venv with space/bin/python", PurePosixPath("/src dir"))
    assert "PYTHON='/opt/venv with space/bin/python'" in script
    assert "SOURCE='/src dir'" in script
    assert 'exec "$PYTHON" -m arenaonair.app --ui' in script
    plist = desktop.macos_info_plist()
    assert plist["CFBundleExecutable"] == desktop.APP_NAME and plist["CFBundleIconFile"] == "AppIcon"


@pytest.mark.skipif(sys.platform != "darwin", reason="builds a macOS app bundle")
def test_install_macos_builds_a_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "LSREGISTER", shutil.which("true"))  # keep temp bundles out of Launch Services
    app = desktop.install_macos(tmp_path, "/usr/bin/python3", None)
    assert plistlib.loads((app / "Contents" / "Info.plist").read_bytes())["CFBundleIdentifier"] == desktop.BUNDLE_ID
    assert (app / "Contents" / "Resources" / "AppIcon.icns").read_bytes()[:4] == b"icns"
    assert (app / "Contents" / "MacOS" / desktop.APP_NAME).stat().st_mode & 0o111
    desktop.install_macos(tmp_path, "/usr/bin/python3", None)  # reinstall replaces its own bundle
    foreign = tmp_path / "elsewhere"
    (foreign / f"{desktop.APP_NAME}.app").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        desktop.install_macos(foreign, "/usr/bin/python3", None)


def test_linux_entry_quotes_paths():
    entry = desktop.linux_desktop_entry("/home/me/my venv/bin/python", PurePosixPath("/src"),
                                        PurePosixPath("/icons/icon.png"))
    assert 'Exec=env PYTHONPATH=/src "/home/me/my venv/bin/python" -m arenaonair.app --ui' in entry
    assert "Icon=/icons/icon.png" in entry and "Terminal=false" in entry


def test_windows_icon_packs_png_entries():
    pytest.importorskip("PySide6")
    data = desktop.ico_bytes(desktop.ICON_PNG, sizes=(16, 256))
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 2)
    width, height, *_rest, size, offset = struct.unpack("<BBBBHHII", data[22:38])
    assert (width, height) == (0, 0)  # 0 means 256 in an .ico directory
    assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + size == len(data)
