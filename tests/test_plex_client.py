"""PlexClient: one reused connection, async methods, reconnect on drops."""
import asyncio
from types import SimpleNamespace

import pytest
import requests

from services import plex_service
from services.plex_service import PlexClient, _resolve_track


class FakeServer:
    instances = 0

    def __init__(self, url, token):
        FakeServer.instances += 1
        self.fail_next = 0

    def fetchItem(self, key):
        if self.fail_next:
            self.fail_next -= 1
            raise requests.exceptions.ConnectionError("connection reset")
        return SimpleNamespace(media=[SimpleNamespace(parts=[SimpleNamespace(key=f"/library/parts/{key}/1/f.flac")])],
                               delete=lambda: None)


@pytest.fixture
def client(monkeypatch):
    FakeServer.instances = 0
    monkeypatch.setattr(plex_service, "PlexServer", FakeServer)
    c = PlexClient()
    asyncio.run(c.configure("http://plex.test", "token", "Music"))
    return c


def test_unconfigured_client_says_so():
    with pytest.raises(plex_service.NotConfiguredError):
        asyncio.run(PlexClient().track_stream_key("1"))


def test_reconfiguring_drops_the_old_connection(client):
    client._server_conn()
    asyncio.run(client.configure("http://other.test", "token2", "Music"))
    client._server_conn()
    assert FakeServer.instances == 2


def test_connection_is_reused(client):
    async def go():
        await client.track_stream_key("1")
        await client.track_stream_key("2")
    asyncio.run(go())
    assert FakeServer.instances == 1


def test_read_reconnects_once_after_a_dropped_connection(client):
    client._server_conn().fail_next = 1
    assert asyncio.run(client.track_stream_key("5")) == "/library/parts/5/1/f.flac"
    assert FakeServer.instances == 2


def test_changes_are_never_retried_automatically(client):
    client._server_conn().fail_next = 1
    with pytest.raises(requests.exceptions.ConnectionError):
        asyncio.run(client.delete_artist("5"))
    assert FakeServer.instances == 1  # dropped, but not reconnected to repeat the delete


def fake_track(key, title):
    return SimpleNamespace(ratingKey=key, title=title,
                           media=[SimpleNamespace(parts=[SimpleNamespace(key=f"/p/{key}", file=f"/music/X/{key}.flac")])])


class TracksServer:
    def __init__(self, tracks):
        self.tracks = tracks

    def fetchItems(self, key, cls=None):
        return self.tracks


def test_resolve_prefers_kept_then_lastfm_then_random():
    server = TracksServer([fake_track(1, "Intro"), fake_track(2, "Hit Song")])
    track, source, files = _resolve_track(server, "9", "hit song", "1")
    assert (track["rating_key"], source) == ("1", "kept")
    assert files == ["/music/X/1.flac", "/music/X/2.flac"]
    track, source, _ = _resolve_track(server, "9", "hit song", "404")
    assert (track["rating_key"], source) == ("2", "lastfm")
    _, source, _ = _resolve_track(server, "9", None, None)
    assert source == "plex_random"


def test_mbid_read_from_plex_guids():
    item = SimpleNamespace(guids=[SimpleNamespace(id="plex://artist/abc"), SimpleNamespace(id="mbid://1234-5678")])
    assert plex_service._mbid(item) == "1234-5678"
    assert plex_service._mbid(SimpleNamespace(guids=[])) is None
