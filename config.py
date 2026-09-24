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
    """Operational options. Connection details and secrets (Plex, Lidarr,
    Last.fm, the login password) are set in the web UI or, optionally, in the
    environment: see services/settings_service.py."""
    # Add deleted artists to Lidarr's import list exclusions so import lists
    # (Spotify, Last.fm, etc.) don't re-add and re-download them.
    LIDARR_ADD_IMPORT_EXCLUSION: bool = _env_bool("LIDARR_ADD_IMPORT_EXCLUSION", True)
    # Deletes wait this long before running, and can be undone meanwhile. 0 = run immediately.
    DELETE_GRACE_SECONDS: int = int(os.environ.get("DELETE_GRACE_SECONDS", "300") or 0)
    TRIAGE_PLAYLIST_NAME: str = os.environ.get("TRIAGE_PLAYLIST_NAME", "Artist Triage")
    DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'triage.db'}")
    # Username for the web UI login (HTTP Basic auth). The password is set in the
    # web UI, or with FADERR_PASSWORD, which takes priority.
    FADERR_USERNAME: str = os.environ.get("FADERR_USERNAME", "faderr")


config = Config()
