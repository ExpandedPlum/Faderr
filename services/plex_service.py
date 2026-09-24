"""Plex access through one long-lived client.

plexapi is synchronous (it uses `requests`). PlexClient runs every plexapi
call in a worker thread *inside* its async methods, so callers simply await
them and can't block the event loop by forgetting `asyncio.to_thread`. The
PlexServer connection is created once and reused; if it drops, read-only
calls reconnect and retry once.
"""
import asyncio
import logging
import random
import re
import threading
import unicodedata
from typing import Callable, Optional, TypeVar

import httpx
import requests
from plexapi.audio import Track
from plexapi.server import PlexServer


logger = logging.getLogger(__name__)

T = TypeVar("T")

# Plex rejects very long playlist URIs, so tracks are fetched and added in chunks.
_CHUNK_SIZE = 200


# ── Pure helpers ──────────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"^the\s+", "", name)
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def _mbid(item) -> Optional[str]:
    """The MusicBrainz ID Plex reports for an item (from its `mbid://` GUID), if any."""
    try:
        guids = item.guids or []
    except Exception:
        return None
    for guid in guids:
        gid = getattr(guid, "id", "") or ""
        if gid.startswith("mbid://"):
            return gid[len("mbid://"):] or None
    return None


def _track_part_key(track) -> Optional[str]:
    """Return the Plex part path for direct streaming, e.g. /library/parts/123/.../file.mp3"""
    try:
        return track.media[0].parts[0].key
    except (IndexError, AttributeError):
        return None


def _track_dict(track) -> dict:
    return {
        "title": track.title,
        "rating_key": str(track.ratingKey),
        "stream_key": _track_part_key(track),
    }


def _track_files(tracks) -> list[str]:
    files = []
    for track in tracks:
        for media in getattr(track, "media", None) or []:
            for part in getattr(media, "parts", None) or []:
                if getattr(part, "file", None):
                    files.append(part.file)
    return files


def _chunks(items: list, size: int = _CHUNK_SIZE):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ── Blocking operations (run in worker threads by PlexClient) ────────────────

def _artist_tracks(server: PlexServer, artist_rating_key: str) -> list:
    """All tracks for an artist in a single request (no separate artist fetch)."""
    return server.fetchItems(f"/library/metadata/{int(artist_rating_key)}/allLeaves", cls=Track)


def _all_artists(server: PlexServer, library: str) -> list[dict]:
    section = server.library.section(library)
    # includeGuids (plexapi's default) puts MusicBrainz IDs in this same listing
    artists = section.all(libtype="artist", includeGuids=True)
    return [
        {
            "name": a.title,
            "rating_key": str(a.ratingKey),
            "thumb": a.thumb or None,
            "mbid": _mbid(a),
        }
        for a in artists
        if a.title
    ]


def _resolve_track(
    server: PlexServer, artist_rating_key: str, lastfm_title: Optional[str], keep_track_key: Optional[str],
) -> tuple[Optional[dict], str, list[str]]:
    tracks = _artist_tracks(server, artist_rating_key)
    if not tracks:
        return None, "plex_random", []
    files = _track_files(tracks)

    if keep_track_key:
        for track in tracks:
            if str(track.ratingKey) == keep_track_key:
                return _track_dict(track), "kept", files

    if lastfm_title:
        title_lower = lastfm_title.lower()
        for track in tracks:
            if track.title.lower() == title_lower:
                return _track_dict(track), "lastfm", files

    return _track_dict(random.choice(tracks)), "plex_random", files


def _other_tracks(server: PlexServer, artist_rating_key: str, exclude_key: Optional[str], count: Optional[int]) -> list[dict]:
    tracks = [t for t in _artist_tracks(server, artist_rating_key) if str(t.ratingKey) != exclude_key]
    random.shuffle(tracks)
    if count is not None:
        tracks = tracks[:count]
    return [_track_dict(t) for t in tracks]


def _fetch_tracks(server: PlexServer, track_keys: list[str]) -> list:
    """Fetch many tracks with one request per chunk instead of one per track."""
    return server.fetchItems("/library/metadata/" + ",".join(str(int(k)) for k in track_keys))


def _replace_playlist(server: PlexServer, name: str, track_keys: list[str]) -> None:
    for pl in server.playlists():
        if pl.title == name:
            pl.delete()
            break
    playlist = None
    for chunk in _chunks(track_keys):
        items = _fetch_tracks(server, chunk)
        if not items:
            continue
        if playlist is None:
            playlist = server.createPlaylist(name, items=items)
        else:
            playlist.addItems(items)


