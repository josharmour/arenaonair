"""Every setting in the window: saved to the right TOML table, applied live or on restart, coaching."""
import tomllib
from types import SimpleNamespace

import pytest

from arenaonair import config as config_mod
from arenaonair.app import ArenaOnAirApp
from arenaonair.config import Config, load, save_settings
from arenaonair.llm_booth import JsonClient, ModelError, validate_output
from arenaonair.settings import BY_KEY, SETTINGS

SAMPLES = {"text": "Marshall", "path": "some/file.log", "secret": "hunter2"}
SPECIAL = {"llm_base_url": "https://example.invalid/v1", "relay_bind": "0.0.0.0:9000", "overlay_port": 8787,
           "window": 12, "tts_platform": "linux"}


def sample(spec):
    """A valid value for the setting that differs from its default."""
    if spec.key in SPECIAL:
        return SPECIAL[spec.key]
    default = config_mod.default(spec.key)
    if spec.kind == "choice":
        return next(v for v, _ in reversed(spec.choices) if v is not None and v != default)
    if spec.kind == "bool":
        return not default
    if spec.kind in ("int", "float"):
        low, high, step = spec.bounds
        value = low + step * 3
        return int(value) if spec.kind == "int" else round(float(value), 2)
    return SAMPLES[spec.kind]


@pytest.mark.parametrize("spec", SETTINGS, ids=lambda s: s.key)
def test_each_setting_round_trips_through_config_toml(spec, tmp_path):
    path = tmp_path / "config.toml"
    value = sample(spec)
    save_settings(path, {spec.section: {spec.name: value}})
    assert getattr(load(path), spec.key) == value


