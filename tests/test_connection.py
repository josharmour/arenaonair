import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arenaonair import connection
from arenaonair.config import Config, load
from arenaonair.llm_booth import JsonClient, ModelError


@pytest.fixture
def provider():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(dict(self.headers))
            if self.path == '/redirect/models':
                self.send_response(302)
                self.send_header('Location', '/v1/models')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'data': [{'id': 'local-model'}]}).encode())
        def do_POST(self):
            calls.append(dict(self.headers))
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}).encode())
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', calls
    server.shutdown()
    server.server_close()
    thread.join()


def test_local_discovery_and_generation_without_api_key(provider):
    base, calls = provider
    assert connection.models(base) == ['local-model']
    client = JsonClient(Config(llm_base_url=base + '/v1', llm_model='local-model', llm_profile='generic'))
    assert client._request('test', {}) == {}
    assert all('Authorization' not in call for call in calls)


def test_redirect_does_not_forward_credentials(provider):
    base, calls = provider
    with pytest.raises(connection.ConnectionError, match='302'):
        connection.models(base + '/redirect', 'secret')
    assert len(calls) == 1


def test_trial_provision_reuses_secret_and_config_has_no_key(tmp_path, monkeypatch):
    sent = []
    def request(url, key, body):
        sent.append((key, body))
        return {'matches_remaining': 5}
    monkeypatch.setattr(connection, 'request_json', request)
    path = tmp_path / 'config.toml'
    values, _ = connection.start_trial(path)
    connection.save_connection(path, values)
    connection.start_trial(path)
    assert sent[0] == sent[1] and sent[0][0].startswith('aoa_')
    assert sent[0][0] not in path.read_text()
    assert load(path).llm_base_url == connection.TRIAL_URL


def test_trial_requires_real_match_and_default_requires_connection():
    with pytest.raises(ModelError, match='connection_required'):
        JsonClient(Config())._request('test', {})
    with pytest.raises(ModelError, match='trial_waiting_match'):
        JsonClient(Config(llm_base_url=connection.TRIAL_URL))._request('test', {})


@pytest.mark.parametrize('value', ['https://key@example.com/v1', 'https://example.com/v1?key=secret',
                                   'file:///tmp/key', 'http://localhost:99999'])
def test_endpoint_rejects_unsafe_or_invalid_urls(value):
    with pytest.raises(connection.ConnectionError):
        connection.endpoint(value)


def test_endpoint_port_preserves_api_path():
    assert connection.endpoint('localhost', '1234') == 'http://localhost:1234/v1'
    assert connection.endpoint('http://localhost/api/v1', '8000') == 'http://localhost:8000/api/v1'


def test_new_user_gets_generative_booth_without_network(tmp_path):
    from arenaonair.app import ArenaOnAirApp
    app = ArenaOnAirApp(Config(carddb_path=str(tmp_path / 'cards.sqlite'), history_enabled=False))
    try:
        assert app.narration_mode == 'llm' and app.llm is not None
        assert 'Connect the generative booth' in app.warnings[0]
    finally:
        app.stop()


def test_trial_startup_log_is_primed_before_generating(tmp_path, monkeypatch):
    from arenaonair.app import ArenaOnAirApp
    from types import SimpleNamespace
    app = ArenaOnAirApp(Config(llm_base_url=connection.TRIAL_URL, carddb_path=str(tmp_path / 'cards'),
                              history_enabled=False, poll_interval=.005))
    batches = iter([[(1, 'existing log')], [(2, 'new activity')]])
    seen = []
    class Watcher:
        def close(self):
            pass
        def poll(self):
            try:
                return next(batches)
            except StopIteration:
                app._stop_event.set()
                return []
    app.watcher = Watcher()
    def feed(prev, backlog, ts, raw):
        seen.append((raw, app._trial_priming))
        if app._trial_priming:
            # Even a game-start/turn event cannot trigger generation while priming.
            app._handle_event(SimpleNamespace(kind='turn_start'), None)
        return prev, backlog
    monkeypatch.setattr(app, '_feed_line', feed)
    try:
        app._watch_loop()
        assert seen == [('existing log', True), ('new activity', False)]
    finally:
        app.stop()
