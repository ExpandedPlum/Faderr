"""Numbered schema migrations.

These are the single source of truth for the database schema: a new database
runs every migration from 1, an existing one runs only those it hasn't had.
The applied version lives in the `schema_version` table. Never edit a
migration that has shipped; add a new one instead.

Before migrating an existing SQLite file, a backup copy is written next to it.
"""
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


def _columns(conn: Connection, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: Connection, table: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).first()
    return row is not None


# ── 1: the original schema, including columns added before migrations existed ──

def _m1_initial(conn: Connection) -> None:
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS triage_artists (
            id INTEGER NOT NULL PRIMARY KEY,
            artist_name VARCHAR NOT NULL,
            plex_artist_key VARCHAR NOT NULL,
            thumb_url VARCHAR,
            track_title VARCHAR,
            track_key VARCHAR,
            stream_key VARCHAR,
            source VARCHAR,
            decision VARCHAR,
            decided_at DATETIME,
            lidarr_id INTEGER,
            notes VARCHAR,
            created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
        )
    """)
    existing = _columns(conn, "triage_artists")
    for column in ("stream_key", "thumb_url", "notes"):
        if column not in existing:
            conn.exec_driver_sql(f"ALTER TABLE triage_artists ADD COLUMN {column} VARCHAR")


# ── 2: one row per Plex artist, and only keep/delete decisions ──

# Older versions had an "explore" state ("listening to more", still undecided)
# and "explore_keep"; they map onto the two decisions that remain.
_DECISION_MAP = {"keep": "keep", "delete": "delete", "explore_keep": "keep", "explore_delete": "delete"}


def _pick_keeper(rows: list[dict]) -> dict:
    """Of several rows for one Plex artist, keep the one that matters most:
    a delete (its files are gone), then the latest other decision, then the oldest."""
    def rank(row):
        decision = row["decision"]
        return (
            decision == "delete",
            decision is not None,
            row["decided_at"] or "",
            -row["id"],
        )
    return max(rows, key=rank)


def _m2_one_row_per_artist(conn: Connection) -> None:
    conn.exec_driver_sql("""
        CREATE TABLE triage_artists_new (
            id INTEGER NOT NULL PRIMARY KEY,
            artist_name VARCHAR NOT NULL,
            plex_artist_key VARCHAR NOT NULL UNIQUE,
            mbid VARCHAR,
            thumb_url VARCHAR,
            track_title VARCHAR,
            track_key VARCHAR,
            stream_key VARCHAR,
            source VARCHAR,
            track_pinned BOOLEAN NOT NULL DEFAULT 0,
            decision VARCHAR CHECK (decision IS NULL OR decision IN ('keep', 'delete')),
            decided_at DATETIME,
            lidarr_id INTEGER,
            notes VARCHAR,
            created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
            updated_at DATETIME
        )
    """)
    result = conn.exec_driver_sql("""
        SELECT id, artist_name, plex_artist_key, thumb_url, track_title, track_key, stream_key,
               source, decision, decided_at, lidarr_id, notes, created_at
        FROM triage_artists
    """)
    names = list(result.keys())
    rows = [dict(zip(names, r)) for r in result.fetchall()]

    by_key: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        row["decision"] = _DECISION_MAP.get(row["decision"])
        if row["decision"] is None:
            row["decided_at"] = None
        by_key[row["plex_artist_key"]].append(row)

    dropped = 0
    for key, group in by_key.items():
        keeper = dict(_pick_keeper(group))
        if not keeper["notes"]:
            keeper["notes"] = next((r["notes"] for r in group if r["notes"]), None)
        dropped += len(group) - 1
        conn.exec_driver_sql(
            """INSERT INTO triage_artists_new
               (id, artist_name, plex_artist_key, thumb_url, track_title, track_key, stream_key,
                source, decision, decided_at, lidarr_id, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (keeper["id"], keeper["artist_name"], key, keeper["thumb_url"], keeper["track_title"],
             keeper["track_key"], keeper["stream_key"], keeper["source"], keeper["decision"],
             keeper["decided_at"], keeper["lidarr_id"], keeper["notes"], keeper["created_at"]),
        )
    if dropped:
        logger.info("Migration 2: merged %d duplicate artist rows", dropped)

    conn.exec_driver_sql("DROP TABLE triage_artists")
    conn.exec_driver_sql("ALTER TABLE triage_artists_new RENAME TO triage_artists")
    conn.exec_driver_sql("CREATE INDEX ix_triage_artists_decision ON triage_artists (decision)")


