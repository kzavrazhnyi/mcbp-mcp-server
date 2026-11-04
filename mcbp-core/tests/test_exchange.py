"""exchange_enqueue / exchange_run / exchange_result: respx for the wire format and error
mapping of POST /ai/v1/exchange/{action}, the offline mock for the success shapes, and the
registry gating (two write tools, one read tool, MCP surface only)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ConditionFailedError,
    ForbiddenError,
    LicenseRequiredError,
    NotConfiguredError,
    NotFoundError,
    ParameterError,
    UpstreamError,
)
from mcbp_core.tools import TOOLS, tool_specs

OID = "00485286-a015-11ea-826d-782bcbe5684f"
OBJ = {"base": "DemoBASTrade", "metadata": "document", "type": "РеализацияТоваровУслуг",
       "id": OID}
RESULT = {"object": {"id": OID, "presentation": "Видаткова накладна 1 від 03.01.2020"},
          "queue": [{"processed": True, "result": "OK!", "date_execution": "2026-11-04 11:33:30",
                     "receiver_id": "c52be10f-d832-11f1-a97b-f8b54d8ff317",
                     "user": "Адміністратор"}],
          "exchange": None}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


def _err(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": f"{code} text"}})


# --- wire format ---

@respx.mock
async def test_enqueue_posts_the_object_body():
    route = respx.post("http://test/ai/v1/exchange/enqueue").mock(
        return_value=httpx.Response(200, json={"queued": True}))
    client = await _live()
    try:
        out = await client.exchange_enqueue("DemoBASTrade", metadata="document",
                                            type_="РеализацияТоваровУслуг", object_id=OID)
    finally:
        await client.shutdown()
    assert out == {"queued": True}
    assert json.loads(route.calls.last.request.content) == OBJ


@respx.mock
async def test_run_posts_only_base():
    route = respx.post("http://test/ai/v1/exchange/run").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    client = await _live()
    try:
        assert await client.exchange_run("DemoBASTrade") == {"ok": True}
    finally:
        await client.shutdown()
    assert json.loads(route.calls.last.request.content) == {"base": "DemoBASTrade"}


@respx.mock
async def test_result_posts_the_object_body_and_returns_the_shape():
    route = respx.post("http://test/ai/v1/exchange/result").mock(
        return_value=httpx.Response(200, json=RESULT))
    client = await _live()
    try:
        out = await TOOLS["exchange_result"].executor(client, dict(OBJ))
    finally:
        await client.shutdown()
    assert out == RESULT
    assert json.loads(route.calls.last.request.content) == OBJ


@respx.mock
async def test_run_is_not_retried_on_503():
    route = respx.post("http://test/ai/v1/exchange/run").mock(return_value=httpx.Response(503))
    client = await _live()
    try:
        with pytest.raises(UpstreamError):
            await client.exchange_run("DemoBASTrade")
    finally:
        await client.shutdown()
    assert route.call_count == 1


# --- error mapping ---

@pytest.mark.parametrize(("status", "code", "exc"), [
    (400, "BAD_PARAMETER", ParameterError),
    (404, "NOT_FOUND", NotFoundError),
    (422, "NOT_CONFIGURED", NotConfiguredError),
    (422, "CONDITION_FAILED", ConditionFailedError),
    (403, "FORBIDDEN", ForbiddenError),
    (403, "LICENSE_REQUIRED", LicenseRequiredError),
])
@respx.mock
async def test_enqueue_error_codes_map_to_typed_errors(status, code, exc):
    respx.post("http://test/ai/v1/exchange/enqueue").mock(return_value=_err(status, code))
    client = await _live()
    try:
        with pytest.raises(exc) as info:
            await TOOLS["exchange_enqueue"].executor(client, dict(OBJ))
    finally:
        await client.shutdown()
    assert info.value.code == code


@respx.mock
async def test_result_not_found():
    respx.post("http://test/ai/v1/exchange/result").mock(return_value=_err(404, "NOT_FOUND"))
    client = await _live()
    try:
        with pytest.raises(NotFoundError):
            await client.exchange_result("DemoBASTrade", metadata="document", type_="X",
                                         object_id=OID)
    finally:
        await client.shutdown()


# --- mock mode ---

async def test_mock_round_trip():
    client = MCBPClient(ConnectionConfig(mock=True))
    assert await TOOLS["exchange_enqueue"].executor(client, dict(OBJ)) == {"queued": True}
    assert await TOOLS["exchange_run"].executor(client, {"base": "DemoBASTrade"}) == {"ok": True}
    out = await TOOLS["exchange_result"].executor(client, dict(OBJ))
    assert out["object"]["id"] == OID
    assert out["queue"][0]["processed"] is True
    assert out["exchange"] is None


async def test_mock_rejects_a_non_uuid_id_and_a_missing_base():
    client = MCBPClient(ConnectionConfig(mock=True))
    with pytest.raises(ParameterError):
        await client.exchange_enqueue("DemoBASTrade", metadata="document", type_="X",
                                      object_id="not-a-uuid")
    with pytest.raises(ParameterError):
        await client.exchange_run("")


async def test_mock_unknown_action_is_not_found():
    client = MCBPClient(ConnectionConfig(mock=True))
    with pytest.raises(NotFoundError) as info:
        await client._request("POST", "/ai/v1/exchange/bogus", json={"base": "B"})
    assert "enqueue" in info.value.message


# --- registry gating ---

def test_exchange_tools_are_mcp_only():
    for name in ("exchange_enqueue", "exchange_run", "exchange_result"):
        assert TOOLS[name].surfaces == frozenset({"mcp"})
    backend = {s.name for s in tool_specs(surface="backend", include_write=True)}
    assert not backend & {"exchange_enqueue", "exchange_run", "exchange_result"}


def test_write_gate_hides_enqueue_and_run_but_not_result():
    read = {s.name for s in tool_specs(surface="mcp")}
    write = {s.name for s in tool_specs(surface="mcp", include_write=True)}
    assert "exchange_result" in read
    assert not read & {"exchange_enqueue", "exchange_run"}
    assert {"exchange_enqueue", "exchange_run", "exchange_result"} <= write


def test_descriptions_state_the_order_and_the_queue_flush():
    for name in ("exchange_enqueue", "exchange_run", "exchange_result"):
        text = TOOLS[name].description_en
        assert "exchange_enqueue -> exchange_run -> exchange_result" in text or "Step" in text
    assert "WHOLE queue" in TOOLS["exchange_run"].description_en
    assert "processed: false" in TOOLS["exchange_result"].description_en


def test_required_parameters():
    assert TOOLS["exchange_run"].parameters["required"] == ["base"]
    for name in ("exchange_enqueue", "exchange_result"):
        assert TOOLS[name].parameters["required"] == ["base", "metadata", "type", "id"]
