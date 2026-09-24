"""Lidarr access (async) and matching Plex artists to Lidarr artists."""
import logging
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx

from config import config
from services.plex_service import normalize_name

logger = logging.getLogger(__name__)

# Deleting a large artist on network storage can take a while
_DELETE_TIMEOUT = 120


# ── Matching ──────────────────────────────────────────────────────────────────

def name_key(name: str) -> str:
    """Matching key for an artist name.
    Uses the ASCII-folded normalization where possible. Names with no ASCII
    letters or digits (non-Latin scripts, symbol-only names like "!!!") would
    normalize to "" and collide with each other, so those fall back to a
    case-folded, whitespace-collapsed form of the original name instead."""
    key = normalize_name(name or "")
    if key:
        return key
    return " ".join(unicodedata.normalize("NFKC", name or "").casefold().split())


def _folder_name(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _dir_components(file_paths: Iterable[str]) -> set[str]:
    """Every directory name that appears in the given file paths (case-folded)."""
    components: set[str] = set()
    for path in file_paths:
        parts = path.replace("\\", "/").split("/")[:-1]  # drop the filename
        components.update(p.casefold() for p in parts if p)
    return components


@dataclass
class LidarrMatch:
    lidarr_id: Optional[int]
    # True when a Lidarr artist *might* own this artist's files but we can't
    # pin down which one. Deleting in this state is unsafe.
    ambiguous: bool
    reason: str
    folder: Optional[str] = None  # the matched Lidarr artist's folder, for the audit log


def _matched(artist: dict, reason: str) -> LidarrMatch:
    return LidarrMatch(artist["id"], False, reason, _folder_name(artist.get("path") or "") or None)


def resolve_artist(
    artist_name: str, file_paths: list[str], lidarr_artists: list[dict], mbid: Optional[str] = None,
) -> LidarrMatch:
    """Find the Lidarr artist that owns a Plex artist's files.

    Lidarr stores every artist in its own folder, and Plex reads those same
    files, so the Lidarr artist folder name must appear in the Plex file paths.
    A folder match is what makes any match trustworthy:

    - If Plex knows the artist's MusicBrainz ID and a Lidarr artist has the
      same ID, that artist is the match, but only if its folder also appears
      in the Plex paths. If it doesn't, one of the two is wrong, so the match
      is ambiguous.
    - Otherwise the folder decides, and the name breaks ties between several
      folder matches.
    - A name match without a folder match (e.g. a different artist with the
      same name) is ambiguous rather than trusted.
    """
    key = name_key(artist_name)
    name_matches = [a for a in lidarr_artists if key and name_key(a.get("artistName", "")) == key]

    if not file_paths:
        if name_matches:
            return LidarrMatch(None, True, "Plex reported no file paths, so the Lidarr match can't be verified")
        return LidarrMatch(None, True, "Plex reported no file paths for this artist")

    components = _dir_components(file_paths)
    path_matches = [
        a for a in lidarr_artists
        if a.get("path") and _folder_name(a["path"]).casefold() in components
    ]

    if mbid:
        mbid_matches = [
            a for a in lidarr_artists if (a.get("foreignArtistId") or "").lower() == mbid.lower()
        ]
        if mbid_matches:
            verified = [a for a in mbid_matches if a in path_matches]
            if len(verified) == 1:
                return _matched(verified[0], "matched MusicBrainz ID and Lidarr artist folder")
            return LidarrMatch(
                None, True,
                "Lidarr has an artist with this MusicBrainz ID, but its folder isn't where Plex finds the files",
            )

    if len(path_matches) == 1:
        return _matched(path_matches[0], "matched Lidarr artist folder")
    if len(path_matches) > 1:
        name_and_path = [a for a in path_matches if a in name_matches]
        if len(name_and_path) == 1:
            return _matched(name_and_path[0], "matched Lidarr artist folder and name")
        names = ", ".join(sorted(a.get("artistName", "?") for a in path_matches))
        return LidarrMatch(None, True, f"files match several Lidarr artist folders ({names})")
    if name_matches:
        return LidarrMatch(
            None, True,
            "a Lidarr artist has this name but its folder doesn't match the Plex file paths",
        )
    return LidarrMatch(None, False, "not managed by Lidarr")


# ── API client ────────────────────────────────────────────────────────────────

class LidarrClient:
    """Async Lidarr API client. Lidarr is called rarely (once per generation,
    a few times per delete), so each call uses a short-lived connection; that
    also keeps the client independent of any particular event loop."""

    def __init__(self, url: str, api_key: str):
        self._base = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}

    def _client(self, read_timeout: float = 15) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base, headers=self._headers, timeout=httpx.Timeout(15, read=read_timeout),
        )

    async def all_artists(self) -> list[dict]:
        """Return all artists Lidarr knows about."""
        async with self._client() as client:
            resp = await client.get("/api/v1/artist")
            resp.raise_for_status()
            return resp.json()

    async def delete_artist(self, lidarr_id: int, delete_files: bool = True) -> None:
        """Remove an artist from Lidarr, deleting audio files via Lidarr's own file handling.
        Empty folders left behind can be cleaned up via Lidarr → System → Tasks → Clean Up Recycle Bin
        or the 'Clean Empty Folders' task."""
        add_exclusion = config.LIDARR_ADD_IMPORT_EXCLUSION
        async with self._client(read_timeout=_DELETE_TIMEOUT) as client:
            resp = await client.delete(
                f"/api/v1/artist/{lidarr_id}",
                params={
                    "deleteFiles": str(delete_files).lower(),
                    "addImportListExclusion": str(add_exclusion).lower(),
                },
            )
            resp.raise_for_status()
        logger.info(
            "Deleted Lidarr artist id=%s (deleteFiles=%s, addImportListExclusion=%s)",
            lidarr_id, delete_files, add_exclusion,
        )

    async def artist_exists(self, lidarr_id: int) -> bool:
        """True if Lidarr still has this artist, False if it's gone (404)."""
        async with self._client() as client:
            resp = await client.get(f"/api/v1/artist/{lidarr_id}")
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            return True

    async def unmonitor_artist(self, lidarr_id: int) -> None:
        """Mark an artist as unmonitored in Lidarr so it won't be re-downloaded.
        Used as a fallback when full deletion fails."""
        async with self._client() as client:
            resp = await client.get(f"/api/v1/artist/{lidarr_id}")
            resp.raise_for_status()
            artist_data = resp.json()
            artist_data["monitored"] = False
            resp = await client.put(f"/api/v1/artist/{lidarr_id}", json=artist_data)
            resp.raise_for_status()
        logger.info("Unmonitored Lidarr artist id=%s", lidarr_id)


lidarr = LidarrClient(config.LIDARR_URL, config.LIDARR_API_KEY)
