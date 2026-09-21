#!/usr/bin/env python3
"""Build the local grpId->card cache from public Scryfall bulk data.

Downloads the Scryfall bulk-data index, picks the ``oracle_cards`` entry,
streams the ~250MB JSON array without holding it fully in memory, and inserts
every card that carries an ``arena_id`` into a SQLite database keyed by that
id (which equals the MTGA grpId).

Usage::

    python -m tools.build_carddb [--out PATH]

Default output: ``~/.cache/arenaonair/cards.sqlite`` (kept out of the repo).

Scryfall API etiquette honored here:
- every api.scryfall.com request sends ``Accept: application/json;q=0.9,*/*;q=0.8``
  and a descriptive User-Agent (missing either yields 403),
- transient failures retry with exponential backoff,
- HTTP 429 honors the ``Retry-After`` header,
- the bulk download itself is a signed CDN URL from the index response, so no
  special headers are needed there.
"""

from __future__ import annotations

import argparse
import codecs
import gzip
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

BULK_INDEX_URL = "https://api.scryfall.com/bulk-data"
ACCEPT_HEADER = "application/json;q=0.9,*/*;q=0.8"
USER_AGENT = "ArenaOnAir/0.1 (github.com/josharmour)"
ORACLE_ENTRY_TYPE = "oracle_cards"

