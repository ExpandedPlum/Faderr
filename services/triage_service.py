"""Triage queue reads, decisions and the derived Plex playlist.

Decisions are enforced by the database, not by in-process locks: every change
is a conditional UPDATE (e.g. "set keep WHERE the artist isn't being
deleted"), so the rules hold even with several server processes. No
transaction is ever held open across a call to Plex or Lidarr.
"""
import logging
from typing import Optional

from sqlalchemy import func, or_, select, update

from config import config
from models import Deletion, TriageArtist, async_session, utcnow
from services import deletion_service
from services.plex_service import plex

logger = logging.getLogger(__name__)

DECISION_CHOICES = ("keep", "delete")


def _error(message: str, status_code: int = 404) -> dict:
    return {"error": message, "status_code": status_code}


_NOT_FOUND = _error("Artist not found")


async def _artist_dict(session, artist: TriageArtist) -> dict:
    jobs = await deletion_service.latest_jobs(session, [artist.id])
    return artist.to_dict(jobs.get(artist.id))


async def _artist_dicts(session, artists: list[TriageArtist]) -> list[dict]:
    deleted_ids = [a.id for a in artists if a.decision == "delete"]
    jobs = await deletion_service.latest_jobs(session, deleted_ids)
    return [a.to_dict(jobs.get(a.id)) for a in artists]


# ── Reads ─────────────────────────────────────────────────────────────────────

async def get_all_artists_for_list(status: Optional[str] = None, search: Optional[str] = None) -> list[dict]:
    """Return all artists, optionally filtered by decision status and/or name search."""
    async with async_session() as session:
        q = select(TriageArtist).order_by(TriageArtist.artist_name)
        if status == "undecided":
            q = q.where(TriageArtist.decision.is_(None))
        elif status in DECISION_CHOICES:
            q = q.where(TriageArtist.decision == status)
        if search:
            q = q.where(TriageArtist.artist_name.ilike(f"%{search}%"))
        artists = list((await session.execute(q)).scalars())
        return await _artist_dicts(session, artists)


async def get_artist_row(artist_id: int) -> Optional[TriageArtist]:
    async with async_session() as session:
        return await session.get(TriageArtist, artist_id)


async def get_artist_by_id(artist_id: int) -> Optional[dict]:
    async with async_session() as session:
        artist = await session.get(TriageArtist, artist_id)
        return await _artist_dict(session, artist) if artist else None


async def get_current_artist() -> Optional[dict]:
    """Return the first undecided artist."""
    async with async_session() as session:
        artist = (await session.execute(
            select(TriageArtist).where(TriageArtist.decision.is_(None)).order_by(TriageArtist.id).limit(1)
        )).scalars().first()
        return artist.to_dict() if artist else None


async def count_undecided(session) -> int:
    return (await session.execute(
        select(func.count()).select_from(TriageArtist).where(TriageArtist.decision.is_(None))
    )).scalar()


async def get_stats() -> dict:
    async with async_session() as session:
        counts = dict((await session.execute(
            select(TriageArtist.decision, func.count()).group_by(TriageArtist.decision)
        )).all())
        jobs = dict((await session.execute(
            select(Deletion.status, func.count())
            .where(Deletion.status.in_(("pending", "running", "failed")))
            .group_by(Deletion.status)
        )).all())
    total = sum(counts.values())
    undecided = counts.get(None, 0)
    return {
        "total": total,
        "triaged": total - undecided,
        "remaining": undecided,
        "keep": counts.get("keep", 0),
        "deleted": counts.get("delete", 0),
        "delete_pending": jobs.get("pending", 0) + jobs.get("running", 0),
        "delete_failed": jobs.get("failed", 0),
    }


async def get_history(decision_filter: Optional[str] = None) -> list[dict]:
    async with async_session() as session:
        q = select(TriageArtist).where(TriageArtist.decision.is_not(None))
        if decision_filter in DECISION_CHOICES:
            q = q.where(TriageArtist.decision == decision_filter)
        q = q.order_by(TriageArtist.decided_at.desc())
        artists = list((await session.execute(q)).scalars())
        return await _artist_dicts(session, artists)


# ── Decisions ─────────────────────────────────────────────────────────────────

