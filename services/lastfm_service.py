import asyncio
import logging
from typing import Optional

import httpx

from config import config

logger = logging.getLogger(__name__)

LASTFM_BASE = "http://ws.audioscrobbler.com/2.0/"
_SEMAPHORE = asyncio.Semaphore(5)


async def get_top_track(artist_name: str, client: httpx.AsyncClient) -> Optional[str]:
    """Return the name of the artist's top track on Last.fm, or None."""
    async with _SEMAPHORE:
        for attempt in range(3):
            try:
                resp = await client.get(
                    LASTFM_BASE,
                    params={
                        "method": "artist.gettoptracks",
                        "artist": artist_name,
                        "api_key": config.LASTFM_API_KEY,
                        "format": "json",
                        "limit": 1,
                    },
                    timeout=10.0,
                )
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning("Last.fm rate limited, backing off %ss", wait)
                    await asyncio.sleep(wait)
                    continue
                data = resp.json()
                tracks = data.get("toptracks", {}).get("track", [])
                if tracks:
                    return tracks[0]["name"]
                return None
            except Exception as exc:
                logger.warning("Last.fm lookup failed for %r: %s", artist_name, exc)
                if attempt < 2:
                    await asyncio.sleep(1)
        return None


async def get_artist_bio(artist_name: str) -> Optional[str]:
    """Return a plain-text bio summary for an artist from Last.fm, or None."""
    import re
    async with httpx.AsyncClient() as client:
        for attempt in range(3):
            try:
                resp = await client.get(
                    LASTFM_BASE,
                    params={
                        "method": "artist.getinfo",
                        "artist": artist_name,
                        "api_key": config.LASTFM_API_KEY,
                        "format": "json",
                        "autocorrect": 1,
                    },
                    timeout=10.0,
                )
                if resp.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                data = resp.json()
                summary = data.get("artist", {}).get("bio", {}).get("summary", "")
                if not summary:
                    return None
                # Strip the "Read more on Last.fm" anchor tag Last.fm appends
                summary = re.sub(r'<a\b[^>]*>.*?</a>', '', summary, flags=re.IGNORECASE | re.DOTALL)
                summary = re.sub(r'<[^>]+>', '', summary)  # strip any remaining HTML tags
                summary = summary.strip().strip(".")
                return summary or None
            except Exception as exc:
                logger.warning("Last.fm bio lookup failed for %r: %s", artist_name, exc)
                if attempt < 2:
                    await asyncio.sleep(1)
    return None


