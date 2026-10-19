"""GET /ai/v1/eventlog — `MCBPClient.get_event_log`, its mock, and the `get_event_log` tool."""
from __future__ import annotations

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import ForbiddenError, ParameterError
from mcbp_core.tools import TOOLS, tool_specs


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def _mock_client() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    return client


async def test_mock_default_levels_are_error_and_warning():
    client = await _mock_client()
    result = await client.get_event_log()
    assert {row["level"] for row in result["data"]} == {"error", "warning"}
    assert result["truncated"] is False
    assert set(result["data"][0]) == {
        "date", "level", "event", "user", "metadata", "data_presentation", "comment",
        "session", "application", "computer",
    }


async def test_mock_level_list_event_prefix_and_limit():
    client = await _mock_client()
    result = await client.get_event_log(levels=["information", "warning"], event="_$Session$_.*",
                                        limit=1)
    assert [row["level"] for row in result["data"]] == ["warning"]
    assert result["truncated"] is True


async def test_mock_unknown_level_is_parameter_error():
    client = await _mock_client()
    with pytest.raises(ParameterError, match="allowed: error, warning, information, note"):
        await client.get_event_log(levels="fatal")


async def test_mock_to_before_from_is_parameter_error():
    client = await _mock_client()
    with pytest.raises(ParameterError, match="earlier than"):
        await client.get_event_log(date_from="2026-08-02", date_to="2026-08-01")


@respx.mock
async def test_sends_every_param_as_query_string():
    route = respx.get("http://test/ai/v1/eventlog").mock(
        return_value=httpx.Response(200, json={"data": [], "truncated": False,
                                               "from": "", "to": ""})
    )
    client = _live_client()
    await client.startup()
    try:
        await client.get_event_log(levels=["error", "note"], date_from="2026-08-01",
                                   date_to="2026-08-02", event="_$Data$_.Update", user="Admin",
                                   metadata="Catalog.X", limit=10)
        params = dict(route.calls[0].request.url.params)
        assert params == {"level": "error,note", "from": "2026-08-01", "to": "2026-08-02",
                          "event": "_$Data$_.Update", "user": "Admin", "metadata": "Catalog.X",
                          "limit": "10"}
    finally:
        await client.shutdown()


@respx.mock
async def test_omits_unset_params():
    route = respx.get("http://test/ai/v1/eventlog").mock(
        return_value=httpx.Response(200, json={"data": [], "truncated": False})
    )
    client = _live_client()
    await client.startup()
    try:
        await client.get_event_log()
        assert dict(route.calls[0].request.url.params) == {}
    finally:
        await client.shutdown()


@respx.mock
async def test_no_event_log_right_is_forbidden_error():
    respx.get("http://test/ai/v1/eventlog").mock(
        return_value=httpx.Response(403, json={"error": {
            "code": "FORBIDDEN", "message": "The current user has no EventLog right"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ForbiddenError, match="EventLog right"):
            await client.get_event_log()
    finally:
        await client.shutdown()


def test_tool_is_read_only_mcp_only_and_appended_last():
    spec = TOOLS["get_event_log"]
    assert spec.read_only is True
    assert spec.surfaces == frozenset({"mcp"})
    assert list(TOOLS).index("get_event_log") == len(TOOLS) - 3
    assert list(TOOLS)[0] == "search_catalog"
    assert spec.parameters["required"] == []
    assert spec in tool_specs(surface="mcp")
    assert spec not in tool_specs(surface="backend", include_write=True)


async def test_executor_passes_arguments_by_name():
    client = await _mock_client()
    result = await TOOLS["get_event_log"].executor(
        client, {"level": ["error"], "from": "2026-08-01", "to": "2026-08-01"},
    )
    assert [row["level"] for row in result["data"]] == ["error"]
    assert result["from"] == "2026-08-01 00:00:00"


async def test_masker_hides_user_comment_data_presentation_and_keeps_log_fields_readable():
    from mcbp_core.masking import Masker

    client = await _mock_client()
    result = await client.get_event_log(levels=["error"])
    original = result["data"][0]
    masked = Masker().mask(result)
    row = masked["data"][0]
    for key in ("user", "comment", "data_presentation", "computer"):
        assert row[key] != original[key], key
    for key in ("date", "level", "event", "metadata", "session", "application"):
        assert row[key] == original[key], key
    assert (masked["from"], masked["to"], masked["truncated"]) == (
        result["from"], result["to"], result["truncated"])
