"""Build an atomic local card-context cache from official bulk exports.

Run: PYTHONPATH=src:. python -m tools.build_card_knowledge
Use --from-dir for saved oracle_cards.json.gz, oracle_tags.json.gz,
spellbook.json.gz and index.json (Scryfall's bulk-data index).
No per-card API calls or EDHREC scraping. Existing cache survives failed builds.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import urllib.request

from arenaonair.card_knowledge import DEFAULT_KNOWLEDGE_PATH, SCHEMA_VERSION, rules_text, timestamp
from tools.build_carddb import fetch_bulk_index, iter_json_objects, _headers, _open_with_retry

SPELLBOOK_URL = 'https://json.commanderspellbook.com/variants.json.gz'
SCHEMA = '''
CREATE TABLE sources (name TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE cards (oracle_id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX cards_name ON cards(name);
CREATE TABLE tags (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE card_tags (oracle_id TEXT, tag_id TEXT, PRIMARY KEY(oracle_id, tag_id));
CREATE TABLE combos (id TEXT PRIMARY KEY, card_count INTEGER, payload TEXT NOT NULL);
CREATE TABLE combo_cards (oracle_id TEXT, combo_id TEXT, PRIMARY KEY(oracle_id, combo_id));
'''


def iter_variants(stream, metadata):
    """Stream the variants array in Spellbook's envelope, bounded to one record.

    Preserve its export timestamp. Unlike json.load, memory doesn't grow with
    the entire catalog (hundreds of MB of repeated images and card metadata).
    """
    buf = stream.read(65536)
    match = re.search(r'"variants"\s*:\s*\[', buf)
    if not match:
        raise ValueError('missing Spellbook variants array')
    metadata.update(json.loads(buf[:match.start()] + '"variants": []}'))
    buf = buf[match.end():]
    decoder = json.JSONDecoder()
    need_value = True
    while True:
        buf = buf.lstrip()
        if buf.startswith(']'):
            if need_value and metadata.get('_seen'):
                raise ValueError('trailing comma in variants')
            metadata.pop('_seen', None)
            # Consume the remaining envelope to validate JSON and gzip CRC;
            # aliases are irrelevant to evidence, but truncation is an error.
            tail = (buf[1:] + stream.read(16_000_001)).strip()
            if len(tail) > 16_000_000:
                raise ValueError('Spellbook envelope tail exceeds size limit')
            json.loads('{' + (tail[1:] if tail.startswith(',') else tail))
            return
        if not need_value and buf:
            if not buf.startswith(','):
                raise ValueError('invalid variant separator')
            buf, need_value = buf[1:].lstrip(), True
        if need_value and buf:
            try:
                obj, end = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                pass
            else:
                if not isinstance(obj, dict):
                    raise ValueError('invalid variant')
                yield obj
                metadata['_seen'] = True
                buf, need_value = buf[end:], False
                continue
        if len(buf) > 2_000_000:
            raise ValueError('variant exceeds size limit')
        chunk = stream.read(65536)
        if not chunk:
            raise ValueError('truncated Spellbook export')
        buf += chunk


def combo_record(v):
    if v.get('status') != 'OK' or v.get('spoiler'):
        return None
    # Keep every prerequisite, zone, quantity, commander requirement and step.
    # Never trim a recipe: that could turn a conditional combo into a false one.
    def piece(p, template=False):
        item = dict(p)
        identity = item.pop('template' if template else 'card')
        item.update(name=identity['name'])
        if template:
            item['scryfall_query'] = identity.get('scryfallQuery')
        else:
            item['oracle_id'] = identity['oracleId']
        return item
    result = dict(id=v['id'], source_url='https://commanderspellbook.com/combo/' + v['id'] + '/',
        pieces=[piece(p) for p in v['uses']], additional_requirements=[piece(p, True) for p in v['requires']],
        results=[{'name': p['feature']['name'], 'quantity': p['quantity']} for p in v['produces']],
        mana_needed=v['manaNeeded'], easy_prerequisites=v['easyPrerequisites'],
        notable_prerequisites=v['notablePrerequisites'], steps=v['description'], notes=v.get('notes', ''),
        legalities=v['legalities'])
    return result if result['pieces'] and result['results'] and result['steps'] else None


def build(out_path, directory):
    out_path, directory = Path(out_path), Path(directory)
    index = json.loads((directory / 'index.json').read_text())
    entries = {e['type']: e for e in (index['data'] if isinstance(index, dict) else index)}
    retrieved = datetime.now(timezone.utc).isoformat()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_path.parent, prefix=out_path.name + '.') as temp:
        stage = Path(temp) / 'knowledge.sqlite'
        conn = sqlite3.connect(stage)
        try:
            conn.executescript(SCHEMA)
            conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            def source(key, label, updated, url):
                timestamp(updated)  # Reject missing/unparseable provenance.
                conn.execute('INSERT INTO sources VALUES (?,?)', (key, json.dumps(dict(
                    name=label, updated_at=updated, retrieved_at=retrieved, url=url))))
            source('scryfall', 'Scryfall (EDHREC rank supplied by Scryfall)', entries['oracle_cards']['updated_at'],
                   'https://scryfall.com/docs/api/bulk-data')
            source('scryfall_tags', 'Scryfall Tagger', entries['oracle_tags']['updated_at'],
                   'https://scryfall.com/docs/api/bulk-data')
            with gzip.open(directory / 'oracle_cards.json.gz', 'rb') as stream:
                for c in iter_json_objects(stream):
                    if not c.get('oracle_id') or not c.get('name'):
                        continue
                    card = dict(name=c['name'], oracle_id=c['oracle_id'], rules=rules_text(c), type_line=c.get('type_line', ''),
                        source_url=c['scryfall_uri'], keywords=c.get('keywords', []),
                        commander_context=dict(format='Commander', edhrec_rank=c.get('edhrec_rank'),
                            game_changer=c.get('game_changer'),
                            meaning='EDHREC popularity rank, not win rate; Game Changer is a Commander list designation, not a match outcome.'),
                        legalities=c.get('legalities', {}))
                    conn.execute('INSERT INTO cards VALUES (?,?,?)', (c['oracle_id'], c['name'], json.dumps(card)))
            with gzip.open(directory / 'oracle_tags.json.gz', 'rb') as stream:
                for tag in iter_json_objects(stream):
                    if tag.get('type') != 'oracle':
                        continue
                    data = dict(label=tag['label'], description=tag.get('description'), source_url=tag['uri'])
                    conn.execute('INSERT INTO tags VALUES (?,?)', (tag['id'], json.dumps(data)))
                    conn.executemany('INSERT OR IGNORE INTO card_tags VALUES (?,?)',
                                     ((t['oracle_id'], tag['id']) for t in tag['taggings']))
            meta = {}
            with gzip.open(directory / 'spellbook.json.gz', 'rt') as stream:
                for variant in iter_variants(stream, meta):
                    c = combo_record(variant)
                    if c:
                        conn.execute('INSERT INTO combos VALUES (?,?,?)', (c['id'], len(c['pieces']), json.dumps(c)))
                        conn.executemany('INSERT OR IGNORE INTO combo_cards VALUES (?,?)',
                                         ((p['oracle_id'], c['id']) for p in c['pieces']))
            source('spellbook', 'Commander Spellbook', meta['timestamp'], SPELLBOOK_URL)
            counts = {t: conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                      for t in ('cards', 'tags', 'card_tags', 'combos')}
            if not all(counts.values()):
                raise ValueError('empty bulk dataset; preserving existing cache')
            conn.commit()
            if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('invalid built database')
        finally:
            conn.close()
        stage.replace(out_path)
    return counts


def download(directory):
    entries = fetch_bulk_index()
    (directory / 'index.json').write_text(json.dumps(entries))
    urls = {}
    for kind in ('oracle_cards', 'oracle_tags'):
        entry = next(e for e in entries if e['type'] == kind)
        urls[kind] = entry.get('jsonl_download_uri') or entry['download_uri']
    urls['spellbook'] = SPELLBOOK_URL
    for name, url in urls.items():
        print('Downloading', name, flush=True)
        with _open_with_retry(urllib.request.Request(url, headers=_headers()), stream=True) as response:
            # Store consistently gzipped snapshots, including older plain JSON exports.
            with ExitStack() as stack:
                stream = gzip.GzipFile(fileobj=response) if url.endswith('.gz') or response.headers.get('Content-Encoding') == 'gzip' else response
                if stream is not response:
                    stack.enter_context(stream)
                out = stack.enter_context(gzip.open(directory / (name + '.json.gz'), 'wb'))
                while chunk := stream.read(1024 * 1024):
                    out.write(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    parser.add_argument('--from-dir', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='arenaonair-knowledge-') as temp:
        directory = args.from_dir or Path(temp)
        if not args.from_dir:
            download(directory)
        print(json.dumps(build(args.out.expanduser(), directory)), flush=True)


if __name__ == '__main__':
    main()