def _delete_artist(server: PlexServer, artist_rating_key: str) -> None:
    server.fetchItem(int(artist_rating_key)).delete()


# ── Client ────────────────────────────────────────────────────────────────────

class NotConfiguredError(RuntimeError):
    """Raised when Plex hasn't been set up yet (see the Settings page)."""


class PlexClient:
    """Connection details come from the settings store, which calls
    configure() at startup and whenever they change in the web UI."""

    def __init__(self):
        self._url: Optional[str] = None
        self._token: Optional[str] = None
        self._library: Optional[str] = None
        self._server: Optional[PlexServer] = None
        self._server_lock = threading.Lock()
        # Async HTTP client for streaming media through the app's proxy. It
        # belongs to the running event loop, so it's made in configure().
        self.http: Optional[httpx.AsyncClient] = None

    @property
    def configured(self) -> bool:
        return bool(self._url and self._token and self._library)

    async def configure(self, url: Optional[str], token: Optional[str], library: Optional[str]) -> None:
        """Point the client at a (new) server. Existing connections are dropped."""
        old_http = self.http
        with self._server_lock:
            self._url, self._token, self._library = url, token, library
            self._server = None
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"X-Plex-Token": token},
            timeout=httpx.Timeout(15.0, read=60.0),
        ) if url and token else None
        if old_http is not None:
            await old_http.aclose()

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    def _server_conn(self) -> PlexServer:
        with self._server_lock:
            if not self.configured:
                raise NotConfiguredError("Plex isn't set up yet. Open Settings to connect it.")
            if self._server is None:
                self._server = PlexServer(self._url, self._token)
            return self._server

    def _drop_server(self, server: PlexServer) -> None:
        with self._server_lock:
            if self._server is server:
                self._server = None

    async def _call(self, fn: Callable[..., T], *args, retry: bool = True) -> T:
        """Run a blocking plexapi function in a worker thread. Read-only calls
        (retry=True) reconnect and try once more if the connection dropped;
        calls that change things are never repeated automatically."""
        def run():
            server = self._server_conn()
            try:
                return fn(server, *args)
            except requests.exceptions.ConnectionError:
                self._drop_server(server)
                if not retry:
                    raise
                logger.info("Plex connection dropped; reconnecting")
                return fn(self._server_conn(), *args)
        return await asyncio.to_thread(run)

    async def all_artists(self) -> list[dict]:
        """Every artist in the music library: name, rating_key, thumb (a Plex
        path, no host or token) and mbid (MusicBrainz ID, if Plex has one)."""
        return await self._call(_all_artists, self._library)

    async def resolve_track(
        self, artist_rating_key: str, lastfm_title: Optional[str], keep_track_key: Optional[str] = None,
    ) -> tuple[Optional[dict], str, list[str]]:
        """Pick the artist's triage track with ONE Plex request. Returns
        (track, source, file_paths); source is "kept" (keep_track_key still
        exists), "lastfm" or "plex_random". file_paths are used to match Lidarr."""
        return await self._call(_resolve_track, artist_rating_key, lastfm_title, keep_track_key)

    async def artist_file_paths(self, artist_rating_key: str) -> list[str]:
        return await self._call(lambda s, k: _track_files(_artist_tracks(s, k)), artist_rating_key)

    async def other_tracks(self, artist_rating_key: str, exclude_key: Optional[str], count: Optional[int] = None) -> list[dict]:
        """The artist's tracks other than `exclude_key`, shuffled; at most `count`."""
        return await self._call(_other_tracks, artist_rating_key, exclude_key, count)

    async def track_stream_key(self, track_rating_key: str) -> Optional[str]:
        return await self._call(lambda s, k: _track_part_key(s.fetchItem(int(k))), track_rating_key)

    async def replace_playlist(self, name: str, track_keys: list[str]) -> None:
        """Replace the playlist called `name` with exactly these tracks (none: just delete it)."""
        await self._call(_replace_playlist, name, track_keys, retry=False)

    async def delete_artist(self, artist_rating_key: str) -> None:
        """Delete an artist and all their media from Plex (for artists Lidarr doesn't manage)."""
        await self._call(_delete_artist, artist_rating_key, retry=False)


plex = PlexClient()
