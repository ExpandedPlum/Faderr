import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
from sqlalchemy import delete, func, select

from config import config
from models import TriageArtist, async_session
from services import lastfm_service, lidarr_service, plex_service

logger = logging.getLogger(__name__)

# How many Plex track lookups run in parallel during generation.
_PLEX_CONCURRENCY = 4


class DeletionError(Exception):
    """Raised when an artist can't be deleted safely. Nothing has been deleted."""


async def _noop_progress(evt: dict) -> None:
    return None


async def _best_effort(description: str, fn: Callable, *args) -> None:
    """Run a blocking Plex playlist operation without letting its failure
    undo or block a decision that has already been saved."""
    try:
        await asyncio.to_thread(fn, *args)
    except Exception as exc:
        logger.warning("Playlist update failed (%s): %s", description, exc)


async def generate_triage_playlist(
    on_progress: Callable[[dict], Awaitable[None]] = _noop_progress,
) -> dict:
    """
    Build the triage artist list and Plex playlist.
    Idempotent: preserves past decisions, replaces undecided entries.
    Calls on_progress(event_dict) at key stages for SSE streaming.
    """
    logger.info("Starting playlist generation...")
    await on_progress({"stage": "plex_fetch"})

    # 1. Fetch all Plex artists, reusing a single server connection throughout.
    plex_server = await asyncio.to_thread(plex_service.get_server)
    plex_artists = await asyncio.to_thread(plex_service.get_all_artists, plex_server)
    logger.info("Found %d artists in Plex", len(plex_artists))

    # 2. Fetch Lidarr artists
    await on_progress({"stage": "lidarr_fetch"})
    try:
        lidarr_artists = await asyncio.to_thread(lidarr_service.get_all_artists)
        logger.info("Found %d artists in Lidarr", len(lidarr_artists))
    except Exception as exc:
        logger.warning("Could not reach Lidarr: %s — lidarr_id will be NULL for all", exc)
        lidarr_artists = []

    # 3. Last.fm top track lookup (batch, async) — emit progress per lookup
    artist_names = [a["name"] for a in plex_artists]
    total = len(artist_names)
    logger.info("Fetching Last.fm top tracks for %d artists...", total)

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
            *(tracked_lookup(name, client) for name in artist_names), return_exceptions=True
        )

    for item in pairs:
        if isinstance(item, Exception):
            continue
        name, result = item
        lastfm_results[name] = result

    # 4. Resolve a track for each undecided artist. Plex calls are blocking, so
    # they run in worker threads; that keeps the server responsive and lets
    # progress events stream out while this runs.
    decided_names = await _decided_artist_names()
    logger.info("Skipping %d already-decided artists during regeneration", len(decided_names))

    semaphore = asyncio.Semaphore(_PLEX_CONCURRENCY)
    resolved_count = 0

    async def resolve(artist: dict) -> Optional[TriageArtist]:
        nonlocal resolved_count
        name = artist["name"]
        try:
            if name in decided_names:
                return None  # already decided — preserve existing row, don't create a duplicate
            async with semaphore:
                track, source, file_paths = await asyncio.to_thread(
                    plex_service.resolve_track_for_artist,
                    plex_server, artist["rating_key"], lastfm_results.get(name),
                )
            if not track:
                logger.warning("No tracks found for artist %r, skipping", name)
                return None
            match = lidarr_service.resolve_artist(name, file_paths, lidarr_artists)
            return TriageArtist(
                artist_name=name,
                plex_artist_key=artist["rating_key"],
                thumb_url=artist.get("thumb"),
                track_title=track["title"],
                track_key=track["rating_key"],
                stream_key=track.get("stream_key"),
                source=source,
                lidarr_id=match.lidarr_id,
            )
        finally:
            resolved_count += 1
            await on_progress({"stage": "resolving", "done": resolved_count, "total": total})

    results = await asyncio.gather(*(resolve(a) for a in plex_artists))
    rows = [r for r in results if r is not None]

    # 5. Update the database. Re-check decisions inside the transaction: the
    # UI stays usable during generation, so artists may have been decided
    # since step 4 started, and they must not get a second undecided row.
    await on_progress({"stage": "playlist_create"})
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(TriageArtist.artist_name).where(TriageArtist.decision.is_not(None))
            )
            decided_now = {row[0] for row in result.fetchall()}
            rows = [r for r in rows if r.artist_name not in decided_now]
            await session.execute(
                delete(TriageArtist).where(TriageArtist.decision.is_(None))
            )
            session.add_all(rows)

    logger.info("Inserted %d artists into triage DB", len(rows))

    # 6. Build the Plex playlist (reuse the same server connection). The app's
    # own player doesn't need it, so a failure here doesn't fail generation.
    track_keys = [r.track_key for r in rows]
    playlist_warning = None
    try:
        await asyncio.to_thread(
            plex_service.create_or_replace_playlist,
            config.TRIAGE_PLAYLIST_NAME, track_keys, plex_server,
        )
        logger.info("Created Plex playlist %r with %d tracks", config.TRIAGE_PLAYLIST_NAME, len(track_keys))
    except Exception as exc:
        logger.exception("Creating the Plex playlist failed")
        playlist_warning = f"The Plex playlist couldn't be created: {exc}"

    result = {"total_artists": len(rows), "playlist_name": config.TRIAGE_PLAYLIST_NAME}
    if playlist_warning:
        result["playlist_warning"] = playlist_warning
    await on_progress({"stage": "done", **result})
    return result


