"""Bounded, local bug reports; no raw Player.log or unrestricted config dump."""
from collections import deque
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import logging
from pathlib import Path
import platform
import re
import threading

PRIVATE_EVENT_KINDS = frozenset({
    'llm_commentary', 'hand_online', 'tutor_anticipation', 'outs_anticipation',
    'trap_armed', 'trap_sprung', 'bluff_detected', 'clash_of_outs',
})


class BugReportLogs(logging.Handler):
    def __init__(self, *, secrets=(), max_lines=2000):
        super().__init__(logging.INFO)
        self._lines = deque(maxlen=max_lines)
        self._buffer_lock = threading.RLock()
        self._secrets = tuple(s for s in secrets if s)
        self.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))

    def redact(self, text):
        text = str(text).replace(str(Path.home()), '~')
        for secret in self._secrets:
            text = text.replace(secret, '[redacted]')
        text = re.sub(r'(?i)(bearer\s+)[^\s\"\']+', r'\1[redacted]', text)
        text = re.sub(r'(?i)((?:token|secret|password|api[_-]?key|authorization)[\"\']?\s*[:=]\s*[\"\']?)[^\s\"\'&,]+', r'\1[redacted]', text)
        # Signed download URLs are irrelevant to a commentary bug report.
        return re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[query omitted]', text)

    def emit(self, record):
        try:
            message = self.format(record)
            if (getattr(record, 'game_event_kind', None) in PRIVATE_EVENT_KINDS or
                    any('kind=' + kind in message for kind in PRIVATE_EVENT_KINDS)):
                message = f'{self.formatter.formatTime(record)} {record.levelname} {record.name}: [private-game commentary omitted]'
            if any(key in message for key in ('"gameObjects"', '"deckCards"', '"greToClientMessages"')):
                message = '[raw game data omitted]'
            message = self.redact(message)
            if len(message) > 2000:
                message = message[:2000] + ' [truncated]'
            with self._buffer_lock:
                self._lines.append(message)
        except Exception:
            self.handleError(record)

    def text(self):
        with self._buffer_lock:
            return '\n'.join(self._lines)


def build_bug_report(app, logs):
    cfg = app.config
    status = app.status()
    # Allowlisted values only: relay secrets, file paths, hands and decks stay out.
    details = {key: status.get(key) for key in (
        'state', 'match_id', 'queued', 'broadcast_mode', 'route', 'sources', 'enriched', 'narration_mode', 'model')}
    details['game'] = dict(getattr(app, '_diagnostic_game', {}))
    details['voices'] = {key: app.booth.get(key) for key in ('pbp_voice', 'analyst_voice', 'preset')}
    details['handoff_ms'] = cfg.co_caster_delay_ms
    engine = getattr(app.speaker, 'engine', None)
    details['active_speech_engine'] = getattr(engine, 'name', None)
    details['queued_roles'] = {role: sum(u.role == role for u in app.queue.pending())
                              for role in ('play_by_play', 'color_analyst')}
    details['installed_arena_card_db'] = bool(getattr(app.carddb, '_arena_conn', None))
    packages = {}
    for name in ('arenaonair', 'kokoro', 'PySide6', 'websockets'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'not installed'
    header = {
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'platform': platform.system() + ' ' + platform.release(),
        'python': platform.python_version(), 'packages': packages,
        'broadcast': details,
    }
    return logs.redact('ArenaOnAir bug report\n'
        'Recent app logs only; raw game logs, hands, decks, and credentials are excluded.\n\n'
        + json.dumps(header, indent=2, default=str)
        + '\n\nRecent logs (up to 2,000 entries):\n' + logs.text())
