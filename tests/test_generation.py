import asyncio

import pytest

import models
from services import lastfm_service, lidarr_service, plex_service, triage_service
from services.generation_runner import GenerationRunner
from tests.conftest import all_artists


@pytest.fixture
def fake_sources(monkeypatch):
    plex_artists = [
        {"name": "A", "rating_key": "1", "thumb": "/library/metadata/1/thumb/1"},
        {"name": "B", "rating_key": "2", "thumb": None},
        {"name": "C", "rating_key": "3", "thumb": None},
    ]

    async def top_track(name, client):
        return None

    def resolve(server, key, lastfm_title):
        return ({"title": f"t{key}", "rating_key": f"10{key}", "stream_key": None},
                "plex_random", [f"/music/{key}/a.flac"])

    monkeypatch.setattr(plex_service, "get_server", lambda: object())
    monkeypatch.setattr(plex_service, "get_all_artists", lambda server=None: plex_artists)
    monkeypatch.setattr(plex_service, "resolve_track_for_artist", resolve)
    monkeypatch.setattr(plex_service, "create_or_replace_playlist", lambda *a, **k: None)
    monkeypatch.setattr(lidarr_service, "get_all_artists", lambda: [])
    monkeypatch.setattr(lastfm_service, "get_top_track", top_track)


def test_generate_without_progress_callback(fake_sources):
    result = asyncio.run(triage_service.generate_triage_playlist())
    assert result["total_artists"] == 3
    assert [a["artist_name"] for a in all_artists()] == ["A", "B", "C"]


def test_artist_decided_during_generation_is_not_duplicated(fake_sources):
    decided = False

    async def on_progress(evt):
        nonlocal decided
        if evt["stage"] == "resolving" and not decided:
            decided = True
            async with models.async_session() as session:
                session.add(models.TriageArtist(artist_name="B", plex_artist_key="2", decision="keep"))
                await session.commit()

    asyncio.run(triage_service.generate_triage_playlist(on_progress=on_progress))
    rows = all_artists()
    assert sorted(a["artist_name"] for a in rows) == ["A", "B", "C"]
    assert [a["decision"] for a in rows if a["artist_name"] == "B"] == ["keep"]


def test_playlist_failure_does_not_fail_generation(fake_sources, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("URI too long")
    monkeypatch.setattr(plex_service, "create_or_replace_playlist", boom)
    result = asyncio.run(triage_service.generate_triage_playlist())
    assert result["total_artists"] == 3
    assert "playlist_warning" in result


def test_runner_survives_subscriber_leaving(monkeypatch):
    release = None

    async def fake_generate(on_progress):
        await on_progress({"stage": "plex_fetch"})
        await release.wait()
        await on_progress({"stage": "done", "total_artists": 0})

    monkeypatch.setattr(triage_service, "generate_triage_playlist", fake_generate)

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        runner = GenerationRunner()
        q1 = runner.subscribe()
        assert runner.start() is True
        assert (await q1.get())["stage"] == "plex_fetch"
        runner.unsubscribe(q1)  # first client disconnects

        assert runner.start() is False  # second request joins instead
        q2 = runner.subscribe()
        assert (await q2.get())["stage"] == "plex_fetch"  # latest event replayed

        release.set()
        assert (await q2.get())["stage"] == "done"
        await asyncio.sleep(0)
        assert not runner.running

    asyncio.run(scenario())
