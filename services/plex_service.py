"""Plex API interactions.

Everything here is synchronous (plexapi uses `requests`); async callers must
run these functions via `asyncio.to_thread` so they don't block the event loop.
"""
import random
import re
import unicodedata
from typing import Optional

from plexapi.audio import Track
from plexapi.server import PlexServer

from config import config

# Plex rejects very long playlist URIs, so tracks are fetched and added in chunks.
_CHUNK_SIZE = 200


def _get_server() -> PlexServer:
    return PlexServer(config.PLEX_URL, config.PLEX_TOKEN)


def get_server() -> PlexServer:
    """Return a new PlexServer instance for callers that need to reuse one connection."""
    return _get_server()


def _get_music_section(server: PlexServer):
    return server.library.section(config.PLEX_MUSIC_LIBRARY)


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"^the\s+", "", name)
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def get_all_artists(server: Optional[PlexServer] = None) -> list[dict]:
    """Return every artist in the music library. `thumb` is a Plex path
    (no host or token); the app serves it through its own thumbnail proxy."""
    server = server or _get_server()
    section = _get_music_section(server)
    artists = section.all(libtype="artist")
    return [
        {
            "name": a.title,
            "rating_key": str(a.ratingKey),
            "thumb": a.thumb or None,
        }
        for a in artists
        if a.title
    ]


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


def _artist_tracks(server: PlexServer, artist_rating_key: str) -> list:
    """All tracks for an artist in a single request (no separate artist fetch)."""
    return server.fetchItems(f"/library/metadata/{int(artist_rating_key)}/allLeaves", cls=Track)


def resolve_track_for_artist(
    server: PlexServer,
    artist_rating_key: str,
    lastfm_title: Optional[str],
) -> tuple[Optional[dict], str, list[str]]:
    """Resolve the best track for an artist using an existing PlexServer connection.
    Returns (track_dict, source, file_paths) where source is 'lastfm' or 'plex_random'
    and file_paths are the on-disk paths of the artist's tracks (used to match Lidarr).
    Makes only ONE Plex request per artist, regardless of source.
    """
    tracks = _artist_tracks(server, artist_rating_key)
    if not tracks:
        return None, "plex_random", []
    files = _track_files(tracks)

    if lastfm_title:
        title_lower = lastfm_title.lower()
        for track in tracks:
            if track.title.lower() == title_lower:
                return _track_dict(track), "lastfm", files

    return _track_dict(random.choice(tracks)), "plex_random", files


def get_artist_file_paths(artist_rating_key: str) -> list[str]:
    """Return the on-disk file paths of every track by this artist."""
    return _track_files(_artist_tracks(_get_server(), artist_rating_key))


def get_additional_tracks(artist_rating_key: str, exclude_key: str, count: int = 5) -> list[dict]:
    """Get up to `count` additional tracks from an artist, excluding a specific track."""
    tracks = [t for t in _artist_tracks(_get_server(), artist_rating_key) if str(t.ratingKey) != exclude_key]
    selected = random.sample(tracks, min(count, len(tracks)))
    return [_track_dict(t) for t in selected]


def get_all_tracks(artist_rating_key: str, exclude_key: Optional[str] = None) -> list[dict]:
    """Return all tracks for an artist, optionally excluding one key, shuffled."""
    tracks = [t for t in _artist_tracks(_get_server(), artist_rating_key) if str(t.ratingKey) != exclude_key]
    random.shuffle(tracks)
    return [_track_dict(t) for t in tracks]


def get_track_stream_key(track_rating_key: str) -> Optional[str]:
    """Return the part path for a single track."""
    return _track_part_key(_get_server().fetchItem(int(track_rating_key)))


def _chunks(items: list, size: int = _CHUNK_SIZE):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fetch_tracks(server: PlexServer, track_keys: list[str]) -> list:
    """Fetch many tracks with one request per chunk instead of one per track."""
    return server.fetchItems("/library/metadata/" + ",".join(str(int(k)) for k in track_keys))


def _find_playlist(server: PlexServer, name: str):
    for pl in server.playlists():
        if pl.title == name:
            return pl
    return None


def create_or_replace_playlist(name: str, track_keys: list[str], server: Optional[PlexServer] = None):
    """Create the triage playlist, replacing it if it already exists.
    Pass an existing `server` to reuse the connection (avoids an extra PlexServer() init)."""
    if server is None:
        server = _get_server()
    existing = _find_playlist(server, name)
    if existing is not None:
        existing.delete()
    if not track_keys:
        return
    playlist = None
    for chunk in _chunks(track_keys):
        items = _fetch_tracks(server, chunk)
        if not items:
            continue
        if playlist is None:
            playlist = server.createPlaylist(name, items=items)
        else:
            playlist.addItems(items)


def append_tracks_to_playlist(name: str, track_keys: list[str]):
    """Add tracks to an existing playlist."""
    if not track_keys:
        return
    server = _get_server()
    playlist = _find_playlist(server, name)
    if playlist is None:
        return
    for chunk in _chunks(track_keys):
        playlist.addItems(_fetch_tracks(server, chunk))


def remove_track_from_playlist(name: str, track_key: str):
    """Remove a specific track from the playlist."""
    server = _get_server()
    playlist = _find_playlist(server, name)
    if playlist is None:
        return
    for item in playlist.items():
        if str(item.ratingKey) == track_key:
            playlist.removeItems([item])
            return


def replace_track_in_playlist(name: str, old_track_key: str, new_track_key: str):
    """Swap one track for another in the playlist (best-effort; order not guaranteed)."""
    server = _get_server()
    playlist = _find_playlist(server, name)
    if playlist is None:
        return
    for item in playlist.items():
        if str(item.ratingKey) == old_track_key:
            playlist.removeItems([item])
            break
    playlist.addItems([server.fetchItem(int(new_track_key))])


def delete_artist_from_plex(artist_rating_key: str):
    """Delete an artist and all their media from Plex (fallback for non-Lidarr artists)."""
    server = _get_server()
    artist = server.fetchItem(int(artist_rating_key))
    artist.delete()
