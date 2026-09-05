"""Per-request BAS credentials for the HTTP transport.

The stdio server has exactly one identity — the `ONEC_*` env block — and that stays true. Over
HTTP the process serves many people, and a single service account would make every action in the
BAS registration log look identical. So an HTTP caller may present its OWN BAS account in an
`Authorization: Basic` header, and BAS rights become the access control.

Three pieces live here:

  * `Credentials` + `parse_authorization()` — the header, validated, never echoed back.
  * a `ContextVar` carrying this request's credentials from the ASGI layer to the tool executor.
  * `ClientPool` — one `MCBPClient` (and therefore one httpx pool AND one metadata cache) per
    distinct credential set, reused across that caller's requests.

WHY A CONTEXTVAR, AND WHY IT IS SAFE HERE. `ServerRequestContext` (what `on_call_tool` receives
on `mcp==2.0.0`) has NO `transport` field at all, so the SDK's `TransportContext.headers` never
reaches a handler; and `StreamableHTTPSessionManager` only populates that field on the modern
(2026-07-28) path anyway. Capturing the headers in our own ASGI wrapper is therefore the only
route. That the value survives into the handler's task was verified empirically against
`mcp==2.0.0` on a live uvicorn run — stateful sessions, stateless mode, and the modern path, and
under four concurrent overlapping tool calls each seeing its own header. It is a per-REQUEST
value, not a per-session one: a session whose `initialize` carried header A and whose `tools/call`
carried header B executes as B.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
from mcbp_core.client import ConnectionConfig, MCBPClient

if TYPE_CHECKING:
    from mcbp_mcp_server.server import AppContext, Settings

log = logging.getLogger("mcbp_mcp_server")

# A long-lived process must not accumulate one httpx pool per person who ever connected. Both
# limits are deliberately generous: eviction closes a client, which throws away its warmed
# metadata cache, so it should happen on abandonment, not on ordinary idleness between turns.
MAX_CLIENTS = 32
IDLE_TTL_S = 1800.0


class CredentialsError(Exception):
    """A present but unusable `Authorization` header.

    Never carries the header value: the message goes into an HTTP response and a log line, and
    the header is a password. Absent header is NOT this error — that is the env fallback.
    """


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str = field(repr=False)

    @property
    def cache_key(self) -> str:
        """Identifies the credential set without carrying it. The password never appears in a
        dict key, a log line, or an eviction message — only this digest does."""
        raw = f"{self.user}\0{self.password}".encode()
        return hashlib.sha256(raw).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - defensive, exercised only by a stray log
        return f"Credentials(user={self.user!r})"


def parse_authorization(value: str) -> Credentials:
    """`Basic <base64 user:password>` -> `Credentials`. Raises `CredentialsError` on anything
    else — a malformed header must never fall back to the env account, which would mean a user
    with a bad config quietly acting as the service identity."""
    scheme, _, payload = value.partition(" ")
    if scheme.lower() != "basic":
        raise CredentialsError(
            "Authorization must use the Basic scheme: 'Basic <base64 of user:password>'"
        )
    payload = payload.strip()
    if not payload:
        raise CredentialsError("Authorization: Basic is missing its base64 credentials")
    try:
        decoded = base64.b64decode(payload, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise CredentialsError(
            "Authorization: Basic value is not valid base64 of a UTF-8 'user:password'"
        ) from e
    user, sep, password = decoded.partition(":")
    if not sep:
        raise CredentialsError("Authorization: Basic credentials must be 'user:password'")
    if not user:
        raise CredentialsError("Authorization: Basic credentials have an empty user name")
    return Credentials(user=user, password=password)


_request_credentials: ContextVar[Credentials | None] = ContextVar(
    "mcbp_request_credentials", default=None,
)


def set_request_credentials(creds: Credentials | None) -> Token[Credentials | None]:
    return _request_credentials.set(creds)


def reset_request_credentials(token: Token[Credentials | None]) -> None:
    _request_credentials.reset(token)


def current_credentials() -> Credentials | None:
    """`None` on stdio (no headers exist) and for an HTTP request with no `Authorization` — both
    mean "use the env identity"."""
    return _request_credentials.get()


@dataclass
class _Entry:
    client: MCBPClient
    last_used: float
    inflight: int = 0


class ClientPool:
    """One live `MCBPClient` per credential set, reused across that caller's requests.

    A fresh client per tool call would mean a fresh httpx pool and a cold metadata cache on every
    model turn. Conversely the cache MUST NOT be shared: it is per client instance, which is
    exactly what keeps one user's metadata (and connection) out of another's requests.
    """

    def __init__(self, settings: Settings, *, max_clients: int = MAX_CLIENTS,
                 idle_ttl_s: float = IDLE_TTL_S) -> None:
        self._settings = settings
        self._max_clients = max_clients
        self._idle_ttl_s = idle_ttl_s
        self._entries: dict[str, _Entry] = {}
        self._lock = anyio.Lock()

    @property
    def size(self) -> int:
        return len(self._entries)

    def _build(self, creds: Credentials) -> MCBPClient:
        s = self._settings
        return MCBPClient(ConnectionConfig(
            base_url=s.base_url,
            user=creds.user,
            password=creds.password,
            timeout_s=s.timeout,
            pool_max=s.pool_max,
            mock=False,
            verify=s.verify_ssl,
        ))

    @asynccontextmanager
    async def lease(self, creds: Credentials) -> AsyncIterator[MCBPClient]:
        """Hands out the client for `creds`, held against eviction for the duration."""
        key = creds.cache_key
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                client = self._build(creds)
                await client.startup()
                entry = _Entry(client=client, last_used=anyio.current_time())
                self._entries[key] = entry
                log.info("BAS client opened for user %s (pool size %d)", creds.user, self.size)
            entry.inflight += 1
            entry.last_used = anyio.current_time()
            await self._evict_locked()
        try:
            yield entry.client
        finally:
            async with self._lock:
                entry.inflight -= 1
                entry.last_used = anyio.current_time()

    async def _evict_locked(self) -> None:
        """Closes idle clients: anything unused past the TTL, then the least recently used while
        over the size cap. An entry with a call in flight is never closed — `aclose()` on a live
        httpx pool would abort that caller's request."""
        now = anyio.current_time()
        stale = [
            key for key, e in self._entries.items()
            if e.inflight == 0 and now - e.last_used > self._idle_ttl_s
        ]
        for key in stale:
            await self._close(key, "idle")
        if len(self._entries) <= self._max_clients:
            return
        by_age = sorted(
            (k for k, e in self._entries.items() if e.inflight == 0),
            key=lambda k: self._entries[k].last_used,
        )
        for key in by_age[: len(self._entries) - self._max_clients]:
            await self._close(key, "pool full")

    async def _close(self, key: str, reason: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:  # pragma: no cover - only reachable on a concurrent pop
            return
        log.info("closing BAS client %s (%s)", key[:8], reason)
        await entry.client.shutdown()

    async def aclose(self) -> None:
        async with self._lock:
            for key in list(self._entries):
                await self._close(key, "shutdown")


@asynccontextmanager
async def client_for(app: AppContext) -> AsyncIterator[MCBPClient]:
    """The client this request must run as: the caller's own when an `Authorization` header was
    presented, otherwise the process's env identity (always the case on stdio)."""
    creds = current_credentials()
    if creds is None or app.pool is None:
        yield app.client
        return
    async with app.pool.lease(creds) as client:
        yield client
