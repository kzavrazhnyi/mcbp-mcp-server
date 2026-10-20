"""POST /ai/v1/client/auth/start — `MCBPClient.start_client_auth` and its mock."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    LicenseRequiredError,
    MCBPError,
    ParameterError,
    WriteFailedError,
)
from mcbp_core.tools import TOOLS

_URL = "http://test/ai/v1/client/auth/start"
_DATA_KEYS = {"login", "known", "status", "chat_id", "name", "contact", "counterparty",
              "contact_created", "allowed", "matches", "chat_id_conflict"}


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def _mock_client() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    return client


async def test_mock_lookup_writes_nothing():
    client = await _mock_client()
    data = (await client.start_client_auth("+38 (097) 455-36-63", confirmed=False))["data"]
    assert set(data) == _DATA_KEYS
    assert data["login"] == "380974553663"
    assert (data["known"], data["status"], data["contact"]) == (False, None, None)


async def test_mock_keeps_only_ascii_digits():
    client = await _mock_client()
    data = (await client.start_client_auth("38097٤²455٣663", confirmed=False))["data"]
    assert data["login"] == "38097455663"


async def test_mock_bot_confirmation_with_chat_id_is_active():
    client = await _mock_client()
    data = (await client.start_client_auth("380974553663", confirmed=True, chat_id=123456789,
                                           name="Іван"))["data"]
    assert set(data) == _DATA_KEYS
    assert (data["status"], data["chat_id"], data["name"]) == ("Active", "123456789", "Іван")
    assert set(data["contact"]) == {"id", "type", "name"}
    assert data["allowed"] is False
    assert data["chat_id_conflict"] is False


async def test_mock_confirmation_without_chat_id_is_pending():
    client = await _mock_client()
    data = (await client.start_client_auth("A@B.ua", confirmed=True))["data"]
    assert (data["login"], data["status"], data["chat_id"]) == ("a@b.ua", "Pending", None)


@pytest.mark.parametrize("login", ["", "   "])
async def test_blank_login_is_parameter_error_before_http(login):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_URL)
        await client.startup()
        try:
            with pytest.raises(ParameterError, match="login"):
                await client.start_client_auth(login, confirmed=True)
        finally:
            await client.shutdown()
        assert not route.called


@pytest.mark.parametrize("confirmed", ["true", 1, None])
async def test_non_bool_confirmed_is_parameter_error_before_http(confirmed):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_URL)
        await client.startup()
        try:
            with pytest.raises(ParameterError, match="confirmed"):
                await client.start_client_auth("380974553663", confirmed=confirmed)
        finally:
            await client.shutdown()
        assert not route.called


@respx.mock
async def test_sends_json_body_and_omits_unset_fields():
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.start_client_auth(" 380974553663 ", confirmed=True, chat_id=42)
        await client.start_client_auth("a@b.ua", confirmed=False)
    finally:
        await client.shutdown()
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies == [{"login": "380974553663", "confirmed": True, "chat_id": 42},
                      {"login": "a@b.ua", "confirmed": False}]
    assert route.calls[0].request.url.query == b""


@respx.mock
async def test_post_is_not_retried_on_503():
    route = respx.post(_URL).mock(return_value=httpx.Response(503, json={"error": {
        "code": "UNAVAILABLE", "message": "busy"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(MCBPError):
            await client.start_client_auth("380974553663", confirmed=True, chat_id=42)
    finally:
        await client.shutdown()
    assert route.call_count == 1


@pytest.mark.parametrize(("status", "code", "exc"), [
    (403, "LICENSE_REQUIRED", LicenseRequiredError),
    (422, "BAD_PARAMETER", ParameterError),
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
            await client.start_client_auth("380974553663", confirmed=True)
    finally:
        await client.shutdown()


@respx.mock
async def test_log_hides_login_and_chat_id(caplog):
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        with caplog.at_level(logging.INFO):
            await client.start_client_auth("380974553663", confirmed=True, chat_id=987654321)
    finally:
        await client.shutdown()
    assert "client/auth/start" in caplog.text
    assert "380974553663" not in caplog.text
    assert "987654321" not in caplog.text


def test_not_a_model_tool():
    assert not any("auth" in name for name in TOOLS)
