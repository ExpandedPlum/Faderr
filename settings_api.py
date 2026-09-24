"""Settings page API: connect Plex (Sign in with Plex, or URL + token),
Lidarr and Last.fm, and set the login password.

Every connection is tested before it is saved. Tokens and API keys are only
ever sent *to* the server; responses show whether they are set, never their
values. Sign-in state between steps (the account token, the chosen server) is
kept in the database, not in the browser or in process memory.
"""
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import config
from services import plex_auth
from services.settings_service import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings")

MIN_PASSWORD_LENGTH = 8


def _view() -> dict:
    return {**store.public_view(), "username": config.FADERR_USERNAME}


def _require_editable(*keys: str) -> None:
    for key in keys:
        if store.current.sources.get(key) == "env":
            raise HTTPException(409, "This is set in the environment (.env), so it can't be changed here. "
                                     "Remove it from .env to manage it from this page.")


@router.get("")
async def get_settings():
    await store.load()
    return _view()


# ── Plex: Sign in with Plex ───────────────────────────────────────────────────

@router.post("/plex/pin")
async def plex_pin():
    _require_editable("plex_url", "plex_token", "plex_library")
    client_id = await store.plex_client_id()
    try:
        return await plex_auth.create_pin(client_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Couldn't reach plex.tv to start signing in: {exc}")


@router.get("/plex/pin/{pin_id}")
async def plex_pin_status(pin_id: int):
    """Poll until the user approves the sign-in; then list their servers."""
    client_id = await store.plex_client_id()
    try:
        account_token = await plex_auth.check_pin(client_id, pin_id)
    except plex_auth.PinExpired:
        raise HTTPException(410, "This sign-in expired. Start again.")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Couldn't reach plex.tv: {exc}")
    if not account_token:
        return {"authorized": False}
    try:
        servers = await plex_auth.list_servers(client_id, account_token)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Signed in, but couldn't list your Plex servers: {exc}")
    await store.set_pending({"servers": servers})
    return {
        "authorized": True,
        "servers": [{"id": s["id"], "name": s["name"], "owned": s["owned"]} for s in servers],
    }


class ServerChoice(BaseModel):
    server_id: str


@router.post("/plex/server")
async def plex_choose_server(body: ServerChoice):
    pending = await store.get_pending()
    if not pending:
        raise HTTPException(409, "Sign in with Plex first (or the sign-in expired).")
    server = next((s for s in pending.get("servers", []) if s["id"] == body.server_id), None)
    if server is None:
        raise HTTPException(404, "That server isn't on this Plex account.")
    client_id = await store.plex_client_id()
    try:
        url, info = await plex_auth.first_working(server, client_id)
    except plex_auth.NoWorkingConnection as exc:
        raise HTTPException(502, str(exc))
    pending["chosen"] = {"url": url, "token": server["access_token"], "name": info["name"]}
    await store.set_pending(pending)
    return {"server_name": info["name"], "url": url, "libraries": info["libraries"]}


class LibraryChoice(BaseModel):
    library: str


@router.post("/plex/library")
async def plex_choose_library(body: LibraryChoice):
    _require_editable("plex_url", "plex_token", "plex_library")
    pending = await store.get_pending()
    chosen = (pending or {}).get("chosen")
    if not chosen:
        raise HTTPException(409, "Choose a server first (or the sign-in expired).")
    client_id = await store.plex_client_id()
    try:
        info = await plex_auth.server_info(chosen["url"], chosen["token"], client_id)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Couldn't reach the Plex server: {exc}")
    if body.library not in info["libraries"]:
        raise HTTPException(400, f"No music library called {body.library!r} on {info['name']}.")
    await store.save({
        "plex_url": chosen["url"], "plex_token": chosen["token"],
        "plex_library": body.library, "plex_server_name": info["name"],
    })
    await store.clear_pending()
    return _view()


# ── Plex: URL and token by hand ───────────────────────────────────────────────

class ManualPlex(BaseModel):
    url: str
    token: Optional[str] = None  # blank: keep the saved token
    library: Optional[str] = None  # blank: just test and list libraries


@router.post("/plex/manual")
async def plex_manual(body: ManualPlex):
    _require_editable("plex_url", "plex_token", "plex_library")
    token = (body.token or "").strip() or store.current.get("plex_token")
    url = body.url.strip().rstrip("/")
    if not url or not token:
        raise HTTPException(400, "Enter the server URL and a token.")
    try:
        info = await plex_auth.server_info(url, token, await store.plex_client_id())
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Couldn't connect to Plex at {url}: {exc}")
    if not body.library:
        return {"server_name": info["name"], "libraries": info["libraries"]}
    if body.library not in info["libraries"]:
        raise HTTPException(400, f"No music library called {body.library!r} on {info['name']}.")
    await store.save({"plex_url": url, "plex_token": token, "plex_library": body.library,
                      "plex_server_name": info["name"]})
    return _view()


# ── Lidarr and Last.fm ────────────────────────────────────────────────────────

class LidarrBody(BaseModel):
    url: str
    api_key: Optional[str] = None  # blank: keep the saved key


@router.post("/lidarr")
async def save_lidarr(body: LidarrBody):
    _require_editable("lidarr_url", "lidarr_api_key")
    url = body.url.strip().rstrip("/")
    api_key = (body.api_key or "").strip() or store.current.get("lidarr_api_key")
    if not url or not api_key:
        raise HTTPException(400, "Enter the Lidarr URL and API key.")
    try:
        async with plex_auth.http_client() as client:
            resp = await client.get(f"{url}/api/v1/system/status", headers={"X-Api-Key": api_key})
        if resp.status_code == 401:
            raise HTTPException(400, "Lidarr rejected the API key.")
        resp.raise_for_status()
        version = resp.json().get("version")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Couldn't connect to Lidarr at {url}: {exc}")
    await store.save({"lidarr_url": url, "lidarr_api_key": api_key})
    return {**_view(), "lidarr_version": version}


class LastfmBody(BaseModel):
    api_key: Optional[str] = None  # blank: remove it (Last.fm is optional)


@router.post("/lastfm")
async def save_lastfm(body: LastfmBody):
    _require_editable("lastfm_api_key")
    api_key = (body.api_key or "").strip()
    if api_key:
        try:
            async with plex_auth.http_client() as client:
                resp = await client.get("https://ws.audioscrobbler.com/2.0/", params={
                    "method": "artist.getinfo", "artist": "Cher", "api_key": api_key, "format": "json",
                })
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f"Couldn't reach Last.fm: {exc}")
        if data.get("error"):
            raise HTTPException(400, f"Last.fm rejected the key: {data.get('message', 'error')}")
    await store.save({"lastfm_api_key": api_key or None})
    return _view()


# ── Login password ────────────────────────────────────────────────────────────

class PasswordBody(BaseModel):
    password: Optional[str] = None
    remove: bool = False


@router.post("/password")
async def set_password(body: PasswordBody):
    if store.current.password_env:
        raise HTTPException(409, "The password is set with FADERR_PASSWORD in .env, so it can't be changed here.")
    if body.remove:
        await store.set_password(None)
        return _view()
    if not body.password or len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    await store.set_password(body.password)
    return _view()
