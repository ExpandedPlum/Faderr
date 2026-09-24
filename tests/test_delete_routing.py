import asyncio

import pytest

from models import TriageArtist
from services import lidarr_service, plex_service, triage_service


class Calls:
    def __init__(self):
        self.lidarr_deleted = []
        self.unmonitored = []
        self.plex_deleted = []


@pytest.fixture
def env(monkeypatch):
    calls = Calls()
    state = {
        "files": ["/music/Band/Album/01.flac"],
        "lidarr": [{"id": 5, "artistName": "Band", "path": "/data/Band"}],
        "lidarr_error": None,
        "delete_error": None,
        "unmonitor_error": None,
    }

    def get_all_artists():
        if state["lidarr_error"]:
            raise state["lidarr_error"]
        return state["lidarr"]

    def delete_artist(lidarr_id, delete_files=True):
        if state["delete_error"]:
            raise state["delete_error"]
        calls.lidarr_deleted.append(lidarr_id)

    def unmonitor(lidarr_id):
        if state["unmonitor_error"]:
            raise state["unmonitor_error"]
        calls.unmonitored.append(lidarr_id)

    monkeypatch.setattr(plex_service, "get_artist_file_paths", lambda key: state["files"])
    monkeypatch.setattr(plex_service, "delete_artist_from_plex", lambda key: calls.plex_deleted.append(key))
    monkeypatch.setattr(lidarr_service, "get_all_artists", get_all_artists)
    monkeypatch.setattr(lidarr_service, "delete_artist", delete_artist)
    monkeypatch.setattr(lidarr_service, "unmonitor_artist", unmonitor)
    return calls, state


def run_delete(name="Band", lidarr_id=None):
    artist = TriageArtist(artist_name=name, plex_artist_key="42", lidarr_id=lidarr_id)
    asyncio.run(triage_service._delete_artist(artist))
    return artist


def test_matched_artist_deleted_via_lidarr_only(env):
    calls, _ = env
    artist = run_delete()
    assert calls.lidarr_deleted == [5]
    assert calls.plex_deleted == []
    assert artist.lidarr_id == 5


def test_stale_cached_lidarr_id_is_ignored(env):
    calls, _ = env
    run_delete(lidarr_id=999)
    assert calls.lidarr_deleted == [5]


def test_lidarr_unreachable_deletes_nothing(env):
    calls, state = env
    state["lidarr_error"] = RuntimeError("connection refused")
    with pytest.raises(triage_service.DeletionError):
        run_delete()
    assert calls.lidarr_deleted == [] and calls.plex_deleted == []


def test_ambiguous_match_deletes_nothing(env):
    calls, state = env
    state["lidarr"] = [{"id": 5, "artistName": "Band", "path": "/data/Some Other Band"}]
    with pytest.raises(triage_service.DeletionError):
        run_delete()
    assert calls.lidarr_deleted == [] and calls.plex_deleted == []


def test_artist_not_in_lidarr_falls_back_to_plex(env):
    calls, state = env
    state["lidarr"] = [{"id": 9, "artistName": "Someone Else", "path": "/data/Someone Else"}]
    run_delete()
    assert calls.lidarr_deleted == []
    assert calls.plex_deleted == ["42"]


def test_lidarr_delete_fails_but_unmonitor_works_then_plex(env):
    calls, state = env
    state["delete_error"] = RuntimeError("500")
    run_delete()
    assert calls.unmonitored == [5]
    assert calls.plex_deleted == ["42"]


def test_lidarr_delete_and_unmonitor_fail_deletes_nothing(env):
    calls, state = env
    state["delete_error"] = RuntimeError("500")
    state["unmonitor_error"] = RuntimeError("500")
    with pytest.raises(triage_service.DeletionError):
        run_delete()
    assert calls.plex_deleted == []
