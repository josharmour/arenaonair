"""LogWatcher: cross-platform tailing of the MTGA Player.log.

Polling-based tail (v1 truth). DESIGN.md mentions watchdog; we ship a
pure-stdlib polling loop instead -- same observable behavior, no third-party
dependency, trivially testable without filesystem-event injection. That
deviation is deliberate and recorded in the task report.

Public surface:
    LogWatcher(path=None, *, anchor=True, ...)
        .lines()  -> blocking iterator of (ts, raw_line)   [LogSource protocol]
        .poll()   -> list[(ts, raw_line)]                  [non-generator core]
        .stalled_seconds                 seconds since last new line
        .close()

Rotation/truncation handling, partial-line buffering, and the startup anchor
scan live here; the watcher owns nothing game-related.
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Iterator

# A UnityCrossThreadLogger line carrying one of these markers starts real
# match traffic; the anchor scan seeks back to the last such line.
_ANCHOR_TAG = "[UnityCrossThreadLogger]"
_ANCHOR_MARKERS = (": Match to ", ": GreToClientEvent")
_DEFAULT_ANCHOR_WINDOW_LINES = 2000

_READ_CHUNK = 65536


class LogWatcher:
    """Tails one Player.log, yielding raw lines with wall-clock timestamps.

    Parameters:
    path -- explicit log path; when omitted the platform default is resolved
    lazily on first poll (constructing a watcher never touches the FS).
    anchor -- on first open of an existing non-empty log, scan backwards
    (bounded window) for the last UnityCrossThreadLogger line that is match
    traffic (": Match to " / ": GreToClientEvent") and start emitting from
    there instead of replaying megabytes of boot noise.
    anchor_window_lines -- backward-scan bound in lines (default ~2000).
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        anchor: bool = True,
        anchor_window_lines: int = _DEFAULT_ANCHOR_WINDOW_LINES,
    ) -> None:

        self._explicit_path = Path(path) if path is not None else None
        self._anchor_enabled = anchor
        self._anchor_window_lines = max(1, int(anchor_window_lines))

        self._fh: io.BufferedReader | None = None
        self._opened_path: Path | None = None
        self._offset: int = 0                      # bytes consumed from current fh
        self._pending: bytearray = bytearray()     # partial trailing line buffer
        self._identity: tuple[int, int] | None = None  # (st_dev, st_ino)
        self._last_line_ts: float | None = None    # wall clock of last emitted line
        self._anchored_generations: set[Path] = set()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:

        if self._explicit_path is not None:
            return self._explicit_path
        from arenaonair.platform import default_player_log_path

        return default_player_log_path()

    @property
    def stalled_seconds(self) -> float:

        if self._last_line_ts is None:
            return 0.0
        return max(0.0, time.time() - self._last_line_ts)

    def poll(self) -> list[tuple[float, str]]:

        out: list[tuple[float, str]] = []
        try:
            target = self.path
        except Exception:
            return out  # platform default not resolvable yet; retry next poll

        if self._fh is None or self._need_reopen(target):
            if not self._open(target):
                return out

        assert self._fh is not None
        data = self._read_available()
        if data:
            out.extend(self._consume(data))
            return out

        # No new bytes: catch a pure truncation (size dropped below our offset)
        # that produced nothing readable this round.
        self._check_shrunk()
        return out

    def lines(self) -> Iterator[tuple[float, str]]:

        while True:
            batch = self.poll()
            yield from batch
            if not batch:
                time.sleep(0.25)

    def close(self) -> None:

        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _stat_identity(self, target: Path) -> tuple[int, int] | None:

        try:
            st = target.stat()
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    def _need_reopen(self, target: Path) -> bool:

        ident = self._stat_identity(target)
        if ident is None:
            return False  # vanished momentarily; keep reading buffered handle
        return self._identity is not None and ident != self._identity

    def _open(self, target: Path) -> bool:

        try:
            fh = open(target, "rb")
        except OSError:
            return False

        was_open = self._fh is not None
        if was_open:
            self._fh.close()

        try:
            st = os.fstat(fh.fileno())
            size = st.st_size
            ident = (st.st_dev, st.st_ino)
            first_time = target not in self._anchored_generations

            start = 0
            if size > 0 and first_time and self._anchor_enabled:
                start = self._anchor_offset(fh)
                self._anchored_generations.add(target)

            fh.seek(start)
            self._fh = fh
            self._opened_path = target
            self._identity = ident
            self._offset = start
            self._pending.clear()
            return True
        except OSError:
            fh.close()
            return False

    def _current_size(self) -> int:

        try:
            assert self._fh is not None
            return os.fstat(self._fh.fileno()).st_size
        except OSError:
            return 0

    def _check_shrunk(self) -> bool:

        if self._fh is None:
            return False
        size = self._current_size()

        # Idle-with-pending is NORMAL here: ``size == offset`` merely means we
        # drained every byte available while an unfinished trailing line still
        # awaits its newline -- rewinding would replay already-emitted lines.
        #
        # Rewind ONLY on positive truncation evidence: the file physically
        # shrank below our consumed offset, so bytes we already emitted are
        # gone from this generation.
        #
        # Rotation/file replacement never reaches this path with an intact
        # handle; identity changes route through ``_need_reopen`` -> ``_open``,
        # which starts the fresh generation at its own offset with an empty
        # pending buffer.
        if size < self._offset:
            # Truncated under us: drop the partial buffer too -- its bytes
            # belonged to the dead generation.
            try:
                self._fh.seek(0)
                self._offset = 0
                self._pending.clear()
                return True
            except OSError:
                return False
        return False

    def _read_available(self) -> bytes:

        assert self._fh is not None
        chunks: list[bytes] = []
        while True:
            try:
                chunk = self._fh.read(_READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunk) < _READ_CHUNK:
                break
        data = b"".join(chunks)
        if data:
            try:
                size_now = os.fstat(self._fh.fileno()).st_size
                pos_before_read = size_now - len(data)
                if pos_before_read < 0 or size_now < pos_before_read + len(data):
                    pass  # shrink handled by _check_shrunk on idle polls
            except OSError:
                pass
            self._offset += len(data)
        return data

    def _consume(self, data: bytes) -> list[tuple[float, str]]:

        buf = bytes(self._pending) + data if self._pending else data
        lines: list[tuple[float, str]] = []
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl == -1:
                break
            raw = buf[start:nl]
            start = nl + 1
            text = raw.decode("utf-8", errors="replace").rstrip("\r")
            ts = time.time()
            lines.append((ts, text))
            self._last_line_ts = ts
        remainder = buf[start:]
        if remainder != bytes(self._pending):
            self._pending.clear()
            self._pending.extend(remainder)
        return lines

    def _anchor_offset(self, fh: io.BufferedReader) -> int:

        # Scan backwards for the last match-traffic marker line within a
        # bounded window; fall back to 0 (full replay) when none is found.
        try:
            size = os.fstat(fh.fileno()).st_size
        except OSError:
            return 0

        window_bytes = self._anchor_window_lines * 512
        start_pos = max(0, size - window_bytes)
        fh.seek(start_pos)
        blob = fh.read(size - start_pos)
        if start_pos > 0:
            # Discard the possibly-partial first line of the window.
            first_nl = blob.find(b"\n")
            if first_nl != -1:
                blob = blob[first_nl + 1:]

        lines = blob.split(b"\n")
        anchor_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if _ANCHOR_TAG.encode() not in line:
                continue
            if any(m.encode() in line for m in _ANCHOR_MARKERS):
                anchor_idx = i
                break

        if anchor_idx == -1:
            return 0
        # Byte offset of the line AFTER the anchor line: emit the anchor line
        # itself (it is match traffic worth seeing) plus everything following.
        offset = start_pos + sum(len(l) + 1 for l in lines[:anchor_idx])
        return min(offset, size)

    def __repr__(self) -> str:

        state = "open" if self._fh is not None else "closed"
        return f"<LogWatcher path={self._opened_path!r} {state} offset={self._offset}>"
