import os
from dotenv import load_dotenv

load_dotenv()


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
    TRIAGE_PLAYLIST_NAME: str = os.environ.get("TRIAGE_PLAYLIST_NAME", "Artist Triage")
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///triage.db")
    # HTTP Basic auth for the web UI and API. Auth is enabled when a password is set.
    FADERR_USERNAME: str = os.environ.get("FADERR_USERNAME", "faderr")
    FADERR_PASSWORD: str = os.environ.get("FADERR_PASSWORD", "")


config = Config()
