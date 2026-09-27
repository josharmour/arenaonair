"""Desktop window: broadcast status, the booth (casters, focus, coaching, voices, speed),
every other setting, and copyable bug reports."""
import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QRadioButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .config import Config
from .diagnostics import build_bug_report
from .platform.tts import KOKORO_VOICES
from .settings import BY_KEY, GROUPS, SETTINGS

ICON_PATH = Path(__file__).resolve().parent / 'assets' / 'icon.png'
#: Commentary focus choices: (setting, label, tooltip, confirmation).
FOCUS_CHOICES = (
    ('calls', 'Mostly calls', 'Follows every play; a read on the game when its shape changes.',
     'Mostly calls: the booth follows every play.'),
    ('balanced', 'Balanced', 'Short calls on routine plays; the analyst checks in on the game every round.',
     'Balanced: short calls, with a read on the game every round.'),
    ('analysis', 'Mostly analysis', 'Few routine calls; a read on how the game is going every turn.',
     'Mostly analysis: fewer calls, a read on the game every turn.'),
)
_ACCENTS = {'af': 'American female', 'am': 'American male', 'bf': 'British female', 'bm': 'British male'}
_NOT_SAVED = ' (Only for this session: the choice could not be saved.)'
#: Marks settings that take effect on the next launch.
RESTART_MARK = ' ↻'


def voice_label(voice_id, description=''):
    """'am_adam' -> 'Adam · American male — crisp sports shoutcaster'."""
    prefix, _, name = voice_id.partition('_')
    label = f'{name.capitalize()} · {_ACCENTS[prefix]}' if name and prefix in _ACCENTS else voice_id
    note = description.partition('(')[2].rstrip(')').removeprefix('Default ').strip()
    return f'{label} — {note[:1].lower() + note[1:]}' if note else label


def _neural_voices(app):
    """Whether the booth speaks with Kokoro, whose voices the picker lists."""
    speaker = getattr(app, 'speaker', None)
    engines = getattr(speaker, 'engines', None) or [getattr(speaker, 'engine', None)]
    names = {getattr(e, 'name', None) for e in engines} - {None}
    return not names or 'kokoro' in names


