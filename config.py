import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    PLEX_URL: str = os.environ["PLEX_URL"]
    PLEX_TOKEN: str = os.environ["PLEX_TOKEN"]
    PLEX_MUSIC_LIBRARY: str = os.environ["PLEX_MUSIC_LIBRARY"]
    LASTFM_API_KEY: str = os.environ["LASTFM_API_KEY"]
    LIDARR_URL: str = os.environ["LIDARR_URL"]
    LIDARR_API_KEY: str = os.environ["LIDARR_API_KEY"]
    TRIAGE_PLAYLIST_NAME: str = os.environ.get("TRIAGE_PLAYLIST_NAME", "Artist Triage")
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///triage.db")


config = Config()
