"""Sign in with Plex, and finding a usable server connection.

Uses plex.tv's PIN flow (the same endpoints plexapi's MyPlexPinLogin uses):
create a PIN, send the user to app.plex.tv to approve it, then poll the PIN
until it carries a token. Every step is a plain HTTP call, so no state is
kept in memory between requests.

The account token is only used to list the account's servers. Faderr stores
the chosen server's own access token, which is narrower than the account token.
"""
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
PLEX_AUTH_APP = "https://app.plex.tv/auth/#!"
PRODUCT = "Faderr"


class PinExpired(Exception):
    """The PIN is unknown to plex.tv (expired or never existed)."""


class NoWorkingConnection(Exception):
    def __init__(self, tried: list[str]):
        super().__init__("None of the server's addresses could be reached from Faderr: " + ", ".join(tried))
        self.tried = tried


def _headers(client_id: str, token: Optional[str] = None) -> dict:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": "1.0",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Device-Name": PRODUCT,
        "X-Plex-Platform": "Web",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


# Tests swap in a mock transport here; the client itself is always built by http_client().
_transport: Optional[httpx.AsyncBaseTransport] = None


def http_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """All outbound HTTP for sign-in and connection tests goes through here."""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), transport=_transport)


# ── PIN flow ──────────────────────────────────────────────────────────────────

def auth_url(client_id: str, code: str) -> str:
    params = {
        "clientID": client_id,
        "code": code,
        "context[device][product]": PRODUCT,
        "context[device][deviceName]": PRODUCT,
        "context[device][platform]": "Web",
    }
    return f"{PLEX_AUTH_APP}?{urlencode(params)}"


async def create_pin(client_id: str) -> dict:
    """Start a sign-in. Returns {"id", "code", "auth_url"}."""
    async with http_client() as client:
        resp = await client.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"}, headers=_headers(client_id))
        resp.raise_for_status()
        data = resp.json()
    return {"id": data["id"], "code": data["code"], "auth_url": auth_url(client_id, data["code"])}


async def check_pin(client_id: str, pin_id: int) -> Optional[str]:
    """The account token once the user has approved the PIN, else None."""
    async with http_client() as client:
        resp = await client.get(f"{PLEX_TV}/api/v2/pins/{int(pin_id)}", headers=_headers(client_id))
        if resp.status_code == 404:
            raise PinExpired()
        resp.raise_for_status()
        return resp.json().get("authToken") or None


# ── Servers ───────────────────────────────────────────────────────────────────

async def list_servers(client_id: str, account_token: str) -> list[dict]:
    """The Plex Media Servers this account can use, with their connections and
    their own access tokens."""
    async with http_client() as client:
        resp = await client.get(
            f"{PLEX_TV}/api/v2/resources",
            params={"includeHttps": 1, "includeRelay": 1, "includeIPv6": 1},
            headers=_headers(client_id, account_token),
        )
        resp.raise_for_status()
        resources = resp.json()
    servers = []
    for r in resources:
        if "server" not in (r.get("provides") or "").split(","):
            continue
        servers.append({
            "id": r.get("clientIdentifier"),
            "name": r.get("name") or "Plex Media Server",
            "owned": bool(r.get("owned")),
            "access_token": r.get("accessToken") or account_token,
            "connections": r.get("connections") or [],
        })
    return servers


def candidate_urls(server: dict) -> list[str]:
    """Addresses to try, best first: local before remote before relay, and
    HTTPS before HTTP. A local connection is also tried over plain HTTP on its
    IP address, which works even when the network's DNS blocks Plex's
    *.plex.direct names (common with DNS rebinding protection)."""
    ranked = []
    for c in server.get("connections", []):
        uri = (c.get("uri") or "").rstrip("/")
        if not uri:
            continue
        tier = 2 if c.get("relay") else (0 if c.get("local") else 1)
        ipv6 = 1 if c.get("IPv6") else 0
        ranked.append(((tier, 0 if uri.startswith("https") else 1, ipv6), uri))
        address, port = c.get("address"), c.get("port")
        if c.get("local") and not c.get("relay") and address and port:
            host = f"[{address}]" if ":" in address else address
            ranked.append(((tier, 1, ipv6), f"http://{host}:{port}"))
    seen, urls = set(), []
    for _, uri in sorted(ranked, key=lambda x: x[0]):
        if uri not in seen:
            seen.add(uri)
            urls.append(uri)
    return urls


async def server_info(url: str, token: str, client_id: str = PRODUCT) -> dict:
    """Connect to a server and describe it: {"name", "libraries"} where
    libraries are the titles of its music libraries. Raises on failure."""
    url = url.rstrip("/")
    async with http_client(timeout=6.0) as client:
        root = await client.get(f"{url}/", headers=_headers(client_id, token))
        root.raise_for_status()
        sections = await client.get(f"{url}/library/sections", headers=_headers(client_id, token))
        sections.raise_for_status()
    container = root.json().get("MediaContainer", {})
    directories = sections.json().get("MediaContainer", {}).get("Directory", []) or []
    return {
        "name": container.get("friendlyName") or "Plex Media Server",
        "libraries": [d.get("title") for d in directories if d.get("type") == "artist" and d.get("title")],
    }


async def first_working(server: dict, client_id: str) -> tuple[str, dict]:
    """Find the first address of `server` that Faderr can actually reach.
    Returns (url, server_info)."""
    tried = []
    for url in candidate_urls(server):
        try:
            return url, await server_info(url, server["access_token"], client_id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Plex address %s didn't work: %s", url, exc)
            tried.append(url)
    raise NoWorkingConnection(tried)
