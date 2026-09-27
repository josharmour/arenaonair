"""Source identity, freshness, offline lookup and complete recipe contracts."""
from datetime import datetime, timezone
import gzip
import io
import json
import sqlite3
import threading
import urllib.request

import pytest

from arenaonair.card_knowledge import CardKnowledge, MAX_AGE
from arenaonair.carddb import CardInfo
from arenaonair.llm_booth import ModelError, validate_output
from tools.build_card_knowledge import build, iter_variants

NOW = 1_790_000_000
DATE = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


@pytest.fixture
def exported(tmp_path):
    def write(name, obj):
        with gzip.open(tmp_path / (name + '.json.gz'), 'wt') as f:
            json.dump(obj, f)
    cards = [dict(oracle_id='opt', name='Opt', oracle_text='Scry 1. Draw a card.', type_line='Instant',
                  scryfall_uri='https://scryfall.com/card/test/opt', keywords=['Scry'],
                  edhrec_rank=123, game_changer=False),
             dict(oracle_id='a-opt', name='A-Opt', oracle_text='Draw a card.', type_line='Instant',
                  scryfall_uri='https://scryfall.com/card/test/a-opt', keywords=[])]
    write('oracle_cards', cards)
    write('oracle_tags', [dict(id='cantrip', type='oracle', label='cantrip',
        description='Cards that draw a card.', uri='https://tagger.scryfall.com/tags/card/cantrip',
        taggings=[dict(oracle_id='opt')])])
    # Synthetic recipe deliberately has additional requirements, so tests catch
    # any projection which drops conditions and invents a two-card combo.
    piece = lambda oid, name: dict(card=dict(oracleId=oid, name=name), quantity=1,
        zoneLocations=['B'], battlefieldCardState='Untapped', mustBeCommander=True)
    combo = dict(id='test', status='OK', spoiler=False, uses=[piece('opt', 'Opt'), piece('partner', 'Partner')],
        requires=[dict(template=dict(name='Extra permanent', scryfallQuery='is:permanent'), quantity=2, zoneLocations=['H'])],
        produces=[dict(feature=dict(name='Infinite test counters'), quantity=1)],
        manaNeeded='{G}', easyPrerequisites='At least 10 life.', notablePrerequisites='A token entered this turn.',
        description='Pay the cost. Repeat the actions.', notes='Conditional fixture only.', legalities=dict(commander=True, brawl=False))
    write('spellbook', dict(timestamp=DATE, version='test', variants=[combo], aliases=[]))
    (tmp_path / 'index.json').write_text(json.dumps(dict(data=[
        dict(type=k, updated_at=DATE) for k in ('oracle_cards', 'oracle_tags')])))
    path = tmp_path / 'knowledge.sqlite'
    assert build(path, tmp_path) == dict(cards=2, tags=1, card_tags=1, combos=1)
    return path, tmp_path, combo


def test_local_sources_and_complete_combo_requirements(exported, monkeypatch):
    path, _, original = exported
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('runtime network lookup'))
    cache = CardKnowledge(path, now=lambda: NOW)
    facts = cache.lookup('Opt', rules='Scry 1. Draw a card.')
    assert [f['kind'] for f in facts] == ['card_profile', 'card_roles', 'catalog_combo']
    assert facts[0]['commander_context']['edhrec_rank'] == 123
    assert facts[1]['tags'][0]['label'] == 'cantrip'
    combo = facts[2]
    assert combo['pieces'][0]['mustBeCommander'] is True
    assert combo['pieces'][0]['zoneLocations'] == ['B']
    assert combo['pieces'][0]['battlefieldCardState'] == 'Untapped'
    assert combo['additional_requirements'][0]['quantity'] == 2
    assert combo['mana_needed'] == original['manaNeeded']
    assert combo['easy_prerequisites'] == original['easyPrerequisites']
    assert combo['notable_prerequisites'] == original['notablePrerequisites']
    assert combo['steps'] == original['description']
    assert combo['legalities']['brawl'] is False
    assert all(f['source']['updated_at'] == DATE for f in facts)
    cache.close()


def test_exact_arena_identity_and_conflicting_rules(exported):
    cache = CardKnowledge(exported[0], now=lambda: NOW)
    assert cache.lookup('opt') == []
    assert cache.lookup('Opt', rules='Wrong rules') == []
    a = cache.lookup('A-Opt')
    assert len(a) == 1 and a[0]['oracle_id'] == 'a-opt'
    assert a[0]['commander_context']['edhrec_rank'] is None
    with sqlite3.connect(exported[0]) as conn:
        payload = json.dumps(dict(name='Opt', rules='Other rules'))
        conn.execute('INSERT INTO cards VALUES (?,?,?)', ('ambiguous', 'Opt', payload))
    assert cache.lookup('Opt') == []
    assert cache.lookup('Opt', rules='Scry 1. Draw a card.')[0]['oracle_id'] == 'opt'
    cache.close()


def test_missing_corrupt_and_stale_data_fail_quietly(exported, tmp_path):
    assert CardKnowledge(tmp_path / 'missing').lookup('Opt') == []
    corrupt = tmp_path / 'corrupt'; corrupt.write_bytes(b'not sqlite')
    assert CardKnowledge(corrupt).lookup('Opt') == []
    assert CardKnowledge(exported[0], now=lambda: NOW + MAX_AGE + 1).lookup('Opt') == []
    with sqlite3.connect(exported[0]) as conn:
        conn.execute("UPDATE sources SET payload='[]'")
    assert CardKnowledge(exported[0], now=lambda: NOW).lookup('Opt') == []


