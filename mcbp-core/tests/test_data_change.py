"""Per-base "AI data change allowed" switch: `/health` passthrough and the 403
`DATA_CHANGE_DISABLED` a write route answers when it is off."""
from __future__ import annotations

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import DataChangeDisabledError, ForbiddenError, MCBPError
from mcbp_core.tools import tool_specs

_DISABLED_BODY = {"error": {
    "code": "DATA_CHANGE_DISABLED",
    "message": "Data change by AI is disabled in this base (general setting AllowDataChange)",
}}


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def _health_of(body: dict) -> dict:
    respx.get("http://test/ai/v1/health").mock(return_value=httpx.Response(200, json=body))
    client = _live_client()
    await client.startup()
    try:
        return await client.health()
    finally:
        await client.shutdown()


@pytest.mark.parametrize("raw", [True, False])
@respx.mock
async def test_health_passes_allow_data_change_through(raw: bool) -> None:
    result = await _health_of({"status": "ok", "key": True, "allow_data_change": raw})
    assert result["allow_data_change"] is raw


@pytest.mark.parametrize("body_extra", [{}, {"allow_data_change": None},
                                        {"allow_data_change": "false"}])
@respx.mock
async def test_health_missing_allow_data_change_is_none_not_false(body_extra: dict) -> None:
    result = await _health_of({"status": "ok", "key": True, **body_extra})
    assert result["allow_data_change"] is None


async def test_mock_health_allows_data_change_by_default() -> None:
    client = MCBPClient(ConnectionConfig(mock=True))
    assert (await client.health())["allow_data_change"] is True


@pytest.mark.parametrize("value", [False, None])
async def test_mock_health_allow_data_change_is_configurable(value: bool | None) -> None:
    client = MCBPClient(ConnectionConfig(mock=True, mock_allow_data_change=value))
    assert (await client.health())["allow_data_change"] is value


@respx.mock
async def test_403_data_change_disabled_maps_to_dedicated_error() -> None:
    respx.post("http://test/ai/v1/objects/Контрагенты").mock(
        return_value=httpx.Response(403, json=_DISABLED_BODY)
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(DataChangeDisabledError) as info:
            await client.create_object("Контрагенты", {"Description": "X"})
    finally:
        await client.shutdown()
    exc = info.value
    assert isinstance(exc, ForbiddenError)
    assert exc.code == "DATA_CHANGE_DISABLED"
    assert exc.http_status == 403
    assert "disabled in this base by its administrator" in exc.message
    assert "Do not retry" in exc.message
    assert "AllowDataChange" in exc.message  # BAS's own text is kept


@respx.mock
async def test_write_tool_surfaces_data_change_disabled_as_typed_error() -> None:
    """The tool layer raises it unchanged — the host maps it to a tool error like any
    other non-fatal MCBPError."""
    respx.post("http://test/ai/v1/objects/Контрагенты").mock(
        return_value=httpx.Response(403, json=_DISABLED_BODY)
    )
    spec = {s.name: s for s in tool_specs(include_write=True)}["write_object"]
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(MCBPError) as info:
            await spec.executor(client, {"type": "Контрагенты", "payload": {"Description": "X"}})
    finally:
        await client.shutdown()
    assert isinstance(info.value, DataChangeDisabledError)


def test_data_change_disabled_message_without_bas_text() -> None:
    exc = DataChangeDisabledError()
    assert exc.code == "DATA_CHANGE_DISABLED"
    assert "BAS said" not in exc.message
    assert exc.message.startswith("Data changes are disabled in this base")