MAX_RETRIES = 5
BASE_BACKOFF_S = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    arena_id   INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    type_line  TEXT NOT NULL,
    mana_cost  TEXT NOT NULL,
    card_types TEXT NOT NULL
);
"""


def _headers() -> Dict[str, str]:
    return {"Accept": ACCEPT_HEADER, "User-Agent": USER_AGENT}


def _open_with_retry(req: urllib.request.Request, *, stream: bool):
    """GET a URL with exponential backoff; honors Retry-After on 429.

    Returns the response object (caller closes it).  ``stream`` callers should
    read incrementally.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            return resp
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else BASE_BACKOFF_S * (2**attempt)
                print(f"  429 rate limited; sleeping {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                last_exc = exc
                continue
            if 500 <= exc.code < 600 or exc.code in (408,):
                delay = BASE_BACKOFF_S * (2**attempt)
                print(f"  HTTP {exc.code}; retrying in {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                last_exc = exc
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            delay = BASE_BACKOFF_S * (2**attempt)
            print(f"  network error ({exc}); retrying in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
            last_exc = exc
    raise RuntimeError(f"GET {req.full_url} failed after {MAX_RETRIES} attempts") from last_exc


def fetch_bulk_index() -> List[Dict[str, Any]]:
    req = urllib.request.Request(BULK_INDEX_URL, headers=_headers())
    with _open_with_retry(req, stream=False) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data", [])


def pick_default_cards_uri(entries: List[Dict[str, Any]]) -> str:
    """Pick the 'Default Cards' bulk entry's download URI.

    ``default_cards`` (not ``oracle_cards``) is used because the oracle file
    keeps only one representative printing per oracle id and frequently picks
    a paper-only printing that lacks ``arena_id``, dropping many MTGA ids.
    Default Cards includes every printable card, so all MTGA printings with
    an ``arena_id`` are present; duplicates are resolved by
    :func:`printing_score`.
    """
    for entry in entries:
        if entry.get("type") == "default_cards":
            # Newer index payloads expose the NDJSON file as
            # ``jsonl_download_uri``; older ones used ``download_uri``.
            uri = entry.get("jsonl_download_uri") or entry.get("download_uri")
            if uri:
                return uri
    raise RuntimeError("no 'default_cards' entry in Scryfall bulk-data index")


def pick_oracle_uri(entries: List[Dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("type") == ORACLE_ENTRY_TYPE:
            # Newer index payloads expose the NDJSON file as
            # ``jsonl_download_uri``; older ones used ``download_uri``.
            uri = entry.get("jsonl_download_uri") or entry.get("download_uri")
            if uri:
                return uri
    raise RuntimeError(f"no '{ORACLE_ENTRY_TYPE}' entry in Scryfall bulk-data index")


def _chunk_text(stream: Any) -> Iterator[str]:
    """Yield decoded text chunks from a byte stream (UTF-8, incremental)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        if not chunk:
            break
        text = decoder.decode(chunk)
        if text:
            yield text
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


def iter_json_objects(stream: Any) -> Iterator[Dict[str, Any]]:
    """Incrementally decode card objects from a Scryfall bulk download.

    Auto-detects the payload format: newline-delimited JSON (one object per
    line, the current ``jsonl_download_uri`` shape) or a single top-level JSON
    array of objects (older ``download_uri`` shape).  Streams rather than
    slurping — peak memory is one card object plus the current line/element
    buffer.
    """
    fmt_pending = ""
    fmt: Optional[str] = None  # "ndjson" | "array"; decided on first content

    nd_buf = ""
    arr_seen_open = False  # consumed the top-level '[' opener
    arr_depth = 0          # bracket nesting inside the current element
    arr_in_string = False
    arr_escaped = False
    arr_collecting = False  # currently buffering a top-level element
    arr_buf: List[str] = []
    decoder = json.JSONDecoder()

    def decide(head: str) -> str:
        stripped = head.lstrip()
        if stripped.startswith("{"):
            return "ndjson"
        if stripped.startswith("["):
            return "array"
        raise ValueError("bulk payload is neither NDJSON nor a JSON array")

    for text in _chunk_text(stream):
        if fmt is None:
            fmt_pending += text
            if not fmt_pending.strip():
                continue
            fmt = decide(fmt_pending)
            work, fmt_pending = fmt_pending, ""
        else:
            work = text

        if fmt == "ndjson":
            nd_buf += work
            # Process all complete lines; keep the trailing fragment.
            lines = nd_buf.split("\n")
            nd_buf = lines.pop()
            for line in lines:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
            continue

        # ---- top-level JSON array of objects ------------------------------
        i = 0
        n = len(work)
        while i < n:
            ch = work[i]
            if arr_in_string:
                arr_buf.append(ch)
                if arr_escaped:
                    arr_escaped = False
                elif ch == "\\":
                    arr_escaped = True
                elif ch == '"':
                    arr_in_string = False
                i += 1
                continue
            if ch == '"':
                if arr_collecting:
                    arr_buf.append(ch)
                    arr_in_string = True
                i += 1
                continue
            if ch in "[{":
                if not arr_seen_open:
                    arr_seen_open = True  # top-level array opener: skip it
                    i += 1
                    continue
                if not arr_collecting:
                    arr_collecting = True   # first char of a new element
                    arr_buf.clear()
                arr_depth += 1
                arr_buf.append(ch)
                i += 1
                continue
            if ch in "]}":
                if arr_collecting:
                    arr_depth -= 1
                    arr_buf.append(ch)
                    if arr_depth == 0:
                        # top-level element fully buffered -> decode and yield it
                        elem = "".join(arr_buf).strip()
                        arr_buf.clear()
                        arr_collecting = False
                        if elem:
                            obj, _ = decoder.raw_decode(elem)
                            yield obj
                # else: top-level array closer / separator -> ignore
                i += 1
                continue
            if arr_collecting:
                arr_buf.append(ch)
            i += 1  # commas/whitespace between elements at depth 0 are skipped

    # Flush any trailing NDJSON line without a final newline.
    if fmt == "ndjson":
        tail_line = nd_buf.strip()
        if tail_line:
            obj = json.loads(tail_line)
            if isinstance(obj, dict):
                yield obj


def open_download_stream(uri: str):
    """Open the signed CDN bulk download; transparently gunzip if needed."""
    req = urllib.request.Request(uri, method="GET")
    resp = _open_with_retry(req, stream=True)
    raw: io.BufferedIOBase = resp  # type: ignore[assignment]
    if getattr(resp, "headers", None) and resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.GzipFile(fileobj=resp)  # type: ignore[arg-type]
    elif uri.endswith(".gz"):
        raw = gzip.GzipFile(fileobj=resp)  # type: ignore[arg-type]
    return raw


def card_row(card: Dict[str, Any]) -> Optional[tuple]:
    arena_id = card.get("arena_id")
    if arena_id is None:
        return None
    name = card.get("name") or ""
    type_line = card.get("type_line") or ""
    mana_cost = card.get("mana_cost") or ""
    types: List[str] = []
    seen: set[str] = set()
    for part in card.get("card_faces") or []:
        for t in part.get("types") or []:
            if t not in seen:
                seen.add(t)
                types.append(t)
    if not types:
        # Fall back to the words before the em-dash in the type line
        # (supertypes + card types; subtypes after the dash are excluded).
        head = type_line.split("\u2014")[0]
        types = [w for w in head.split() if w and w != "//"]
    csv_types = ",".join(types)
    return (int(arena_id), name, type_line, mana_cost, csv_types)


def printing_score(card: Dict[str, Any]) -> tuple:
    """Rank candidate printings sharing one arena_id.

    Digital (MTGA) printings win, then non-promo, then newest release.
    """
    return (
        1 if card.get("digital") else 0,
        0 if card.get("promo") else 1,
        card.get("released_at") or "",
    )


def build(out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    print("Fetching Scryfall bulk-data index...")
    entries = fetch_bulk_index()
    uri = pick_default_cards_uri(entries)
    print(f"Default Cards download: {uri}")

    import sqlite3

    conn = sqlite3.connect(str(tmp_path))
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute(_SCHEMA)

    # Multiple printings can share one arena_id; keep the best-ranked one
    # in memory (one row per id) and insert at the end.
    best: Dict[int, tuple] = {}
    scanned = 0
    stream_ctx = open_download_stream(uri)
    try:
        for card in iter_json_objects(stream_ctx):
            scanned += 1
            row = card_row(card)
            if row is None:
                continue
            arena_id = row[0]
            prev = best.get(arena_id)
            if prev is None or printing_score(card) > prev[0]:
                best[arena_id] = (printing_score(card), row)
            if scanned and scanned % 50000 == 0:
                print(f"  scanned {scanned} cards, {len(best)} distinct arena_ids...")
    finally:
        try:
            stream_ctx.close()
        except Exception:
            pass

    conn.executemany(
        "INSERT INTO cards (arena_id, name, type_line, mana_cost, card_types) "
        "VALUES (?, ?, ?, ?, ?)",
        (row for _, row in best.values()),
    )
    conn.commit()

    total = int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
    conn.close()

    tmp_path.replace(out_path)
    print(
        f"Done: {total} rows from {scanned} cards "
        f"({len(best)} distinct arena_ids)"
    )
    print(f"Wrote {out_path}")
    return total


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(Path.home() / ".cache" / "arenaonair" / "cards.sqlite"),
        help="output sqlite path (default ~/.cache/arenaonair/cards.sqlite)",
    )
    args = parser.parse_args(argv)
    build(Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
