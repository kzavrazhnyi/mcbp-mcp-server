"""Per-request BAS credentials: header parsing, the client pool, and the two identity paths.

Offline throughout — `respx` intercepts the outgoing BAS calls, so what is asserted is exactly
what would go on the wire: which account each tool call reaches BAS as. The end-to-end direction
(a real uvicorn run, a real MCP client) was verified separately during the design probe; these
tests pin the parts that a later edit could silently break.
"""
from __future__ import annotations

import logging

import httpx
import pytest
import respx
from mcbp_core.client import ConnectionConfig, MCBPClient
from mcp import Client
from mcp.server import Server
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from mcbp_mcp_server.credentials import (
    ClientPool,
    Credentials,
    CredentialsError,
    client_for,
    current_credentials,
    parse_authorization,
    reset_request_credentials,
    set_request_credentials,
)
from mcbp_mcp_server.http import _ManagerEndpoint
from mcbp_mcp_server.registry import make_on_call_tool, make_on_list_tools
from mcbp_mcp_server.server import AppContext, Settings

BASE = "http://bas.test/base/hs/mcbp_ai"

_SETTINGS = Settings(
    base_url=BASE, user="env_service", password="env-secret", timeout=5.0, pool_max=4,
)


def _basic(user: str, password: str) -> str:
    import base64

    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --- header parsing ---

def test_parses_basic_header():
    creds = parse_authorization(_basic("vasyl", "s3cret"))
    assert (creds.user, creds.password) == ("vasyl", "s3cret")


def test_empty_password_is_legal():
    # Some BAS publications (basmbdemo) genuinely have no password — same rule as ONEC_PASSWORD.
    assert parse_authorization(_basic("vasyl", "")).password == ""


@pytest.mark.parametrize("value", [
    "Bearer abc",                       # wrong scheme
    "Basic",                            # no payload
    "Basic ",                           # blank payload
    "Basic !!!not-base64!!!",           # undecodable
    "Basic bm9jb2xvbg==",               # 'nocolon' — no user:password separator
    "Basic OnBhc3N3b3Jk",               # ':password' — empty user
])
def test_malformed_header_raises(value):
    with pytest.raises(CredentialsError):
        parse_authorization(value)


def test_error_never_echoes_the_header():
    try:
        parse_authorization(_basic("vasyl", "s3cret").replace("Basic", "Bearer"))
    except CredentialsError as e:
        assert "s3cret" not in str(e)
        assert "vasyl" not in str(e)


def test_cache_key_hides_the_password_and_separates_users():
    a = Credentials("vasyl", "s3cret")
    b = Credentials("vasyl", "other")
    c = Credentials("olena", "s3cret")
    assert a.cache_key not in (b.cache_key, c.cache_key)
    assert "s3cret" not in a.cache_key
    assert "s3cret" not in repr(a)


# --- the pool ---

async def test_pool_reuses_one_client_per_credential_set():
    pool = ClientPool(_SETTINGS)
    creds = Credentials("vasyl", "pw")
    async with pool.lease(creds) as first:
        pass
    async with pool.lease(creds) as second:
        pass
    async with pool.lease(Credentials("olena", "pw")) as other:
        pass
    assert first is second
    assert other is not first
    assert pool.size == 2
    await pool.aclose()
    assert pool.size == 0


async def test_pool_evicts_least_recently_used_over_the_cap():
    pool = ClientPool(_SETTINGS, max_clients=2)
    for user in ("a", "b", "c"):
        async with pool.lease(Credentials(user, "pw")):
            pass
    assert pool.size == 2


async def test_pool_never_evicts_a_client_with_a_call_in_flight():
    pool = ClientPool(_SETTINGS, max_clients=1, idle_ttl_s=-1.0)
    held = Credentials("busy", "pw")
    async with pool.lease(held) as busy_client:
        async with pool.lease(Credentials("other", "pw")):
            pass
        # The busy entry survived an eviction pass it would otherwise have failed.
        async with pool.lease(held) as again:
            assert again is busy_client
    await pool.aclose()


async def test_pool_logs_the_user_but_never_the_password(caplog):
    pool = ClientPool(_SETTINGS)
    with caplog.at_level(logging.INFO, logger="mcbp_mcp_server"):
        async with pool.lease(Credentials("vasyl", "s3cret")):
            pass
        await pool.aclose()
    text = caplog.text
    assert "vasyl" in text
    assert "s3cret" not in text


# --- which identity a tool call runs as ---

def _server(pool: ClientPool | None, env_client: MCBPClient) -> Server[AppContext]:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(server: Server[AppContext]) -> AsyncIterator[AppContext]:
        yield AppContext(client=env_client, allow_write=False, pool=pool)

    return Server(
        "mcbp-ai-test",
        lifespan=_lifespan,
        on_list_tools=make_on_list_tools(False),
        on_call_tool=make_on_call_tool(False),
    )


async def _env_client() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(
        base_url=BASE, user="env_service", password="env-secret", mock=False,
    ))
    await client.startup()
    return client


