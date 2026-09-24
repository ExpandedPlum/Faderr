import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # app.py mounts "static" and "templates" by relative path

_DB_DIR = tempfile.mkdtemp(prefix="faderr-tests-")
os.environ.update({
    "PLEX_URL": "http://plex.test:32400",
    "PLEX_TOKEN": "PLEX-SECRET-TOKEN",
    "PLEX_MUSIC_LIBRARY": "Music",
    "LASTFM_API_KEY": "lastfm-key",
    "LIDARR_URL": "http://lidarr.test:8686",
    "LIDARR_API_KEY": "lidarr-key",
    "DATABASE_URL": f"sqlite+aiosqlite:///{_DB_DIR}/test.db",
    "FADERR_PASSWORD": "",
})

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import models  # noqa: E402

# No connection pooling in tests: each test (and TestClient) runs its own event
# loop, and pooled aiosqlite connections can't move between loops.
models.engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
models.async_session.configure(bind=models.engine)


async def _reset_db():
    async with models.engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.drop_all)
    await models.init_db()


@pytest.fixture(autouse=True)
def fresh_db():
    asyncio.run(_reset_db())
    yield


def add_artists(*rows):
    """Insert TriageArtist rows built from keyword dicts; returns their ids."""
    async def _add():
        async with models.async_session() as session:
            objs = [models.TriageArtist(**{"plex_artist_key": "1", **r}) for r in rows]
            session.add_all(objs)
            await session.commit()
            return [o.id for o in objs]
    return asyncio.run(_add())


def all_artists():
    from sqlalchemy import select

    async def _all():
        async with models.async_session() as session:
            result = await session.execute(select(models.TriageArtist).order_by(models.TriageArtist.id))
            return [r.to_dict() for r in result.scalars().all()]
    return asyncio.run(_all())
