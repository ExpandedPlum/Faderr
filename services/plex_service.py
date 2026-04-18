import random
from typing import Optional

from plexapi.server import PlexServer

from config import config


def _get_server() -> PlexServer:
    return PlexServer(config.PLEX_URL, config.PLEX_TOKEN)


def get_server() -> PlexServer:
    """Return a new PlexServer instance for callers that need to reuse one connection."""
    return _get_server()


def _get_music_section(server: PlexServer):
    return server.library.section(config.PLEX_MUSIC_LIBRARY)


def normalize_name(name: str) -> str:
    import unicodedata
    import re
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"^the\s+", "", name)
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def thumb_url(thumb_path: Optional[str]) -> Optional[str]:
    """Build a full Plex thumb URL with auth token."""
    if not thumb_path:
        return None
    return f"{config.PLEX_URL.rstrip('/')}{thumb_path}?X-Plex-Token={config.PLEX_TOKEN}"


def get_all_artists() -> list[dict]:
    server = _get_server()
    section = _get_music_section(server)
    artists = section.all(libtype="artist")
    return [
        {
            "name": a.title,
            "rating_key": str(a.ratingKey),
            "thumb": thumb_url(a.thumb),
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


def get_random_track(artist_rating_key: str) -> Optional[dict]:
    server = _get_server()
    artist = server.fetchItem(int(artist_rating_key))
    tracks = artist.tracks()
    if not tracks:
        return None
    track = random.choice(tracks)
    return {
        "title": track.title,
        "rating_key": str(track.ratingKey),
        "stream_key": _track_part_key(track),
    }


def find_track_by_title(artist_rating_key: str, title: str) -> Optional[dict]:
    """Try to find a track by title within an artist's library (case-insensitive)."""
    server = _get_server()
    artist = server.fetchItem(int(artist_rating_key))
    tracks = artist.tracks()
    title_lower = title.lower()
    for track in tracks:
        if track.title.lower() == title_lower:
            return {
                "title": track.title,
                "rating_key": str(track.ratingKey),
                "stream_key": _track_part_key(track),
            }
    return None


def resolve_track_for_artist(
    server: PlexServer,
    artist_rating_key: str,
    lastfm_title: Optional[str],
) -> tuple[Optional[dict], str]:
    """Resolve the best track for an artist using an existing PlexServer connection.
    Returns (track_dict, source) where source is 'lastfm' or 'plex_random'.
    Makes only ONE fetchItem + ONE tracks() call per artist, regardless of source.
    """
    artist = server.fetchItem(int(artist_rating_key))
    tracks = artist.tracks()
    if not tracks:
        return None, "plex_random"

    if lastfm_title:
        title_lower = lastfm_title.lower()
        for track in tracks:
            if track.title.lower() == title_lower:
                return {
                    "title": track.title,
                    "rating_key": str(track.ratingKey),
                    "stream_key": _track_part_key(track),
                }, "lastfm"

    track = random.choice(tracks)
    return {
        "title": track.title,
        "rating_key": str(track.ratingKey),
        "stream_key": _track_part_key(track),
    }, "plex_random"


def get_additional_tracks(artist_rating_key: str, exclude_key: str, count: int = 5) -> list[dict]:
    """Get up to `count` additional tracks from an artist, excluding a specific track."""
    server = _get_server()
    artist = server.fetchItem(int(artist_rating_key))
    tracks = [t for t in artist.tracks() if str(t.ratingKey) != exclude_key]
    selected = random.sample(tracks, min(count, len(tracks)))
    return [{"title": t.title, "rating_key": str(t.ratingKey), "stream_key": _track_part_key(t)} for t in selected]


def get_all_tracks(artist_rating_key: str, exclude_key: Optional[str] = None) -> list[dict]:
    """Return all tracks for an artist, optionally excluding one key, shuffled."""
    server = _get_server()
    artist = server.fetchItem(int(artist_rating_key))
    tracks = [t for t in artist.tracks() if str(t.ratingKey) != exclude_key]
    random.shuffle(tracks)
    return [{"title": t.title, "rating_key": str(t.ratingKey), "stream_key": _track_part_key(t)} for t in tracks]


def create_or_replace_playlist(name: str, track_keys: list[str], server: Optional[PlexServer] = None):
    """Create the triage playlist, replacing it if it already exists.
    Pass an existing `server` to reuse the connection (avoids an extra PlexServer() init)."""
    if server is None:
        server = _get_server()
    # Remove existing playlist with this name
    for pl in server.playlists():
        if pl.title == name:
            pl.delete()
            break
    if not track_keys:
        return
    items = [server.fetchItem(int(k)) for k in track_keys]
    server.createPlaylist(name, items=items)


def append_tracks_to_playlist(name: str, track_keys: list[str]):
    """Add tracks to an existing playlist."""
    server = _get_server()
    playlist = None
    for pl in server.playlists():
        if pl.title == name:
            playlist = pl
            break
    if playlist is None or not track_keys:
        return
    items = [server.fetchItem(int(k)) for k in track_keys]
    playlist.addItems(items)


def remove_track_from_playlist(name: str, track_key: str):
    """Remove a specific track from the playlist."""
    server = _get_server()
    for pl in server.playlists():
        if pl.title == name:
            for item in pl.items():
                if str(item.ratingKey) == track_key:
                    pl.removeItems([item])
                    return
            break


def replace_track_in_playlist(name: str, old_track_key: str, new_track_key: str):
    """Swap one track for another in the playlist (best-effort; order not guaranteed)."""
    server = _get_server()
    for pl in server.playlists():
        if pl.title == name:
            for item in pl.items():
                if str(item.ratingKey) == old_track_key:
                    pl.removeItems([item])
                    break
            new_item = server.fetchItem(int(new_track_key))
            pl.addItems([new_item])
            return


def delete_artist_from_plex(artist_rating_key: str):
    """Delete an artist and all their media from Plex (fallback for non-Lidarr artists)."""
    server = _get_server()
    artist = server.fetchItem(int(artist_rating_key))
    artist.delete()
