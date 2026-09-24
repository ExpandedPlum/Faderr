import asyncio
import json
from datetime import timedelta

import pytest

import models
from services import generation_service, triage_service
from services.generation_service import GenerationRunner
from tests.conftest import all_artists, track


@pytest.fixture
def library(fake_plex, fake_lidarr, no_lastfm):
    fake_plex.artists = [
        {"name": "A", "rating_key": "1", "thumb": "/library/metadata/1/thumb/1", "mbid": None},
        {"name": "B", "rating_key": "2", "thumb": None, "mbid": None},
        {"name": "C", "rating_key": "3", "thumb": None, "mbid": None},
    ]
    fake_plex.tracks = {
        "1": [track("11"), track("12")],
        "2": [track("21"), track("22")],
        "3": [track("31")],
    }
    return fake_plex


def generate(on_progress=None):
    if on_progress is None:
        return asyncio.run(generation_service.generate())
    return asyncio.run(generation_service.generate(on_progress=on_progress))


def by_name():
    return {a["artist_name"]: a for a in all_artists()}


def test_first_generation_creates_queue_and_playlist(library):
    result = generate()
    assert (result["total_artists"], result["added"], result["removed"]) == (3, 3, 0)
    assert sorted(by_name()) == ["A", "B", "C"]
    assert library.playlists == [("Artist Triage", ["11", "21", "31"])]


def test_regenerate_keeps_ids_and_notes(library):
    generate()
    before = by_name()
    asyncio.run(triage_service.update_notes(before["A"]["id"], "check the live album"))
    result = generate()
    after = by_name()
    assert {n: a["id"] for n, a in after.items()} == {n: a["id"] for n, a in before.items()}
    assert after["A"]["notes"] == "check the live album"
    assert (result["added"], result["updated"]) == (0, 3)


def test_skipped_track_survives_regeneration(library):
    generate()
    a = by_name()["A"]
    skipped = asyncio.run(triage_service.skip_track(a["id"]))
    assert skipped["track_key"] == "12"
    generate()
    a = by_name()["A"]
    assert a["track_key"] == "12" and a["track_pinned"]


def test_pinned_track_replaced_if_it_left_plex(library):
    generate()
    asyncio.run(triage_service.skip_track(by_name()["A"]["id"]))
    library.tracks["1"] = [track("11")]  # track 12 was removed from Plex
    generate()
    a = by_name()["A"]
    assert a["track_key"] == "11" and not a["track_pinned"]


def test_artists_that_left_plex_are_removed_unless_decided(library):
    generate()
    ids = {n: a["id"] for n, a in by_name().items()}
    asyncio.run(triage_service.make_decision(ids["B"], "keep"))
    library.artists = [a for a in library.artists if a["name"] == "A"]  # B and C left Plex
    result = generate()
    assert sorted(by_name()) == ["A", "B"]  # B stays as history; C (undecided) is gone
    assert result["removed"] == 1


def test_decided_artists_are_not_looked_up_or_changed(library):
    generate()
    b = by_name()["B"]
    asyncio.run(triage_service.make_decision(b["id"], "keep"))
    library.calls.clear()
    library.tracks["2"] = [track("29")]
    generate()
    assert ("resolve_track", "2", None) not in library.calls
    assert by_name()["B"]["track_key"] == b["track_key"]


def test_artist_decided_during_generation_is_not_overwritten(library):
    generate()
    b_id = by_name()["B"]["id"]
    decided = False

    async def on_progress(evt):
        nonlocal decided
        if evt["stage"] == "resolving" and not decided:
            decided = True
            await triage_service.make_decision(b_id, "keep")

    library.tracks["2"] = [track("29")]
    generate(on_progress)
    b = by_name()["B"]
    assert b["decision"] == "keep" and b["track_key"] == "21"
    assert len(all_artists()) == 3


def test_one_failing_artist_is_skipped_not_fatal(library):
    library.fail_keys = {"2"}
    result = generate()
    assert result["failed_artists"] == ["B"]
    assert sorted(by_name()) == ["A", "C"]


