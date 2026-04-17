import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import delete, func, select

from config import config
from models import TriageArtist, async_session
from services import lastfm_service, lidarr_service, plex_service

logger = logging.getLogger(__name__)


_noop_progress: Callable[[dict], Awaitable[None]] = lambda evt: None


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

    # 1. Fetch all Plex artists
    plex_artists = plex_service.get_all_artists()
    logger.info("Found %d artists in Plex", len(plex_artists))

    # 2. Fetch Lidarr artists and build index
    await on_progress({"stage": "lidarr_fetch"})
    try:
        lidarr_artists = lidarr_service.get_all_artists()
        lidarr_index = lidarr_service.build_lidarr_index(lidarr_artists)
        logger.info("Found %d artists in Lidarr", len(lidarr_artists))
    except Exception as exc:
        logger.warning("Could not reach Lidarr: %s — lidarr_id will be NULL for all", exc)
        lidarr_index = {}

    # 3. Last.fm top track lookup (batch, async) — emit progress per lookup
    artist_names = [a["name"] for a in plex_artists]
    total = len(artist_names)
    logger.info("Fetching Last.fm top tracks for %d artists...", total)

    # Wrap lastfm batch to emit progress after each result
    import asyncio
    import httpx
    from services.lastfm_service import get_top_track

    lastfm_results: dict[str, Any] = {}
    done_count = 0

    async def tracked_lookup(name: str, client: httpx.AsyncClient):
        nonlocal done_count
        result = await get_top_track(name, client)
        done_count += 1
        await on_progress({"stage": "lastfm", "done": done_count, "total": total})
        return name, result

    async with httpx.AsyncClient() as client:
        tasks = [tracked_lookup(name, client) for name in artist_names]
        pairs = await asyncio.gather(*tasks, return_exceptions=True)

    for item in pairs:
        if isinstance(item, Exception):
            continue
        name, result = item
        lastfm_results[name] = result

    # 4. Resolve tracks for each artist
    rows = []
    for idx, artist in enumerate(plex_artists):
        await on_progress({"stage": "resolving", "done": idx + 1, "total": total})
        name = artist["name"]
        plex_key = artist["rating_key"]
        lidarr_id = lidarr_service.find_lidarr_id(name, lidarr_index)

        lastfm_track = lastfm_results.get(name)
        track = None
        source = "plex_random"

        if lastfm_track:
            track = plex_service.find_track_by_title(plex_key, lastfm_track)
            if track:
                source = "lastfm"

        if not track:
            track = plex_service.get_random_track(plex_key)

        if not track:
            logger.warning("No tracks found for artist %r, skipping", name)
            continue

        rows.append(
            TriageArtist(
                artist_name=name,
                plex_artist_key=plex_key,
                thumb_url=artist.get("thumb"),
                track_title=track["title"],
                track_key=track["rating_key"],
                stream_key=track.get("stream_key"),
                source=source,
                lidarr_id=lidarr_id,
            )
        )

    # 5. Update the database
    await on_progress({"stage": "playlist_create"})
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                delete(TriageArtist).where(TriageArtist.decision.is_(None))
            )
            session.add_all(rows)

    logger.info("Inserted %d artists into triage DB", len(rows))

    # 6. Build the Plex playlist
    track_keys = [r.track_key for r in rows]
    plex_service.create_or_replace_playlist(config.TRIAGE_PLAYLIST_NAME, track_keys)
    logger.info("Created Plex playlist %r with %d tracks", config.TRIAGE_PLAYLIST_NAME, len(track_keys))

    result = {"total_artists": len(rows), "playlist_name": config.TRIAGE_PLAYLIST_NAME}
    await on_progress({"stage": "done", **result})
    return result


async def get_all_artists_for_list(status: Optional[str] = None, search: Optional[str] = None) -> list[dict]:
    """Return all artists, optionally filtered by decision status and/or name search."""
    async with async_session() as session:
        q = select(TriageArtist).order_by(TriageArtist.artist_name)
        if status == "undecided":
            q = q.where(TriageArtist.decision.is_(None))
        elif status == "explore_keep":
            q = q.where(TriageArtist.decision == "explore_keep")
        elif status and status != "all":
            q = q.where(TriageArtist.decision == status)
        if search:
            q = q.where(TriageArtist.artist_name.ilike(f"%{search}%"))
        result = await session.execute(q)
        return [r.to_dict() for r in result.scalars().all()]


