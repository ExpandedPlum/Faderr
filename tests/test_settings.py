"""Settings from the web UI: env precedence, setup mode, Sign in with Plex,
password, and secrets never leaving the server."""
import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
from services import lastfm_service, plex_auth
from services.settings_service import FIELDS, hash_password, store, verify_password
from tests.conftest import reload_settings

CSRF = {"X-Faderr-Request": "1"}
ACCOUNT_TOKEN = "ACCOUNT-TOKEN-xyz"
SERVER_TOKEN = "SERVER-TOKEN-abc"


@pytest.fixture
def unconfigured(monkeypatch):
    """No connection settings in the environment: a fresh install."""
    for spec in FIELDS.values():
        if spec.env:
            monkeypatch.delenv(spec.env, raising=False)
    reload_settings()


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def web(monkeypatch):
    """Fake plex.tv, Plex server, Lidarr and Last.fm for the settings API."""
    state = {"pin_polls": 0, "requests": []}

    def handler(request: httpx.Request):
        url = str(request.url)
        state["requests"].append(request)
        host = request.url.host
        if host == "plex.tv" and request.method == "POST" and request.url.path == "/api/v2/pins":
            return httpx.Response(201, json={"id": 77, "code": "abcd"})
        if host == "plex.tv" and request.url.path == "/api/v2/pins/77":
            state["pin_polls"] += 1
            token = ACCOUNT_TOKEN if state["pin_polls"] >= 2 else None
            return httpx.Response(200, json={"id": 77, "authToken": token})
        if host == "plex.tv" and request.url.path == "/api/v2/pins/99":
            return httpx.Response(404)
        if host == "plex.tv" and request.url.path == "/api/v2/resources":
            assert request.headers["x-plex-token"] == ACCOUNT_TOKEN
            return httpx.Response(200, json=[
                {"name": "My Phone", "provides": "client,player", "clientIdentifier": "phone"},
                {"name": "Home Server", "provides": "server", "clientIdentifier": "srv1", "owned": True,
                 "accessToken": SERVER_TOKEN, "connections": [
                     {"uri": "https://1-2-3-4.abc.plex.direct:32400", "address": "192.168.1.10",
                      "port": 32400, "local": True, "relay": False},
                     {"uri": "https://relay.plex.direct:8443", "address": "1.1.1.1",
                      "port": 8443, "local": False, "relay": True},
                 ]},
            ])
        if "plex.direct" in host:
            raise httpx.ConnectError("DNS rebinding protection", request=request)
        if host in ("192.168.1.10", "plex.manual"):
            if request.headers.get("x-plex-token") not in (SERVER_TOKEN, "MANUAL-TOKEN"):
                return httpx.Response(401)
            if request.url.path == "/":
                return httpx.Response(200, json={"MediaContainer": {"friendlyName": "Home Server"}})
            if request.url.path == "/library/sections":
                return httpx.Response(200, json={"MediaContainer": {"Directory": [
                    {"title": "Movies", "type": "movie"}, {"title": "Music", "type": "artist"},
                ]}})
        if host == "lidarr.home":
            if request.headers.get("x-api-key") != "GOOD-LIDARR-KEY":
                return httpx.Response(401)
            return httpx.Response(200, json={"version": "2.5.0"})
        if host == "ws.audioscrobbler.com":
            if request.url.params.get("api_key") != "GOOD-LASTFM-KEY":
                return httpx.Response(403, json={"error": 10, "message": "Invalid API key"})
            return httpx.Response(200, json={"artist": {"name": "Cher"}})
        return httpx.Response(404, text=f"unexpected {url}")

    # Only the transport is replaced, so the real http_client() is exercised
    monkeypatch.setattr(plex_auth, "_transport", httpx.MockTransport(handler))
    return state


def settings(client):
    return client.get("/api/settings").json()


# ── Precedence and secrecy ────────────────────────────────────────────────────

def test_env_values_win_and_are_read_only(client):
    view = settings(client)
    assert view["configured"]
    assert view["fields"]["plex_url"]["source"] == "env"
    resp = client.post("/api/settings/lidarr", json={"url": "http://lidarr.home"}, headers=CSRF)
    assert resp.status_code == 409


def test_secrets_are_never_returned(client):
    import os
    body = client.get("/api/settings").text
    for name in ("PLEX_TOKEN", "LIDARR_API_KEY", "LASTFM_API_KEY"):
        assert os.environ[name] not in body
    assert json.loads(body)["fields"]["plex_token"]["set"] is True


# ── Setup mode ────────────────────────────────────────────────────────────────

