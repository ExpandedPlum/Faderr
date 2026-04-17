import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from config import config
from models import TriageArtist, async_session, init_db
from services import triage_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="Faderr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Generation (SSE) ──────────────────────────────────────────────────────────

@app.post("/api/generate/stream")
async def generate_stream():
    """Stream playlist generation progress as Server-Sent Events."""
    queue: asyncio.Queue = asyncio.Queue()

    async def on_progress(event: dict):
        await queue.put(event)

    async def run_generation():
        try:
            await triage_service.generate_triage_playlist(on_progress=on_progress)
        except Exception as exc:
            logger.exception("Generation failed")
            await queue.put({"stage": "error", "message": str(exc)})

    async def event_stream():
        task = asyncio.create_task(run_generation())
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=120.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break
        finally:
            task.cancel()

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


@app.get("/api/artists/exploring")
async def exploring_artists():
    return await triage_service.get_exploring_artists()


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
        tracks = ps.get_all_tracks(artist["plex_artist_key"], exclude_key=artist["track_key"])
        return tracks
    except Exception as exc:
        logger.exception("Failed to get tracks for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Streaming ─────────────────────────────────────────────────────────────────

@app.get("/api/stream-key")
async def stream_by_key(key: str):
    """Stream a track directly by its Plex part path (used by the play queue)."""
    url = f"{config.PLEX_URL.rstrip('/')}{key}?X-Plex-Token={config.PLEX_TOKEN}&download=0"
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/stream/{artist_id}")
async def stream(artist_id: int):
    """Redirect to the Plex direct-play URL for this artist's triage track."""
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        artist = result.scalars().first()

    if not artist or not artist.stream_key:
        raise HTTPException(status_code=404, detail="Stream not available for this artist")

    url = f"{config.PLEX_URL.rstrip('/')}{artist.stream_key}?X-Plex-Token={config.PLEX_TOKEN}&download=0"
    return RedirectResponse(url=url, status_code=302)


# ── Decisions ─────────────────────────────────────────────────────────────────

class DecisionBody(BaseModel):
    decision: str


@app.post("/api/artists/{artist_id}/decide")
async def decide(artist_id: int, body: DecisionBody):
    valid = {"keep", "explore", "delete", "explore_keep", "explore_delete"}
    if body.decision not in valid:
        raise HTTPException(status_code=400, detail=f"decision must be one of {valid}")
    try:
        result = await triage_service.make_decision(artist_id, body.decision)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Decision failed for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/artists/{artist_id}/undo")
async def undo(artist_id: int):
    try:
        result = await triage_service.undo_decision(artist_id)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Undo failed for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/artists/{artist_id}/skip")
async def skip(artist_id: int):
    try:
        result = await triage_service.skip_track(artist_id)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
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
        result = await triage_service.update_notes(artist_id, body.notes)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Notes update failed for artist_id=%s", artist_id)
        raise HTTPException(status_code=500, detail=str(exc))