def test_failed_refresh_preserves_cache_and_success_reopens(exported):
    path, directory, _ = exported
    cache = CardKnowledge(path, now=lambda: NOW)
    assert cache.lookup('Opt')[0]['commander_context']['edhrec_rank'] == 123
    before = path.read_bytes()
    with gzip.open(directory / 'spellbook.json.gz', 'wt') as f:
        f.write('{"timestamp": "' + DATE + '", "variants": [{"broken":')
    with pytest.raises(ValueError):
        build(path, directory)
    assert path.read_bytes() == before
    # Replacement snapshots are picked up without restarting the booth.
    replacement = directory / 'new.sqlite'
    replacement.write_bytes(before)
    with sqlite3.connect(replacement) as conn:
        p = json.loads(conn.execute("SELECT payload FROM cards WHERE oracle_id='opt'").fetchone()[0])
        p['commander_context']['edhrec_rank'] = 42
        conn.execute("UPDATE cards SET payload=? WHERE oracle_id='opt'", (json.dumps(p),))
    # A SQLite context commits the transaction but does not close the handle.
    conn.close()
    replacement.replace(path)
    assert cache.lookup('Opt')[0]['commander_context']['edhrec_rank'] == 42
    cache.close()


@pytest.mark.parametrize('payload', ['{"variants":[{"id":1}', '{"variants":[{"id":1},]}', '{"variants":[1]}', '{"variants":[]'])
def test_incomplete_or_invalid_combo_export_rejected(payload):
    with pytest.raises(ValueError):
        list(iter_variants(io.StringIO(payload), {}))


def test_knowledge_attached_on_worker_without_ingestion_lock(exported):
    from test_llm_booth import setup_booth, cast, snapshot
    b, _, _, writer, _ = setup_booth()
    class Cards:
        def lookup(self, gid):
            return CardInfo('Opt', 'Instant', '{U}', ('Instant',), 'Scry 1. Draw a card.')
    b.carddb = Cards()
    cache = CardKnowledge(exported[0], now=lambda: NOW)
    class CheckedCache:
        def lookup(self, *args, **kwargs):
            # A different thread can obtain the ingestion lock during disk I/O.
            acquired = []
            def check():
                ok = b.lock.acquire(timeout=.2)
                acquired.append(ok)
                if ok: b.lock.release()
            thread = threading.Thread(target=check); thread.start(); thread.join()
            assert acquired == [True]
            return cache.lookup(*args, **kwargs)
    b.knowledge = CheckedCache()
    b.submit(cast(), snapshot())
    work = b.take_work()
    assert not any(k.startswith('knowledge:') for k in work.context['facts'])
    b.process(work)
    assert any(f.get('kind') == 'catalog_combo' for f in writer.contexts[0]['facts'].values())
    assert b.counts['knowledge_facts'] == 3
    cache.close()


def test_oversize_recipes_omitted_intact():
    from test_llm_booth import setup_booth
    b, *_ = setup_booth()
    class Knowledge:
        def lookup(self, *a, **k):
            return [dict(kind='catalog_combo', steps='x' * 7000)]
    b.knowledge = Knowledge()
    context = dict(facts={'card:1': dict(name='Opt', rules='')})
    b._enrich(context)
    assert list(context['facts']) == ['card:1']


def test_type_line_disambiguates_token_with_identical_name_and_rules(exported):
    path, _, _ = exported
    with sqlite3.connect(path) as conn:
        card = json.loads(conn.execute("SELECT payload FROM cards WHERE oracle_id='opt'").fetchone()[0])
        card.update(oracle_id='token', type_line='Token Instant')
        conn.execute('INSERT INTO cards VALUES (?,?,?)', ('token', 'Opt', json.dumps(card)))
    cache = CardKnowledge(path, now=lambda: NOW)
    assert not cache.lookup('Opt', rules='Scry 1. Draw a card.')
    assert cache.lookup('Opt', rules='Scry 1. Draw a card.', type_line='Instant')[0]['oracle_id'] == 'opt'
    cache.close()


def test_sourced_commander_designation_allowed_and_combo_partners_required():
    context = dict(facts={'event:1': dict(kind='cast'), 'knowledge:1': dict(commander_context=dict(game_changer=True))},
                   new_events=['event:1'], heard=[], earlier_points=[])
    output = dict(turns=[dict(role='color_analyst', text='Demonic Tutor is on the Commander Game Changer list.', refs=list(context['facts']))])
    assert validate_output(output, context)
    context['facts']['knowledge:1'] = dict(kind='catalog_combo', pieces=[dict(name='Scurry Oak'), dict(name='Partner')])
    output['turns'][0]['text'] = 'Scurry Oak can make infinite tokens.'
    with pytest.raises(ModelError, match='unsupported_meta'):
        validate_output(output, context)


@pytest.mark.parametrize('text', [
    'Opt is a game changer.', 'Opt is a Commander Game Changer.',
    'Opt has a high Arena win rate.', 'Opt creates infinite mana.',
    'Opt is popular in Standard.',
])
def test_unsupported_meta_claims_rejected(exported, text):
    cache = CardKnowledge(exported[0], now=lambda: NOW)
    context = dict(facts={'event:1': dict(kind='cast', name='Opt'), 'knowledge:1': cache.lookup('Opt')[0]},
                   new_events=['event:1'], heard=[], earlier_points=[])
    output = dict(turns=[dict(role='color_analyst', text=text, refs=list(context['facts']))])
    with pytest.raises(ModelError, match='unsupported_meta'):
        validate_output(output, context)
    cache.close()
