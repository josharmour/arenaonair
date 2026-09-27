"""Bug report contents and the real Qt clipboard-button path."""
import logging
import os
from types import SimpleNamespace

import pytest

from arenaonair.config import Config
from arenaonair.diagnostics import BugReportLogs, build_bug_report
from arenaonair.speech import SpeechQueue


def app_stub():
    return SimpleNamespace(
        config=Config(broadcast_mode='dual', relay_secret='test-secret'),
        booth={'pbp_voice': 'am_adam', 'analyst_voice': 'am_onyx', 'preset': 'sports_desk'},
        status=lambda: {'state': 'in_match', 'broadcast_mode': 'dual', 'match_id': 'match-1',
                        'queued': 0, 'last_utterance': 'DO NOT COPY PRIVATE HAND',
                        'relay_secret': 'test-secret'},
        queue=SpeechQueue(), speaker=None, carddb=None,
        _diagnostic_game={'stage': 'GameStage_Play', 'turn': 4},
    )


def record(text, **extra):
    rec = logging.LogRecord('arenaonair.speech', logging.INFO, __file__, 1, text, (), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_report_copies_public_commentary_and_useful_status_only():
    logs = BugReportLogs(secrets=('test-secret',))
    logs.emit(record('Speaking color_analyst [am_onyx]: Public reaction'))
    logs.emit(record('secret=test-secret token=other-token Authorization: Bearer abcdef'))
    logs.emit(record('Hidden Counterspell', game_event_kind='trap_armed'))
    logs.emit(record('speech delivery FAILED kind=hand_online text=Hidden Dragon'))
    logs.emit(record('{"gameObjects": [{"name": "Hidden card"}]}'))
    report = build_bug_report(app_stub(), logs)
    assert 'Public reaction' in report and 'am_onyx' in report
    assert 'GameStage_Play' in report and 'match-1' in report
    for private in ('test-secret', 'other-token', 'abcdef', 'Counterspell', 'Hidden Dragon', 'Hidden card', 'DO NOT COPY PRIVATE HAND'):
        assert private not in report
    assert '[private-game commentary omitted]' in report
    assert '[raw game data omitted]' in report


def test_log_buffer_is_bounded_and_redacts_paths_and_urls():
    from pathlib import Path
    logs = BugReportLogs(max_lines=2)
    logs.emit(record('first'))
    logs.emit(record(str(Path.home() / 'example.log')))
    logs.emit(record('https://example.com/model?signature=sensitive'))
    text = logs.text()
    assert 'first' not in text
    assert str(Path.home()) not in text
    assert 'sensitive' not in text
    assert '?[query omitted]' in text


def test_copy_button_puts_report_on_clipboard_without_stopping_broadcast(monkeypatch):
    # Offscreen plugin keeps this check independent of the user's pasteboard.
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    from arenaonair.ui import BroadcastWindow
    qt = QApplication.instance() or QApplication([])
    logs = BugReportLogs()
    logs.emit(record('Speaking play_by_play [am_adam]: Grim Tutor.'))
    app = app_stub()
    window = BroadcastWindow(app, logs)
    try:
        window.copy_button.click()
        text = qt.clipboard().text()
        assert text.startswith('ArenaOnAir bug report')
        assert 'Grim Tutor' in text and 'GameStage_Play' in text
        assert window.feedback.text().startswith('Copied')
        assert app.status()['state'] == 'in_match'
        logs.emit(record('Later commentary'))
        window.refresh()
        window.copy_button.click()
        assert 'Later commentary' in qt.clipboard().text()
    finally:
        window.timer.stop()
        window.close()


def test_ui_and_no_ui_flags_are_explicit():
    from arenaonair.app import _build_arg_parser
    parser = _build_arg_parser()
    assert parser.parse_args([]).ui is None
    assert parser.parse_args(['--ui']).ui is True
    assert parser.parse_args(['--no-ui']).ui is False
    with pytest.raises(SystemExit):
        parser.parse_args(['--ui', '--no-ui'])


def test_cli_ui_wires_report_buffer_and_cleans_up(monkeypatch):
    import arenaonair.app as module
    pytest.importorskip('PySide6')
    import arenaonair.ui as ui
    called = []
    fake = app_stub()
    fake.start = lambda: called.append('start')
    fake.stop = lambda: called.append('stop')
    monkeypatch.setattr(module, 'ArenaOnAirApp', lambda *a, **kw: fake)
    def show(app, logs):
        called.append('window')
        assert logs in logging.getLogger().handlers
        assert 'ArenaOnAir bug report' in build_bug_report(app, logs)
    monkeypatch.setattr(ui, 'run_dashboard', show)
    before = list(logging.getLogger().handlers)
    assert module.main(['--ui', '--dry-run']) == 0
    assert called == ['start', 'window', 'stop']
    assert logging.getLogger().handlers == before