def test_failing_artist_keeps_its_existing_row(library):
    generate()
    library.fail_keys = {"2"}
    generate()
    assert "B" in by_name()


def test_artist_with_no_tracks_is_dropped(library):
    generate()
    library.tracks["3"] = []
    generate()
    assert sorted(by_name()) == ["A", "B"]


def test_same_name_artists_are_tracked_separately(library):
    library.artists = [
        {"name": "Nirvana", "rating_key": "1", "thumb": None, "mbid": None},
        {"name": "Nirvana", "rating_key": "2", "thumb": None, "mbid": None},
    ]
    generate()
    first = all_artists()[0]
    asyncio.run(triage_service.make_decision(first["id"], "keep"))
    generate()
    rows = sorted((a["plex_artist_key"], a["decision"]) for a in all_artists())
    assert rows == [("1", "keep"), ("2", None)]


def test_mbid_and_lidarr_match_recorded(library, fake_lidarr):
    library.artists[0]["mbid"] = "mbid-a"
    library.files["1"] = ["/music/A/album/01.flac"]
    fake_lidarr.artists = [{"id": 7, "artistName": "A", "path": "/data/A", "foreignArtistId": "mbid-a"}]
    generate()
    assert all_artists()[0]["lidarr_id"] == 7


def test_playlist_failure_does_not_fail_generation(library):
    library.playlist_error = RuntimeError("URI too long")
    result = generate()
    assert result["total_artists"] == 3 and "playlist_warning" in result


def test_sync_playlist_uses_only_undecided_artists(library):
    generate()
    asyncio.run(triage_service.make_decision(by_name()["A"]["id"], "keep"))
    library.playlists.clear()
    assert asyncio.run(triage_service.sync_playlist())["tracks"] == 2
    assert library.playlists == [("Artist Triage", ["21", "31"])]


def test_decisions_do_not_touch_the_playlist(library):
    generate()
    library.playlists.clear()
    asyncio.run(triage_service.make_decision(by_name()["A"]["id"], "keep"))
    asyncio.run(triage_service.skip_track(by_name()["B"]["id"]))
    assert library.playlists == []


# ── The shared lock and progress ──────────────────────────────────────────────

async def _state():
    async with models.async_session() as session:
        return await session.get(models.GenerationState, 1)


def test_second_start_joins_instead_of_starting_another(monkeypatch):
    release = None

    async def fake_generate(on_progress):
        await on_progress({"stage": "plex_fetch"})
        await release.wait()
        return {"total_artists": 0}

    monkeypatch.setattr(generation_service, "generate", fake_generate)

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        first, second = GenerationRunner(), GenerationRunner()  # e.g. two server processes
        assert await first.start() is True
        await asyncio.sleep(0.05)
        assert await second.start() is False
        status = await second.status()
        assert status["running"] and status["progress"]["stage"] == "plex_fetch"
        release.set()
        await first._task
        status = await second.status()
        assert not status["running"] and status["result"]["stage"] == "done"
        assert await second.start() is True  # free again
        release.set()
        await second._task

    asyncio.run(scenario())


def test_abandoned_lock_is_taken_over(monkeypatch):
    async def fake_generate(on_progress):
        return {"total_artists": 0}

    monkeypatch.setattr(generation_service, "generate", fake_generate)

    async def scenario():
        async with models.async_session() as session:
            async with session.begin():
                state = await session.get(models.GenerationState, 1)
                state.owner = "dead-process"
                state.heartbeat_at = models.utcnow() - timedelta(minutes=10)
        runner = GenerationRunner()
        assert not (await runner.status())["running"]
        assert await runner.start() is True
        await runner._task
        assert (await _state()).owner is None

    asyncio.run(scenario())


def test_failure_releases_lock_with_error_result(monkeypatch):
    async def fake_generate(on_progress):
        raise RuntimeError("Plex is down")

    monkeypatch.setattr(generation_service, "generate", fake_generate)

    async def scenario():
        runner = GenerationRunner()
        await runner.start()
        await runner._task
        state = await _state()
        assert state.owner is None
        assert json.loads(state.result) == {"stage": "error", "message": "Plex is down"}

    asyncio.run(scenario())