async def make_decision(artist_id: int, decision: str) -> dict:
    """Record Keep, or queue a Delete (which runs after the grace period).
    A deleted or pending-delete artist can't be given another decision; undo
    the delete first while it's still pending."""
    if decision not in DECISION_CHOICES:
        return _error(f"Unknown decision: {decision}", 400)
    now = utcnow()
    async with async_session() as session:
        async with session.begin():
            artist = await session.get(TriageArtist, artist_id)
            if artist is None:
                return _NOT_FOUND
            previous = artist.decision
            result = await session.execute(
                update(TriageArtist)
                .where(TriageArtist.id == artist_id)
                .where(or_(TriageArtist.decision.is_(None), TriageArtist.decision != "delete"))
                .values(decision=decision, decided_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return _error(
                    "This artist is being deleted or has been deleted. "
                    "A pending delete can be undone from History.", 409,
                )
            if decision == "delete":
                session.add(deletion_service.new_job(artist, previous))
        await session.refresh(artist)
        response = await _artist_dict(session, artist)
    if decision == "delete":
        deletion_service.worker.wake()
    return response


async def undo_decision(artist_id: int) -> dict:
    """Return an artist to undecided. A delete can only be undone while its
    job hasn't started (pending) or has failed; that restores the decision
    the artist had before."""
    async with async_session() as session:
        async with session.begin():
            artist = await session.get(TriageArtist, artist_id)
            if artist is None:
                return _NOT_FOUND
            if artist.decision == "delete":
                job = await deletion_service.active_job(session, artist_id)
                if job is None or not await deletion_service.cancel(session, job):
                    return _error("This delete can't be undone: it has already run or is running.", 409)
            elif artist.decision is not None:
                await session.execute(
                    update(TriageArtist)
                    .where(TriageArtist.id == artist_id, TriageArtist.decision == artist.decision)
                    .values(decision=None, decided_at=None)
                    .execution_options(synchronize_session=False)
                )
        await session.refresh(artist)
        return await _artist_dict(session, artist)


async def cancel_deletion(job_id: int) -> dict:
    """Cancel a pending or failed deletion job (same as undoing the delete)."""
    async with async_session() as session:
        async with session.begin():
            job = await session.get(Deletion, job_id)
            if job is None:
                return _error("Deletion not found")
            if not await deletion_service.cancel(session, job):
                return _error("This delete can't be cancelled: it has already run or is running.", 409)
        await session.refresh(job)
        return job.to_dict()


async def skip_track(artist_id: int) -> dict:
    """Pick a different random track for this artist. The pick is pinned, so
    regenerating keeps it."""
    artist = await get_artist_row(artist_id)
    if artist is None:
        return _NOT_FOUND
    if artist.decision == "delete":
        return _error("This artist has been deleted.", 409)
    alternatives = await plex.other_tracks(artist.plex_artist_key, artist.track_key, count=1)
    if not alternatives:
        return _error("No alternative tracks found for this artist")

    new_track = alternatives[0]
    async with async_session() as session:
        async with session.begin():
            # Only if nothing changed meanwhile (another skip, or a delete)
            result = await session.execute(
                update(TriageArtist)
                .where(TriageArtist.id == artist_id, TriageArtist.track_key == artist.track_key)
                .where(or_(TriageArtist.decision.is_(None), TriageArtist.decision != "delete"))
                .values(track_title=new_track["title"], track_key=new_track["rating_key"],
                        stream_key=new_track.get("stream_key"), source="plex_random", track_pinned=True)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return _error("The artist changed while picking a new track; try again.", 409)
        artist = await session.get(TriageArtist, artist_id)
        return await _artist_dict(session, artist)


async def update_notes(artist_id: int, notes: str) -> dict:
    async with async_session() as session:
        async with session.begin():
            artist = await session.get(TriageArtist, artist_id)
            if artist is None:
                return _NOT_FOUND
            artist.notes = notes
        return await _artist_dict(session, artist)


# ── Derived Plex playlist ─────────────────────────────────────────────────────

async def sync_playlist() -> dict:
    """Rebuild the Plex playlist from the queue: one track per undecided artist.
    The playlist is only ever derived from the database, never edited
    decision by decision, so it can't drift."""
    async with async_session() as session:
        keys = [k for (k,) in (await session.execute(
            select(TriageArtist.track_key)
            .where(TriageArtist.decision.is_(None), TriageArtist.track_key.is_not(None))
            .order_by(TriageArtist.id)
        )).all()]
    await plex.replace_playlist(config.TRIAGE_PLAYLIST_NAME, keys)
    logger.info("Rebuilt Plex playlist %r with %d tracks", config.TRIAGE_PLAYLIST_NAME, len(keys))
    return {"playlist_name": config.TRIAGE_PLAYLIST_NAME, "tracks": len(keys)}
