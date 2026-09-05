"""Streamable HTTP transport for the `mcbp-ai` MCP server.

A second entry point NEXT TO stdio, not a replacement: `create_server()`, the tool registry and
the `lifespan` that owns `MCBPClient` are reused untouched. `StreamableHTTPSessionManager` takes
the low-level `Server` directly, so this module only wires ASGI — it holds no knowledge of BAS
routes or of the tool set.

Deployment notes that are NOT obvious from the code:
  * `Route`, not `Mount`. Starlette compiles `Mount("/mcp")` to `^/mcp/(?P<path>.*)$`, so an exact
    POST to `/mcp` misses the mount and gets a 307 to `/mcp/` — and that bare form is exactly what
    a user types into a connector URL field.
  * DNS-rebinding protection is opt-in here: the SDK's own default when no settings are passed is
    "disabled", and enabling it with an empty allow-list would answer every request with 421.
    Set MCP_HTTP_ALLOWED_HOSTS once the public hostname is known.
  * nginx in front needs `proxy_buffering off` (SSE) and a read timeout above the client's own
    300 s tool-call limit.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from mcbp_mcp_server.server import (
    Settings,
    SettingsError,
    _optional_float,
    _optional_int,
    _truthy,
    create_server,
    load_settings,
)

log = logging.getLogger("mcbp_mcp_server")


@dataclass(frozen=True)
class HttpSettings:
    host: str = "127.0.0.1"
    port: int = 8010
    path: str = "/mcp"
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    session_idle_timeout: float = 1800.0
    stateless: bool = False


def _csv(env: Mapping[str, str], name: str) -> list[str]:
    return [item.strip() for item in env.get(name, "").split(",") if item.strip()]


def load_http_settings(env: Mapping[str, str] | None = None) -> HttpSettings:
    """Reads `MCP_HTTP_*` from `env` (defaults to `os.environ`). Pure, like `load_settings()`:
    raises `SettingsError`, never prints or exits. Binds to loopback by default — the process is
    meant to sit behind nginx, and a default of 0.0.0.0 would publish an unauthenticated endpoint
    the moment someone forgets the reverse proxy."""
    e = os.environ if env is None else env
    path = e.get("MCP_HTTP_PATH", "/mcp").rstrip("/") or "/"
    if not path.startswith("/"):
        raise SettingsError(f"MCP_HTTP_PATH must start with '/', got {path!r}")
    return HttpSettings(
        host=e.get("MCP_HTTP_HOST", "127.0.0.1"),
        port=_optional_int(e, "MCP_HTTP_PORT", 8010),
        path=path,
        allowed_hosts=_csv(e, "MCP_HTTP_ALLOWED_HOSTS"),
        allowed_origins=_csv(e, "MCP_HTTP_ALLOWED_ORIGINS"),
        session_idle_timeout=_optional_float(e, "MCP_HTTP_SESSION_IDLE_TIMEOUT", 1800.0),
        stateless=_truthy(e.get("MCP_HTTP_STATELESS")),
    )


def _security(settings: HttpSettings) -> TransportSecuritySettings | None:
    """`None` leaves the SDK middleware at its own permissive default. Protection is enabled only
    once a host allow-list exists, because the middleware rejects EVERY request (421) when
    enabled with an empty list."""
    if not settings.allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )


class _ManagerEndpoint:
    """Raw ASGI adapter so `Route` treats this as an ASGI app rather than a `func(request)`
    endpoint — Starlette makes that decision with `inspect.isfunction/ismethod`, and
    `manager.handle_request` is a bound method, which would be misread as the latter."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)


async def _healthz(request: Request) -> PlainTextResponse:
    """Liveness only — says the process is up, NOT that BAS is reachable. The MCP endpoint itself
    cannot serve as a probe: it answers 400 without a negotiated session."""
    return PlainTextResponse("ok")


def create_app(settings: HttpSettings | None = None) -> Starlette:
    """Builds the ASGI app without touching the network. `create_server()` is called once; the
    `MCBPClient` behind it is still built lazily by the server's own `lifespan`."""
    http_settings = load_http_settings() if settings is None else settings
    manager = StreamableHTTPSessionManager(
        create_server(),
        json_response=False,
        stateless=http_settings.stateless,
        security_settings=_security(http_settings),
        session_idle_timeout=http_settings.session_idle_timeout,
    )

    @asynccontextmanager
    async def app_lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            log.info("MCP Streamable HTTP ready on %s", http_settings.path)
            yield

    return Starlette(
        routes=[
            # Route, not Mount — see the module docstring.
            Route(
                http_settings.path,
                endpoint=_ManagerEndpoint(manager),
                methods=["GET", "POST", "DELETE"],
            ),
            Route("/healthz", endpoint=_healthz, methods=["GET"]),
        ],
        lifespan=app_lifespan,
    )


def main() -> None:
    """Entry point for the `mcbp-http` console script."""
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        onec: Settings = load_settings()
        http_settings = load_http_settings()
    except SettingsError as e:
        log.error(str(e))
        sys.exit(1)

    import uvicorn

    if not http_settings.allowed_hosts:
        log.warning(
            "MCP_HTTP_ALLOWED_HOSTS is unset — DNS-rebinding protection is off. Set it to the "
            "public hostname before exposing this endpoint."
        )
    log.info(
        "starting mcbp-ai MCP over HTTP on %s:%s%s (BAS %s, write=%s)",
        http_settings.host, http_settings.port, http_settings.path,
        onec.base_url, onec.allow_write,
    )
    uvicorn.run(
        create_app(http_settings),
        host=http_settings.host,
        port=http_settings.port,
        log_level="info",
    )