def _seen_users(route: respx.Route) -> list[str]:
    import base64

    users = []
    for call in route.calls:
        raw = call.request.headers["authorization"]
        users.append(base64.b64decode(raw.split(" ", 1)[1]).decode().split(":")[0])
    return users


@respx.mock
async def test_header_credentials_reach_the_bas_request():
    route = respx.get(f"{BASE}/ai/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "service": "MCBP_AI", "key": True}),
    )
    env_client = await _env_client()
    pool = ClientPool(_SETTINGS)
    token = set_request_credentials(Credentials("vasyl", "s3cret"))
    try:
        async with Client(_server(pool, env_client)) as client:
            await client.call_tool("health", {})
    finally:
        reset_request_credentials(token)
        await pool.aclose()
        await env_client.shutdown()
    assert _seen_users(route) == ["vasyl"]


@respx.mock
async def test_absent_header_falls_back_to_the_env_identity():
    route = respx.get(f"{BASE}/ai/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "service": "MCBP_AI", "key": True}),
    )
    env_client = await _env_client()
    pool = ClientPool(_SETTINGS)
    assert current_credentials() is None
    async with Client(_server(pool, env_client)) as client:
        await client.call_tool("health", {})
    await pool.aclose()
    await env_client.shutdown()
    assert _seen_users(route) == ["env_service"]
    assert pool.size == 0  # the fallback path must not populate the per-caller pool


@respx.mock
async def test_two_callers_get_two_clients_and_share_no_metadata_cache():
    # Same route, a DIFFERENT answer per caller: BAS shows each account only what it may see.
    def _by_user(request: httpx.Request) -> httpx.Response:
        import base64

        raw = request.headers["authorization"]
        user = base64.b64decode(raw.split(" ", 1)[1]).decode().split(":")[0]
        return httpx.Response(200, json={"Catalogs": [f"OnlyVisibleTo_{user}"]})

    route = respx.get(f"{BASE}/ai/v1/metadata/Catalogs").mock(side_effect=_by_user)
    env_client = await _env_client()
    pool = ClientPool(_SETTINGS)
    server = _server(pool, env_client)
    answers = {}
    for user in ("vasyl", "olena", "vasyl"):
        token = set_request_credentials(Credentials(user, "pw"))
        try:
            async with Client(server) as client:
                result = await client.call_tool("list_metadata", {"metadata": "Catalogs"})
        finally:
            reset_request_credentials(token)
        answers.setdefault(user, []).append(result.content[0].text)
    await pool.aclose()
    await env_client.shutdown()

    assert "OnlyVisibleTo_vasyl" in answers["vasyl"][0]
    assert "OnlyVisibleTo_olena" in answers["olena"][0]
    # vasyl's repeat is served from vasyl's OWN cache: no second BAS call, and never olena's data.
    assert answers["vasyl"][1] == answers["vasyl"][0]
    assert _seen_users(route) == ["vasyl", "olena"]


async def test_client_for_yields_the_env_client_when_there_is_no_pool():
    # This is the stdio shape: no headers exist, so nothing can ever populate a pool.
    env_client = MCBPClient(ConnectionConfig(mock=True))
    await env_client.startup()
    ctx = AppContext(client=env_client, allow_write=False, pool=None)
    async with client_for(ctx) as client:
        assert client is env_client
    await env_client.shutdown()


# --- the ASGI layer ---

class _RecordingManager:
    """Stands in for `StreamableHTTPSessionManager`: records what the tool executor would see."""

    def __init__(self) -> None:
        self.seen: list[Credentials | None] = []

    async def handle_request(self, scope, receive, send) -> None:
        self.seen.append(current_credentials())
        from starlette.responses import PlainTextResponse

        await PlainTextResponse("ok")(scope, receive, send)


def _endpoint_app() -> tuple[Starlette, _RecordingManager]:
    manager = _RecordingManager()
    app = Starlette(routes=[Route("/mcp", endpoint=_ManagerEndpoint(manager), methods=["POST"])])
    return app, manager


def test_endpoint_hands_the_header_credentials_to_the_executor():
    app, manager = _endpoint_app()
    with TestClient(app) as client:
        response = client.post("/mcp", headers={"authorization": _basic("vasyl", "s3cret")})
    assert response.status_code == 200
    assert manager.seen == [Credentials("vasyl", "s3cret")]


def test_endpoint_leaves_the_credentials_unset_without_a_header():
    app, manager = _endpoint_app()
    with TestClient(app) as client:
        client.post("/mcp")
    assert manager.seen == [None]


def test_endpoint_rejects_a_broken_header_instead_of_falling_back():
    # The whole point: a bad header must NOT quietly act as the service account.
    app, manager = _endpoint_app()
    with TestClient(app) as client:
        response = client.post("/mcp", headers={"authorization": "Bearer nope"})
    assert response.status_code == 400  # not 401: that would send MCP clients into OAuth discovery
    assert response.json()["error"]["code"] == "BAD_AUTHORIZATION"
    assert manager.seen == []


