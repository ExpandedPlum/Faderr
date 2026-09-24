import asyncio
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import migrations
import models

LEGACY_SCHEMA = """CREATE TABLE triage_artists (
    id INTEGER NOT NULL PRIMARY KEY, artist_name VARCHAR NOT NULL, plex_artist_key VARCHAR NOT NULL,
    thumb_url VARCHAR, track_title VARCHAR, track_key VARCHAR, source VARCHAR, decision VARCHAR,
    decided_at DATETIME, lidarr_id INTEGER, created_at DATETIME DEFAULT (CURRENT_TIMESTAMP))"""


def migrate(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    models.configure_sqlite(engine)

    async def go():
        await migrations.migrate(engine)
        await engine.dispose()
    asyncio.run(go())


def make_legacy(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO triage_artists (id, artist_name, plex_artist_key, decision, decided_at, track_key) "
        "VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_fresh_database_gets_latest_schema(tmp_path):
    db = tmp_path / "new.db"
    migrate(db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == migrations.LATEST
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"triage_artists", "deletions", "generation_state", "schema_version"} <= tables
    assert list(tmp_path.iterdir()) == [db]  # nothing to back up


def test_legacy_database_is_migrated_deduplicated_and_backed_up(tmp_path):
    db = tmp_path / "triage.db"
    make_legacy(db, [
        (1, "A", "10", None, None, "t1"),
        (2, "A", "10", "keep", "2026-01-02", "t2"),       # duplicate: the decided row wins
        (3, "B", "20", "explore", "2026-01-01", "t3"),    # listening more = still undecided
        (4, "C", "30", "explore_keep", "2026-01-01", "t4"),
        (5, "D", "40", None, None, "t5"),
        (6, "D", "40", "delete", "2026-01-03", "t6"),     # a delete always wins
    ])
    migrate(db)
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id, plex_artist_key, decision, decided_at FROM triage_artists ORDER BY id").fetchall()
    assert rows == [(2, "10", "keep", "2026-01-02"), (3, "20", None, None),
                    (4, "30", "keep", "2026-01-01"), (6, "40", "delete", "2026-01-03")]
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == migrations.LATEST
    backups = sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".bak"))
    assert backups == [f"triage.db.pre-v{migrations.LATEST}.bak"]
    old = sqlite3.connect(tmp_path / backups[0])
    assert old.execute("SELECT COUNT(*) FROM triage_artists").fetchone()[0] == 6


def test_constraints_enforced_after_migration(tmp_path):
    db = tmp_path / "triage.db"
    make_legacy(db, [(1, "A", "10", None, None, "t1")])
    migrate(db)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO triage_artists (artist_name, plex_artist_key) VALUES ('Z', '10')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("UPDATE triage_artists SET decision = 'explore'")


def test_migrating_twice_is_a_no_op(tmp_path):
    db = tmp_path / "triage.db"
    make_legacy(db, [(1, "A", "10", "keep", "2026-01-01", "t1")])
    migrate(db)
    migrate(db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM triage_artists").fetchone()[0] == 1
    assert len([p for p in tmp_path.iterdir() if p.name.endswith(".bak")]) == 1


def test_failed_migration_changes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "triage.db"
    make_legacy(db, [(1, "A", "10", "keep", "2026-01-01", "t1")])

    def broken(conn):
        conn.exec_driver_sql("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("boom")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:2] + [broken])
    with pytest.raises(RuntimeError, match="boom"):
        migrate(db)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"triage_artists"}  # rolled back, including migrations 1 and 2
    assert "plex_artist_key" in {r[1] for r in conn.execute("PRAGMA table_info(triage_artists)")}


def test_newer_database_is_refused(tmp_path):
    db = tmp_path / "triage.db"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        migrate(db)