# ── 3: deletion jobs and the shared generation lock ──

def _m3_jobs(conn: Connection) -> None:
    conn.exec_driver_sql("""
        CREATE TABLE deletions (
            id INTEGER NOT NULL PRIMARY KEY,
            artist_id INTEGER,
            artist_name VARCHAR NOT NULL,
            plex_artist_key VARCHAR NOT NULL,
            previous_decision VARCHAR,
            status VARCHAR NOT NULL
                CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
            run_after DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            started_at DATETIME,
            heartbeat_at DATETIME,
            finished_at DATETIME,
            attempts INTEGER NOT NULL DEFAULT 0,
            route VARCHAR,
            lidarr_id INTEGER,
            lidarr_folder VARCHAR,
            match_reason VARCHAR,
            error VARCHAR
        )
    """)
    conn.exec_driver_sql("CREATE INDEX ix_deletions_status_run_after ON deletions (status, run_after)")
    conn.exec_driver_sql("CREATE INDEX ix_deletions_artist ON deletions (artist_id)")
    # At most one unfinished job per artist, enforced by the database
    conn.exec_driver_sql("""
        CREATE UNIQUE INDEX ux_deletions_active_artist ON deletions (artist_id)
        WHERE status IN ('pending', 'running', 'failed')
    """)
    conn.exec_driver_sql("""
        CREATE TABLE generation_state (
            id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
            owner VARCHAR,
            started_at DATETIME,
            heartbeat_at DATETIME,
            finished_at DATETIME,
            progress TEXT,
            result TEXT
        )
    """)
    conn.exec_driver_sql("INSERT INTO generation_state (id) VALUES (1)")


# ── 4: settings configured in the web UI ──

def _m4_settings(conn: Connection) -> None:
    conn.exec_driver_sql("""
        CREATE TABLE settings (
            key VARCHAR NOT NULL PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
        )
    """)


MIGRATIONS: list[Callable[[Connection], None]] = [
    _m1_initial,
    _m2_one_row_per_artist,
    _m3_jobs,
    _m4_settings,
]
LATEST = len(MIGRATIONS)


def _get_version(conn: Connection) -> int:
    conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.exec_driver_sql("SELECT version FROM schema_version").first()
    if row is None:
        conn.exec_driver_sql("INSERT INTO schema_version (version) VALUES (0)")
        return 0
    return row[0]


def _apply(conn: Connection) -> None:
    version = _get_version(conn)
    if version > LATEST:
        raise RuntimeError(
            f"The database is at schema version {version}, newer than this Faderr ({LATEST}). "
            "Upgrade Faderr, or restore a backup."
        )
    for number in range(version + 1, LATEST + 1):
        logger.info("Applying database migration %d", number)
        MIGRATIONS[number - 1](conn)
        conn.exec_driver_sql("UPDATE schema_version SET version = ?", (number,))


def _sqlite_file(engine: AsyncEngine) -> Optional[Path]:
    url = engine.url
    if not url.get_backend_name() == "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def _backup_if_needed(path: Path) -> Optional[Path]:
    """Copy an existing database before it is migrated, so an upgrade can be undone."""
    if not path.exists():
        return None
    src = sqlite3.connect(path)
    try:
        has_data = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='triage_artists'"
        ).fetchone()
        if not has_data:
            return None
        try:
            version = src.execute("SELECT version FROM schema_version").fetchone()[0]
        except sqlite3.OperationalError:
            version = 0
        if version >= LATEST:
            return None
        backup = path.with_name(f"{path.name}.pre-v{LATEST}.bak")
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.pre-v{LATEST}.{n}.bak")
            n += 1
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
        logger.info("Backed up the database to %s before migrating", backup)
        return backup
    finally:
        src.close()


async def migrate(engine: AsyncEngine) -> None:
    path = _sqlite_file(engine)
    if path is not None:
        _backup_if_needed(path)
    # One transaction: a failed migration leaves the database as it was
    async with engine.begin() as conn:
        await conn.run_sync(_apply)
