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

from .watcher import LogWatcher

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

    def __init__(self, bind_host: str, port: int, secret: str = "") -> None:

        self._bind_host = bind_host
        self._port = int(port)
        self._secret = secret

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

                    await self._pump(ws)

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

        watcher = LogWatcher(path=self._log_path, anchor=False)
        try:
            while not self._stop_evt.is_set():
                batch = watcher.poll()
                for _ts, line in batch:
                    frame = {
                        "type": "log",
                        "line": line,
                        # Informational only; never used for ordering/seats.
                        "ts_client_wallclock": time.time(),
                    }
                    await ws.send(json.dumps(frame))
                    self.sent_count += 1
                if not batch:
                    await asyncio.sleep(self._poll_interval)
        finally:
            watcher.close()


async def _demo() -> None:  # pragma: no cover - manual smoke helper

    server = RelayServer("127.0.0.1", 0)
    await server.start()
    print(f"relay demo on :{server.port} backend={server.backend}")
    await server.close()


if __name__ == "__main__":  # pragma: no cover

    asyncio.run(_demo())