async def _decided_artist_names() -> set[str]:
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist.artist_name).where(TriageArtist.decision.is_not(None))
        )
        return {row[0] for row in result.fetchall()}


async def get_all_artists_for_list(status: Optional[str] = None, search: Optional[str] = None) -> list[dict]:
    """Return all artists, optionally filtered by decision status and/or name search."""
    async with async_session() as session:
        q = select(TriageArtist).order_by(TriageArtist.artist_name)
        if status == "undecided":
            q = q.where(TriageArtist.decision.is_(None))
        elif status == "keep":
            # "Kept after exploring" is still a keep
            q = q.where(TriageArtist.decision.in_(("keep", "explore_keep")))
        elif status and status != "all":
            q = q.where(TriageArtist.decision == status)
        if search:
            q = q.where(TriageArtist.artist_name.ilike(f"%{search}%"))
        result = await session.execute(q)
        return [r.to_dict() for r in result.scalars().all()]


async def get_artist_by_id(artist_id: int) -> Optional[dict]:
    row = await get_artist_row(artist_id)
    return row.to_dict() if row else None


async def get_artist_row(artist_id: int) -> Optional[TriageArtist]:
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        return result.scalars().first()


async def update_notes(artist_id: int, notes: str) -> dict:
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        artist = result.scalars().first()
        if not artist:
            return {"error": "Artist not found"}
        artist.notes = notes
        await session.commit()
        return artist.to_dict()


async def skip_track(artist_id: int) -> dict:
    """Pick a different random track for this artist and update DB + Plex playlist."""
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        artist = result.scalars().first()
        if not artist:
            return {"error": "Artist not found"}

        old_track_key = artist.track_key
        alternatives = await asyncio.to_thread(
            plex_service.get_additional_tracks, artist.plex_artist_key, old_track_key, 1
        )
        if not alternatives:
            return {"error": "No alternative tracks found for this artist"}

        new_track = alternatives[0]
        artist.track_title = new_track["title"]
        artist.track_key = new_track["rating_key"]
        artist.stream_key = new_track.get("stream_key")
        artist.source = "plex_random"
        await session.commit()

    await _best_effort(
        "replace skipped track", plex_service.replace_track_in_playlist,
        config.TRIAGE_PLAYLIST_NAME, old_track_key, new_track["rating_key"],
    )
    return artist.to_dict()


async def get_current_artist() -> Optional[dict]:
    """Return the first undecided artist."""
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist)
            .where(TriageArtist.decision.is_(None))
            .order_by(TriageArtist.id)
            .limit(1)
        )
        row = result.scalars().first()
        return row.to_dict() if row else None


async def get_stats() -> dict:
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist.decision, func.count()).group_by(TriageArtist.decision)
        )
        counts = {decision: count for decision, count in result.all()}
    total = sum(counts.values())
    undecided = counts.get(None, 0)
    return {
        "total": total,
        "triaged": total - undecided,
        "remaining": undecided,
        "keep": counts.get("keep", 0),
        "explore": counts.get("explore", 0),
        "explore_keep": counts.get("explore_keep", 0),
        "deleted": counts.get("delete", 0),
    }


async def get_history(decision_filter: Optional[str] = None) -> list[dict]:
    async with async_session() as session:
        q = select(TriageArtist).where(TriageArtist.decision.is_not(None))
        if decision_filter:
            q = q.where(TriageArtist.decision == decision_filter)
        q = q.order_by(TriageArtist.decided_at.desc())
        result = await session.execute(q)
        return [r.to_dict() for r in result.scalars().all()]


