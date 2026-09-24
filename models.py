"""Database models.

The schema itself is created and evolved by the numbered migrations in
`migrations.py`; these classes only map it. Keep the two in step.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, event
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

import migrations
from config import config

logger = logging.getLogger(__name__)



def configure_sqlite(engine: AsyncEngine) -> None:
    """Make SQLite transactions real and safe to share between processes.

    Python's sqlite driver doesn't start a transaction for DDL or reads, so
    migrations wouldn't be atomic and "read, then conditionally update" logic
    could interleave. Following SQLAlchemy's recipe for aiosqlite, every
    transaction begins with BEGIN IMMEDIATE, which takes the write lock up
    front: concurrent writers (other requests, the deletion worker, another
    server process) queue for up to 30s instead of failing.
    """
    if engine.url.get_backend_name() != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None  # we issue BEGIN ourselves
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 30000")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


engine = create_async_engine(config.DATABASE_URL, echo=False)
configure_sqlite(engine)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Decisions an artist can have. NULL means undecided.
DECISIONS = ("keep", "delete")

# Deletion job states. "pending" jobs wait for their grace period; "failed"
# jobs wait for the user to retry or cancel. Both still hold the artist.
DELETION_ACTIVE = ("pending", "running", "failed")
DELETION_STATUSES = ("pending", "running", "done", "failed", "cancelled")


def utcnow() -> datetime:
    """Current UTC time as a naive datetime, which is how SQLite stores it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """Serialize a stored (naive UTC) datetime so browsers read it as UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class TriageArtist(Base):
    __tablename__ = "triage_artists"

    id = Column(Integer, primary_key=True)
    artist_name = Column(String, nullable=False)
    plex_artist_key = Column(String, nullable=False, unique=True)
    mbid = Column(String)  # MusicBrainz artist ID reported by Plex, if any
    thumb_url = Column(String)  # Plex path; served through the app's proxy
    track_title = Column(String)
    track_key = Column(String)
    stream_key = Column(String)
    source = Column(String)  # "lastfm" or "plex_random"
    track_pinned = Column(Boolean, nullable=False, default=False)  # chosen with Skip; kept on regenerate
    decision = Column(String)  # NULL, "keep" or "delete"
    decided_at = Column(DateTime)
    lidarr_id = Column(Integer)
    notes = Column(String)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self, deletion: Optional["Deletion"] = None):
        return {
            "id": self.id,
            "artist_name": self.artist_name,
            "plex_artist_key": self.plex_artist_key,
            # Served through the app's proxy so the Plex token never reaches the browser
            "thumb": f"/api/artists/{self.id}/thumb" if self.thumb_url else None,
            "track_title": self.track_title,
            "track_key": self.track_key,
            "stream_key": self.stream_key,
            "source": self.source,
            "decision": self.decision,
            "decided_at": iso_utc(self.decided_at),
            "lidarr_id": self.lidarr_id,
            "notes": self.notes,
            "created_at": iso_utc(self.created_at),
            "deletion": deletion.to_dict() if deletion else None,
        }


class Deletion(Base):
    """One request to delete an artist, and the record of what happened."""
    __tablename__ = "deletions"

    id = Column(Integer, primary_key=True)
    artist_id = Column(Integer)
    artist_name = Column(String, nullable=False)
    plex_artist_key = Column(String, nullable=False)
    previous_decision = Column(String)  # restored if the delete is cancelled
    status = Column(String, nullable=False)
    run_after = Column(DateTime, nullable=False)  # end of the grace period
    created_at = Column(DateTime, nullable=False, default=utcnow)
    started_at = Column(DateTime)
    heartbeat_at = Column(DateTime)
    finished_at = Column(DateTime)
    attempts = Column(Integer, nullable=False, default=0)
    route = Column(String)  # how the files were removed, e.g. "lidarr" or "plex"
    lidarr_id = Column(Integer)
    lidarr_folder = Column(String)
    match_reason = Column(String)
    error = Column(String)

    @property
    def cancellable(self) -> bool:
        return self.status in ("pending", "failed")

    def to_dict(self):
        return {
            "id": self.id,
            "artist_id": self.artist_id,
            "artist_name": self.artist_name,
            "status": self.status,
            "run_after": iso_utc(self.run_after),
            "created_at": iso_utc(self.created_at),
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "attempts": self.attempts,
            "route": self.route,
            "lidarr_id": self.lidarr_id,
            "lidarr_folder": self.lidarr_folder,
            "match_reason": self.match_reason,
            "error": self.error,
            "cancellable": self.cancellable,
        }


class GenerationState(Base):
    """Single row (id=1) recording the current or last generation run. It acts
    as a lock shared by every server process, and holds the latest progress so
    any process can stream it."""
    __tablename__ = "generation_state"

    id = Column(Integer, primary_key=True)
    owner = Column(String)  # set while a run holds the lock
    started_at = Column(DateTime)
    heartbeat_at = Column(DateTime)
    finished_at = Column(DateTime)
    progress = Column(Text)  # JSON of the latest progress event
    result = Column(Text)  # JSON of the final "done" or "error" event


async def init_db():
    await migrations.migrate(engine)
