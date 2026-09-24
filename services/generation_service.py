"""Building the triage queue from Plex, and running that as a background job.

The queue is updated in place, one row per Plex artist (the table enforces
it): existing undecided rows keep their ID, notes and any track chosen with
Skip; decided rows are never touched; only undecided rows for artists that
have left Plex are removed.

Runs are coordinated through the single `generation_state` row, so they are
exclusive across every server process, and progress is stored there so any
process can stream it. A client disconnecting never cancels a run.
"""
import asyncio
import json
import logging
import os
import socket
import time
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

import httpx
from sqlalchemy import delete, select, update

from config import config
from models import GenerationState, TriageArtist, async_session, utcnow
from services import lastfm_service, triage_service
from services.lidarr_service import lidarr, resolve_artist
from services.plex_service import plex

logger = logging.getLogger(__name__)

# How many Plex track lookups run at once.
_PLEX_CONCURRENCY = 4
# A run's lock is considered abandoned (e.g. the process died) after this long
# without a heartbeat, and another run may take it over.
LOCK_STALE_AFTER = timedelta(minutes=5)
_HEARTBEAT_SECONDS = 30
# Progress is written at most this often (stage changes are always written).
_PROGRESS_MIN_INTERVAL = 0.25

ProgressFn = Callable[[dict], Awaitable[None]]


async def _noop_progress(evt: dict) -> None:
    return None


async def generate(on_progress: ProgressFn = _noop_progress) -> dict:
    """Refresh the triage queue from Plex. Returns a summary."""
    logger.info("Starting generation...")
    await on_progress({"stage": "plex_fetch"})
    plex_artists = await plex.all_artists()
    logger.info("Found %d artists in Plex", len(plex_artists))

    await on_progress({"stage": "lidarr_fetch"})
    try:
        lidarr_artists = await lidarr.all_artists()
        logger.info("Found %d artists in Lidarr", len(lidarr_artists))
    except Exception as exc:
        logger.warning("Could not reach Lidarr: %s — lidarr_id will be NULL for all", exc)
        lidarr_artists = []

    # Snapshot of existing rows. Decided artists need no work at all.
    existing = await asyncio.shield(_snapshot())
    todo = [a for a in plex_artists if existing.get(a["rating_key"], (None,))[0] is None]
    logger.info("%d artists to (re)resolve; %d already decided",
                len(todo), len(plex_artists) - len(todo))

    # Last.fm top tracks, emitting progress per lookup
    total = len(todo)
    lastfm_results: dict[str, Any] = {}
    done_count = 0

    async def tracked_lookup(name: str, client: httpx.AsyncClient):
        nonlocal done_count
        result = await lastfm_service.get_top_track(name, client)
        done_count += 1
        await on_progress({"stage": "lastfm", "done": done_count, "total": total})
        return name, result

    async with httpx.AsyncClient() as client:
        pairs = await asyncio.gather(
            *(tracked_lookup(a["name"], client) for a in todo), return_exceptions=True
        )
    for item in pairs:
        if not isinstance(item, Exception):
            lastfm_results[item[0]] = item[1]

    # Resolve a track for each artist, a few at a time
    semaphore = asyncio.Semaphore(_PLEX_CONCURRENCY)
    resolved_count = 0
    failed: list[str] = []

    async def resolve(artist: dict) -> Optional[dict]:
        nonlocal resolved_count
        name, key = artist["name"], artist["rating_key"]
        _, old_track, pinned = existing.get(key, (None, None, False))
        try:
            async with semaphore:
                try:
                    track, source, file_paths = await plex.resolve_track(
                        key, lastfm_results.get(name), old_track if pinned else None,
                    )
                except Exception as exc:
                    # One broken artist shouldn't sink the whole run
                    logger.warning("Couldn't load tracks for %r, skipping: %s", name, exc)
                    failed.append(name)
                    return None
            if not track:
                logger.warning("No tracks found for artist %r, skipping", name)
                return {"key": key, "no_tracks": True}
            match = resolve_artist(name, file_paths, lidarr_artists, artist.get("mbid"))
            return {
                "key": key,
                "name": name,
                "thumb": artist.get("thumb"),
                "mbid": artist.get("mbid"),
                "track": track,
                # "kept" means the pinned (skipped-to) track still exists: leave it alone
                "source": None if source == "kept" else source,
                "lidarr_id": match.lidarr_id,
            }
        finally:
            resolved_count += 1
            await on_progress({"stage": "resolving", "done": resolved_count, "total": total})

    resolved = [r for r in await asyncio.gather(*(resolve(a) for a in todo)) if r is not None]

    await on_progress({"stage": "saving"})
    # Shielded: a shutdown mid-save must not abandon the transaction (and its lock)
    counts = await asyncio.shield(_save(resolved, {a["rating_key"] for a in plex_artists}))
    logger.info("Queue updated: %s", counts)

    # The Plex playlist is derived from the queue; it isn't needed for triage
    await on_progress({"stage": "playlist_create"})
    summary: dict[str, Any] = {**counts, "playlist_name": config.TRIAGE_PLAYLIST_NAME}
    try:
        await triage_service.sync_playlist()
    except Exception as exc:
        logger.exception("Creating the Plex playlist failed")
        summary["playlist_warning"] = f"The Plex playlist couldn't be created: {exc}"
    if failed:
        summary["failed_artists"] = failed
    return summary