async def make_decision(artist_id: int, decision: str) -> dict:
    """
    Process a triage decision for an artist.
    decision: "keep" | "explore" | "delete"
    For "explore", also accepts "explore_keep" / "explore_delete" for final decisions.

    The decision is saved first; Plex playlist updates run afterwards and are
    best-effort, so an unreachable Plex server can't block a keep. Deletion is
    the exception: the artist must actually be deleted before it is recorded.
    """
    playlist_ops: list[tuple[str, Callable, tuple]] = []

    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        artist = result.scalars().first()
        if not artist:
            return {"error": "Artist not found"}

        now = datetime.now(timezone.utc)
        remove_track = (
            "remove track", plex_service.remove_track_from_playlist,
            (config.TRIAGE_PLAYLIST_NAME, artist.track_key),
        )

        if decision == "keep":
            artist.decision = "keep"
            artist.decided_at = now
            playlist_ops.append(remove_track)

        elif decision == "explore":
            artist.decision = "explore"
            artist.decided_at = now
            playlist_ops.append((
                "add explore tracks", _append_explore_tracks,
                (artist.plex_artist_key, artist.track_key),
            ))

        elif decision == "explore_keep":
            artist.decision = "explore_keep"
            artist.decided_at = now

        elif decision in ("delete", "explore_delete"):
            await _delete_artist(artist)
            artist.decision = "delete"
            artist.decided_at = now
            playlist_ops.append(remove_track)

        else:
            return {"error": f"Unknown decision: {decision}", "status_code": 400}

        await session.commit()

    for description, fn, args in playlist_ops:
        await _best_effort(description, fn, *args)
    return artist.to_dict()


def _append_explore_tracks(plex_artist_key: str, track_key: str):
    extra = plex_service.get_additional_tracks(plex_artist_key, track_key, count=5)
    if extra:
        plex_service.append_tracks_to_playlist(
            config.TRIAGE_PLAYLIST_NAME, [t["rating_key"] for t in extra]
        )


async def undo_decision(artist_id: int) -> dict:
    """Revert a decision back to undecided and re-add the track to the playlist."""
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        artist = result.scalars().first()
        if not artist:
            return {"error": "Artist not found"}
        if artist.decision == "delete":
            # The files are gone; putting the artist back in the queue would
            # only offer a track that can no longer be played.
            return {"error": "A delete can't be undone: the artist's files have been removed.", "status_code": 409}
        if artist.decision is None:
            return artist.to_dict()

        artist.decision = None
        artist.decided_at = None
        await session.commit()

    await _best_effort(
        "re-add undone track", plex_service.append_tracks_to_playlist,
        config.TRIAGE_PLAYLIST_NAME, [artist.track_key],
    )
    return artist.to_dict()


async def _delete_artist(artist: TriageArtist):
    """Internal: delete an artist's files, choosing Lidarr or Plex safely.

    The owning Lidarr artist is resolved live (never from the cached id) by
    matching its folder against the Plex file paths. Plex is only used to
    delete when Lidarr was reachable and definitely doesn't manage the files,
    or when Lidarr has been told to stop monitoring them; otherwise Lidarr
    would download the deleted files again. Anything uncertain raises
    DeletionError before anything is deleted.

    Lidarr's deleteFiles=true removes the audio files; any empty folders that
    remain on the media server can be cleaned up via Lidarr's built-in
    'Clean Empty Folders' task."""
    name = artist.artist_name

    try:
        file_paths = await asyncio.to_thread(plex_service.get_artist_file_paths, artist.plex_artist_key)
    except Exception as exc:
        raise DeletionError(f"Couldn't read {name!r}'s files from Plex ({exc}). Nothing was deleted.") from exc

    try:
        lidarr_artists = await asyncio.to_thread(lidarr_service.get_all_artists)
    except Exception as exc:
        raise DeletionError(
            f"Lidarr is unreachable ({exc}), so it can't be checked whether it manages {name!r}. "
            "Nothing was deleted; try again once Lidarr is back."
        ) from exc

    match = lidarr_service.resolve_artist(name, file_paths, lidarr_artists)
    if match.ambiguous:
        raise DeletionError(
            f"Not deleting {name!r}: {match.reason}. Delete it manually in Lidarr or Plex."
        )

    if match.lidarr_id:
        artist.lidarr_id = match.lidarr_id
        try:
            await asyncio.to_thread(lidarr_service.delete_artist, match.lidarr_id, True)
            logger.info("Deleted artist %r via Lidarr (id=%s)", name, match.lidarr_id)
            return
        except Exception as exc:
            logger.error("Lidarr delete failed for %r: %s — attempting unmonitor", name, exc)
            try:
                await asyncio.to_thread(lidarr_service.unmonitor_artist, match.lidarr_id)
                logger.info("Unmonitored artist %r in Lidarr; deleting files via Plex", name)
            except Exception as unmon_exc:
                raise DeletionError(
                    f"Lidarr couldn't delete or unmonitor {name!r} ({exc}; {unmon_exc}). "
                    "Nothing was deleted, because deleting through Plex would make Lidarr download it again."
                ) from unmon_exc
    else:
        logger.info("%r is not managed by Lidarr (%s) — deleting via Plex", name, match.reason)

    # Delete via Plex API (runs on the Plex server, so files are deleted remotely)
    try:
        await asyncio.to_thread(plex_service.delete_artist_from_plex, artist.plex_artist_key)
        logger.info("Deleted artist %r via Plex", name)
    except Exception as exc:
        logger.error("Plex delete failed for %r: %s", name, exc)
        raise
