"""Connection settings and secrets, configured in the web UI.

Each setting can come from two places:

- the environment (or .env), which always wins, so existing deployments and
  people who prefer config files keep working, and a value set there is shown
  read-only in the UI; or
- the `settings` table, written from the Settings page.

Secrets never leave the server: the UI only learns whether they are set (and
their last few characters). The login password is stored as a scrypt hash.

Each process caches the effective settings and re-reads them at most every few
seconds, so a change saved through one server process reaches the others.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from sqlalchemy import select

from models import Setting, async_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldSpec:
    env: Optional[str]  # environment variable that overrides it, if any
    secret: bool = False
    label: str = ""


FIELDS: dict[str, FieldSpec] = {
    "plex_url": FieldSpec("PLEX_URL", label="Plex server URL"),
    "plex_token": FieldSpec("PLEX_TOKEN", secret=True, label="Plex token"),
    "plex_library": FieldSpec("PLEX_MUSIC_LIBRARY", label="Plex music library"),
    "plex_server_name": FieldSpec(None, label="Plex server"),
    "lidarr_url": FieldSpec("LIDARR_URL", label="Lidarr URL"),
    "lidarr_api_key": FieldSpec("LIDARR_API_KEY", secret=True, label="Lidarr API key"),
    "lastfm_api_key": FieldSpec("LASTFM_API_KEY", secret=True, label="Last.fm API key"),
}
PASSWORD_ENV = "FADERR_PASSWORD"

# Internal keys (not user-editable fields)
_PASSWORD_HASH = "password_hash"
_PLEX_CLIENT_ID = "plex_client_id"
_PLEX_PENDING = "plex_pending"  # sign-in in progress: account token, chosen server
PENDING_TTL = timedelta(minutes=30)

_REFRESH_SECONDS = 5.0

REQUIRED = ("plex_url", "plex_token", "plex_library", "lidarr_url", "lidarr_api_key")


# ── Password hashing (scrypt, stdlib) ─────────────────────────────────────────

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                dklen=len(expected), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def mask(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return "••••" + value[-4:] if len(value) > 8 else "••••"


# ── Store ─────────────────────────────────────────────────────────────────────

@dataclass
class Effective:
    values: dict[str, Optional[str]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # "env" | "saved" | "unset"
    password_env: Optional[str] = None
    password_hash: Optional[str] = None

    def get(self, key: str) -> Optional[str]:
        return self.values.get(key)


class SettingsStore:
    def __init__(self):
        self.current = Effective()
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self._applied: Optional[tuple] = None

    # Reading

    async def _read_rows(self) -> dict[str, Optional[str]]:
        async with async_session() as session:
            return {row.key: row.value for row in (await session.execute(select(Setting))).scalars()}

    def _merge(self, rows: dict[str, Optional[str]]) -> Effective:
        eff = Effective()
        for key, spec in FIELDS.items():
            env_value = os.environ.get(spec.env, "").strip() if spec.env else ""
            if env_value:
                eff.values[key], eff.sources[key] = env_value, "env"
            elif rows.get(key):
                eff.values[key], eff.sources[key] = rows[key], "saved"
            else:
                eff.values[key], eff.sources[key] = None, "unset"
        eff.password_env = os.environ.get(PASSWORD_ENV) or None
        eff.password_hash = rows.get(_PASSWORD_HASH) or None
        return eff

    async def load(self) -> Effective:
        """Re-read settings and reconfigure the Plex and Lidarr clients if their
        connection details changed."""
        async with self._lock:
            self.current = self._merge(await self._read_rows())
            self._loaded_at = time.monotonic()
            await self._apply()
            return self.current

    async def refresh_if_stale(self) -> None:
        if time.monotonic() - self._loaded_at >= _REFRESH_SECONDS:
            await self.load()

    async def _apply(self) -> None:
        from services.lidarr_service import lidarr
        from services.plex_service import plex

        v = self.current.values
        wanted = (v.get("plex_url"), v.get("plex_token"), v.get("plex_library"),
                  v.get("lidarr_url"), v.get("lidarr_api_key"))
        if wanted == self._applied:
            return
        await plex.configure(v.get("plex_url"), v.get("plex_token"), v.get("plex_library"))
        lidarr.configure(v.get("lidarr_url"), v.get("lidarr_api_key"))
        self._applied = wanted

    # State

    def configured(self) -> bool:
        return all(self.current.get(k) for k in REQUIRED)

    def missing(self) -> list[str]:
        return [FIELDS[k].label for k in REQUIRED if not self.current.get(k)]

    def password_required(self) -> bool:
        return bool(self.current.password_env or self.current.password_hash)

    def check_password(self, password: str) -> bool:
        if self.current.password_env is not None:
            return hmac.compare_digest(password.encode(), self.current.password_env.encode())
        if self.current.password_hash:
            return verify_password(password, self.current.password_hash)
        return True

    def public_view(self) -> dict:
        """What the Settings page may see: values for plain fields, masked
        secrets, and where each came from. Never the secrets themselves."""
        fields = {}
        for key, spec in FIELDS.items():
            value = self.current.get(key)
            fields[key] = {
                "label": spec.label,
                "value": mask(value) if spec.secret else value,
                "set": bool(value),
                "source": self.current.sources.get(key, "unset"),
                "secret": spec.secret,
            }
        return {
            "configured": self.configured(),
            "missing": self.missing(),
            "fields": fields,
            "password": {
                "set": self.password_required(),
                "source": "env" if self.current.password_env else ("saved" if self.current.password_hash else "unset"),
            },
        }

    # Writing

    async def _write(self, values: dict[str, Optional[str]]) -> None:
        async with async_session() as session:
            async with session.begin():
                for key, value in values.items():
                    row = await session.get(Setting, key)
                    if value is None:
                        if row is not None:
                            await session.delete(row)
                    elif row is None:
                        session.add(Setting(key=key, value=value))
                    else:
                        row.value = value

    async def save(self, values: dict[str, Optional[str]]) -> Effective:
        """Save user-editable fields. Fields provided by the environment can't be
        changed here (the environment would override them anyway)."""
        for key in values:
            if key not in FIELDS:
                raise ValueError(f"Unknown setting: {key}")
            if self.current.sources.get(key) == "env":
                raise ValueError(f"{FIELDS[key].label} is set in the environment (.env), so it can't be changed here")
        await self._write(values)
        return await self.load()

    async def set_password(self, password: Optional[str]) -> None:
        if self.current.password_env:
            raise ValueError("The password is set with FADERR_PASSWORD in the environment, so it can't be changed here")
        await self._write({_PASSWORD_HASH: hash_password(password) if password else None})
        await self.load()

    # Plex sign-in state

    async def plex_client_id(self) -> str:
        """This Faderr install's Plex client identifier (created once)."""
        rows = await self._read_rows()
        if rows.get(_PLEX_CLIENT_ID):
            return rows[_PLEX_CLIENT_ID]
        client_id = f"faderr-{uuid.uuid4()}"
        await self._write({_PLEX_CLIENT_ID: client_id})
        return client_id

    async def get_pending(self) -> Optional[dict]:
        raw = (await self._read_rows()).get(_PLEX_PENDING)
        if not raw:
            return None
        pending = json.loads(raw)
        if time.time() - pending.get("created", 0) > PENDING_TTL.total_seconds():
            await self.clear_pending()
            return None
        return pending

    async def set_pending(self, pending: dict) -> None:
        pending.setdefault("created", time.time())
        await self._write({_PLEX_PENDING: json.dumps(pending)})

    async def clear_pending(self) -> None:
        await self._write({_PLEX_PENDING: None})


store = SettingsStore()
