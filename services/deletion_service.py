"""Deleting artists as queued, recorded jobs.

Choosing Delete doesn't delete anything straight away. It creates a row in
`deletions` due after a grace period (DELETE_GRACE_SECONDS), during which the
delete can be undone. A background worker then runs due jobs and records how
each one went: which route removed the files, the Lidarr artist and folder
matched, or why it failed. Because jobs live in the database, they survive
restarts, can be retried, and form an audit log of everything deleted.

Jobs are claimed with a conditional update (pending → running), so several
server processes can each run a worker without running a job twice. A
running job updates a heartbeat; one whose heartbeat stops (the process
died mid-delete) is marked failed for the user to check and retry, rather
than silently re-run.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import httpx
from sqlalchemy import func, select, update

from config import config
from models import DELETION_ACTIVE, Deletion, TriageArtist, async_session, utcnow
from services.lidarr_service import lidarr, resolve_artist
from services.plex_service import plex

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=5)
_HEARTBEAT_SECONDS = 30
_MAX_IDLE_SECONDS = 30

_LIDARR_TIMEOUT_HINT = (
    "Lidarr didn't respond in time and may still be deleting {name!r}. "
    "Check Lidarr; if the artist is still there, retry."
)


class DeletionError(Exception):
    """Raised when Faderr won't delete an artist because it can't be done
    safely. Faderr has deleted nothing; the message says what to check."""


@dataclass
class DeletionOutcome:
    route: str  # "lidarr", "lidarr_already_gone", "plex", "plex_after_unmonitor"
    lidarr_id: Optional[int] = None
    folder: Optional[str] = None
    reason: Optional[str] = None


def grace_period() -> timedelta:
    return timedelta(seconds=max(0, config.DELETE_GRACE_SECONDS))


# ── The delete itself ────────────────────────────────────────────────────────

async def execute(artist_name: str, plex_artist_key: str, mbid: Optional[str] = None) -> DeletionOutcome:
    """Delete an artist's files, choosing Lidarr or Plex safely.

    The owning Lidarr artist is resolved live by matching its folder (and
    MusicBrainz ID, when known) against the Plex file paths. Plex is only used
    to delete when Lidarr was reachable and definitely doesn't manage the
    files, or when Lidarr has been told to stop monitoring them; otherwise
    Lidarr would download the deleted files again. Anything uncertain raises
    DeletionError before anything is deleted.
    """
    name = artist_name

    try:
        file_paths = await plex.artist_file_paths(plex_artist_key)
    except Exception as exc:
        raise DeletionError(f"Couldn't read {name!r}'s files from Plex ({exc}). Nothing was deleted.") from exc

    try:
        lidarr_artists = await lidarr.all_artists()
    except Exception as exc:
        raise DeletionError(
            f"Lidarr is unreachable ({exc}), so it can't be checked whether it manages {name!r}. "
            "Nothing was deleted; retry once Lidarr is back."
        ) from exc

    match = resolve_artist(name, file_paths, lidarr_artists, mbid)
    if match.ambiguous:
        raise DeletionError(f"Not deleting {name!r}: {match.reason}. Delete it manually in Lidarr or Plex.")

    route = "plex"
    if match.lidarr_id:
        try:
            await lidarr.delete_artist(match.lidarr_id, True)
            logger.info("Deleted artist %r via Lidarr (id=%s)", name, match.lidarr_id)
            return DeletionOutcome("lidarr", match.lidarr_id, match.folder, match.reason)
        except Exception as exc:
            # A failed request doesn't always mean a failed delete (e.g. a
            # timeout while Lidarr kept working), so ask Lidarr before acting.
            try:
                still_there = await lidarr.artist_exists(match.lidarr_id)
            except Exception as check_exc:
                raise DeletionError(
                    f"Lidarr delete failed for {name!r} ({exc}) and Lidarr couldn't be asked "
                    f"whether it went through ({check_exc}). Check Lidarr before retrying."
                ) from check_exc
            if not still_there:
                logger.info("Lidarr delete for %r reported %s, but the artist is gone — treating as deleted", name, exc)
                return DeletionOutcome("lidarr_already_gone", match.lidarr_id, match.folder, match.reason)
            if isinstance(exc, httpx.TimeoutException):
                # Lidarr may still be deleting; deleting through Plex now would race it
                raise DeletionError(_LIDARR_TIMEOUT_HINT.format(name=name)) from exc
            logger.error("Lidarr delete failed for %r: %s — attempting unmonitor", name, exc)
            try:
                await lidarr.unmonitor_artist(match.lidarr_id)
                logger.info("Unmonitored artist %r in Lidarr; deleting files via Plex", name)
            except Exception as unmon_exc:
                raise DeletionError(
                    f"Lidarr couldn't delete or unmonitor {name!r} ({exc}; {unmon_exc}). "
                    "Nothing was deleted, because deleting through Plex would make Lidarr download it again."
                ) from unmon_exc
            route = "plex_after_unmonitor"
    else:
        logger.info("%r is not managed by Lidarr (%s) — deleting via Plex", name, match.reason)

    # Plex deletes the files on the Plex server itself
    await plex.delete_artist(plex_artist_key)
    logger.info("Deleted artist %r via Plex", name)
    return DeletionOutcome(route, match.lidarr_id, match.folder, match.reason)


# ── Queue operations (short transactions, no network calls inside) ──────────

def new_job(artist: TriageArtist, previous_decision: Optional[str]) -> Deletion:
    return Deletion(
        artist_id=artist.id,
        artist_name=artist.artist_name,
        plex_artist_key=artist.plex_artist_key,
        previous_decision=previous_decision,
        status="pending",
        run_after=utcnow() + grace_period(),
    )


async def active_job(session, artist_id: int) -> Optional[Deletion]:
    result = await session.execute(
        select(Deletion).where(Deletion.artist_id == artist_id, Deletion.status.in_(DELETION_ACTIVE))
    )
    return result.scalars().first()


async def latest_jobs(session, artist_ids: list[int]) -> dict[int, Deletion]:
    """The most recent job for each of these artists."""
    if not artist_ids:
        return {}
    latest = (
        select(func.max(Deletion.id))
        .where(Deletion.artist_id.in_(artist_ids))
        .group_by(Deletion.artist_id)
    )
    result = await session.execute(select(Deletion).where(Deletion.id.in_(latest)))
    return {job.artist_id: job for job in result.scalars()}


async def cancel(session, job: Deletion) -> bool:
    """Cancel a pending or failed job and restore the artist's earlier
    decision. Returns False if the job can no longer be cancelled (it has
    started or finished). Runs inside the caller's transaction."""
    result = await session.execute(
        update(Deletion)
        .where(Deletion.id == job.id, Deletion.status.in_(("pending", "failed")))
        .values(status="cancelled", finished_at=utcnow())
    )
    if result.rowcount != 1:
        return False
    await session.execute(
        update(TriageArtist)
        .where(TriageArtist.id == job.artist_id, TriageArtist.decision == "delete")
        .values(
            decision=job.previous_decision,
            decided_at=utcnow() if job.previous_decision else None,
        )
    )
    return True


