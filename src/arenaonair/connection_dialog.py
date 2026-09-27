"""Connection setup; discovery and network checks run outside the Qt thread."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

from . import connection as api


class ConnectionDialog(QDialog):
    def __init__(self, config, config_path, parent=None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.setWindowTitle("Connect the generative booth")
        self.setMinimumWidth(540)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        layout = QVBoxLayout(self)
        intro = QLabel("Original conversation between two casters, powered by a language model. "
                       "Try five matches on our server, subscribe through Patreon, or use your own provider.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        layout.addLayout(form)
        self.mode = QComboBox()
        self.mode.addItem("Five free matches · hosted GLM 5.3", "trial")
        self.mode.addItem("Premium · I have a Patreon key", "premium")
        self.mode.addItem("My own provider · local or remote", "custom")
        form.addRow("Connection", self.mode)
        self.custom = QWidget()
        fields = QFormLayout(self.custom)
        fields.setContentsMargins(0, 0, 0, 0)
        self.url = QLineEdit(config.llm_base_url or "http://127.0.0.1")
        self.port = QLineEdit()
        self.port.setPlaceholderText("Optional; for example 11434, 1234, 8000")
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(config.llm_model or api.MODEL)
        fields.addRow("Endpoint", self.url)
        fields.addRow("Port", self.port)
        fields.addRow("Model", self.model)
        buttons = QHBoxLayout()
        self.detect = QPushButton("Find local providers")
        self.detect.clicked.connect(lambda: self._run(api.discover_local, self._discovered))
        self.fetch = QPushButton("Load models")
        self.fetch.clicked.connect(self._load_models)
        buttons.addWidget(self.detect)
        buttons.addWidget(self.fetch)
        fields.addRow(buttons)
        self.providers = QComboBox()
        self.providers.hide()
        self.providers.activated.connect(self._pick_provider)
        fields.addRow(self.providers)
        form.addRow(self.custom)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("Paste key; leave blank if your local server needs none")
        self.key_label = QLabel("API key")
        form.addRow(self.key_label, self.key)
        self.privacy = QLabel("Hosted commentary sends selected game facts, card context and recent booth dialogue "
                             "to our server. Speech is synthesized on your computer after voice downloads. "
                             "A custom provider receives that context instead. Local-only use requires a local model.")
        self.privacy.setWordWrap(True)
        layout.addWidget(self.privacy)
        self.note = QLabel("No payment details needed for the trial. A match counts when its first model request succeeds. "
                           "Reconnecting and games within a best-of-three match share that match.")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        row = QHBoxLayout()
        subscribe = QPushButton("Subscribe / get my Patreon key")
        subscribe.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(api.SUBSCRIBE_URL)))
        row.addWidget(subscribe)
        self.connect_button = QPushButton("Connect and restart")
        self.connect_button.clicked.connect(self._connect)
        row.addWidget(self.connect_button)
        layout.addLayout(row)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        mode = "trial" if config.llm_base_url in ("", api.TRIAL_URL) else (
            "premium" if config.llm_base_url == api.HOSTED_URL else "custom")
        self.mode.setCurrentIndex(self.mode.findData(mode))
        # Retain an existing key only for its current provider; never send it during discovery.
        self._original_mode = mode
        self._original_url = config.llm_base_url
        self._original_key = ""
        if config.llm_key_file and mode != "trial":
            try:
                self._original_key = Path(config.llm_key_file).expanduser().read_text().strip()
                self.key.setText(self._original_key)
            except OSError:
                pass
        self._mode_changed()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)

    def _mode_changed(self):
        custom = self.mode.currentData() == "custom"
        self.custom.setVisible(custom)
        self.key.setVisible(self.mode.currentData() != "trial")
        self.key_label.setVisible(self.mode.currentData() != "trial")
        if hasattr(self, '_original_mode'):
            self.key.setText(self._original_key if self.mode.currentData() == self._original_mode else "")

    def _run(self, work, done):
        if self.future:
            return
        self.note.setText("Connecting…")
        for button in (self.connect_button, self.detect, self.fetch, self.mode):
            button.setEnabled(False)
        self._done = done
        self.future = self.pool.submit(work)

    def _poll(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        for button in (self.connect_button, self.detect, self.fetch, self.mode):
            button.setEnabled(True)
        try:
            self._done(future.result())
        except (api.ConnectionError, OSError, ValueError) as exc:
            self.note.setText(str(exc) if isinstance(exc, api.ConnectionError) else "Could not save the connection. Check the settings folder permissions.")
        except Exception:
            self.note.setText("Could not complete the connection. Please try again.")

    def _base_and_key(self):
        base = api.endpoint(self.url.text(), self.port.text().strip() or None)
        key = self.key.text().strip()
        if key and key == self._original_key and base != self._original_url:
            raise api.ConnectionError("The endpoint changed. Clear the saved key or paste a key for this provider.")
        return base, key

    def _load_models(self):
        try:
            base, key = self._base_and_key()
        except api.ConnectionError as exc:
            self.note.setText(str(exc))
            return
        self._run(lambda: api.models(base, key), self._models_loaded)

    def _models_loaded(self, names):
        current = self.model.currentText()
        self.model.clear()
        self.model.addItems(names)
        if current in names:
            self.model.setCurrentText(current)
        self.note.setText("Models found. Choose one that supports JSON-schema chat responses, then connect.")

    def _discovered(self, results):
        self._found = results
        self.providers.clear()
        for base, names in results:
            self.providers.addItem(f"{base} — {', '.join(names[:3])}")
        self.providers.setVisible(bool(results))
        if results:
            self._pick_provider(0)
        else:
            self.note.setText("No provider found on local ports 11434, 1234, 8000 or 8080. "
                              "Start your server, or enter its endpoint and port.")

    def _pick_provider(self, index):
        base, names = self._found[index]
        self.url.setText(base)
        self.port.clear()
        self.key.clear()
        self._models_loaded(names)

    def _connect(self):
        mode = self.mode.currentData()
        if mode == "trial":
            self._run(lambda: api.start_trial(self.config_path), self._save_trial)
            return
        try:
            base, key = (api.HOSTED_URL, self.key.text().strip()) if mode == "premium" else self._base_and_key()
            model = api.MODEL if mode == "premium" else self.model.currentText().strip()
            if not model or (mode == "premium" and not key):
                raise api.ConnectionError("Enter your Patreon key." if mode == "premium" else "Choose a model.")
        except api.ConnectionError as exc:
            self.note.setText(str(exc))
            return
        def check():
            if model not in api.models(base, key):
                raise api.ConnectionError("That model is not available to this key. Load models or check your subscription.")
            return {"base_url": base, "model": model, "profile": "glm" if "glm" in model.lower() else "generic"}, key
        self._run(check, self._save_provider)

    def _save_provider(self, result):
        values, key = result
        values['key_file'] = str(api.write_key(self.config_path.parent / "provider.key", key)) if key else ""
        api.save_connection(self.config_path, values)
        self.accept()

    def _save_trial(self, result):
        values, status = result
        if status["matches_remaining"] <= 0:
            self.note.setText("Your five free matches are used. Subscribe through Patreon or choose your own provider.")
            return
        api.save_connection(self.config_path, values)
        self.accept()

    def done(self, result):
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)
