import logging
from typing import Optional

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


def build_lidarr_index(lidarr_artists: list[dict]) -> dict[str, int]:
    """Build a normalized-name -> lidarr_id lookup map."""
    return {normalize_name(a["artistName"]): a["id"] for a in lidarr_artists}


def find_lidarr_id(artist_name: str, index: dict[str, int]) -> Optional[int]:
    return index.get(normalize_name(artist_name))


def delete_artist(lidarr_id: int, delete_files: bool = True):
    """Remove an artist from Lidarr, deleting audio files via Lidarr's own file handling.
    Empty folders left behind can be cleaned up via Lidarr → System → Tasks → Clean Up Recycle Bin
    or the 'Clean Empty Folders' task."""
    with httpx.Client(headers=_HEADERS, timeout=30) as client:
        resp = client.delete(
            _url(f"/api/v1/artist/{lidarr_id}"),
            params={"deleteFiles": str(delete_files).lower(), "addImportListExclusion": "false"},
        )
        resp.raise_for_status()
        logger.info("Deleted Lidarr artist id=%s (deleteFiles=%s)", lidarr_id, delete_files)
