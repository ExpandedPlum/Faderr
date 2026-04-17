import logging

from sqlalchemy import Column, DateTime, Integer, String, func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import config

logger = logging.getLogger(__name__)

engine = create_async_engine(config.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class TriageArtist(Base):
    __tablename__ = "triage_artists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    artist_name = Column(String, nullable=False)
    plex_artist_key = Column(String, nullable=False)
    thumb_url = Column(String)
    track_title = Column(String)
    track_key = Column(String)
    stream_key = Column(String)
    source = Column(String)  # "lastfm" or "plex_random"
    decision = Column(String)  # NULL, "keep", "explore", "explore_keep", "delete"
    decided_at = Column(DateTime)
    lidarr_id = Column(Integer)
    notes = Column(String)
    created_at = Column(DateTime, server_default=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "artist_name": self.artist_name,
            "plex_artist_key": self.plex_artist_key,
            "thumb": self.thumb_url,
            "track_title": self.track_title,
            "track_key": self.track_key,
            "stream_key": self.stream_key,
            "source": self.source,
            "decision": self.decision,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "lidarr_id": self.lidarr_id,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Columns added after v1 — applied at startup for existing databases
_MIGRATION_COLUMNS = [
    ("stream_key", "TEXT"),
    ("thumb_url", "TEXT"),
    ("notes", "TEXT"),
]


async def _migrate_schema(conn):
    result = await conn.execute(text("PRAGMA table_info(triage_artists)"))
    existing = {row[1] for row in result.fetchall()}
    for col_name, col_type in _MIGRATION_COLUMNS:
        if col_name not in existing:
            try:
                await conn.execute(text(f"ALTER TABLE triage_artists ADD COLUMN {col_name} {col_type}"))
                logger.info("Migration: added column %s", col_name)
            except Exception as exc:
                logger.warning("Migration skipped for %s: %s", col_name, exc)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_schema(conn)
