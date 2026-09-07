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
import tomllib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from mcbp_mcp_server.credentials import (
    Credentials,
    CredentialsError,
    TokenEntry,
    TokenRegistry,
    parse_authorization,
    reset_request_credentials,
    set_request_credentials,
)
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
    require_auth: bool = False


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
        require_auth=_truthy(e.get("MCP_REQUIRE_AUTH")),
    )


def load_token_registry(env: Mapping[str, str] | None = None) -> TokenRegistry:
    """Reads the `MCP_TOKENS_FILE` TOML registry. Pure, like `load_http_settings()`.

    An unset variable yields an EMPTY registry, not an error — Bearer is then simply not
    accepted, which is the stdio/Basic-only deployment. A variable that IS set and points at a
    missing or malformed file is a startup error: silently serving with Bearer disabled would
    look exactly like a working server until someone's token is rejected.

    Expected shape (one table per person)::

        [[users]]
        token = "<random string>"
        onec_user = "Директор-Корнієнко"
        onec_password = ""
        label = "Директор"

    Error messages name the file and the entry number, never a token, a password, or any other
    file content.
    """
    e = os.environ if env is None else env
    raw_path = (e.get("MCP_TOKENS_FILE") or "").strip()
    if not raw_path:
        return TokenRegistry()
    path = Path(raw_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SettingsError(f"MCP_TOKENS_FILE points at a file that does not exist: {path}") from exc
    except OSError as exc:
        raise SettingsError(f"MCP_TOKENS_FILE {path} cannot be read: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise SettingsError(f"MCP_TOKENS_FILE {path} is not valid UTF-8") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"MCP_TOKENS_FILE {path} is not valid TOML: {exc}") from exc
    return _registry_from(data, path)


def _registry_from(data: dict[str, object], path: Path) -> TokenRegistry:
    users = data.get("users", [])
    if not isinstance(users, list):
        raise SettingsError(f"MCP_TOKENS_FILE {path} must define an array of tables '[[users]]'")
    entries: list[TokenEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(users, start=1):
        where = f"MCP_TOKENS_FILE {path}, [[users]] entry #{index}"
        if not isinstance(item, dict):
            raise SettingsError(f"{where} must be a table")
        token = _entry_str(item, "token", where, required=True)
        user = _entry_str(item, "onec_user", where, required=True)
        # Empty is legal and common (basmbdemo has no password) — only a missing key fails.
        password = _entry_str(item, "onec_password", where, required=False, allow_empty=True)
        if "onec_password" not in item:
            raise SettingsError(f"{where} is missing 'onec_password' (use \"\" for none)")
        label = _entry_str(item, "label", where, required=False, allow_empty=True)
        if token in seen:
            raise SettingsError(f"{where} repeats a token already used by an earlier entry")
        seen.add(token)
        entries.append(TokenEntry(
            token=token,
            credentials=Credentials(user=user, password=password),
            label=label,
        ))
    return TokenRegistry(entries)


def _entry_str(item: dict[str, object], key: str, where: str, *, required: bool,
               allow_empty: bool = False) -> str:
    value = item.get(key)
    if value is None:
        if required:
            raise SettingsError(f"{where} is missing '{key}'")
        return ""
    if not isinstance(value, str):
        raise SettingsError(f"{where} has a non-string '{key}'")
    if not value and not allow_empty:
        raise SettingsError(f"{where} has an empty '{key}'")
    return value


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
    `manager.handle_request` is a bound method, which would be misread as the latter.

    It is also where the caller's BAS credentials are picked up. This has to happen HERE: the
    SDK hands `on_call_tool` a `ServerRequestContext`, which carries no transport metadata at
    all, and the manager attaches `TransportContext.headers` only on the modern protocol path.
    Our own ASGI layer sees the headers whatever the protocol version — see `credentials.py`
    for the propagation evidence."""

    def __init__(self, manager: StreamableHTTPSessionManager,
                 tokens: TokenRegistry | None = None, *, require_auth: bool = False) -> None:
        self._manager = manager
        self._tokens = tokens
        self._require_auth = require_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raw = Headers(scope=scope).get("authorization")
        try:
            if raw is None:
                # With MCP_REQUIRE_AUTH the env account stops being a fallback: on a port that is
                # reachable by anyone, an unauthenticated request would otherwise act as the
                # service identity.
                if self._require_auth:
                    raise CredentialsError(
                        "Authorization is required: send 'Bearer <token>' or "
                        "'Basic <base64 of user:password>'"
                    )
                creds = None
            else:
                creds = parse_authorization(raw, self._tokens)
        except CredentialsError as e:
            # 400, deliberately NOT 401: MCP clients read a 401 as "begin OAuth discovery" and
            # would chase an authorization server that does not exist instead of showing the
            # reason. Falling through to the env account is not an option either — that is the
            # silent-service-account failure this whole feature exists to remove.
            log.warning("rejected request with an unusable Authorization header: %s", e)
            await JSONResponse({"error": {"code": "BAD_AUTHORIZATION", "message": str(e)}},
                               status_code=400)(scope, receive, send)
            return
        token = set_request_credentials(creds)
        try:
            await self._manager.handle_request(scope, receive, send)
        finally:
            reset_request_credentials(token)


async def _healthz(request: Request) -> PlainTextResponse:
    """Liveness only — says the process is up, NOT that BAS is reachable. The MCP endpoint itself
    cannot serve as a probe: it answers 400 without a negotiated session."""
    return PlainTextResponse("ok")


def create_app(settings: HttpSettings | None = None,
               tokens: TokenRegistry | None = None) -> Starlette:
    """Builds the ASGI app without touching the network. `create_server()` is called once; the
    `MCBPClient` behind it is still built lazily by the server's own `lifespan`."""
    http_settings = load_http_settings() if settings is None else settings
    registry = load_token_registry() if tokens is None else tokens
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
                endpoint=_ManagerEndpoint(
                    manager, registry, require_auth=http_settings.require_auth,
                ),
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
        tokens = load_token_registry()
    except SettingsError as e:
        log.error(str(e))
        sys.exit(1)

    import uvicorn

    if not http_settings.allowed_hosts:
        log.warning(
            "MCP_HTTP_ALLOWED_HOSTS is unset — DNS-rebinding protection is off. Set it to the "
            "public hostname before exposing this endpoint."
        )
    if http_settings.require_auth and not len(tokens):
        log.warning(
            "MCP_REQUIRE_AUTH is on with an empty token registry — only Basic callers can "
            "connect. Set MCP_TOKENS_FILE to accept Bearer tokens."
        )
    log.info(
        "starting mcbp-ai MCP over HTTP on %s:%s%s (BAS %s, write=%s, require_auth=%s, "
        "bearer users: %s)",
        http_settings.host, http_settings.port, http_settings.path,
        onec.base_url, onec.allow_write, http_settings.require_auth,
        ", ".join(tokens.labels) or "none",
    )
    uvicorn.run(
        create_app(http_settings, tokens),
        host=http_settings.host,
        port=http_settings.port,
        log_level="info",
    )
