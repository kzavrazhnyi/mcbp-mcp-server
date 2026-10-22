"""POST /ai/v1/client/auth/revoke — `MCBPClient.revoke_client_auth` and its mock."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ForbiddenError,
    LicenseRequiredError,
    MCBPError,
    NotConfiguredError,
    NotFoundError,
    ParameterError,
    WriteFailedError,
)

_URL = "http://test/ai/v1/client/auth/revoke"


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def _mock_client() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    return client


async def test_mock_start_returns_session_epoch_zero_for_new_login():
    client = await _mock_client()
    data = (await client.start_client_auth("380501110001", confirmed=False))["data"]
    assert data["session_epoch"] == 0


async def test_mock_revoke_increments_epoch_and_start_reports_it():
    client = await _mock_client()
    await client.start_client_auth("+38 (050) 111-00-02", confirmed=True, chat_id=7)
    first = (await client.revoke_client_auth("380501110002"))["data"]
    second = (await client.revoke_client_auth(" 380501110002 "))["data"]
    assert first == {"login": "380501110002", "session_epoch": 1}
    assert second["session_epoch"] == 2
    started = (await client.start_client_auth("380501110002", confirmed=False))["data"]
    assert started["session_epoch"] == 2


async def test_mock_revoke_unknown_login_is_not_found():
    client = await _mock_client()
    with pytest.raises(NotFoundError):
        await client.revoke_client_auth("never-started@example.ua")


@pytest.mark.parametrize("login", ["", "   ", None])
async def test_blank_login_is_parameter_error_before_http(login):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_URL)
        await client.startup()
        try:
            with pytest.raises(ParameterError, match="login"):
                await client.revoke_client_auth(login)
        finally:
            await client.shutdown()
        assert not route.called


@respx.mock
async def test_sends_stripped_login_and_returns_body():
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={
        "data": {"login": "380974553663", "session_epoch": 5}}))
    client = _live_client()
    await client.startup()
    try:
        result = await client.revoke_client_auth(" 380974553663 ")
    finally:
        await client.shutdown()
    assert json.loads(route.calls[0].request.content) == {"login": "380974553663"}
    assert result == {"data": {"login": "380974553663", "session_epoch": 5}}


@respx.mock
async def test_post_is_not_retried_on_503():
    route = respx.post(_URL).mock(return_value=httpx.Response(503, json={"error": {
        "code": "UNAVAILABLE", "message": "busy"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(MCBPError):
            await client.revoke_client_auth("380974553663")
    finally:
        await client.shutdown()
    assert route.call_count == 1


@pytest.mark.parametrize(("status", "code", "exc"), [
    (404, "NOT_FOUND", NotFoundError),
    (422, "NOT_CONFIGURED", NotConfiguredError),
    (422, "BAD_PARAMETER", ParameterError),
    (403, "FORBIDDEN", ForbiddenError),
    (403, "LICENSE_REQUIRED", LicenseRequiredError),
    (400, "BAD_JSON", ParameterError),
    (500, "WRITE_FAILED", WriteFailedError),
])
@respx.mock
async def test_upstream_error_codes_are_typed(status, code, exc):
    respx.post(_URL).mock(return_value=httpx.Response(status, json={"error": {
        "code": code, "message": "rejected"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(exc):
            await client.revoke_client_auth("380974553663")
    finally:
        await client.shutdown()