async def retry(job_id: int) -> Optional[Deletion]:
    """Queue a failed job to run again now. Returns the job, or None if it isn't failed."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                update(Deletion)
                .where(Deletion.id == job_id, Deletion.status == "failed")
                .values(status="pending", run_after=utcnow(), error=None, finished_at=None)
            )
            if result.rowcount != 1:
                return None
            job = await session.get(Deletion, job_id)
    worker.wake()
    return job


async def list_jobs(limit: int = 200) -> list[dict]:
    async with async_session() as session:
        result = await session.execute(select(Deletion).order_by(Deletion.id.desc()).limit(limit))
        return [job.to_dict() for job in result.scalars()]


async def recover_stale() -> int:
    """Mark jobs whose worker stopped mid-delete as failed, so the user can
    check what happened and retry. They are never re-run automatically,
    because the delete may have partly happened."""
    now = utcnow()
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                update(Deletion)
                .where(Deletion.status == "running", Deletion.heartbeat_at < now - STALE_AFTER)
                .values(
                    status="failed", finished_at=now,
                    error="Interrupted while deleting (the server stopped). Check Lidarr and Plex, then retry or undo.",
                )
            )
    if result.rowcount:
        logger.warning("Marked %d interrupted deletion(s) as failed", result.rowcount)
    return result.rowcount


async def claim_next() -> Optional[int]:
    """Claim the next due job. The conditional update means only one worker
    (in any process) can move a given job from pending to running."""
    now = utcnow()
    async with async_session() as session:
        async with session.begin():
            job_id = (await session.execute(
                select(Deletion.id)
                .where(Deletion.status == "pending", Deletion.run_after <= now)
                .order_by(Deletion.run_after, Deletion.id)
                .limit(1)
            )).scalar()
            if job_id is None:
                return None
            result = await session.execute(
                update(Deletion)
                .where(Deletion.id == job_id, Deletion.status == "pending")
                .values(status="running", started_at=now, heartbeat_at=now, attempts=Deletion.attempts + 1)
            )
            return job_id if result.rowcount == 1 else None


async def seconds_until_next_due() -> Optional[float]:
    async with async_session() as session:
        next_due = (await session.execute(
            select(func.min(Deletion.run_after)).where(Deletion.status == "pending")
        )).scalar()
    if next_due is None:
        return None
    return max(0.0, (next_due - utcnow()).total_seconds())


async def _finish(job_id: int, **values) -> None:
    # Shielded: a shutdown can't abandon this transaction halfway (and leave the database locked)
    await asyncio.shield(_finish_now(job_id, values))


async def _finish_now(job_id: int, values: dict) -> None:
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                update(Deletion)
                .where(Deletion.id == job_id, Deletion.status == "running")
                .values(finished_at=utcnow(), **values)
            )


async def _beat(job_id: int) -> None:
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                update(Deletion).where(Deletion.id == job_id, Deletion.status == "running")
                .values(heartbeat_at=utcnow())
            )


async def _heartbeat(job_id: int) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        try:
            await asyncio.shield(_beat(job_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Deletion heartbeat failed for job %s: %s", job_id, exc)


async def run_job(job_id: int) -> None:
    async with async_session() as session:
        job = await session.get(Deletion, job_id)
        artist = await session.get(TriageArtist, job.artist_id) if job.artist_id else None
        mbid = artist.mbid if artist else None
        name, key = job.artist_name, job.plex_artist_key

    heartbeat = asyncio.create_task(_heartbeat(job_id))
    try:
        outcome = await execute(name, key, mbid)
    except asyncio.CancelledError:
        raise
    except DeletionError as exc:
        logger.warning("Deletion of %r refused: %s", name, exc)
        await _finish(job_id, status="failed", error=str(exc))
    except Exception as exc:
        logger.exception("Deletion of %r failed", name)
        await _finish(job_id, status="failed", error=f"Delete failed: {exc}")
    else:
        await _finish(
            job_id, status="done", route=outcome.route, lidarr_id=outcome.lidarr_id,
            lidarr_folder=outcome.folder, match_reason=outcome.reason, error=None,
        )
        if outcome.lidarr_id and job.artist_id:
            async with async_session() as session:
                async with session.begin():
                    await session.execute(
                        update(TriageArtist).where(TriageArtist.id == job.artist_id)
                        .values(lidarr_id=outcome.lidarr_id)
                    )
    finally:
        heartbeat.cancel()


# ── Worker ────────────────────────────────────────────────────────────────────

class DeletionWorker:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._stopping = False

    def start(self) -> None:
        self._wake = asyncio.Event()  # bind to the running loop
        self._stopping = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self, timeout: float = 10) -> None:
        """Stop at the next safe point. Cancelling mid-query would abandon a
        database connection with its transaction (and SQLite's write lock)
        still open, so the worker is asked to stop and given time to finish
        its current step. Only a delete still running after `timeout` is
        cancelled; it is then reported as interrupted on the next start."""
        if self._task is None:
            return
        self._stopping = True
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception:
            pass
        self._task = None
        self._stopping = False

    def wake(self) -> None:
        """Check for due jobs now (e.g. one was just queued or retried)."""
        self._wake.set()

    async def run_due(self) -> int:
        """Run every job that is due. Returns how many ran."""
        await recover_stale()
        ran = 0
        while not self._stopping and (job_id := await claim_next()) is not None:
            await run_job(job_id)
            ran += 1
        return ran

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.run_due()
                wait = await seconds_until_next_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Deletion worker error")
                wait = None
            timeout = _MAX_IDLE_SECONDS if wait is None else min(max(wait, 0.5), _MAX_IDLE_SECONDS)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()


worker = DeletionWorker()
