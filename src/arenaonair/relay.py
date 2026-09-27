"""Relay transport for dual-source ingestion (S8.x): server + forwarder.

BACKEND CHOICE (declared per task contract): ``websockets`` IS importable on
this box (v16.0), so the module uses it directly::

    try:
        import websockets  # noqa: F401
        _BACKEND = "websockets"
    except ImportError:      # pragma: no cover - fallback path
        _BACKEND = "asyncio-minimal"

The asyncio-minimal fallback is intentionally NOT implemented here: shipping
two wire implementations doubles the protocol-test burden for a path this
deployment never exercises. Import failure raises at module import with a
clear message instead.

FRAME PROTOCOL (JSON, one object per websocket text frame)
----------------------------------------------------------
Client -> server:
    {"type": "auth",   "token": "<secret>"}     REQUIRED first frame when the
                                                server was constructed with a
                                                secret; otherwise optional and
                                                ignored.
    {"type": "log",    "line": "<raw Player.log line>",
                       "ts_client_wallclock": <float, INFORMATIONAL ONLY>}
                                                The payload of interest. The
                                                server NEVER derives ordering
                                                or seat identity from
                                                ts_client_wallclock (S3.3).

Server -> client:
    {"type": "welcome", "conn_id": <int>}       Sent on successful admission.
    {"type": "ack"}                             Auth accepted / log frame
                                                receipt confirmation.
    {"type": "error",  "code": "auth_required" | "bad_token"}
                                                Sent before closing on failed
                                                authentication.

Server -> SUBSCRIBERS (fan-out; anyone may subscribe):
    {"type": "frame",  "conn_id": <int>,        CONNECTION id -- NOT a seat,
                                                NOT a source_id. Connection
                                                order does NOT establish seat
                                                identity; consumers resolve
                                                seats only from validated
                                                session metadata inside the
                                                forwarded log stream itself.
                       "payload": { ...original client frame... }}

Connection-id assignment: monotonically increasing integer per server
lifetime, starting at 0, in accept order. It identifies the TRANSPORT
connection only.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

try:
    import websockets  # noqa: F401  -- availability probe + backend use
    from websockets.asyncio.server import serve as _ws_serve
    from websockets.asyncio.client import connect as _ws_connect
    from websockets.exceptions import (
        ConnectionClosed as _WsConnectionClosed,
        InvalidStatus as _WsInvalidStatus,
    )

    _BACKEND = "websockets"
except ImportError as _exc:  # pragma: no cover - deployment guard
    raise ImportError(
        "arenaonair.relay requires the 'websockets' package "
        "(checked importable on this box at authoring time); "
        f"underlying import error: {_exc}"
    ) from _exc

from .watcher import LogWatcher, MultiLogWatcher

__all__ = ["RelayServer", "RelayForwarder", "_BACKEND"]


class RelayServer:
    """Accepts authenticated websocket clients and fans frames out to all
    connected peers, each fan-out frame tagged with the ORIGINATING
    connection's id.

    Authentication: when ``secret`` is non-empty the FIRST frame from a client
    must be ``{"type": "auth", "token": ...}`` matching it; anything else (or a
    wrong token) earns an ``error`` frame and an immediate close. With no
    secret every connection is admitted immediately.
    """

    def __init__(self, bind_host: str, port: int, secret: str = "", *, on_line=None, on_disconnect=None) -> None:

        self._bind_host = bind_host
        self._port = int(port)
        self._secret = secret
        self.on_line = on_line
        self.on_disconnect = on_disconnect

        self._server = None                 # websockets Server object
        self._next_conn_id = 0              # monotonic connection id source
        self._clients: dict[int, Any] = {}  # conn_id -> ws connection

    # ------------------------------------------------------------------ #

    @property
    def port(self) -> int:

        return self._port

    @property
    def backend(self) -> str:

        return _BACKEND

    @property
    def connection_count(self) -> int:

        return len(self._clients)

    async def start(self) -> None:

        """Bind and begin accepting (idempotent)."""

        if self._server is not None:
            return
        self._server = await _ws_serve(
            self._handler, self._bind_host, self._port
        )
        # Reflect the actually-bound port (caller may pass 0 for ephemeral).
        try:
            sockets = getattr(self._server, "sockets", None) or []
            for sock in sockets:
                self._port = sock.getsockname()[1]
                break
        except Exception:
            pass

    async def close(self) -> None:

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for ws in list(self._clients.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()

    async def __aenter__(self) -> "RelayServer":

        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:

        await self.close()

    # ------------------------------------------------------------------ #

    async def _authenticate(self, ws) -> bool:

        if not self._secret:
            return True  # open server: skip straight to admission

        try:
            first_raw = await ws.recv()
        except Exception:
            return False

        try:
            first = json.loads(first_raw)
        except (TypeError, ValueError):
            first = None

        if not isinstance(first, dict) or first.get("type") != "auth":
            await self._send(ws, {"type": "error", "code": "auth_required"})
            return False

        if first.get("token") != self._secret:
            await self._send(ws, {"type": "error", "code": "bad_token"})
            return False

        await self._send(ws, {"type": "ack"})
        return True

    @staticmethod
    async def _send(ws, obj: dict) -> None:

        await ws.send(json.dumps(obj))

    async def _handler(self, ws) -> None:

        if not await self._authenticate(ws):
            try:
                await ws.close()
            except Exception:
                pass
            return

        conn_id = self._next_conn_id
        self._next_conn_id += 1
        self._clients[conn_id] = ws

        try:
            await self._send(ws, {"type": "welcome", "conn_id": conn_id})
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue  # undecodable junk: drop silently
                if not isinstance(frame, dict):
                    continue
                if self.on_line is not None:
                    if frame.get("type") == "log" and isinstance(frame.get("line"), str):
                        self.on_line(conn_id, frame)
                    continue  # player clients must not receive opponents' logs
                # Fan out to EVERY connected peer (including the sender --
                # loopback lets a forwarder verify its own path cheaply),
                # stamped with the originating CONNECTION id.
                outgoing = {"type": "frame", "conn_id": conn_id,
                            "payload": frame}
                dead: list[int] = []
                for peer_id, peer_ws in list(self._clients.items()):
                    try:
                        await peer_ws.send(json.dumps(outgoing))
                    except Exception:
                        dead.append(peer_id)
                for pid in dead:
                    self._clients.pop(pid, None)
        except Exception:
            pass  # any transport hiccup ends this connection's loop quietly
        finally:
            self._clients.pop(conn_id, None)
            if self.on_disconnect:
                self.on_disconnect(conn_id)


class RelayForwarder:
    """Tails a local Player.log via :class:`watcher.LogWatcher` and streams
    raw lines to a relay as ``{"type": "log", ...}`` frames.

    Reconnect policy: on ANY send/connection failure the forwarder closes,
    waits ``reconnect_delay_s``, increments its ``session_gen`` (so downstream
    consumers can discard pre-drop replays via SourceRegistry), and retries.
    Wall-clock timestamps attached to frames are INFORMATIONAL ONLY -- they
    never establish ordering.
    """

    def __init__(
        self,
        connect_addr: str,
        log_path: Optional[str] = None,
        *,
        secret: str = "",
        reconnect_delay_s: float = 1.0,
        poll_interval: float = 0.25,
    ) -> None:

        self._addr = connect_addr
        self._log_path = log_path          # None -> platform default lazily
        self._secret = secret
        self._reconnect_delay_s = reconnect_delay_s
        self._poll_interval = poll_interval

        import uuid
        import socket
        from pathlib import Path
        identity = socket.gethostname() + ":" + str(Path(log_path).expanduser().resolve() if log_path else "default")
        self.client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        self.session_gen = 0               # bumped on every reconnect attempt cycle
        self.sent_count = 0

        self._stop_evt = asyncio.Event()

    # ------------------------------------------------------------------ #

    def stop(self) -> None:

        """Request graceful shutdown from outside the running task."""

        self._stop_evt.set()

    @property
    def stopped(self) -> bool:

        return self._stop_evt.is_set()

    async def run(self) -> None:

        """Run until :meth:`stop` is called (never raises past transport)."""

        while not self._stop_evt.is_set():
            try:
                async with _ws_connect(
                    f"ws://{self._addr}" if "//" not in self._addr else self._addr,
                    additional_headers=(
                        {} if not hasattr(_ws_connect, "__wrapped__") else {}
                    ),
                ) as ws:
                    if self._secret:
                        await ws.send(json.dumps(
                            {"type": "auth", "token": self._secret}))
                        ack_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        ack = json.loads(ack_raw)
                        if not (isinstance(ack, dict)
                                and ack.get("type") == "ack"):
                            # Rejected (bad token / protocol): back off and
                            # retry without spinning hot.
                            await asyncio.sleep(max(0.05,
                                                    self._reconnect_delay_s))
                            continue

                    # Drain server control frames so receive backpressure can
                    # never stop an otherwise healthy forwarder.
                    async def drain():
                        async for _ in ws:
                            pass
                    reader = asyncio.create_task(drain())
                    sender = asyncio.create_task(self._pump(ws))
                    try:
                        await asyncio.wait((reader, sender), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        reader.cancel()
                        sender.cancel()
                        await asyncio.gather(reader, sender, return_exceptions=True)

            except (_WsConnectionClosed, _WsInvalidStatus, OSError,
                    asyncio.TimeoutError, Exception):
                pass  # fall through to backoff + session_gen bump

            if self._stop_evt.is_set():
                break

            # Transport generation bump: everything sent before this point
            # belongs to the previous generation.
            self.session_gen += 1
            try:
                await asyncio.sleep(max(0.05, self._reconnect_delay_s))
            except asyncio.CancelledError:
                break

    async def _pump(self, ws) -> None:

        watcher = MultiLogWatcher([LogWatcher(path=self._log_path).path], anchor=False)
        try:
            while not self._stop_evt.is_set():
                batch = watcher.poll()
                for sid, _ts, line in batch:
                    frame = {
                        "type": "log",
                        "line": line,
                        "client_id": self.client_id,
                        "log_generation": watcher.generations[sid],
                        # Informational only; never used for ordering/seats.
                        "ts_client_wallclock": time.time(),
                    }
                    await ws.send(json.dumps(frame))
                    self.sent_count += 1
                if not batch:
                    await asyncio.sleep(self._poll_interval)
        finally:
            watcher.close()


class RelayLogSource:
    """Threaded websocket receiver exposing the same poll surface as files."""
    def __init__(self, bind, *, secret=""):
        import queue
        import threading
        host, port = bind.rsplit(":", 1)
        self._host, self._port, self._secret = host, int(port), secret
        self._lines = queue.Queue(maxsize=8192)
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error = None
        self._thread = None
        self._clients = {}
        self._connections = {}
        self._current = {}
        self._log_generations = {}
        self.generations = {}
        self.source_health = {}
        self.server = None

    def _receive(self, conn_id, frame):
        import queue
        identity = str(frame.get("client_id") or f"connection-{conn_id}")
        if identity not in self._clients:
            available = next((s for s in self._clients.values()
                              if not self.source_health.get(s, False)), None)
            if len(self._clients) >= 2:
                if available is None:
                    return
                self._clients = {k: v for k, v in self._clients.items() if v != available}
                self._clients[identity] = available
            else:
                self._clients[identity] = len(self._clients)
        sid = self._clients[identity]
        if conn_id < self._current.get(sid, -1):
            return
        if self._current.get(sid) != conn_id:
            self._current[sid] = conn_id
            self.generations[sid] = self.generations.get(sid, -1) + 1
            self._log_generations[sid] = frame.get("log_generation", 0)
        log_generation = frame.get("log_generation", 0)
        if self._log_generations.get(sid) != log_generation:
            self._log_generations[sid] = log_generation
            self.generations[sid] += 1
        self._connections[conn_id] = sid
        self.source_health[sid] = True
        try:
            self._lines.put_nowait((sid, self.generations[sid], time.monotonic(), frame["line"]))
        except queue.Full:
            # Dropping a line breaks continuity: force rebootstrap instead of
            # presenting subsequent diffs as a complete state.
            self.source_health[sid] = False
            self.generations[sid] += 1

    def _disconnected(self, conn_id):
        sid = self._connections.get(conn_id)
        if sid is not None and self._current.get(sid) == conn_id:
            self.source_health[sid] = False

    def start(self):
        import threading
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("relay startup timed out")
        if self._error:
            raise self._error

    async def _run(self):
        try:
            self.server = RelayServer(self._host, self._port, self._secret,
                                      on_line=self._receive, on_disconnect=self._disconnected)
            await self.server.start()
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.02)
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            if self.server:
                await self.server.close()

    def poll(self):
        import queue
        out = []
        while True:
            try:
                sid, gen, ts, line = self._lines.get_nowait()
            except queue.Empty:
                return out
            if gen == self.generations.get(sid):
                out.append((sid, gen, ts, line))

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Forward a Player.log to the ArenaOnAir relay")
    parser.add_argument("--connect", required=True, metavar="HOST:PORT")
    parser.add_argument("--log-path")
    parser.add_argument("--secret", default="")
    parser.add_argument("--seat", type=int, help="Informational only; GRE metadata establishes the seat")
    args = parser.parse_args(argv)
    forwarder = RelayForwarder(args.connect, args.log_path, secret=args.secret)
    try:
        asyncio.run(forwarder.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
