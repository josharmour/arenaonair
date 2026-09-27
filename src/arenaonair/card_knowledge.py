"""Local, sourced card context. This module never performs network requests."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

DEFAULT_KNOWLEDGE_PATH = Path.home() / '.cache/arenaonair/knowledge.sqlite'
SCHEMA_VERSION = 1
MAX_AGE = 14 * 86400


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def rules_text(card):
    return card.get('oracle_text') or '\n'.join(
        f['name'] + ': ' + f['oracle_text'] for f in card.get('card_faces', []) if f.get('oracle_text'))


class CardKnowledge:
    """Read-only SQLite snapshots, re-opened after atomic refreshes.

    Missing, incompatible, stale, or conflicting records supply no evidence.
    Exact names preserve Arena's A- rebalanced identities; no fuzzy matching.
    """
    def __init__(self, path=None, *, now=time.time):
        self.path = Path(path or os.environ.get('ARENAONAIR_KNOWLEDGE_DB') or DEFAULT_KNOWLEDGE_PATH).expanduser()
        self.now = now
        self.lock = threading.RLock()
        self.conn = None
        self.signature = None
        self.sources = {}

    def close(self):
        with self.lock:
            if self.conn:
                self.conn.close()
            self.conn = None
            self.signature = None

    def _open(self):
        stat = self.path.stat()
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if signature != self.signature:
            self.close()
            conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, check_same_thread=False)
            try:
                if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError('unsupported knowledge schema')
                self.sources = {k: json.loads(v) for k, v in conn.execute('SELECT name, payload FROM sources')}
            except Exception:
                conn.close()
                raise
            self.conn, self.signature = conn, signature

    def _source(self, name):
        source = self.sources.get(name)
        if not source:
            return None
        # A later download of an old export must not make old claims fresh.
        age = self.now() - min(timestamp(source['updated_at']), timestamp(source['retrieved_at']))
        return source if -86400 <= age <= MAX_AGE else None

    def lookup(self, name, *, rules='', type_line='', public_names=()):
        with self.lock:
            try:
                self._open()
                source = self._source('scryfall')
                if not source:
                    return []
                rows = self.conn.execute('SELECT oracle_id, payload FROM cards WHERE name=?', (name,)).fetchall()
                matches = [(oid, json.loads(raw)) for oid, raw in rows]
                if rules:
                    matches = [(oid, card) for oid, card in matches
                               if ' '.join(rules.split()) == ' '.join(card['rules'].split())]
                if type_line:
                    matches = [(oid, card) for oid, card in matches if card.get('type_line') == type_line]
                if len(matches) != 1:
                    return []
                oracle_id, card = matches[0]
                facts = [dict(card, kind='card_profile', source=source)]
                tags_source = self._source('scryfall_tags')
                if tags_source:
                    tags = [json.loads(r[0]) for r in self.conn.execute(
                        'SELECT t.payload FROM tags t JOIN card_tags c ON c.tag_id=t.id '
                        'WHERE c.oracle_id=? ORDER BY t.id LIMIT 8', (oracle_id,))]
                    if tags:
                        facts.append(dict(kind='card_roles', name=name, tags=tags, source=tags_source,
                                          scope='Community functional classifications; not observed effects or deck intent.'))
                combo_source = self._source('spellbook')
                if combo_source:
                    rows = self.conn.execute(
                        'SELECT c.payload FROM combos c JOIN combo_cards p ON p.combo_id=c.id '
                        'WHERE p.oracle_id=? ORDER BY c.card_count, c.id LIMIT 32', (oracle_id,))
                    combos = [json.loads(r[0]) for r in rows]
                    public = set(public_names) - {name}
                    combos.sort(key=lambda c: -sum(p['name'] in public for p in c['pieces']))
                    for combo in combos[:2]:
                        facts.append(dict(combo, kind='catalog_combo', source=combo_source,
                            scope='Commander combo catalog; theoretical recipe, NOT evidence of a combo available or happening in this match.'))
                return facts
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
                return []
            finally:
                # Windows cannot replace a database while a reader holds it
                # open. Keep the file handle scoped to this lookup so a cache
                # refresh can replace the snapshot while the booth is idle.
                self.close()
