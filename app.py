import asyncio
import base64
import binascii
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

from config import config
from models import init_db
from services import triage_service
from services.generation_runner import runner as generation_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# Header the frontend sends on every state-changing request. A cross-site page
# can't add a custom header without a CORS preflight (which this app never
# approves), so requiring it blocks CSRF against the decide/delete endpoints.
CSRF_HEADER = "x-faderr-request"

# Only these Plex paths may be proxied.
_PART_KEY_RE = re.compile(r"^/library/parts/\d+/[^?#]*$")
_THUMB_PATH_RE = re.compile(r"^/library/metadata/\d+/[A-Za-z]+(/\d+)?$")

# Response headers passed through from Plex when proxying media.
_PASSTHROUGH_HEADERS = (
    "content-type", "content-length", "content-range", "accept-ranges",
    "content-encoding", "last-modified", "etag", "cache-control",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    if not config.FADERR_PASSWORD:
        logger.warning(
            "FADERR_PASSWORD is not set: anyone who can reach this server can delete artists. "
            "Set FADERR_PASSWORD in .env to require a login."
        )
    app.state.plex_http = httpx.AsyncClient(
        base_url=config.PLEX_URL.rstrip("/"),
        headers={"X-Plex-Token": config.PLEX_TOKEN},
        timeout=httpx.Timeout(15.0, read=None),
    )
    try:
        yield
    finally:
        await app.state.plex_http.aclose()


app = FastAPI(title="Faderr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Security ──────────────────────────────────────────────────────────────────

def _basic_auth_ok(header: Optional[str]) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, sep, password = decoded.partition(":")
    if not sep:
        return False
    user_ok = secrets.compare_digest(username.encode(), config.FADERR_USERNAME.encode())
    pass_ok = secrets.compare_digest(password.encode(), config.FADERR_PASSWORD.encode())
    return user_ok and pass_ok


@app.middleware("http")
async def security(request: Request, call_next):
    if config.FADERR_PASSWORD and not _basic_auth_ok(request.headers.get("authorization")):
        return Response(
            "Authentication required", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Faderr", charset="UTF-8"'},
        )
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        return JSONResponse({"detail": "Missing request header"}, status_code=403)
    return await call_next(request)


def _raise_for_result(result: dict) -> dict:
    if "error" in result:
        raise HTTPException(status_code=result.get("status_code", 404), detail=result["error"])
    return result


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Generation (SSE) ──────────────────────────────────────────────────────────

@app.get("/api/generate/status")
async def generate_status():
    return {"running": generation_runner.running}


@app.post("/api/generate/stream")
async def generate_stream():
    """Start playlist generation (or join the run in progress) and stream its
    progress as Server-Sent Events. Generation runs in the background, so a
    client disconnecting doesn't cancel it."""
    queue = generation_runner.subscribe()
    generation_runner.start()

    async def event_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break
        finally:
            generation_runner.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Artist reads ──────────────────────────────────────────────────────────────

@app.get("/api/artists")
async def list_artists(status: Optional[str] = None, search: Optional[str] = None):
    return await triage_service.get_all_artists_for_list(status=status, search=search)


@app.get("/api/artists/current")
async def current_artist():
    artist = await triage_service.get_current_artist()
    if artist is None:
        return {"done": True}
    return artist


@app.get("/api/artists/history")
async def history(decision: Optional[str] = None):
    return await triage_service.get_history(decision)


@app.get("/api/stats")
async def stats():
    return await triage_service.get_stats()


@app.get("/api/artists/{artist_id}")
async def get_artist(artist_id: int):
    artist = await triage_service.get_artist_by_id(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    return artist


@app.get("/api/artists/{artist_id}/bio")
async def get_bio(artist_id: int):
    from services import lastfm_service as lfm
    artist = await triage_service.get_artist_by_id(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    bio = await lfm.get_artist_bio(artist["artist_name"])
    return {"bio": bio}


@app.get("/api/artists/{artist_id}/tracks")
async def get_artist_tracks(artist_id: int):
    """Return all tracks for an artist (shuffled), excluding the triage track."""
    from services import plex_service as ps
    artist = await triage_service.get_artist_by_id(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    try:
        return await asyncio.to_thread(ps.get_all_tracks, artist["plex_artist_key"], artist["track_key"])
    except Exception as exc:
        logger.exception("Failed to get tracks for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Media proxy ───────────────────────────────────────────────────────────────
# Audio and artwork are fetched from Plex by the server and relayed to the
# browser, so the Plex token (which grants admin access to Plex) never leaves
# the server, and the browser only needs to reach Faderr, not Plex.

async def _proxy_plex(request: Request, path: str) -> StreamingResponse:
    client: httpx.AsyncClient = request.app.state.plex_http
    headers = {}
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    try:
        upstream = await client.send(client.build_request("GET", path, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        logger.warning("Plex request failed for %s: %s", path, exc)
        raise HTTPException(status_code=502, detail="Couldn't reach Plex")
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(status_code=502, detail=f"Plex returned {upstream.status_code}")
    passthrough = {k: upstream.headers[k] for k in _PASSTHROUGH_HEADERS if k in upstream.headers}
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=passthrough,
        background=BackgroundTask(upstream.aclose),
    )


def _checked_part_key(key: Optional[str]) -> str:
    if not key or not _PART_KEY_RE.match(key) or ".." in key:
        raise HTTPException(status_code=404, detail="Stream not available")
    return key


@app.get("/api/stream/{artist_id}")
async def stream(artist_id: int, request: Request):
    """Stream this artist's triage track."""
    artist = await triage_service.get_artist_row(artist_id)
    if not artist or not artist.stream_key:
        raise HTTPException(status_code=404, detail="Stream not available for this artist")
    return await _proxy_plex(request, _checked_part_key(artist.stream_key))


@app.get("/api/tracks/{rating_key}/stream")
async def stream_track(rating_key: int, request: Request):
    """Stream any library track by its Plex rating key (used by the play queue)."""
    from services import plex_service as ps
    try:
        key = await asyncio.to_thread(ps.get_track_stream_key, str(rating_key))
    except Exception as exc:
        logger.warning("Track lookup failed for rating_key=%s: %s", rating_key, exc)
        raise HTTPException(status_code=404, detail="Track not found")
    return await _proxy_plex(request, _checked_part_key(key))


@app.get("/api/artists/{artist_id}/thumb")
async def artist_thumb(artist_id: int, request: Request):
    artist = await triage_service.get_artist_row(artist_id)
    if not artist or not artist.thumb_url:
        raise HTTPException(status_code=404, detail="No thumbnail")
    # Rows created before the proxy existed hold a full URL (with the token);
    # only the path is ever used.
    path = urlsplit(artist.thumb_url).path
    if not _THUMB_PATH_RE.match(path):
        raise HTTPException(status_code=404, detail="No thumbnail")
    return await _proxy_plex(request, path)


# ── Decisions ─────────────────────────────────────────────────────────────────

class DecisionBody(BaseModel):
    decision: str


@app.post("/api/artists/{artist_id}/decide")
async def decide(artist_id: int, body: DecisionBody):
    valid = {"keep", "explore", "delete", "explore_keep", "explore_delete"}
    if body.decision not in valid:
        raise HTTPException(status_code=400, detail=f"decision must be one of {valid}")
    try:
        return _raise_for_result(await triage_service.make_decision(artist_id, body.decision))
    except HTTPException:
        raise
    except triage_service.DeletionError as exc:
        logger.warning("Deletion refused for artist_id=%s: %s", artist_id, exc)
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("Decision failed for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/artists/{artist_id}/undo")
async def undo(artist_id: int):
    try:
        return _raise_for_result(await triage_service.undo_decision(artist_id))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Undo failed for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/artists/{artist_id}/skip")
async def skip(artist_id: int):
    try:
        return _raise_for_result(await triage_service.skip_track(artist_id))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Skip failed for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


class NotesBody(BaseModel):
    notes: str


@app.patch("/api/artists/{artist_id}/notes")
async def notes(artist_id: int, body: NotesBody):
    try:
        return _raise_for_result(await triage_service.update_notes(artist_id, body.notes))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Notes update failed for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))
