import logging
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx

from config import config
from services.plex_service import normalize_name

logger = logging.getLogger(__name__)

_HEADERS = {"X-Api-Key": config.LIDARR_API_KEY}


def _url(path: str) -> str:
    return f"{config.LIDARR_URL.rstrip('/')}{path}"


def get_all_artists() -> list[dict]:
    """Return all artists Lidarr knows about."""
    with httpx.Client(headers=_HEADERS, timeout=15) as client:
        resp = client.get(_url("/api/v1/artist"))
        resp.raise_for_status()
        return resp.json()


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
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()


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


def resolve_artist(artist_name: str, file_paths: list[str], lidarr_artists: list[dict]) -> LidarrMatch:
    """Find the Lidarr artist that owns a Plex artist's files.

    Lidarr stores every artist in its own folder, and Plex reads those same
    files, so the Lidarr artist folder name must appear in the Plex file paths.
    Matching on the folder is what makes a match trustworthy; the name only
    breaks ties between several folder matches. A name match without a folder
    match (e.g. a different artist with the same name) is reported as
    ambiguous rather than trusted.
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
        if a.get("path") and _folder_name(a["path"]) in components
    ]

    if len(path_matches) == 1:
        return LidarrMatch(path_matches[0]["id"], False, "matched Lidarr artist folder")
    if len(path_matches) > 1:
        name_and_path = [a for a in path_matches if a in name_matches]
        if len(name_and_path) == 1:
            return LidarrMatch(name_and_path[0]["id"], False, "matched Lidarr artist folder and name")
        names = ", ".join(sorted(a.get("artistName", "?") for a in path_matches))
        return LidarrMatch(None, True, f"files match several Lidarr artist folders ({names})")
    if name_matches:
        return LidarrMatch(
            None, True,
            "a Lidarr artist has this name but its folder doesn't match the Plex file paths",
        )
    return LidarrMatch(None, False, "not managed by Lidarr")


def delete_artist(lidarr_id: int, delete_files: bool = True):
    """Remove an artist from Lidarr, deleting audio files via Lidarr's own file handling.
    Empty folders left behind can be cleaned up via Lidarr → System → Tasks → Clean Up Recycle Bin
    or the 'Clean Empty Folders' task."""
    add_exclusion = config.LIDARR_ADD_IMPORT_EXCLUSION
    with httpx.Client(headers=_HEADERS, timeout=30) as client:
        resp = client.delete(
            _url(f"/api/v1/artist/{lidarr_id}"),
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


def unmonitor_artist(lidarr_id: int):
    """Mark an artist as unmonitored in Lidarr so it won't be re-downloaded.
    Used as a fallback when full deletion fails."""
    with httpx.Client(headers=_HEADERS, timeout=15) as client:
        resp = client.get(_url(f"/api/v1/artist/{lidarr_id}"))
        resp.raise_for_status()
        artist_data = resp.json()
        artist_data["monitored"] = False
        resp = client.put(_url(f"/api/v1/artist/{lidarr_id}"), json=artist_data)
        resp.raise_for_status()
        logger.info("Unmonitored Lidarr artist id=%s", lidarr_id)