def test_endpoint_never_leaks_the_password_into_the_rejection(caplog):
    app, _ = _endpoint_app()
    with caplog.at_level(logging.WARNING, logger="mcbp_mcp_server"), TestClient(app) as client:
        response = client.post(
            "/mcp", headers={"authorization": _basic("vasyl", "s3cret").replace("Basic", "Digest")},
        )
    assert "s3cret" not in response.text
    assert "s3cret" not in caplog.text


def test_credentials_do_not_leak_between_requests():
    app, manager = _endpoint_app()
    with TestClient(app) as client:
        client.post("/mcp", headers={"authorization": _basic("vasyl", "pw")})
        client.post("/mcp")
    assert manager.seen == [Credentials("vasyl", "pw"), None]


# --- the whole HTTP stack, end to end ---

async def test_header_survives_the_real_transport_into_the_tool_executor(monkeypatch):
    """The one thing the in-memory tests cannot cover: that the credentials captured in our ASGI
    layer are still in scope when the SDK finally runs the tool handler, several tasks deep. No
    socket and no BAS — `_request` is intercepted, so what is observed is the identity the client
    WOULD have used on the wire."""
    import json

    from mcbp_mcp_server.http import HttpSettings, create_app

    for name, value in {"ONEC_BASE_URL": BASE, "ONEC_USER": "env_service",
                        "ONEC_PASSWORD": "env-secret", "MCP_ALLOW_WRITE": ""}.items():
        monkeypatch.setenv(name, value)

    seen: list[str] = []

    async def _fake_request(self, method, path, **kw):
        seen.append(self._cfg.user)
        return {"status": "ok", "service": "MCBP_AI", "key": True}

    monkeypatch.setattr(MCBPClient, "_request", _fake_request)

    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "health", "arguments": {}}}
    accept = {"accept": "application/json, text/event-stream",
              "content-type": "application/json"}

    def exchange(auth: str | None) -> str:
        # A fresh app per exchange: the SDK's session manager refuses a second `run()`.
        headers = dict(accept) | ({"authorization": auth} if auth else {})
        with TestClient(create_app(HttpSettings(path="/mcp"))) as http:
            r = http.post("/mcp", json=body, headers=headers)
            assert r.status_code == 200, r.text
            headers["mcp-session-id"] = r.headers["mcp-session-id"]
            http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                      headers=headers)
            r = http.post("/mcp", json=call, headers=headers)
            assert r.status_code == 200, r.text
            return r.text

    seen.clear()
    exchange(_basic("vasyl", "s3cret"))
    assert seen[-1] == "vasyl"

    seen.clear()
    exchange(None)
    assert seen[-1] == "env_service"

    # And the rejection happens before any BAS contact at all — the only entry `seen` may hold
    # is the startup health probe, which runs as the env identity on app startup.
    with TestClient(create_app(HttpSettings(path="/mcp"))) as http:
        seen.clear()
        r = http.post("/mcp", json=body, headers=dict(accept) | {"authorization": "Bearer x"})
    assert r.status_code == 400
    assert json.loads(r.text)["error"]["code"] == "BAD_AUTHORIZATION"
    assert seen == []


async def test_one_session_many_calls_never_pins_the_first_callers_identity(monkeypatch):
    """The failure this rules out: a session initialized by A silently executing B's later calls
    as A. Credentials are read per REQUEST, so ONE session driven through many exchanges with
    alternating headers must reach BAS as the sender of each individual call — including the
    late ones, past any plausible internal caching threshold. If this ever fails, one user's
    session has begun acting under another user's account: that is a design problem, not a bug
    to patch around."""
    from mcbp_mcp_server.http import HttpSettings, create_app

    for name, value in {"ONEC_BASE_URL": BASE, "ONEC_USER": "env_service",
                        "ONEC_PASSWORD": "env-secret", "MCP_ALLOW_WRITE": ""}.items():
        monkeypatch.setenv(name, value)

    seen: list[str] = []

    async def _fake_request(self, method, path, **kw):
        seen.append(self._cfg.user)
        return {"status": "ok", "service": "MCBP_AI", "key": True}

    monkeypatch.setattr(MCBPClient, "_request", _fake_request)

    accept = {"accept": "application/json, text/event-stream",
              "content-type": "application/json"}
    users = ["vasyl", "olena"] * 8  # 16 exchanges on ONE session

    with TestClient(create_app(HttpSettings(path="/mcp"))) as http:
        # The session is created by the FIRST user; every later call carries its own header.
        init = dict(accept) | {"authorization": _basic(users[0], "pw")}
        r = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}}, headers=init)
        assert r.status_code == 200, r.text
        session_id = r.headers["mcp-session-id"]
        http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                  headers=init | {"mcp-session-id": session_id})

        seen.clear()  # drop the startup health probe
        for i, user in enumerate(users):
            headers = dict(accept) | {"authorization": _basic(user, "pw"),
                                      "mcp-session-id": session_id}
            r = http.post("/mcp", json={
                "jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                "params": {"name": "health", "arguments": {}}}, headers=headers)
            assert r.status_code == 200, r.text

    assert seen == users
