"""Phase 3 skeleton: Settings/load_settings, ONEC_BASE_URL normalization, lifespan pieces,
create_server() not touching the network. Tool registration is Phase 4 — nothing here calls
`create_server().run()` or opens a real connection."""
from __future__ import annotations

import logging

import pytest

from mcp.server import Server

from mcbp_mcp_server.server import (
    AppContext,
    Settings,
    SettingsError,
    _normalize_base_url,
    _probe_health,
    create_server,
    load_settings,
)

_REQUIRED_ENV = {
    "ONEC_BASE_URL": "http://localhost/base/hs/mcbp_ai",
    "ONEC_USER": "ai_service",
    "ONEC_PASSWORD": "secret",
}


# --- ONEC_BASE_URL normalization ---

@pytest.mark.parametrize(
    "raw",
    [
        "http://localhost/basmbdemo/hs/mcbp_ai",
        "http://localhost/basmbdemo/hs/mcbp_ai/ai/v1",
        "http://localhost/basmbdemo/hs/mcbp_ai/ai/v1/",
        "http://localhost/basmbdemo/hs/mcbp_ai/AI/V1",
        "http://localhost/basmbdemo/hs/mcbp_ai/Ai/V1/",
    ],
)
def test_normalize_base_url_all_forms_converge(raw):
    assert _normalize_base_url(raw) == "http://localhost/basmbdemo/hs/mcbp_ai"


def test_normalize_base_url_does_not_mangle_mid_path_occurrence():
    # "ai/v1" appears, but not as the trailing service-route suffix — must be left alone.
    url = "http://localhost/ai/v1max/hs/mcbp_ai"
    assert _normalize_base_url(url) == url


def test_load_settings_normalizes_base_url():
    env = dict(_REQUIRED_ENV, ONEC_BASE_URL="http://host/base/hs/mcbp_ai/ai/v1/")
    settings = load_settings(env)
    assert settings.base_url == "http://host/base/hs/mcbp_ai"


# --- fail-fast on missing required vars ---

@pytest.mark.parametrize("missing", ["ONEC_BASE_URL", "ONEC_USER", "ONEC_PASSWORD"])
def test_load_settings_fails_fast_on_missing_required_var(missing):
    env = dict(_REQUIRED_ENV)
    del env[missing]
    with pytest.raises(SettingsError) as exc_info:
        load_settings(env)
    assert missing in str(exc_info.value)


def test_load_settings_accepts_an_explicit_empty_password():
    # Some BAS publications (e.g. basmbdemo) genuinely have no password, and .mcp.json's
    # ${VAR:-} substitution collapses "unset" and "set to empty" into the same wire value
    # anyway — an empty ONEC_PASSWORD must be accepted, not treated as missing.
    env = dict(_REQUIRED_ENV, ONEC_PASSWORD="")
    assert load_settings(env).password == ""


def test_load_settings_fails_on_malformed_timeout():
    env = dict(_REQUIRED_ENV, ONEC_TIMEOUT="not-a-number")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(env)
    assert "ONEC_TIMEOUT" in str(exc_info.value)


def test_load_settings_fails_on_malformed_pool_max():
    env = dict(_REQUIRED_ENV, ONEC_POOL_MAX="lots")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(env)
    assert "ONEC_POOL_MAX" in str(exc_info.value)


# --- defaults ---

def test_load_settings_defaults_when_optional_vars_absent():
    settings = load_settings(_REQUIRED_ENV)
    assert settings == Settings(
        base_url="http://localhost/base/hs/mcbp_ai",
        user="ai_service",
        password="secret",
        timeout=30.0,
        pool_max=10,
        allow_write=False,
        verify_ssl=True,
    )


# --- MCP_ALLOW_WRITE truthiness table ---

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("YES", True),
        ("0", False),
        (None, False),
        ("garbage", False),
        ("", False),
    ],
)
def test_mcp_allow_write_truthiness_table(raw, expected):
    env = dict(_REQUIRED_ENV)
    if raw is not None:
        env["MCP_ALLOW_WRITE"] = raw
    assert load_settings(env).allow_write is expected


# --- ONEC_VERIFY_SSL truthiness (default on) ---

@pytest.mark.parametrize(
    "raw,expected",
    [(None, True), ("1", True), ("0", False), ("false", False)],
)
def test_onec_verify_ssl_truthiness(raw, expected):
    env = dict(_REQUIRED_ENV)
    if raw is not None:
        env["ONEC_VERIFY_SSL"] = raw
    assert load_settings(env).verify_ssl is expected


# --- create_server() does not touch the network ---

def test_create_server_does_not_touch_network(monkeypatch):
    # No ONEC_* set at all in the environment this test runs under — if create_server() (or
    # anything it calls at construction time) tried to build a client / read settings eagerly,
    # this would raise SettingsError right here instead of only failing once something connects.
    monkeypatch.delenv("ONEC_BASE_URL", raising=False)
    monkeypatch.delenv("ONEC_USER", raising=False)
    monkeypatch.delenv("ONEC_PASSWORD", raising=False)
    server = create_server()
    assert server is not None


def test_create_server_returns_low_level_server():
    # MCPServer.add_tool has no input_schema parameter (see the skill's SDK-baseline note) —
    # Phase 4 needs the low-level Server's on_list_tools/on_call_tool, so the skeleton must
    # already be built on it.
    assert isinstance(create_server(), Server)


# --- advisory health probe ---

class _FakeMCBPClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def health(self):
        if self._exc is not None:
            raise self._exc
        return self._response


async def test_probe_health_warns_on_key_false(caplog):
    client = _FakeMCBPClient(response={"status": "ok", "service": "MCBP_AI", "key": False})
    with caplog.at_level(logging.WARNING, logger="mcbp_mcp_server"):
        await _probe_health(client)
    assert any("key:false" in r.message for r in caplog.records)


async def test_probe_health_silent_on_key_true(caplog):
    client = _FakeMCBPClient(response={"status": "ok", "service": "MCBP_AI", "key": True})
    with caplog.at_level(logging.WARNING, logger="mcbp_mcp_server"):
        await _probe_health(client)
    assert caplog.records == []


async def test_probe_health_never_raises_on_transport_failure(caplog):
    client = _FakeMCBPClient(exc=RuntimeError("connection refused"))
    with caplog.at_level(logging.WARNING, logger="mcbp_mcp_server"):
        await _probe_health(client)  # must not raise
    assert any("probe failed" in r.message for r in caplog.records)


def test_app_context_holds_client_and_allow_write():
    client = _FakeMCBPClient()
    ctx = AppContext(client=client, allow_write=True)
    assert ctx.client is client
    assert ctx.allow_write is True
