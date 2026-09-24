import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
    "DELETE_GRACE_SECONDS": "300",
})

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import models  # noqa: E402
from services import lastfm_service  # noqa: E402
from services.lidarr_service import lidarr  # noqa: E402
from services.plex_service import plex  # noqa: E402

# No connection pooling in tests: each test (and TestClient) runs its own event
# loop, and pooled aiosqlite connections can't move between loops.
models.engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
models.configure_sqlite(models.engine)
models.async_session.configure(bind=models.engine)


async def _reset_db():
    async with models.engine.begin() as conn:
        tables = (await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )).fetchall()
        for (name,) in tables:
            await conn.exec_driver_sql(f"DROP TABLE {name}")
    await models.init_db()


@pytest.fixture(autouse=True)
def fresh_db():
    asyncio.run(_reset_db())
    yield


def add_artists(*rows):
    """Insert TriageArtist rows built from keyword dicts; returns their ids.
    plex_artist_key defaults to a unique value per row."""
    async def _add():
        async with models.async_session() as session:
            objs = []
            for i, r in enumerate(rows):
                objs.append(models.TriageArtist(**{"plex_artist_key": f"auto-{i}-{r.get('artist_name')}", **r}))
            session.add_all(objs)
            await session.commit()
            return [o.id for o in objs]
    return asyncio.run(_add())


def all_artists():
    async def _all():
        async with models.async_session() as session:
            result = await session.execute(select(models.TriageArtist).order_by(models.TriageArtist.id))
            return [r.to_dict() | {"track_pinned": r.track_pinned} for r in result.scalars().all()]
    return asyncio.run(_all())


def all_jobs():
    async def _all():
        async with models.async_session() as session:
            result = await session.execute(select(models.Deletion).order_by(models.Deletion.id))
            return [j.to_dict() | {"previous_decision": j.previous_decision} for j in result.scalars().all()]
    return asyncio.run(_all())


def track(key: str, title: str = None) -> dict:
    return {"title": title or f"Track {key}", "rating_key": key, "stream_key": f"/library/parts/{key}/1/file.flac"}


@pytest.fixture
def fake_plex(monkeypatch):
    """Replace the Plex client's methods with an in-memory fake."""
    f = SimpleNamespace(
        artists=[],        # dicts: name, rating_key, thumb, mbid
        tracks={},         # artist key -> [track dicts]
        files={},          # artist key -> [file paths]
        fail_keys=set(),   # artist keys whose track lookup raises
        deleted=[],
        playlists=[],
        playlist_error=None,
        calls=[],
    )

    async def all_artists():
        return f.artists

    async def resolve_track(key, lastfm_title, keep_track_key=None):
        f.calls.append(("resolve_track", key, keep_track_key))
        if key in f.fail_keys:
            raise RuntimeError("Plex 500 for this artist")
        tracks = f.tracks.get(key, [])
        files = f.files.get(key, [f"/music/{key}/album/01.flac"])
        if not tracks:
            return None, "plex_random", []
        if keep_track_key:
            for t in tracks:
                if t["rating_key"] == keep_track_key:
                    return t, "kept", files
        if lastfm_title:
            for t in tracks:
                if t["title"].lower() == lastfm_title.lower():
                    return t, "lastfm", files
        return tracks[0], "plex_random", files

    async def other_tracks(key, exclude_key, count=None):
        others = [t for t in f.tracks.get(key, []) if t["rating_key"] != exclude_key]
        return others[:count] if count is not None else others

    async def artist_file_paths(key):
        return f.files.get(key, [f"/music/{key}/album/01.flac"])

    async def delete_artist(key):
        f.deleted.append(key)

    async def replace_playlist(name, keys):
        if f.playlist_error:
            raise f.playlist_error
        f.playlists.append((name, list(keys)))

    async def track_stream_key(rating_key):
        return f"/library/parts/{rating_key}/1/file.flac"

    for name, fn in dict(all_artists=all_artists, resolve_track=resolve_track, other_tracks=other_tracks,
                         artist_file_paths=artist_file_paths, delete_artist=delete_artist,
                         replace_playlist=replace_playlist, track_stream_key=track_stream_key).items():
        monkeypatch.setattr(plex, name, fn)
    return f


@pytest.fixture
def fake_lidarr(monkeypatch):
    """Replace the Lidarr client's methods with an in-memory fake."""
    f = SimpleNamespace(
        artists=[],              # dicts: id, artistName, path, foreignArtistId
        list_error=None,
        delete_error=None,
        exists=True,             # what Lidarr says after a failed delete
        exists_error=None,
        unmonitor_error=None,
        deleted=[],
        unmonitored=[],
    )

    async def all_artists():
        if f.list_error:
            raise f.list_error
        return f.artists

    async def delete_artist(lidarr_id, delete_files=True):
        if f.delete_error:
            raise f.delete_error
        f.deleted.append(lidarr_id)

    async def artist_exists(lidarr_id):
        if f.exists_error:
            raise f.exists_error
        return f.exists

    async def unmonitor_artist(lidarr_id):
        if f.unmonitor_error:
            raise f.unmonitor_error
        f.unmonitored.append(lidarr_id)

    for name, fn in dict(all_artists=all_artists, delete_artist=delete_artist,
                         artist_exists=artist_exists, unmonitor_artist=unmonitor_artist).items():
        monkeypatch.setattr(lidarr, name, fn)
    return f


@pytest.fixture
def no_lastfm(monkeypatch):
    async def top_track(name, client):
        return None
    monkeypatch.setattr(lastfm_service, "get_top_track", top_track)