async def get_artist_by_id(artist_id: int) -> Optional[dict]:
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        row = result.scalars().first()
        return row.to_dict() if row else None


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
        alternatives = plex_service.get_additional_tracks(
            artist.plex_artist_key, exclude_key=old_track_key, count=1
        )
        if not alternatives:
            return {"error": "No alternative tracks found for this artist"}

        new_track = alternatives[0]
        # Get stream_key for the new track by fetching it
        from plexapi.server import PlexServer
        server = PlexServer(config.PLEX_URL, config.PLEX_TOKEN)
        plex_track = server.fetchItem(int(new_track["rating_key"]))
        new_stream_key = None
        try:
            new_stream_key = plex_track.media[0].parts[0].key
        except (IndexError, AttributeError):
            pass

        artist.track_title = new_track["title"]
        artist.track_key = new_track["rating_key"]
        artist.stream_key = new_stream_key
        artist.source = "plex_random"
        await session.commit()

        # Update Plex playlist
        plex_service.replace_track_in_playlist(
            config.TRIAGE_PLAYLIST_NAME, old_track_key, new_track["rating_key"]
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
        total = (await session.execute(select(func.count()).select_from(TriageArtist))).scalar()
        undecided = (await session.execute(
            select(func.count()).select_from(TriageArtist).where(TriageArtist.decision.is_(None))
        )).scalar()
        keep = (await session.execute(
            select(func.count()).select_from(TriageArtist).where(TriageArtist.decision == "keep")
        )).scalar()
        explore = (await session.execute(
            select(func.count()).select_from(TriageArtist).where(TriageArtist.decision == "explore")
        )).scalar()
        explore_final = (await session.execute(
            select(func.count()).select_from(TriageArtist).where(TriageArtist.decision == "explore_keep")
        )).scalar()
        deleted = (await session.execute(
            select(func.count()).select_from(TriageArtist).where(TriageArtist.decision == "delete")
        )).scalar()
    triaged = total - undecided
    return {
        "total": total,
        "triaged": triaged,
        "remaining": undecided,
        "keep": keep,
        "explore": explore,
        "explore_keep": explore_final,
        "deleted": deleted,
    }


async def get_exploring_artists() -> list[dict]:
    """Return artists in the 'explore' state awaiting a final keep/delete."""
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist)
            .where(TriageArtist.decision == "explore")
            .order_by(TriageArtist.decided_at)
        )
        return [r.to_dict() for r in result.scalars().all()]


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
    """
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        artist = result.scalars().first()
        if not artist:
            return {"error": "Artist not found"}

        now = datetime.now(timezone.utc)

        if decision == "keep":
            artist.decision = "keep"
            artist.decided_at = now
            plex_service.remove_track_from_playlist(config.TRIAGE_PLAYLIST_NAME, artist.track_key)

        elif decision == "explore":
            artist.decision = "explore"
            artist.decided_at = now
            # Add more tracks from this artist to the playlist
            extra = plex_service.get_additional_tracks(
                artist.plex_artist_key, artist.track_key, count=5
            )
            if extra:
                extra_keys = [t["rating_key"] for t in extra]
                plex_service.append_tracks_to_playlist(config.TRIAGE_PLAYLIST_NAME, extra_keys)

        elif decision in ("explore_keep", "explore_delete"):
            # Final decision on an artist that was previously set to explore
            if decision == "explore_keep":
                artist.decision = "explore_keep"
                artist.decided_at = now
            else:
                artist.decision = "delete"
                artist.decided_at = now
                await _delete_artist(artist)

        elif decision == "delete":
            artist.decision = "delete"
            artist.decided_at = now
            plex_service.remove_track_from_playlist(config.TRIAGE_PLAYLIST_NAME, artist.track_key)
            await _delete_artist(artist)

        else:
            return {"error": f"Unknown decision: {decision}"}

        await session.commit()
        return artist.to_dict()


async def undo_decision(artist_id: int) -> dict:
    """Revert a decision back to undecided and re-add the track to the playlist."""
    async with async_session() as session:
        result = await session.execute(
            select(TriageArtist).where(TriageArtist.id == artist_id)
        )
        artist = result.scalars().first()
        if not artist:
            return {"error": "Artist not found"}

        prev_decision = artist.decision
        artist.decision = None
        artist.decided_at = None
        await session.commit()

        # Re-add the track to the Plex playlist
        if prev_decision != "delete":
            plex_service.append_tracks_to_playlist(
                config.TRIAGE_PLAYLIST_NAME, [artist.track_key]
            )

        return artist.to_dict()


async def _delete_artist(artist: TriageArtist):
    """Internal: delete from Lidarr (with files) or fall back to Plex delete.
    Lidarr's deleteFiles=true removes the audio files; any empty folders that
    remain on the media server can be cleaned up via Lidarr's built-in
    'Clean Empty Folders' task."""
    if artist.lidarr_id:
        try:
            lidarr_service.delete_artist(artist.lidarr_id, delete_files=True)
            logger.info("Deleted artist %r via Lidarr", artist.artist_name)
            return
        except Exception as exc:
            logger.error("Lidarr delete failed for %r: %s — falling back to Plex", artist.artist_name, exc)

    # Fallback: delete via Plex API (runs on the Plex server, so files are deleted remotely)
    try:
        plex_service.delete_artist_from_plex(artist.plex_artist_key)
        logger.info("Deleted artist %r via Plex", artist.artist_name)
    except Exception as exc:
        logger.error("Plex delete failed for %r: %s", artist.artist_name, exc)
        raise
