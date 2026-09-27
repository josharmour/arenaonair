"""Tests for arenaonair.carddb.

Synthetic-SQLite tests always run; the fixture-validation test skips cleanly
when the locally built cache (~/.cache/arenaonair/cards.sqlite) is absent so
CI without a cache stays green.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from arenaonair.carddb import DEFAULT_DB_PATH, CardDb, CardInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    arena_id   INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    type_line  TEXT NOT NULL,
    mana_cost  TEXT NOT NULL,
    card_types TEXT NOT NULL
);
"""


@pytest.fixture()
def synth_db(tmp_path: Path) -> Path:
    db = tmp_path / "cards.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(SCHEMA)
    rows = [
        (75553, "Lightning Bolt", "Instant", "{R}", "Instant"),
        (81286, "Tasha, the Witch Queen", "Legendary Planeswalker — Tasha", "{1}{U}{B}",
         "Legendary,Planeswalker,Tasha"),
        (9135, "Forest", "Basic Land — Forest", "", "Land,Forest"),
        (103511, "Delney, Streetwise Lookout", "Legendary Creature — Human Soldier",
         "{2}{W}{W}", "Legendary,Creature,Human,Soldier"),
    ]
    conn.executemany("INSERT INTO cards VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


class TestLookupHit:
    def test_hit_returns_cardinfo(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        info = db.lookup(75553)
        assert info == CardInfo(
            name="Lightning Bolt",
            type_line="Instant",
            mana_cost="{R}",
            card_types=("Instant",),
        )

    def test_multi_type_csv_parsed(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        info = db.lookup(81286)
        assert info is not None
        assert info.name == "Tasha, the Witch Queen"
        assert info.card_types == ("Legendary", "Planeswalker", "Tasha")
        assert info.mana_cost == "{1}{U}{B}"

    def test_empty_mana_cost_land(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        info = db.lookup(9135)
        assert info is not None
        assert info.mana_cost == ""

    def test_frozen_dataclass(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        info = db.lookup(75553)
        assert info is not None
        with pytest.raises(Exception):
            info.name = "x"  # type: ignore[misc]


class TestLookupMiss:
    def test_unknown_id_returns_none(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        assert db.lookup(999999999) is None

    def test_missing_repeated_does_not_spam(self, synth_db: Path, caplog) -> None:
        db = CardDb(synth_db)
        for _ in range(5):
            assert db.lookup(424242) is None
        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        missing_logs = [r for r in debug_records if "424242" in r.getMessage()]
        assert len(missing_logs) <= 1

    def test_bad_type_returns_none(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        assert db.lookup(None) is None  # type: ignore[arg-type]
        assert db.lookup("75553") is None  # type: ignore[arg-type]


class TestDbAbsent:
    def test_absent_file_returns_none(self, tmp_path: Path) -> None:
        db = CardDb(tmp_path / "nope.sqlite")
        assert db.lookup(75553) is None
        assert db.counts() == (0, 0)

    def test_default_path_constant_outside_repo(self) -> None:
        assert str(DEFAULT_DB_PATH).startswith(str(Path.home()))
        assert "arenaonair/cards.sqlite" in DEFAULT_DB_PATH.as_posix()


class TestCounts:
    def test_counts(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        total, resolved = db.counts()
        assert total == 4
        assert resolved >= 1  # some probe ids resolve against synthetic data


class TestThreadSafety:
    def test_concurrent_lookups(self, synth_db: Path) -> None:
        db = CardDb(synth_db)
        errors: list[Exception] = []
        hits: list[int] = []

        def worker() -> None:
            try:
                for _ in range(200):
                    info = db.lookup(75553)
                    if info is not None and info.name == "Lightning Bolt":
                        hits.append(1)
                    assert db.lookup(-1) is None
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        assert len(hits) == 1600


# --------------------------------------------------------------------------
# Fixture validation against the real built cache (skips if cache absent)
# --------------------------------------------------------------------------

FIXTURE_MATCHES = Path(__file__).resolve().parents[1] / "fixtures" / "matches" / "match_01.jsonl"


def _fixture_battlefield_grpids(limit: int = 30) -> list[int]:
    """Collect distinct grpIds from battlefield-zone gameObjects in the replay."""
    grpids: list[int] = []
    seen: set[int] = set()
    with open(FIXTURE_MATCHES, encoding="utf-8") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            def walk(node: object) -> None:
                if isinstance(node, dict):
                    if (
                        node.get("type") == "GameObjectType_Card"
                        and isinstance(node.get("grpId"), int)
                        and node.get("zoneId") in (27, 28, 30, 35, 36, 37)
                    ):
                        gid = node["grpId"]
                        if gid not in seen and len(seen) < limit:
                            seen.add(gid)
                            grpids.append(gid)
                    for value in node.values():
                        walk(value)
                elif isinstance(node, list):
                    for value in node:
                        walk(value)

            walk(obj)
            if len(seen) >= limit:
                break
    return grpids


@pytest.mark.skipif(
    not DEFAULT_DB_PATH.exists(),
    reason=f"card cache not built yet at {DEFAULT_DB_PATH} (run python -m tools.build_carddb)",
)
class TestFixtureResolution:
    def test_fixture_grpids_resolve(self) -> None:
        grpids = _fixture_battlefield_grpids()
        assert len(grpids) >= 30, f"only found {len(grpids)} distinct battlefield grpIds"
        db = CardDb(DEFAULT_DB_PATH)
        resolved = [gid for gid in grpids if db.lookup(gid) is not None]
        ratio = len(resolved) / len(grpids)
        unresolved = [gid for gid in grpids if gid not in set(resolved)]
        print(f"\nresolved {len(resolved)}/{len(grpids)} ({ratio:.0%}); unresolved={unresolved}")
        assert ratio >= 0.80

    def test_resolved_entries_have_names(self) -> None:
        grpids = _fixture_battlefield_grpids()
        db = CardDb(DEFAULT_DB_PATH)
        infos = [db.lookup(gid) for gid in grpids]
        for info in infos:
            if info is not None:
                assert isinstance(info.name, str) and info.name
                assert isinstance(info.type_line, str)


@pytest.fixture()
def arena_db(tmp_path):
    path = tmp_path / 'Raw_CardDatabase_fixture.mtga'
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE Cards (GrpId INT, TitleId INT, TypeTextId INT, SubtypeTextId INT, OldSchoolManaText TEXT);
        CREATE TABLE Localizations_enUS (LocId INT, Formatted INT, Loc TEXT);
        INSERT INTO Cards VALUES (93987, 21339, 16, 0, 'o1oBoB');
        INSERT INTO Localizations_enUS VALUES (21339, 0, 'Grim Tutor');
        INSERT INTO Localizations_enUS VALUES (16, 0, 'Sorcery');
    ''')
    conn.commit()
    conn.close()
    return path


def test_missing_printing_uses_read_only_arena_database(synth_db, arena_db):
    db = CardDb(synth_db, arena_db_path=arena_db)
    try:
        assert db.lookup(93987) == CardInfo('Grim Tutor', 'Sorcery', '{1}{B}{B}', ('Sorcery',))
        assert db.as_resolver()(93987) == 'Grim Tutor'
        assert db.lookup(75553).name == 'Lightning Bolt'  # cache hit retained
        assert db.lookup(123456789) is None
        with pytest.raises(sqlite3.OperationalError):
            db._arena_conn.execute('DELETE FROM Cards')
    finally:
        db.close()
    assert db._arena_conn is None


def test_arena_lookup_works_without_external_cache(tmp_path, arena_db):
    db = CardDb(tmp_path / 'absent.sqlite', arena_db_path=arena_db)
    assert db.lookup(93987).name == 'Grim Tutor'
    db.close()


def test_incompatible_arena_database_keeps_normal_cache(synth_db, tmp_path):
    wrong = tmp_path / 'broken.mtga'
    wrong.write_text('not sqlite')
    db = CardDb(synth_db, arena_db_path=wrong)
    assert db.lookup(75553).name == 'Lightning Bolt'
    assert db.lookup(93987) is None
    db.close()


def test_rules_cache_build_from_local_bulk_preserves_order_and_unknown_rules(tmp_path, monkeypatch):
    from tools import build_carddb
    source = tmp_path / 'cards.json'
    source.write_text(json.dumps([
        {'arena_id': 1, 'name': 'Grim Tutor', 'type_line': 'Sorcery', 'mana_cost': '{1}{B}{B}',
         'oracle_text': 'Search your library for a card, put that card into your hand, then shuffle. You lose 3 life.'},
        {'arena_id': 2, 'name': 'Unknown rules', 'type_line': 'Creature'},
    ]))
    monkeypatch.setattr(build_carddb, 'fetch_bulk_index', lambda: pytest.fail('must use local source'))
    target = tmp_path / 'cards.sqlite'
    assert build_carddb.build(target, source) == 2
    db = CardDb(target)
    try:
        assert db.lookup(1).oracle_text.endswith('You lose 3 life.')
        assert 'then shuffle' in db.lookup(1).oracle_text
        assert db.lookup(2).oracle_text == ''
    finally:
        db.close()
