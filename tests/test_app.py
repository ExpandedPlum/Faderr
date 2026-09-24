import base64

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
from config import config
from services import plex_service
from tests.conftest import add_artists, all_artists

CSRF = {"X-Faderr-Request": "1"}


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def plex_requests(client):
    """Replace the Plex HTTP client with a fake that records requests."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if "range" in request.headers:
            return httpx.Response(206, stream=httpx.ByteStream(b"part"), headers={
                "content-type": "audio/flac", "content-range": "bytes 0-3/100", "accept-ranges": "bytes",
            })
        return httpx.Response(200, stream=httpx.ByteStream(b"data"), headers={"content-type": "image/jpeg"})

    original = client.app.state.plex_http
    client.app.state.plex_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=config.PLEX_URL,
        headers={"X-Plex-Token": config.PLEX_TOKEN},
    )
    yield seen
    client.app.state.plex_http = original


# ── Auth / CSRF ──────────────────────────────────────────────────────────────

def test_auth_required_when_password_set(client, monkeypatch):
    monkeypatch.setattr(config, "FADERR_PASSWORD", "hunter2")
    assert client.get("/api/stats").status_code == 401
    bad = base64.b64encode(b"faderr:wrong").decode()
    assert client.get("/api/stats", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"faderr:hunter2").decode()
    assert client.get("/api/stats", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_state_changing_requests_need_csrf_header(client):
    (artist_id,) = add_artists({"artist_name": "A", "track_key": "10"})
    resp = client.post(f"/api/artists/{artist_id}/decide", json={"decision": "keep"})
    assert resp.status_code == 403
    assert all_artists()[0]["decision"] is None


# ── Token never reaches the browser ──────────────────────────────────────────

def test_stream_key_redirect_endpoint_is_gone(client):
    resp = client.get("/api/stream-key", params={"key": "@evil.example/x"}, follow_redirects=False)
    assert resp.status_code == 404


def test_artist_json_has_no_token(client):
    add_artists({"artist_name": "A", "thumb_url": "/library/metadata/1/thumb/123"})
    body = client.get("/api/artists").text
    assert config.PLEX_TOKEN not in body
    assert "/api/artists/" in body


def test_stream_is_proxied_with_range(client, plex_requests):
    (artist_id,) = add_artists({"artist_name": "A", "stream_key": "/library/parts/5/1600/file.flac"})
    resp = client.get(f"/api/stream/{artist_id}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.content == b"part"
    assert resp.headers["content-range"] == "bytes 0-3/100"
    assert config.PLEX_TOKEN not in str(resp.headers)
    (req,) = plex_requests
    assert req.url.path == "/library/parts/5/1600/file.flac"
    assert req.headers["x-plex-token"] == config.PLEX_TOKEN
    assert req.headers["range"] == "bytes=0-3"


def test_legacy_thumb_url_only_uses_path(client, plex_requests):
    legacy = f"{config.PLEX_URL}/library/metadata/1/thumb/123?X-Plex-Token={config.PLEX_TOKEN}"
    (artist_id,) = add_artists({"artist_name": "A", "thumb_url": legacy})
    resp = client.get(f"/api/artists/{artist_id}/thumb")
    assert resp.status_code == 200
    assert plex_requests[0].url.path == "/library/metadata/1/thumb/123"


def test_unexpected_paths_are_not_proxied(client, plex_requests):
    (a1, a2) = add_artists(
        {"artist_name": "A", "stream_key": "/library/sections/1/all"},
        {"artist_name": "B", "thumb_url": "http://plex.test/../../etc/passwd"},
    )
    assert client.get(f"/api/stream/{a1}").status_code == 404
    assert client.get(f"/api/artists/{a2}/thumb").status_code == 404
    assert plex_requests == []


# ── Decisions ────────────────────────────────────────────────────────────────

def test_keep_is_saved_when_plex_playlist_update_fails(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("Plex is down")
    monkeypatch.setattr(plex_service, "remove_track_from_playlist", boom)
    (artist_id,) = add_artists({"artist_name": "A", "track_key": "10"})
    resp = client.post(f"/api/artists/{artist_id}/decide", json={"decision": "keep"}, headers=CSRF)
    assert resp.status_code == 200
    assert all_artists()[0]["decision"] == "keep"


def test_undo_delete_is_refused(client):
    (artist_id,) = add_artists({"artist_name": "A", "decision": "delete"})
    resp = client.post(f"/api/artists/{artist_id}/undo", headers=CSRF)
    assert resp.status_code == 409
    assert all_artists()[0]["decision"] == "delete"


def test_refused_delete_returns_409_and_is_not_recorded(client, monkeypatch):
    from services import lidarr_service
    monkeypatch.setattr(plex_service, "get_artist_file_paths", lambda key: ["/m/A/x.flac"])
    def unreachable():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(lidarr_service, "get_all_artists", unreachable)
    (artist_id,) = add_artists({"artist_name": "A", "track_key": "10"})
    resp = client.post(f"/api/artists/{artist_id}/decide", json={"decision": "delete"}, headers=CSRF)
    assert resp.status_code == 409
    assert "Nothing was deleted" in resp.json()["detail"]
    assert all_artists()[0]["decision"] is None


def test_keep_filter_includes_explore_keep(client):
    add_artists(
        {"artist_name": "A", "decision": "keep"},
        {"artist_name": "B", "decision": "explore_keep"},
        {"artist_name": "C", "decision": "delete"},
        {"artist_name": "D"},
    )
    names = [a["artist_name"] for a in client.get("/api/artists", params={"status": "keep"}).json()]
    assert names == ["A", "B"]


def test_stats(client):
    add_artists(
        {"artist_name": "A", "decision": "keep"},
        {"artist_name": "B", "decision": "explore_keep"},
        {"artist_name": "C", "decision": "delete"},
        {"artist_name": "D"},
    )
    assert client.get("/api/stats").json() == {
        "total": 4, "triaged": 3, "remaining": 1,
        "keep": 1, "explore": 0, "explore_keep": 1, "deleted": 1,
    }
