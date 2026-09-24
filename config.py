import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# .env and the default database live next to the app, whatever the working directory
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Config:
    PLEX_URL: str = os.environ["PLEX_URL"]
    PLEX_TOKEN: str = os.environ["PLEX_TOKEN"]
    PLEX_MUSIC_LIBRARY: str = os.environ["PLEX_MUSIC_LIBRARY"]
    LASTFM_API_KEY: str = os.environ["LASTFM_API_KEY"]
    LIDARR_URL: str = os.environ["LIDARR_URL"]
    LIDARR_API_KEY: str = os.environ["LIDARR_API_KEY"]
    # Add deleted artists to Lidarr's import list exclusions so import lists
    # (Spotify, Last.fm, etc.) don't re-add and re-download them.
    LIDARR_ADD_IMPORT_EXCLUSION: bool = _env_bool("LIDARR_ADD_IMPORT_EXCLUSION", True)
    # Deletes wait this long before running, and can be undone meanwhile. 0 = run immediately.
    DELETE_GRACE_SECONDS: int = int(os.environ.get("DELETE_GRACE_SECONDS", "300") or 0)
    TRIAGE_PLAYLIST_NAME: str = os.environ.get("TRIAGE_PLAYLIST_NAME", "Artist Triage")
    DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'triage.db'}")
    # HTTP Basic auth for the web UI and API. Auth is enabled when a password is set.
    FADERR_USERNAME: str = os.environ.get("FADERR_USERNAME", "faderr")
    FADERR_PASSWORD: str = os.environ.get("FADERR_PASSWORD", "")


config = Config()
