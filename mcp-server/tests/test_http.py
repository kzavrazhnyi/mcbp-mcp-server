"""Streamable HTTP transport: settings parsing, DNS-rebinding opt-in, and routing.

No live BAS and no MCP session negotiation here — `create_app()` builds the ASGI app without
touching the network, so these tests cover exactly the wiring that a deploy gets wrong.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcbp_mcp_server.http import (
    HttpSettings,
    _security,
    create_app,
    load_http_settings,
)
from mcbp_mcp_server.server import SettingsError

_REQUIRED_ENV = {
    "ONEC_BASE_URL": "http://localhost/base/hs/mcbp_ai",
    "ONEC_USER": "ai_service",
    "ONEC_PASSWORD": "secret",
}


@pytest.fixture
def served(monkeypatch):
    """Lets `TestClient` enter the real app lifespan. `MCBPClient.startup()` only builds an
    httpx client, so the one thing that would reach the network is the advisory health probe —
    stubbed out here, exactly as the offline-tests rule requires."""
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MCP_ALLOW_WRITE", "")

    async def _no_probe(client):
        return None

    monkeypatch.setattr("mcbp_mcp_server.server._probe_health", _no_probe)


# --- settings ---

def test_defaults_bind_to_loopback():
    # A default of 0.0.0.0 would publish an unauthenticated endpoint the moment the reverse
    # proxy is missing, so loopback is the safe default and worth pinning.
    settings = load_http_settings({})
    assert (settings.host, settings.port, settings.path) == ("127.0.0.1", 8010, "/mcp")


def test_path_trailing_slash_is_stripped():
    assert load_http_settings({"MCP_HTTP_PATH": "/mcp/"}).path == "/mcp"


def test_path_must_be_absolute():
    with pytest.raises(SettingsError):
        load_http_settings({"MCP_HTTP_PATH": "mcp"})


def test_allow_lists_parse_as_csv_and_drop_blanks():
    settings = load_http_settings({"MCP_HTTP_ALLOWED_HOSTS": "mcp.example.com, ,mcp.example.com:443"})
    assert settings.allowed_hosts == ["mcp.example.com", "mcp.example.com:443"]


def test_port_must_be_an_integer():
    with pytest.raises(SettingsError):
        load_http_settings({"MCP_HTTP_PORT": "8010a"})


# --- DNS-rebinding protection is opt-in ---

def test_security_is_none_without_an_allow_list():
    # Enabling protection with an empty allowed_hosts makes the SDK middleware answer 421 to
    # EVERY request — so no list means leave the middleware at its permissive default.
    assert _security(HttpSettings()) is None


def test_security_enabled_once_hosts_are_listed():
    security = _security(HttpSettings(allowed_hosts=["mcp.example.com"]))
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.example.com"]


# --- routing ---

def test_create_app_does_not_require_onec_credentials(monkeypatch):
    # The MCBPClient is built by the server's own lifespan, not here.
    for name in _REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert isinstance(create_app(HttpSettings()), Starlette)


def test_exact_path_is_routed_not_redirected(served):
    # Starlette compiles Mount("/mcp") to ^/mcp/(?P<path>.*)$, so the bare URL a user types into
    # a connector field would 307. Route must match it directly.
    with TestClient(create_app(HttpSettings()), raise_server_exceptions=False) as client:
        response = client.post("/mcp", json={}, headers={"content-type": "application/json"})
    assert response.status_code != 307
    assert response.status_code < 500


def test_healthz_is_up_without_bas(served):
    with TestClient(create_app(HttpSettings())) as client:
        assert client.get("/healthz").text == "ok"


def test_unknown_path_is_404(served):
    with TestClient(create_app(HttpSettings())) as client:
        assert client.get("/").status_code == 404
