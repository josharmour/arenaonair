"""OBS browser-source overlay: live captions, scoreboard and hole-card cam.

A tiny stdlib HTTP server bound to 127.0.0.1 only. OBS adds
``http://127.0.0.1:PORT/`` as a Browser Source (transparent background).
Query ``?show=captions,scoreboard,hand`` picks panels (default: all).

Updates stream over Server-Sent Events. The hand panel receives data only
when the app allows hole cards on air (see ``config.hole_cards_enabled``);
otherwise the server never has the hand to send. Stream delay is applied by
OBS to the whole scene, so captions and audio stay in sync with the game.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import queue
import threading

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>ArenaOnAir overlay</title>
<style>
:root{--ink:#fff;--panel:rgba(12,14,22,.82);--pbp:#f5b942;--analyst:#6ec6ff;--line:rgba(255,255,255,.18)}
html,body{margin:0;background:transparent;color:var(--ink);font:600 26px/1.3 system-ui,-apple-system,"Segoe UI",sans-serif;overflow:hidden}
#board{position:fixed;top:16px;left:50%;transform:translateX(-50%);display:flex;gap:18px;align-items:center;
 background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 18px;font-size:22px}
#board .life{font-size:30px;font-variant-numeric:tabular-nums;min-width:2ch;text-align:center}
#board .turn{opacity:.7;font-size:18px}
#caption{position:fixed;left:50%;bottom:48px;transform:translateX(-50%);max-width:80vw;background:var(--panel);
 border:1px solid var(--line);border-radius:12px;padding:12px 20px;transition:opacity .4s;opacity:0}
#caption.on{opacity:1}
#caption .who{font-size:18px;text-transform:uppercase;letter-spacing:.08em;display:block}
#caption[data-role=play_by_play] .who{color:var(--pbp)}#caption[data-role=color_analyst] .who{color:var(--analyst)}
#hand{position:fixed;right:16px;bottom:48px;background:var(--panel);border:1px solid var(--line);border-radius:12px;
 padding:10px 14px;font-size:18px;max-width:22vw}
#hand h3{margin:0 0 6px;font-size:14px;letter-spacing:.1em;text-transform:uppercase;opacity:.7}
#hand ul{margin:0;padding-left:1.1em}
.hidden{display:none!important}
</style></head><body>
<div id="board" class="hidden"></div>
<div id="caption"><span class="who"></span><span class="text"></span></div>
<div id="hand" class="hidden"><h3>Hole cards</h3><ul></ul></div>
<script>
const show=new Set((new URLSearchParams(location.search).get('show')||'captions,scoreboard,hand').split(','));
const $=s=>document.querySelector(s);let hideTimer;
function esc(t){const d=document.createElement('span');d.textContent=t;return d.innerHTML}
function caption(c){if(!show.has('captions'))return;const el=$('#caption');el.dataset.role=c.role;
 el.querySelector('.who').textContent=c.speaker||'';el.querySelector('.text').textContent=c.text;el.classList.add('on');
 clearTimeout(hideTimer);hideTimer=setTimeout(()=>el.classList.remove('on'),Math.max(4000,c.text.length*70))}
function state(s){const b=$('#board');if(show.has('scoreboard')&&s.players&&s.players.length){
 b.innerHTML=s.players.map(p=>`<span>${esc(p.name)}</span><span class="life">${p.life??'–'}</span>`).join('<span>·</span>')
 +(s.turn?`<span class="turn">Turn ${s.turn}</span>`:'');b.classList.remove('hidden')}else b.classList.add('hidden');
 const h=$('#hand');if(show.has('hand')&&s.hand&&s.hand.length){h.querySelector('ul').innerHTML=s.hand.map(n=>`<li>${esc(n)}</li>`).join('');
 h.classList.remove('hidden')}else h.classList.add('hidden')}
function connect(){const es=new EventSource('/events');es.addEventListener('caption',e=>caption(JSON.parse(e.data)));
 es.addEventListener('state',e=>state(JSON.parse(e.data)));es.onerror=()=>{es.close();setTimeout(connect,2000)}}
connect();
</script></body></html>"""


class OverlayServer:
    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.host, self.port = host, port
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._state: dict = {"players": [], "turn": None, "hand": []}
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        overlay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/":
                    body = _PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/state":
                    body = json.dumps(overlay.state()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/events":
                    overlay._stream(self)
                else:
                    self.send_error(404)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="arenaonair-overlay", daemon=True)
        self._thread.start()
        logger.info("OBS overlay at http://%s:%d/ (add as a Browser Source)", self.host, self.port)

    def close(self) -> None:
        with self._lock:
            for q in self._clients:
                q.put(None)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    # -- publishing -----------------------------------------------------------

    def _broadcast(self, event: str, data: dict) -> None:
        message = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            for q in list(self._clients):
                try:
                    q.put_nowait(message)
                except queue.Full:
                    pass

    def caption(self, role: str, speaker: str | None, text: str) -> None:
        self._broadcast("caption", {"role": role, "speaker": speaker, "text": text})

    def update(self, snap, *, hole_cards: bool) -> None:
        """Publish public scoreboard state (+ the local hand when allowed)."""
        try:
            names = dict(snap.match_meta.player_names)
            players = [{"seat": seat, "name": names.get(seat) or f"Player {seat}", "life": p.life}
                       for seat, p in sorted(snap.players.items())]
            hand = []
            local = snap.local_seat
            knowledge = snap.seat_knowledge.get(local) if local is not None else None
            if hole_cards and knowledge is not None and knowledge.hand_visible:
                for zone in snap.zones.values():
                    if zone.zone_type.lower().replace("zonetype_", "") == "hand" and zone.owner_seat == local:
                        hand.extend(snap.objects[i].name for i in zone.object_ids
                                    if i in snap.objects and snap.objects[i].name)
            state = {"players": players, "turn": snap.turn_info.turn_number, "hand": hand[:15]}
        except Exception:
            logger.debug("overlay state build failed", exc_info=True)
            return
        with self._lock:
            if state == self._state:
                return
            self._state = state
        self._broadcast("state", state)

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    # -- SSE --------------------------------------------------------------------

    def _stream(self, handler) -> None:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.append(q)
            initial = self._state
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            handler.wfile.write(f"event: state\ndata: {json.dumps(initial)}\n\n".encode())
            handler.wfile.flush()
            while True:
                try:
                    message = q.get(timeout=15)
                except queue.Empty:
                    message = ": keepalive\n\n"
                if message is None:
                    return
                handler.wfile.write(message.encode())
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._lock:
                if q in self._clients:
                    self._clients.remove(q)