async def _snapshot() -> dict[str, tuple]:
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist.plex_artist_key, TriageArtist.decision,
                   TriageArtist.track_key, TriageArtist.track_pinned)
        )
        return {key: (decision, track_key, pinned) for key, decision, track_key, pinned in result.all()}


async def _save(resolved: list[dict], plex_keys: set[str]) -> dict:
    """Write the results in one short transaction (no network calls inside).
    Rows are re-read here: the UI stays usable during generation, so decisions
    made since the snapshot win and are never overwritten."""
    added = updated = removed = 0
    async with async_session() as session:
        async with session.begin():
            rows = {r.plex_artist_key: r for r in (await session.execute(select(TriageArtist))).scalars()}
            for item in resolved:
                row = rows.get(item["key"])
                if item.get("no_tracks"):
                    # Nothing to play: drop an undecided row rather than offer a dead track
                    if row is not None and row.decision is None:
                        await session.delete(row)
                        removed += 1
                    continue
                if row is not None and row.decision is not None:
                    continue  # decided while this run was going
                track = item["track"]
                if row is None:
                    session.add(TriageArtist(
                        artist_name=item["name"], plex_artist_key=item["key"], mbid=item["mbid"],
                        thumb_url=item["thumb"], track_title=track["title"], track_key=track["rating_key"],
                        stream_key=track.get("stream_key"), source=item["source"], lidarr_id=item["lidarr_id"],
                    ))
                    added += 1
                    continue
                row.artist_name = item["name"]
                row.thumb_url = item["thumb"]
                row.mbid = item["mbid"]
                row.lidarr_id = item["lidarr_id"]
                if item["source"] is not None:  # a new pick (not the kept pinned track)
                    row.track_title = track["title"]
                    row.track_key = track["rating_key"]
                    row.stream_key = track.get("stream_key")
                    row.source = item["source"]
                    row.track_pinned = False
                updated += 1

            vanished = [r.id for key, r in rows.items() if key not in plex_keys and r.decision is None]
            for i in range(0, len(vanished), 500):
                await session.execute(delete(TriageArtist).where(TriageArtist.id.in_(vanished[i:i + 500])))
            removed += len(vanished)

        total = await triage_service.count_undecided(session)
    return {"total_artists": total, "added": added, "updated": updated, "removed": removed}


# ── Background runs, coordinated through the database ────────────────────────

class GenerationRunner:
    def __init__(self):
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._task: Optional[asyncio.Task] = None
        self._last_write = 0.0
        self._last_stage: Optional[str] = None

    async def start(self) -> bool:
        """Start a run unless one is already going (in any process).
        Returns True if this call started it."""
        now = utcnow()
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(GenerationState)
                    .where(GenerationState.id == 1)
                    .where((GenerationState.owner.is_(None)) | (GenerationState.heartbeat_at < now - LOCK_STALE_AFTER))
                    .values(owner=self.owner, started_at=now, heartbeat_at=now, finished_at=None,
                            progress=json.dumps({"stage": "starting"}), result=None)
                )
        if result.rowcount != 1:
            return False
        self._last_stage = None
        self._task = asyncio.create_task(self._run())
        return True

    async def stop(self) -> None:
        """Cancel this process's run (server shutdown); the lock is released."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def status(self) -> dict:
        async with async_session() as session:
            row = await session.get(GenerationState, 1)
        running = row is not None and row.owner is not None and (
            row.heartbeat_at is not None and row.heartbeat_at >= utcnow() - LOCK_STALE_AFTER
        )
        return {
            "running": running,
            "progress": json.loads(row.progress) if row and row.progress else None,
            "result": json.loads(row.result) if row and row.result else None,
        }

    async def _write(self, **values) -> None:
        # Shielded so that cancelling the run (server shutdown) can't abandon
        # this transaction halfway and leave the database locked
        await asyncio.shield(self._write_now(values))

    async def _write_now(self, values: dict) -> None:
        async with async_session() as session:
            async with session.begin():
                await session.execute(
                    update(GenerationState)
                    .where(GenerationState.id == 1, GenerationState.owner == self.owner)
                    .values(**values)
                )

    async def _progress(self, event: dict) -> None:
        now = time.monotonic()
        stage = event.get("stage")
        if stage == self._last_stage and now - self._last_write < _PROGRESS_MIN_INTERVAL:
            return
        self._last_stage, self._last_write = stage, now
        await self._write(progress=json.dumps(event), heartbeat_at=utcnow())

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            try:
                await self._write(heartbeat_at=utcnow())
            except Exception as exc:
                logger.warning("Generation heartbeat failed: %s", exc)

    async def _run(self) -> None:
        heartbeat = asyncio.create_task(self._heartbeat())
        final: dict = {"stage": "error", "message": "Generation was interrupted"}
        try:
            final = {"stage": "done", **await generate(on_progress=self._progress)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Generation failed")
            final = {"stage": "error", "message": str(exc)}
        finally:
            heartbeat.cancel()
            payload = json.dumps(final)
            try:
                await self._write(owner=None, finished_at=utcnow(), progress=payload, result=payload)
            except Exception as exc:
                logger.error("Couldn't release the generation lock: %s", exc)


runner = GenerationRunner()