def test_fresh_install_goes_to_setup(unconfigured, client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/settings"
    resp = client.get("/api/stats")
    assert resp.status_code == 503 and resp.json()["setup_required"]
    assert client.get("/settings").status_code == 200
    view = settings(client)
    assert not view["configured"]
    assert "Plex token" in view["missing"] and "Lidarr API key" in view["missing"]


def test_sign_in_with_plex_then_lidarr_completes_setup(unconfigured, client, web):
    pin = client.post("/api/settings/plex/pin", headers=CSRF).json()
    assert pin["id"] == 77 and pin["auth_url"].startswith("https://app.plex.tv/auth/#!?")
    assert "code=abcd" in pin["auth_url"]

    assert client.get("/api/settings/plex/pin/77").json() == {"authorized": False}
    approved = client.get("/api/settings/plex/pin/77")
    assert approved.json() == {"authorized": True,
                               "servers": [{"id": "srv1", "name": "Home Server", "owned": True}]}
    assert ACCOUNT_TOKEN not in approved.text

    server = client.post("/api/settings/plex/server", json={"server_id": "srv1"}, headers=CSRF)
    # plex.direct is blocked on this network, so the plain local address is used
    assert server.json() == {"server_name": "Home Server", "url": "http://192.168.1.10:32400",
                             "libraries": ["Music"]}
    assert SERVER_TOKEN not in server.text

    done = client.post("/api/settings/plex/library", json={"library": "Music"}, headers=CSRF)
    assert done.status_code == 200 and SERVER_TOKEN not in done.text
    assert store.current.get("plex_token") == SERVER_TOKEN  # the server's token, not the account's
    assert store.current.get("plex_url") == "http://192.168.1.10:32400"
    assert asyncio.run(store.get_pending()) is None
    assert not settings(client)["configured"]  # Lidarr still missing

    resp = client.post("/api/settings/lidarr", json={"url": "http://lidarr.home/", "api_key": "GOOD-LIDARR-KEY"},
                       headers=CSRF)
    assert resp.status_code == 200 and resp.json()["lidarr_version"] == "2.5.0"
    assert settings(client)["configured"]
    assert client.get("/api/stats").status_code == 200


def test_expired_pin(unconfigured, client, web):
    assert client.get("/api/settings/plex/pin/99").status_code == 410


def test_choosing_a_server_needs_a_sign_in(unconfigured, client, web):
    resp = client.post("/api/settings/plex/server", json={"server_id": "srv1"}, headers=CSRF)
    assert resp.status_code == 409


def test_manual_plex_connection(unconfigured, client, web):
    test = client.post("/api/settings/plex/manual", json={"url": "http://plex.manual:32400", "token": "MANUAL-TOKEN"},
                       headers=CSRF)
    assert test.json() == {"server_name": "Home Server", "libraries": ["Music"]}
    bad = client.post("/api/settings/plex/manual",
                      json={"url": "http://plex.manual:32400", "token": "MANUAL-TOKEN", "library": "Movies"},
                      headers=CSRF)
    assert bad.status_code == 400
    ok = client.post("/api/settings/plex/manual",
                     json={"url": "http://plex.manual:32400", "token": "MANUAL-TOKEN", "library": "Music"},
                     headers=CSRF)
    assert ok.status_code == 200
    assert store.current.get("plex_token") == "MANUAL-TOKEN"


def test_bad_lidarr_key_is_not_saved(unconfigured, client, web):
    resp = client.post("/api/settings/lidarr", json={"url": "http://lidarr.home", "api_key": "WRONG"}, headers=CSRF)
    assert resp.status_code == 400
    assert store.current.get("lidarr_api_key") is None


def test_lastfm_key_is_tested_and_optional(unconfigured, client, web):
    assert client.post("/api/settings/lastfm", json={"api_key": "WRONG"}, headers=CSRF).status_code == 400
    assert client.post("/api/settings/lastfm", json={"api_key": "GOOD-LASTFM-KEY"}, headers=CSRF).status_code == 200
    assert store.current.get("lastfm_api_key") == "GOOD-LASTFM-KEY"
    client.post("/api/settings/lastfm", json={"api_key": ""}, headers=CSRF)
    assert store.current.get("lastfm_api_key") is None


def test_no_lastfm_key_means_no_lookups(unconfigured):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await lastfm_service.get_top_track("Anyone", c)
    assert asyncio.run(go()) is None and calls == []


def test_saved_settings_configure_the_clients(unconfigured, client, web):
    from services.lidarr_service import lidarr
    client.post("/api/settings/lidarr", json={"url": "http://lidarr.home", "api_key": "GOOD-LIDARR-KEY"}, headers=CSRF)
    assert lidarr._base == "http://lidarr.home"


# ── Password ──────────────────────────────────────────────────────────────────

def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_password_set_in_ui_protects_everything(unconfigured, client):
    assert client.post("/api/settings/password", json={"password": "short"}, headers=CSRF).status_code == 400
    assert client.post("/api/settings/password", json={"password": "correct horse"}, headers=CSRF).status_code == 200
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers=basic("faderr", "wrong")).status_code == 401
    assert client.get("/api/settings", headers=basic("faderr", "correct horse")).status_code == 200
    assert "correct horse" not in (store.current.password_hash or "")


def test_env_password_overrides_and_cannot_be_changed(client, monkeypatch):
    monkeypatch.setenv("FADERR_PASSWORD", "from-env-123")
    reload_settings()
    auth = basic("faderr", "from-env-123")
    resp = client.post("/api/settings/password", json={"password": "something-else"}, headers={**CSRF, **auth})
    assert resp.status_code == 409


def test_password_hashing():
    stored = hash_password("hunter22")
    assert stored.startswith("scrypt$") and "hunter22" not in stored
    assert verify_password("hunter22", stored)
    assert not verify_password("hunter23", stored)
    assert not verify_password("hunter22", "garbage")


# ── Choosing a connection ─────────────────────────────────────────────────────

def test_connection_order_prefers_local_then_https():
    server = {"connections": [
        {"uri": "https://relay.plex.direct:8443", "local": False, "relay": True},
        {"uri": "https://remote.plex.direct:32400", "local": False, "relay": False},
        {"uri": "https://local.plex.direct:32400", "address": "192.168.1.10", "port": 32400, "local": True, "relay": False},
    ]}
    assert plex_auth.candidate_urls(server) == [
        "https://local.plex.direct:32400",
        "http://192.168.1.10:32400",
        "https://remote.plex.direct:32400",
        "https://relay.plex.direct:8443",
    ]
