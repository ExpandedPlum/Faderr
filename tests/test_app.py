import base64
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
from services import generation_service
from services.plex_service import plex
from tests.conftest import add_artists, all_artists, reload_settings, track

CSRF = {"X-Faderr-Request": "1"}
PLEX_URL = os.environ["PLEX_URL"]
PLEX_TOKEN = os.environ["PLEX_TOKEN"]


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

    original = plex.http
    plex.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=PLEX_URL,
        headers={"X-Plex-Token": PLEX_TOKEN},
    )
    yield seen
    plex.http = original


# ── Auth / CSRF ──────────────────────────────────────────────────────────────

def test_auth_required_when_password_set(client, monkeypatch):
    monkeypatch.setenv("FADERR_PASSWORD", "hunter2")
    reload_settings()
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
    assert PLEX_TOKEN not in body
    assert "/api/artists/" in body


def test_stream_is_proxied_with_range(client, plex_requests):
    (artist_id,) = add_artists({"artist_name": "A", "stream_key": "/library/parts/5/1600/file.flac"})
    resp = client.get(f"/api/stream/{artist_id}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.content == b"part"
    assert resp.headers["content-range"] == "bytes 0-3/100"
    assert PLEX_TOKEN not in str(resp.headers)
    (req,) = plex_requests
    assert req.url.path == "/library/parts/5/1600/file.flac"
    assert req.headers["x-plex-token"] == PLEX_TOKEN
    assert req.headers["range"] == "bytes=0-3"


def test_legacy_thumb_url_only_uses_path(client, plex_requests):
    legacy = f"{PLEX_URL}/library/metadata/1/thumb/123?X-Plex-Token={PLEX_TOKEN}"
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

def test_keep_and_undo(client):
    (artist_id,) = add_artists({"artist_name": "A", "track_key": "10"})
    resp = client.post(f"/api/artists/{artist_id}/decide", json={"decision": "keep"}, headers=CSRF)
    assert resp.status_code == 200 and resp.json()["decision"] == "keep"
    assert client.post(f"/api/artists/{artist_id}/undo", headers=CSRF).json()["decision"] is None


def test_old_decision_values_are_rejected(client):
    (artist_id,) = add_artists({"artist_name": "A"})
    for decision in ("explore", "explore_keep", "explore_delete"):
        resp = client.post(f"/api/artists/{artist_id}/decide", json={"decision": decision}, headers=CSRF)
        assert resp.status_code == 400


def test_delete_is_queued_and_can_be_undone(client):
    (artist_id,) = add_artists({"artist_name": "A", "track_key": "10"})
    resp = client.post(f"/api/artists/{artist_id}/decide", json={"decision": "delete"}, headers=CSRF)
    assert resp.status_code == 200
    job = resp.json()["deletion"]
    assert job["status"] == "pending" and job["cancellable"]
    assert client.get(f"/api/artists/{artist_id}").json()["deletion"]["id"] == job["id"]
    assert [j["id"] for j in client.get("/api/deletions").json()] == [job["id"]]
    assert client.post(f"/api/artists/{artist_id}/decide", json={"decision": "keep"}, headers=CSRF).status_code == 409

    undone = client.post(f"/api/artists/{artist_id}/undo", headers=CSRF).json()
    assert undone["decision"] is None and undone["deletion"]["status"] == "cancelled"


def test_cancel_and_retry_endpoints(client):
    (artist_id,) = add_artists({"artist_name": "A", "track_key": "10"})
    job = client.post(f"/api/artists/{artist_id}/decide", json={"decision": "delete"}, headers=CSRF).json()["deletion"]
    assert client.post(f"/api/deletions/{job['id']}/retry", headers=CSRF).status_code == 409  # not failed
    assert client.post(f"/api/deletions/{job['id']}/cancel", headers=CSRF).json()["status"] == "cancelled"
    assert all_artists()[0]["decision"] is None
    assert client.post(f"/api/deletions/{job['id']}/cancel", headers=CSRF).status_code == 409


def test_legacy_delete_without_job_cannot_be_undone(client):
    (artist_id,) = add_artists({"artist_name": "A", "decision": "delete"})
    assert client.post(f"/api/artists/{artist_id}/undo", headers=CSRF).status_code == 409
    assert all_artists()[0]["decision"] == "delete"


def test_no_skip_on_deleted_artist(client):
    (artist_id,) = add_artists({"artist_name": "Gone", "decision": "delete", "track_key": "1"})
    assert client.post(f"/api/artists/{artist_id}/skip", headers=CSRF).status_code == 409


def test_filters_and_stats(client):
    add_artists(
        {"artist_name": "A", "decision": "keep"},
        {"artist_name": "C", "decision": "delete"},
        {"artist_name": "D"},
    )
    names = lambda status: [a["artist_name"] for a in client.get("/api/artists", params={"status": status}).json()]
    assert (names("keep"), names("delete"), names("undecided")) == (["A"], ["C"], ["D"])
    assert client.get("/api/stats").json() == {
        "total": 3, "triaged": 2, "remaining": 1, "keep": 1, "deleted": 1,
        "delete_pending": 0, "delete_failed": 0,
    }


# ── Generation stream and playlist ────────────────────────────────────────────

def test_generation_streams_progress_from_the_database(client, monkeypatch):
    async def fake_generate(on_progress):
        await on_progress({"stage": "plex_fetch"})
        await on_progress({"stage": "lastfm", "done": 1, "total": 1})
        return {"total_artists": 1, "added": 1, "removed": 0, "playlist_name": "Artist Triage"}
    monkeypatch.setattr(generation_service, "generate", fake_generate)

    with client.stream("POST", "/api/generate/stream", headers=CSRF) as resp:
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    assert events[-1]["stage"] == "done" and events[-1]["added"] == 1
    assert client.get("/api/generate/status").json() == {"running": False}


def test_playlist_sync_endpoint(client, fake_plex):
    add_artists({"artist_name": "A", "track_key": "11"}, {"artist_name": "B", "track_key": "21", "decision": "keep"})
    assert client.post("/api/playlist/sync", headers=CSRF).json() == {"playlist_name": "Artist Triage", "tracks": 1}
    assert fake_plex.playlists == [("Artist Triage", ["11"])]


def test_playlist_sync_failure_is_reported(client, fake_plex):
    fake_plex.playlist_error = RuntimeError("Plex is down")
    assert client.post("/api/playlist/sync", headers=CSRF).status_code == 502


def test_skip_pins_new_track(client, fake_plex):
    (artist_id,) = add_artists({"artist_name": "A", "plex_artist_key": "1", "track_key": "11"})
    fake_plex.tracks["1"] = [track("11"), track("12")]
    resp = client.post(f"/api/artists/{artist_id}/skip", headers=CSRF).json()
    assert resp["track_key"] == "12"
    assert all_artists()[0]["track_pinned"]


# ── Paths don't depend on the working directory ──────────────────────────────

def test_serves_ui_from_another_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(app_module.app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/static/app.js").status_code == 200
