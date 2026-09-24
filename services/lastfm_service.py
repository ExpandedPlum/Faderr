import asyncio
import logging
import re
from typing import Optional

import httpx

from config import config

logger = logging.getLogger(__name__)

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
_SEMAPHORE = asyncio.Semaphore(5)
_ATTEMPTS = 3

# Last.fm reports many failures as an "error" code in the JSON body rather than
# through the HTTP status. These are temporary and worth retrying:
# 8 operation failed, 11 service offline, 16 temporary error, 29 rate limit exceeded.
# Anything else (e.g. 6 "artist not found") won't change on retry.
_RETRYABLE_ERRORS = {8, 11, 16, 29}


async def _call(client: httpx.AsyncClient, params: dict, artist_name: str) -> Optional[dict]:
    """Call a Last.fm API method, retrying temporary failures with backoff.
    Returns the parsed response, or None if there is no usable result."""
    for attempt in range(_ATTEMPTS):
        last_attempt = attempt == _ATTEMPTS - 1
        wait = 2 ** attempt
        try:
            resp = await client.get(
                LASTFM_BASE,
                params={**params, "api_key": config.LASTFM_API_KEY, "format": "json"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            logger.warning("Last.fm request failed for %r: %s", artist_name, exc)
            if not last_attempt:
                await asyncio.sleep(wait)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            logger.warning("Last.fm returned HTTP %s for %r, backing off %ss", resp.status_code, artist_name, wait)
            if not last_attempt:
                await asyncio.sleep(wait)
            continue

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Last.fm returned a non-JSON response for %r", artist_name)
            return None
        if not isinstance(data, dict):
            return None

        error = data.get("error")
        if error in _RETRYABLE_ERRORS:
            logger.warning("Last.fm error %s for %r (%s), backing off %ss",
                           error, artist_name, data.get("message", ""), wait)
            if not last_attempt:
                await asyncio.sleep(wait)
            continue
        if error:
            logger.debug("Last.fm error %s for %r: %s", error, artist_name, data.get("message", ""))
            return None
        return data
    return None


async def get_top_track(artist_name: str, client: httpx.AsyncClient) -> Optional[str]:
    """Return the name of the artist's top track on Last.fm, or None."""
    async with _SEMAPHORE:
        data = await _call(client, {"method": "artist.gettoptracks", "artist": artist_name, "limit": 1}, artist_name)
    if not data:
        return None
    tracks = (data.get("toptracks") or {}).get("track") or []
    if isinstance(tracks, dict):  # a single result can come back as an object, not a list
        tracks = [tracks]
    for track in tracks:
        if isinstance(track, dict) and track.get("name"):
            return track["name"]
    return None


async def get_artist_bio(artist_name: str) -> Optional[str]:
    """Return a plain-text bio summary for an artist from Last.fm, or None."""
    async with httpx.AsyncClient() as client:
        data = await _call(client, {"method": "artist.getinfo", "artist": artist_name, "autocorrect": 1}, artist_name)
    if not data:
        return None
    summary = ((data.get("artist") or {}).get("bio") or {}).get("summary", "")
    if not summary:
        return None
    # Strip the "Read more on Last.fm" anchor tag Last.fm appends
    summary = re.sub(r'<a\b[^>]*>.*?</a>', '', summary, flags=re.IGNORECASE | re.DOTALL)
    summary = re.sub(r'<[^>]+>', '', summary)  # strip any remaining HTML tags
    summary = summary.strip().strip(".")
    return summary or None
