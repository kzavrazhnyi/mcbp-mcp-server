"""Settings, lifespan, and the `Server` factory for the `mcbp-ai` MCP server.

This module is a thin stdio adapter over `mcbp_core` — it holds no knowledge of BAS routes,
fields, or response shapes. Tool registration itself lives in `registry.py`; this module owns
the process lifecycle: read env -> build a live `MCBPClient` -> serve stdio -> close the client.

Built on the LOW-LEVEL `mcp.server.Server`, not `MCPServer` — `MCPServer.add_tool` derives its
JSON Schema from a Python function's type hints and has no way to take a ready-made schema, while
our tool schemas already live in `mcbp_core.tools.ToolSpec.parameters`. See
`.claude/skills/mcbp-mcp-server/SKILL.md` for the full rationale. The low-level `Server.run()`
wants ready anyio streams instead of raising stdio itself, so `main()` is async under the hood.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from mcbp_core.client import ConnectionConfig, MCBPClient
from mcp.server import Server
from mcp.server.stdio import stdio_server

from mcbp_mcp_server import __version__
from mcbp_mcp_server.credentials import ClientPool
from mcbp_mcp_server.registry import make_on_call_tool, make_on_list_tools

log = logging.getLogger("mcbp_mcp_server")


class SettingsError(Exception):
    """Raised by `load_settings()` for a missing or malformed required env var."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    user: str
    password: str
    timeout: float = 30.0
    pool_max: int = 10
    allow_write: bool = False
    verify_ssl: bool = True


# Strips a trailing '/ai/v1' (any case, optional trailing slash) off ONEC_BASE_URL. Anchored to
# the END of the string, so a URL that merely CONTAINS 'ai/v1' mid-path is left untouched.
_AI_V1_SUFFIX_RE = re.compile(r"/ai/v1/?$", re.IGNORECASE)


def _normalize_base_url(url: str) -> str:
    """`MCBPClient` appends `/ai/v1/...` itself inside every method, so `ConnectionConfig.base_url`
    must stop at the service name. Existing docs and `.mcp.json` show the suffixed form (written
    for a from-scratch client that never existed) — accept both: strip the suffix if present.
    A config that still carries it hits /ai/v1/ai/v1/... and every route (except the
    coincidentally-shaped /health) 404s, with no error pointing at the cause."""
    return _AI_V1_SUFFIX_RE.sub("", url.rstrip("/"))


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise SettingsError(f"{name} is required — set it in the MCP client's env block")
    return value


def _require_present(env: Mapping[str, str], name: str) -> str:
    """Like `_require`, but accepts an explicit empty string — some BAS publications
    (e.g. basmbdemo) genuinely have no password, and `.mcp.json`'s `${VAR:-}` substitution
    collapses "unset" and "set to empty" into the same wire value anyway, so an empty
    `ONEC_PASSWORD` cannot be told apart from a deliberate one. Only a fully absent key fails."""
    if name not in env:
        raise SettingsError(f"{name} is required — set it in the MCP client's env block")
    return env[name]


def _optional_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from e


def _optional_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from e


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes"}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Reads `ONEC_*` / `MCP_ALLOW_WRITE` from `env` (defaults to `os.environ`). Pure: raises
    `SettingsError` on a missing/malformed required var, never prints or exits — `main()` is the
    only place that turns that into a stderr message and a non-zero exit. Safe to call more than
    once (`create_server()` and the lifespan each call it independently)."""
    e = os.environ if env is None else env
    return Settings(
        base_url=_normalize_base_url(_require(e, "ONEC_BASE_URL")),
        user=_require(e, "ONEC_USER"),
        password=_require_present(e, "ONEC_PASSWORD"),
        timeout=_optional_float(e, "ONEC_TIMEOUT", 30.0),
        pool_max=_optional_int(e, "ONEC_POOL_MAX", 10),
        allow_write=_truthy(e.get("MCP_ALLOW_WRITE")),
        verify_ssl=_truthy(e.get("ONEC_VERIFY_SSL", "1")),
    )


@dataclass
class AppContext:
    """`client` is the process identity from the `ONEC_*` env block — the only identity on stdio,
    and the fallback for an HTTP request that presents no `Authorization` header. `pool` holds the
    per-caller clients built from such a header; it stays empty on stdio, which has no headers at
    all, so nothing there can ever populate it."""

    client: MCBPClient
    allow_write: bool
    pool: ClientPool | None = None


async def _probe_health(client: MCBPClient) -> None:
    """Advisory only — never raises, never blocks startup. A server that refuses to start is
    worse than one that warns."""
    try:
        result = await client.health()
    except Exception as e:  # noqa: BLE001 - advisory probe must never block startup
        log.warning(
            "MCBP_AI health probe failed (%s) — every route may be unreachable until this "
            "clears", e,
        )
        return
    if result.get("key") is False:
        log.warning(
            "MCBP_AI health probe returned key:false — the infobase key does not match the "
            "publication; every route except /health will answer 403 KEY_MISMATCH until fixed."
        )


@asynccontextmanager
async def lifespan(server: Server[AppContext]) -> AsyncIterator[AppContext]:
    settings = load_settings()
    client = MCBPClient(ConnectionConfig(
        base_url=settings.base_url,
        user=settings.user,
        password=settings.password,
        timeout_s=settings.timeout,
        pool_max=settings.pool_max,
        mock=False,
        verify=settings.verify_ssl,
    ))
    await client.startup()
    pool = ClientPool(settings)
    try:
        await _probe_health(client)
        yield AppContext(client=client, allow_write=settings.allow_write, pool=pool)
    finally:
        await pool.aclose()
        await client.shutdown()


def create_server() -> Server[AppContext]:
    """Constructs the low-level `Server` without touching the network — the client is built
    lazily by `lifespan` only once a client actually connects. Reads only `MCP_ALLOW_WRITE` (via
    the same `_truthy()` helper `load_settings()` uses — one decision, two read sites, never a
    second parsing rule) so the tool SET is decidable without live `ONEC_*` credentials; the
    per-request `AppContext.allow_write` set up by `lifespan` carries the identical value."""
    allow_write = _truthy(os.environ.get("MCP_ALLOW_WRITE"))
    return Server(
        "mcbp-ai",
        version=__version__,
        lifespan=lifespan,
        on_list_tools=make_on_list_tools(allow_write),
        on_call_tool=make_on_call_tool(allow_write),
    )


async def _serve() -> None:
    server = create_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        load_settings()
    except SettingsError as e:
        log.error(str(e))
        sys.exit(1)
    anyio.run(_serve)