def test_settings_writer_keeps_the_file_and_types(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# mine\nnarration_mode = "llm"\n\n[llm]\nmodel = "glm-5.3-flash" # pinned\n', encoding="utf-8")
    save_settings(path, {None: {"verbosity": "quiet"}, "stream": {"enabled": True, "delay_s": 45.0},
                         "broadcast": {"analyst_lines_per_turn": 3}})
    save_settings(path, {None: {"narration_mode": None}})  # None removes: the default applies again
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# mine\n") and "# pinned" in text
    assert text.index('verbosity = "quiet"') < text.index("[llm]")  # top-level keys stay above tables
    data = tomllib.loads(text)
    assert "narration_mode" not in data and data["stream"] == {"enabled": True, "delay_s": 45.0}
    cfg = load(path)
    assert (cfg.verbosity, cfg.stream_enabled, cfg.stream_delay_s, cfg.analyst_lines_per_turn) == ("quiet", True, 45.0, 3)


def _app(tmp_path, **cfg):
    app = ArenaOnAirApp(Config(carddb_path=str(tmp_path / "none"), **cfg), dry_run=True)
    app.config_path = tmp_path / "config.toml"
    return app


def test_live_settings_apply_now_and_save(tmp_path):
    app = _app(tmp_path)
    try:
        assert app.apply_setting("verbosity", "quiet") == {"saved": True, "restart": False}
        assert app.config.verbosity == "quiet"
        app.apply_setting("persona", "esports")
        assert app.booth["style"].startswith("High-energy") and app.status()["persona"] == "esports"
        app.apply_setting("pbp_name", "Marshall")
        assert app.booth["pbp_name"] == "Marshall"
        app.apply_setting("pbp_name", "")
        assert app.booth["pbp_name"] == "Adam"  # blank: named after the voice again
        app.apply_setting("analyst_lines_per_turn", 0)
        assert app.analyst.lines_per_turn == 0
        app.apply_setting("stream_enabled", True)
        assert app.status()["hole_cards"] is False  # streaming without enough delay
        app.apply_setting("stream_delay_s", 45)
        assert app.status()["hole_cards"] is True
        with pytest.raises(ValueError, match="1024"):
            app.apply_setting("overlay_port", 80)
        saved = load(app.config_path)
        assert (saved.verbosity, saved.persona, saved.stream_delay_s) == ("quiet", "esports", 45.0)
        relaunched = config_mod.resolve_booth(saved)  # the style changed, the voices you hear didn't
        assert (relaunched["pbp_voice"], relaunched["pbp_name"], relaunched["analyst_voice"]) == (
            "am_adam", "Adam", "am_onyx")
    finally:
        app.stop()


def test_voice_pair_sets_both_casters(tmp_path):
    app = _app(tmp_path)
    try:
        app.set_booth_voices(pbp_voice="bf_emma")
        app.apply_setting("booth_preset", "premier_pro_tour")
        assert (app.booth["pbp_voice"], app.booth["pbp_name"]) == ("am_michael", "Michael")
        assert (app.booth["analyst_voice"], app.booth["analyst_name"]) == ("bm_george", "George")
        broadcast = tomllib.loads(app.config_path.read_text(encoding="utf-8"))["broadcast"]
        assert broadcast == {"preset": "premier_pro_tour"}  # the earlier explicit voice gave way
        assert config_mod.resolve_booth(load(app.config_path))["pbp_voice"] == "am_michael"
    finally:
        app.stop()


def test_restart_settings_wait_and_log_sources_must_agree(tmp_path):
    app = _app(tmp_path)
    try:
        assert app.apply_setting("log_path", "arena/Player.log") == {"saved": True, "restart": True}
        assert app.config.log_path is None and app.status()["restart_needed"] == ["log_path"]
        with pytest.raises(ValueError, match="one log source"):
            app.apply_setting("log_player1", "p1.log")
        assert load(app.config_path).log_player1 is None
        app.apply_setting("log_path", "")
        assert app.restart_needed() == []  # back to what this launch runs with
        app.apply_setting("log_player1", "p1.log")
        assert app.restart_needed() == ["log_player1"]
    finally:
        app.stop()


def test_window_relaunches_after_a_clean_shutdown(monkeypatch):
    import arenaonair.app as module
    pytest.importorskip("PySide6")
    import arenaonair.ui as ui
    calls = []
    fake = SimpleNamespace(start=lambda: calls.append("start"), stop=lambda: calls.append("stop"), llm=None,
                           config_path=None)
    monkeypatch.setattr(module, "ArenaOnAirApp", lambda *a, **kw: fake)
    monkeypatch.setattr(ui, "run_dashboard", lambda app, logs: True)
    monkeypatch.setattr(module.os, "execv", lambda exe, argv: calls.append(("exec", exe, argv[1:])))
    monkeypatch.setattr(module.sys, "orig_argv", ["python", "-m", "arenaonair.app", "--ui"])
    assert module.main(["--ui", "--dry-run"]) == 0
    assert calls == ["start", "stop", ("exec", module.sys.executable, ["-m", "arenaonair.app", "--ui"])]


# -- coaching -----------------------------------------------------------------------

def _context(coaching):
    context = {"facts": {"event:1:1": {"kind": "cast", "data": {"name": "Opt"}}}, "new_events": ["event:1:1"],
               "heard": [], "earlier_points": [], "booth": {}}
    if coaching:
        context["coaching"] = "on"
    return context


def test_coaching_lines_pass_only_when_coaching_is_on():
    line = {"turns": [{"role": "color_analyst", "text": "I'd be looking at holding Opt; you should keep mana up.",
                       "refs": ["event:1:1"]}]}
    with pytest.raises(ModelError, match="coaching"):
        validate_output(line, _context(False))
    assert validate_output(line, _context(True))


def test_coaching_reaches_the_writer_and_the_fact_check(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENAONAIR_API_KEY", "test-key")  # never sent: requests are intercepted
    app = _app(tmp_path, narration_mode="llm", llm_base_url="https://example.invalid/v1")
    try:
        app.apply_setting("coaching", True)
        assert load(app.config_path).coaching is True
        context = app.llm._context([])
        assert context["coaching"].startswith("Coaching is on")
        sent = []
        client = JsonClient(app.config)
        client._request = lambda system, payload: sent.append(payload) or {"checks": []}
        client.verify({**context, "facts": {}, "heard": [], "earlier_points": []}, [])
        assert sent[0]["coaching_allowed"] is True
        app.apply_setting("coaching", False)
        assert "coaching" not in app.llm._context([])
    finally:
        app.stop()


# -- the window --------------------------------------------------------------------

@pytest.fixture
def window_for(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from arenaonair.diagnostics import BugReportLogs
    from arenaonair.ui import BroadcastWindow
    QApplication.instance() or QApplication([])
    made = []

    def build(app):
        made.append(BroadcastWindow(app, BugReportLogs()))
        return made[-1]
    yield build
    for window in made:
        window.timer.stop()
        window.close()


def test_settings_tab_offers_every_setting_and_saves_changes(window_for, tmp_path):
    app = _app(tmp_path)
    try:
        window = window_for(app)
        booth_tab = {s.key for s in SETTINGS if s.group == "Booth"}
        assert set(window.setting_widgets) == {s.key for s in SETTINGS} - booth_tab
        verbosity = window.setting_widgets["verbosity"]
        verbosity.setCurrentIndex(0)
        verbosity.activated.emit(0)
        assert app.config.verbosity == "quiet" and load(app.config_path).verbosity == "quiet"
        assert "saved and applied" in window.settings_note.text()
        log_path = window.setting_widgets["log_path"]
        log_path.setText("arena/Player.log")
        log_path.editingFinished.emit()
        window.refresh()
        assert not window.restart_bar.isHidden() and "after a restart" in window.settings_note.text()
        port = window.setting_widgets["overlay_port"]
        port.setValue(80)  # rejected: reverts, and says why
        assert port.value() == 0 and "1024" in window.settings_note.text()
    finally:
        app.stop()


def test_coaching_checkbox_needs_the_ai_booth(window_for):
    state = {"coaching": False, "narration_mode": "legacy"}
    applied = []

    def apply(key, value):
        applied.append((key, value))
        state[key] = value
        return {"saved": True, "restart": False}
    app = SimpleNamespace(booth={"pbp_voice": "am_adam", "analyst_voice": "am_onyx"}, speaker=None, config=Config(),
                          apply_setting=apply,
                          status=lambda: {"state": "watching", "broadcast_mode": "dual", "queued": 0, **state})
    window = window_for(app)
    assert not window.coaching_box.isEnabled()
    state["narration_mode"] = "llm"
    window._sync_booth()
    window.coaching_box.click()
    assert applied == [("coaching", True)] and window.coaching_box.isChecked()
    assert window.voice_note.text().startswith("Coaching on")


def test_restart_button_asks_for_a_relaunch(window_for, tmp_path):
    app = _app(tmp_path)
    try:
        window = window_for(app)
        window.restart_button.click()
        assert window.restart_requested is True
    finally:
        app.stop()


def test_setting_specs_match_config_fields():
    fields = set(Config.__dataclass_fields__)
    assert {s.key for s in SETTINGS} <= fields and len(BY_KEY) == len(SETTINGS)


def test_late_play_calls_are_dropped_before_they_start(tmp_path, monkeypatch):
    from arenaonair.models import Utterance
    monkeypatch.setenv("ARENAONAIR_API_KEY", "test-key")  # never sent: nothing is generated here
    app = _app(tmp_path, narration_mode="llm", llm_base_url="https://example.invalid/v1")
    app.llm.now = lambda: 200.0
    app.llm.valid_for_delivery = lambda utt: True  # isolate the call deadline

    def line(role="play_by_play", age=15.0, salience=2):
        return Utterance("u", "m", "llm_commentary", "Force of Will, on the stack!", salience, 200.0 - age, role=role)
    try:
        assert app.config.llm_call_max_age == 10.0
        assert not app._valid_for_delivery(line())                    # 15 s after the play: too late to call
        assert app._valid_for_delivery(line(age=5.0))
        assert app._valid_for_delivery(line(role="color_analyst"))    # explanations keep the longer limit
        assert app._valid_for_delivery(line(salience=3))              # game-ending calls always play
        app.apply_setting("llm_call_max_age", 20)
        assert app._valid_for_delivery(line())
    finally:
        app.stop()


def test_speech_speed_defaults_to_1_6_and_applies_to_the_next_line(tmp_path):
    from arenaonair.models import Utterance
    app = _app(tmp_path)
    line = Utterance("u", "m", "cast", "Opt.", 1, 0.0, rate=1.05)  # the scripted booth's normal pace
    try:
        assert app._on_air(line).rate == pytest.approx(1.68)
        assert app._on_air(Utterance("u", "m", "cast", "Lethal!", 3, 0.0, rate=1.5)).rate == 2.0  # capped
        app.apply_setting("speech_speed", 1.0)
        assert app._on_air(line).rate == pytest.approx(1.05)
        assert tomllib.loads(app.config_path.read_text(encoding="utf-8"))["broadcast"]["speed"] == 1.0
    finally:
        app.stop()
    assert load(None, speech_speed=9).speech_speed == 2.0


def test_broadcast_tab_speed_control(window_for, tmp_path):
    app = _app(tmp_path)
    try:
        window = window_for(app)
        assert window.speed_box.value() == pytest.approx(1.6)
        window.speed_box.setValue(1.2)
        assert app.config.speech_speed == pytest.approx(1.2) and load(app.config_path).speech_speed == 1.2
        assert "1.2×" in window.voice_note.text()
    finally:
        app.stop()
