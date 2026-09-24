import asyncio
import base64
import binascii
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
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
from services import deletion_service, triage_service
from services.generation_service import runner as generation_runner
from services.plex_service import plex

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
    await plex.start()
    deletion_service.worker.start()
    try:
        yield
    finally:
        await deletion_service.worker.stop()
        await generation_runner.stop()
        await plex.aclose()


# Resolved from this file, not the working directory, so the app also starts
# from a service manager that runs it elsewhere.
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Faderr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    """Service functions report failures as {"error": message, "status_code": code}.
    (Only that shape: a deletion record also has an "error" field, holding its failure reason.)"""
    if "status_code" in result and "error" in result:
        raise HTTPException(status_code=result["status_code"], detail=result["error"])
    return result


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Generation (SSE) ──────────────────────────────────────────────────────────

@app.get("/api/generate/status")
async def generate_status():
    return {"running": (await generation_runner.status())["running"]}


@app.post("/api/generate/stream")
async def generate_stream():
    """Start playlist generation (or join the run in progress, in any server
    process) and stream its progress as Server-Sent Events. Progress is read
    from the database, and a client disconnecting doesn't cancel the run."""
    await generation_runner.start()

    async def event_stream():
        last_sent = None
        idle = 0.0
        while True:
            state = await generation_runner.status()
            event = state["progress"] if state["running"] else (state["result"] or state["progress"])
            if event is not None and event != last_sent:
                yield f"data: {json.dumps(event)}\n\n"
                last_sent, idle = event, 0.0
            if not state["running"]:
                break
            await asyncio.sleep(0.3)
            idle += 0.3
            if idle >= 15:
                yield ": keepalive\n\n"
                idle = 0.0

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Plex playlist ─────────────────────────────────────────────────────────────

@app.post("/api/playlist/sync")
async def sync_playlist():
    """Rebuild the Plex playlist from the current queue."""
    try:
        return await triage_service.sync_playlist()
    except Exception as exc:
        logger.exception("Playlist sync failed")
        raise HTTPException(status_code=502, detail=f"Couldn't update the Plex playlist: {exc}")


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
    artist = await triage_service.get_artist_row(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    return {"bio": await lfm.get_artist_bio(artist.artist_name)}


@app.get("/api/artists/{artist_id}/tracks")
async def get_artist_tracks(artist_id: int):
    """Return all tracks for an artist (shuffled), excluding the triage track."""
    artist = await triage_service.get_artist_row(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    try:
        return await plex.other_tracks(artist.plex_artist_key, artist.track_key)
    except Exception as exc:
        logger.exception("Failed to get tracks for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Media proxy ───────────────────────────────────────────────────────────────
# Audio and artwork are fetched from Plex by the server and relayed to the
# browser, so the Plex token (which grants admin access to Plex) never leaves
# the server, and the browser only needs to reach Faderr, not Plex.

async def _proxy_plex(request: Request, path: str) -> StreamingResponse:
    client: httpx.AsyncClient = plex.http
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
    try:
        key = await plex.track_stream_key(str(rating_key))
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

async def _handled(description: str, coro) -> dict:
    """Await a service call, turning its error dicts and exceptions into HTTP errors."""
    try:
        return _raise_for_result(await coro)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("%s failed", description)
        raise HTTPException(status_code=500, detail=str(exc))


class DecisionBody(BaseModel):
    decision: str


@app.post("/api/artists/{artist_id}/decide")
async def decide(artist_id: int, body: DecisionBody):
    if body.decision not in triage_service.DECISION_CHOICES:
        raise HTTPException(status_code=400, detail=f"decision must be one of {triage_service.DECISION_CHOICES}")
    return await _handled(f"Decision for artist_id={artist_id}", triage_service.make_decision(artist_id, body.decision))


@app.post("/api/artists/{artist_id}/undo")
async def undo(artist_id: int):
    return await _handled(f"Undo for artist_id={artist_id}", triage_service.undo_decision(artist_id))


@app.post("/api/artists/{artist_id}/skip")
async def skip(artist_id: int):
    return await _handled(f"Skip for artist_id={artist_id}", triage_service.skip_track(artist_id))


class NotesBody(BaseModel):
    notes: str


@app.patch("/api/artists/{artist_id}/notes")
async def notes(artist_id: int, body: NotesBody):
    return await _handled(f"Notes update for artist_id={artist_id}", triage_service.update_notes(artist_id, body.notes))


# ── Deletions (queue and audit log) ───────────────────────────────────────────

@app.get("/api/deletions")
async def deletions():
    return await deletion_service.list_jobs()


@app.post("/api/deletions/{job_id}/cancel")
async def cancel_deletion(job_id: int):
    return await _handled(f"Cancel deletion {job_id}", triage_service.cancel_deletion(job_id))


@app.post("/api/deletions/{job_id}/retry")
async def retry_deletion(job_id: int):
    job = await deletion_service.retry(job_id)
    if job is None:
        raise HTTPException(status_code=409, detail="Only a failed delete can be retried.")
    return job.to_dict()