class BroadcastWindow(QWidget):
    def __init__(self, app, logs):
        super().__init__()
        self.app = app
        self.logs = logs
        self.restart_requested = False
        self.setWindowTitle('ArenaOnAir')
        self.resize(820, 720)
        outer = QVBoxLayout(self)
        self.restart_bar = QWidget()
        bar = QHBoxLayout(self.restart_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.restart_label = QLabel('Some saved settings apply after a restart.')
        self.restart_label.setStyleSheet('color: #b45309; font-weight: 600')
        bar.addWidget(self.restart_label, 1)
        self.restart_button = QPushButton('Restart now')
        self.restart_button.clicked.connect(self.request_restart)
        bar.addWidget(self.restart_button)
        self.restart_bar.hide()
        outer.addWidget(self.restart_bar)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        broadcast = QWidget()
        layout = QVBoxLayout(broadcast)
        self.status_label = QLabel('Starting broadcast…')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.warning_label = QLabel('')
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet('color: #b45309; font-weight: 600')
        self.warning_label.hide()
        layout.addWidget(self.warning_label)
        self.extras_label = QLabel('')
        self.extras_label.setWordWrap(True)
        self.extras_label.setTextInteractionFlags(self.extras_label.textInteractionFlags() | Qt.TextSelectableByMouse)
        layout.addWidget(self.extras_label)
        layout.addWidget(self._voice_picker())
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText('Recent commentary and app logs will appear here.')
        self.log_view.setMaximumBlockCount(2000)
        layout.addWidget(self.log_view)
        self.recap_button = QPushButton('Speak last recap')
        self.recap_button.clicked.connect(self.speak_recap)
        layout.addWidget(self.recap_button)
        self.copy_button = QPushButton('Copy bug report')
        self.copy_button.setObjectName('copy_bug_report')
        self.copy_button.clicked.connect(self.copy_bug_report)
        layout.addWidget(self.copy_button)
        self.feedback = QLabel('Copy recent commentary and diagnostics to paste into your chat.')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self.tabs.addTab(broadcast, 'Broadcast')
        self.tabs.addTab(self._settings_tab(), 'Settings')
        self._last_text = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.refresh()

    # -- booth: casters, focus, coaching, voices -------------------------------

    def _voice_picker(self):
        group = QGroupBox('Booth')
        form = QFormLayout(group)
        booth = getattr(self.app, 'booth', {}) or {}
        self.neural = _neural_voices(self.app)
        modes = QHBoxLayout()
        self.mode_buttons = {'dual': QRadioButton('Two casters'), 'solo': QRadioButton('Play-by-play only')}
        self.mode_buttons['dual'].setToolTip('Play-by-play calls the action; the color analyst adds context.')
        self.mode_buttons['solo'].setToolTip('One voice calls the action, with no analyst replies.')
        self._mode_group = QButtonGroup(group)
        for mode, button in self.mode_buttons.items():
            button.clicked.connect(lambda _checked=False, mode=mode: self.pick_mode(mode))
            self._mode_group.addButton(button)
            modes.addWidget(button)
        modes.addStretch(1)
        form.addRow('Casters', modes)
        focus_row = QHBoxLayout()
        self.focus_buttons = {}
        self._focus_group = QButtonGroup(group)
        for focus, label, tip, _note in FOCUS_CHOICES:
            button = QRadioButton(label)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, focus=focus: self.pick_focus(focus))
            self._focus_group.addButton(button)
            focus_row.addWidget(button)
            self.focus_buttons[focus] = button
        focus_row.addStretch(1)
        form.addRow('Focus', focus_row)
        self.coaching_box = QCheckBox('Suggest plays for me')
        self.coaching_box.setToolTip('The AI booth may suggest attacks, blocks and lines, '
                                     'using only what you can see.')
        self.coaching_box.clicked.connect(self.pick_coaching)
        form.addRow('Coaching', self.coaching_box)
        self.voice_boxes = {}
        for key, label in (('pbp_voice', 'Play-by-play'), ('analyst_voice', 'Color analyst')):
            box = QComboBox()
            box.setObjectName(key)
            self._fill_voices(box, booth.get(key), 'Voice engine default' if key == 'pbp_voice' else None)
            box.activated.connect(lambda _index, key=key, box=box: self.pick_voice(key, box.currentData()))
            form.addRow(label, box)
            self.voice_boxes[key] = box
        spec = BY_KEY['speech_speed']
        self.speed_box = QDoubleSpinBox()
        low, high, step = spec.bounds
        self.speed_box.setRange(low, high)
        self.speed_box.setSingleStep(step)
        self.speed_box.setDecimals(1)
        self.speed_box.setSuffix('×')
        self.speed_box.setKeyboardTracking(False)  # commit on Enter, focus change or arrows
        self.speed_box.setToolTip(spec.help)
        self.speed_box.valueChanged.connect(self.pick_speed)
        form.addRow(spec.label, self.speed_box)
        row = QHBoxLayout()
        self.voice_note = QLabel('New lines use the voice you pick; it is saved for next time.' if self.neural else
                                 'The booth is using your system voice. Install the tts extra to choose voices.')
        self.voice_note.setWordWrap(True)
        row.addWidget(self.voice_note, 1)
        self.preview_button = QPushButton('Hear the booth')
        self.preview_button.setToolTip('Each caster says hello in their voice (between games).')
        self.preview_button.clicked.connect(self.preview_voices)
        row.addWidget(self.preview_button)
        form.addRow(row)
        self._sync_booth()
        return group

    @staticmethod
    def _fill_voices(box, current, default=None):
        """List the Kokoro voices; an unset voice shows ``default`` (or nothing)."""
        if (current and current not in KOKORO_VOICES) or (not current and default):
            box.addItem(current or default, current or '')
            box.insertSeparator(box.count())
        accent = None
        for voice_id, description in KOKORO_VOICES.items():
            prefix = voice_id.partition('_')[0]
            if accent not in (None, prefix):
                box.insertSeparator(box.count())
            accent = prefix
            box.addItem(voice_label(voice_id, description), voice_id)
        box.setCurrentIndex(box.findData(current or ''))

    def _sync_booth(self):
        """Show the booth's mode, focus, coaching and voices; solo keeps the analyst's last voice, greyed out."""
        booth = getattr(self.app, 'booth', {}) or {}
        status = self.app.status()
        dual = status.get('broadcast_mode') == 'dual'
        self.mode_buttons['dual' if dual else 'solo'].setChecked(True)
        self.focus_buttons.get(status.get('focus'), self.focus_buttons['balanced']).setChecked(True)
        self.coaching_box.setChecked(bool(status.get('coaching')))
        ai_booth = status.get('narration_mode') == 'llm' and hasattr(self.app, 'apply_setting')
        self.coaching_box.setEnabled(ai_booth)
        if not ai_booth:
            self.coaching_box.setToolTip('Coaching needs the AI booth (Settings > Commentary > Writer).')
        for key, box in self.voice_boxes.items():
            index = box.findData(booth.get(key) or '')
            if index >= 0:
                box.setCurrentIndex(index)
            box.setEnabled(self.neural and (dual or key == 'pbp_voice'))
        self.speed_box.blockSignals(True)
        self.speed_box.setValue(self._speech_speed())
        self.speed_box.blockSignals(False)
        self.speed_box.setEnabled(hasattr(self.app, 'apply_setting'))

    def _speech_speed(self):
        return getattr(getattr(self.app, 'config', None), 'speech_speed', Config.speech_speed)

    def pick_mode(self, mode):
        if mode == self.app.status().get('broadcast_mode'):
            return
        try:
            saved = self.app.set_broadcast_mode(mode)
        except Exception:
            self._sync_booth()
            self.voice_note.setText('Could not change the booth.')
            return
        self._sync_booth()
        self.refresh()
        analyst = (getattr(self.app, 'booth', {}) or {}).get('analyst_name') or 'The color analyst'
        text = f'{analyst} is back in the booth.' if mode == 'dual' else 'Play-by-play only: the analyst is off the air.'
        self.voice_note.setText(text + ('' if saved else _NOT_SAVED))

    def pick_focus(self, focus):
        if focus == self.app.status().get('focus'):
            return
        try:
            saved = self.app.set_commentary_focus(focus)
        except Exception:
            self._sync_booth()
            self.voice_note.setText('Could not change the focus.')
            return
        self._sync_booth()
        note = next(n for f, _l, _t, n in FOCUS_CHOICES if f == focus)
        self.voice_note.setText(note + ('' if saved else _NOT_SAVED))

    def pick_coaching(self, on):
        try:
            saved = self.app.apply_setting('coaching', bool(on))['saved']
        except Exception:
            self._sync_booth()
            self.voice_note.setText('Could not change coaching.')
            return
        self._sync_booth()
        text = ('Coaching on: the booth may suggest plays, using only what you can see.' if on
                else 'Coaching off: the booth describes the game without suggestions.')
        self.voice_note.setText(text + ('' if saved else _NOT_SAVED))

    def pick_speed(self, speed):
        speed = round(speed, 1)
        if speed == round(self._speech_speed(), 1):
            return
        try:
            saved = self.app.apply_setting('speech_speed', speed)['saved']
        except Exception:
            self._sync_booth()
            self.voice_note.setText('Could not change the speed.')
            return
        self._sync_booth()
        self.voice_note.setText(f'The casters talk at {speed:g}× from the next line.' + ('' if saved else _NOT_SAVED))

    def pick_voice(self, key, voice):
        if not voice or voice == (getattr(self.app, 'booth', {}) or {}).get(key):
            return
        try:
            saved = self.app.set_booth_voices(**{key: voice})
        except Exception:
            self.voice_note.setText('Could not switch voices.')
            return
        name = voice_label(voice).split(' · ')[0]
        self.voice_note.setText(f'{name} is on the mic from the next line.' + ('' if saved else _NOT_SAVED))

    def preview_voices(self):
        try:
            count = self.app.preview_voices()
        except Exception:
            self.voice_note.setText('Could not play the booth voices.')
            return
        self.voice_note.setText('Listen: the booth is saying hello.' if count else
                                'The booth can say hello between games, not during one.')

    # -- settings ------------------------------------------------------------------

    def _settings_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        self.setting_widgets = {}
        config = getattr(self.app, 'config', None)
        if config is None or not hasattr(self.app, 'apply_setting'):
            outer.addWidget(QLabel('Settings are unavailable in this mode.'))
            outer.addStretch(1)
            return page
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        column = QVBoxLayout(body)
        for group in GROUPS:
            if group == 'Booth':  # on the Broadcast tab
                continue
            box = QGroupBox(group)
            form = QFormLayout(box)
            for spec in (s for s in SETTINGS if s.group == group):
                widget = self._setting_widget(spec, getattr(config, spec.key, None))
                label = QLabel(spec.label + (RESTART_MARK if spec.restart else ''))
                if spec.help:
                    label.setToolTip(spec.help)
                    widget.setToolTip(spec.help)
                form.addRow(label, widget)
            column.addWidget(box)
        column.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.settings_note = QLabel(f'Changes save to your config file.{RESTART_MARK} marks settings that apply '
                                    'after a restart. Launch flags override saved settings.')
        self.settings_note.setWordWrap(True)
        outer.addWidget(self.settings_note)
        return page

    def _setting_widget(self, spec, current):
        """One control per setting; each commits through ``apply_setting``."""
        if spec.kind == 'choice':
            widget = QComboBox()
            values = [value for value, _label in spec.choices]
            for _value, label in spec.choices:
                widget.addItem(label)
            if current not in values:
                values.append(current)
                widget.addItem(str(current))
            widget.setCurrentIndex(values.index(current))
            state = {'value': current}

            def revert():
                widget.setCurrentIndex(values.index(state['value']))
            widget.activated.connect(lambda i: self._commit(spec, values[i], state, revert))
        elif spec.kind == 'bool':
            widget = QCheckBox()
            widget.setChecked(bool(current))
            state = {'value': bool(current)}
            widget.clicked.connect(lambda on: self._commit(spec, bool(on), state, lambda: widget.setChecked(state['value'])))
        elif spec.kind in ('int', 'float'):
            widget = QSpinBox() if spec.kind == 'int' else QDoubleSpinBox()
            low, high, step = spec.bounds
            if spec.kind == 'float':
                widget.setDecimals(2)
            widget.setRange(low, high)
            widget.setSingleStep(step)
            widget.setKeyboardTracking(False)  # commit on Enter, focus change or arrows
            widget.setValue(current if current is not None else low)
            state = {'value': widget.value()}
            widget.valueChanged.connect(
                lambda v: self._commit(spec, v, state, lambda: widget.setValue(state['value'])))
        else:
            edit = QLineEdit('' if current is None else str(current))
            if spec.help.startswith('Blank:'):
                edit.setPlaceholderText(spec.help)
            if spec.kind == 'secret':
                edit.setEchoMode(QLineEdit.Password)
            state = {'value': edit.text()}

            def commit_text():
                if edit.text().strip() != state['value']:
                    self._commit(spec, edit.text().strip(), state, lambda: edit.setText(state['value']))
            edit.editingFinished.connect(commit_text)
            widget = edit
            if spec.kind == 'path':
                widget = QWidget()
                row = QHBoxLayout(widget)
                row.setContentsMargins(0, 0, 0, 0)
                row.addWidget(edit, 1)
                browse = QPushButton('Browse…')

                def pick():
                    if spec.key == 'data_dir':
                        chosen = QFileDialog.getExistingDirectory(self, spec.label, edit.text())
                    else:
                        chosen = QFileDialog.getOpenFileName(self, spec.label, edit.text())[0]
                    if chosen:
                        edit.setText(chosen)
                        commit_text()
                browse.clicked.connect(pick)
                row.addWidget(browse)
            self.setting_widgets[spec.key] = edit
            return widget
        self.setting_widgets[spec.key] = widget
        return widget

    def _commit(self, spec, value, state, revert):
        if value == state['value']:
            return
        try:
            result = self.app.apply_setting(spec.key, value)
        except ValueError as exc:
            self.settings_note.setText(str(exc))
            revert()
            return
        except Exception:
            self.settings_note.setText(f'Could not change {spec.label}.')
            revert()
            return
        state['value'] = value
        if not result['saved']:
            text = f'{spec.label}: changed for this session only; it could not be saved.'
        elif spec.restart:
            text = f'{spec.label}: saved. It applies after a restart.'
        else:
            text = f'{spec.label}: saved and applied.'
        self.settings_note.setText(text)
        if spec.key in ('booth_preset', 'pbp_name', 'analyst_name'):
            self._sync_booth()
        self.refresh()

    def request_restart(self):
        """Close and relaunch with the saved settings (the caller does the relaunch)."""
        self.restart_requested = True
        QApplication.instance().quit()

    # -- status --------------------------------------------------------------------

    def refresh(self):
        status = self.app.status()
        mode = 'Dual booth' if status['broadcast_mode'] == 'dual' else 'Solo booth'
        state = str(status['state']).replace('_', ' ').lower()
        model = status.get('model', {})
        latency = model.get('request_latency_s')
        state_name = model.get('state', 'legacy')
        if state_name == 'legacy':
            described = 'scripted booth'
        else:
            from .llm_booth import describe_state
            described = describe_state(state_name)
        suffix = f" · {described}" + (f" · model {latency:.1f}s" if latency is not None else "")
        persona = f" · {status['persona']}" if status.get('persona') else ''
        self.status_label.setText(f'{mode}{persona} · {state} · {status["queued"]} queued{suffix}')
        warnings = status.get('warnings') or []
        self.warning_label.setText('\n'.join(warnings))
        self.warning_label.setVisible(bool(warnings))
        extras = []
        if status.get('overlay'):
            extras.append(f"OBS overlay: {status['overlay']}")
        extras.append('Hole cards: on air' if status.get('hole_cards') else 'Hole cards: off air')
        if status.get('last_recap'):
            extras.append(f"Last recap: {status['last_recap']}")
        self.extras_label.setText(' · '.join(extras))
        self.recap_button.setEnabled(bool(status.get('last_recap')))
        self.restart_bar.setVisible(bool(status.get('restart_needed')))
        text = self.logs.text()
        if text != self._last_text:
            bar = self.log_view.verticalScrollBar()
            at_bottom = bar.value() == bar.maximum()
            old_scroll = bar.value()
            self.log_view.setPlainText(text)
            bar.setValue(bar.maximum() if at_bottom else old_scroll)
            self._last_text = text

    def speak_recap(self):
        """Queue the latest recap in the booth voices (between matches)."""
        try:
            count = self.app.speak_last_recap()
            self.feedback.setText(f'Recap queued ({count} lines).' if count else 'No recap to speak yet.')
        except Exception:
            self.feedback.setText('Could not queue the recap.')

    def copy_bug_report(self):
        try:
            report = build_bug_report(self.app, self.logs)
            QApplication.clipboard().setText(report)
            self.feedback.setText('Copied — paste it into your chat.')
        except Exception:
            self.feedback.setText('Could not copy the report. Try again.')


def run_dashboard(app, logs):
    """Show the window until it closes; True means relaunch to apply saved settings."""
    if sys.platform == 'win32':
        try:  # group the taskbar button under ArenaOnAir's icon, not Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ArenaOnAir.Broadcast')
        except Exception:
            pass
    qt_app = QApplication.instance() or QApplication([])
    qt_app.setApplicationName('ArenaOnAir')
    qt_app.setDesktopFileName('arenaonair')  # Linux: matches the install-app menu entry
    # From the macOS app bundle the Dock already shows the bundle's .icns.
    if not (sys.platform == 'darwin' and os.environ.get('ARENAONAIR_APP_BUNDLE')):
        qt_app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = BroadcastWindow(app, logs)
    old_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: qt_app.quit())
    window.show()
    try:
        qt_app.exec()
    finally:
        signal.signal(signal.SIGINT, old_handler)
        window.close()
    return window.restart_requested
