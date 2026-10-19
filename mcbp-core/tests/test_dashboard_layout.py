"""Dashboard layout methods of MCBPClient: respx for the wire format, the in-memory mock for state."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient, reset_mock_dashboard
from mcbp_core.errors import ConflictError, LicenseRequiredError, ParameterError

BODY = {"data": {"tiles": [{"id": "a"}], "revision": 3, "updated": "16.10.2026 10:00:00"}}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


@respx.mock
async def test_get_and_put_wire_format():
    get = respx.get("http://test/ai/v1/dashboard/layout").mock(
        return_value=httpx.Response(200, json=BODY))
    put = respx.put("http://test/ai/v1/dashboard/layout").mock(
        return_value=httpx.Response(200, json=BODY))
    client = await _live()
    assert await client.get_dashboard_layout() == BODY
    assert get.calls.last.request.url.query == b""
    await client.get_dashboard_layout(client_key="k 1")
    assert dict(get.calls.last.request.url.params) == {"client": "k 1"}
    await client.put_dashboard_layout([{"id": "a"}], 2, client_key="k")
    assert json.loads(put.calls.last.request.content) == {"tiles": [{"id": "a"}], "revision": 2}
    assert put.calls.last.request.url.query == b"client=k"
    await client.shutdown()


@respx.mock
async def test_errors_are_typed():
    respx.put("http://test/ai/v1/dashboard/layout").mock(side_effect=[
        httpx.Response(409, json={"error": {"code": "CONFLICT", "message": "stale"}}),
        httpx.Response(422, json={"error": {"code": "BAD_PARAMETER", "message": "tiles"}}),
    ])
    respx.get("http://test/ai/v1/dashboard/layout").mock(return_value=httpx.Response(
        403, json={"error": {"code": "LICENSE_REQUIRED", "message": "no license"}}))
    client = await _live()
    with pytest.raises(ConflictError) as exc:
        await client.put_dashboard_layout([], 1)
    assert exc.value.http_status == 409 and exc.value.code == "CONFLICT"
    with pytest.raises(ParameterError):
        await client.put_dashboard_layout([], 1)
    with pytest.raises(LicenseRequiredError):
        await client.get_dashboard_layout()
    await client.shutdown()


@respx.mock
async def test_log_does_not_leak_tiles_or_client_key(caplog):
    respx.put("http://test/ai/v1/dashboard/layout").mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    with caplog.at_level(logging.INFO):
        await client.put_dashboard_layout([{"title": "SECRET-TILE"}], 0,
                                          client_key="abcdefgh-ijkl")
    assert "SECRET" not in caplog.text and "ijkl" not in caplog.text
    await client.shutdown()


def _mock_client(user: str = "u") -> MCBPClient:
    return MCBPClient(ConnectionConfig(user=user, mock=True))


async def test_mock_empty_then_revision_cycle():
    reset_mock_dashboard()
    client = _mock_client()
    empty = (await client.get_dashboard_layout(client_key="k"))["data"]
    assert empty["tiles"] == [] and empty["revision"] == 0
    saved = (await client.put_dashboard_layout([{"id": "a"}], 0, client_key="k"))["data"]
    assert saved["revision"] == 1 and saved["tiles"] == [{"id": "a"}] and saved["updated"]
    with pytest.raises(ConflictError):
        await client.put_dashboard_layout([], 0, client_key="k")
    again = (await client.put_dashboard_layout([], 1, client_key="k"))["data"]
    assert again["revision"] == 2 and again["tiles"] == []
    assert (await client.get_dashboard_layout(client_key="k"))["data"]["revision"] == 2


async def test_mock_owner_isolation():
    reset_mock_dashboard()
    await _mock_client("u1").put_dashboard_layout([{"id": "a"}], 0, client_key="k")
    assert (await _mock_client("u2").get_dashboard_layout(client_key="k"))["data"]["revision"] == 0
    assert (await _mock_client("u1").get_dashboard_layout(client_key="k2"))["data"]["tiles"] == []


async def test_mock_validation():
    reset_mock_dashboard()
    client = _mock_client()
    with pytest.raises(ParameterError):
        await client.put_dashboard_layout([{}] * 25, 0)
    with pytest.raises(ParameterError):
        await client.put_dashboard_layout([{"blob": "x" * 70000}], 0)
    with pytest.raises(ParameterError):
        await client.put_dashboard_layout("nope", 0)  # type: ignore[arg-type]
    assert (await client.put_dashboard_layout([{}] * 24, 0))["data"]["revision"] == 1
