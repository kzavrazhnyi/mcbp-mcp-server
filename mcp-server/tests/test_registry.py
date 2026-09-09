"""Phase 4: tool registration on the low-level `Server`. Everything here uses the SDK's
in-memory `Client` + `ConnectionConfig(mock=True)` — no network, no live BAS. A dedicated test
lifespan (not `mcbp_mcp_server.server.lifespan`, which needs real `ONEC_*` creds and always
builds `mock=False`) wires a mock `MCBPClient` into `AppContext` so the registry factories can be
exercised in isolation."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import KeyMismatchError
from mcbp_core.tools import ToolSpec, tool_specs
from mcp import Client, MCPError
from mcp.server import Server

from mcbp_mcp_server.registry import make_on_call_tool, make_on_list_tools
from mcbp_mcp_server.server import AppContext


def _mock_server(allow_write: bool = False) -> Server[AppContext]:
    @asynccontextmanager
    async def _test_lifespan(server: Server[AppContext]) -> AsyncIterator[AppContext]:
        client = MCBPClient(ConnectionConfig(mock=True))
        await client.startup()
        try:
            yield AppContext(client=client, allow_write=allow_write)
        finally:
            await client.shutdown()

    return Server(
        "mcbp-ai-test",
        lifespan=_test_lifespan,
        on_list_tools=make_on_list_tools(allow_write),
        on_call_tool=make_on_call_tool(allow_write),
    )


# --- inventory: order and gating ---

async def test_inventory_matches_registry_order_read_only():
    async with Client(_mock_server(allow_write=False)) as client:
        result = await client.list_tools()
    names = [t.name for t in result.tools]
    assert names == [s.name for s in tool_specs(surface="mcp", include_write=False)]


async def test_inventory_matches_registry_order_with_write():
    async with Client(_mock_server(allow_write=True)) as client:
        result = await client.list_tools()
    names = [t.name for t in result.tools]
    assert names == [s.name for s in tool_specs(surface="mcp", include_write=True)]


async def test_gating_ten_without_flag_thirteen_with():
    async with Client(_mock_server(allow_write=False)) as client:
        without = await client.list_tools()
    async with Client(_mock_server(allow_write=True)) as client:
        with_flag = await client.list_tools()
    assert len(without.tools) == 10
    assert len(with_flag.tools) == 13
    added = {t.name for t in with_flag.tools} - {t.name for t in without.tools}
    assert added == {"write_object", "save_context", "patch_object"}


# --- input_schema passed through untouched ---

async def test_input_schema_matches_spec_parameters_unchanged():
    specs = {s.name: s for s in tool_specs(surface="mcp", include_write=True)}
    async with Client(_mock_server(allow_write=True)) as client:
        result = await client.list_tools()
    assert result.tools  # sanity: the mock server actually advertised something
    for tool in result.tools:
        assert tool.input_schema == specs[tool.name].parameters


async def test_description_prefers_english():
    specs = {s.name: s for s in tool_specs(surface="mcp")}
    async with Client(_mock_server()) as client:
        result = await client.list_tools()
    for tool in result.tools:
        spec = specs[tool.name]
        assert tool.description == (spec.description_en or spec.description)


async def test_annotations_read_only_and_destructive_hints():
    async with Client(_mock_server(allow_write=True)) as client:
        result = await client.list_tools()
    by_name = {t.name: t for t in result.tools}
    assert by_name["search_catalog"].annotations.read_only_hint is True
    assert by_name["search_catalog"].annotations.destructive_hint is None
    assert by_name["patch_object"].annotations.read_only_hint is False
    assert by_name["patch_object"].annotations.destructive_hint is True


# --- successful call ---

async def test_successful_call_in_mock_mode_returns_data():
    async with Client(_mock_server()) as client:
        result = await client.call_tool("search_catalog", {"type": "Контрагенты"})
    assert result.is_error is not True
    assert result.content
    assert '"data"' in result.content[0].text


# --- ordinary tool failure: BAD_PARAMETER reaches the model verbatim ---

async def test_bad_parameter_visible_to_model_as_tool_error():
    async with Client(_mock_server()) as client:
        result = await client.call_tool(
            "describe_metadata",
            {"metadata": "Documents", "type": "ЗаказПокупателя", "tabular_section": "НеІснує"},
        )
    assert result.is_error is True
    assert "BAD_PARAMETER" in result.content[0].text


async def test_misspelled_required_parameter_is_bad_parameter_not_a_key_error():
    # Live regression (09.09.2026): `search_catalog` called with `catalog` instead of `type` came
    # back as {"code": 0, "message": "'type'"} — a bare KeyError the model cannot self-correct.
    async with Client(_mock_server()) as client:
        result = await client.call_tool("search_catalog", {"catalog": "Контрагенты"})
    assert result.is_error is True
    text = result.content[0].text
    assert "BAD_PARAMETER" in text
    assert "type" in text


async def _raise_untyped(client: object, args: dict) -> None:
    raise RuntimeError("upstream exploded")


async def test_untyped_error_keeps_its_message_as_a_tool_error(monkeypatch):
    spec = ToolSpec(
        "boom",
        "raises a plain RuntimeError for the test",
        {"type": "object", "properties": {}, "required": []},
        _raise_untyped,
    )
    monkeypatch.setattr("mcbp_mcp_server.registry.tool_specs", lambda **kwargs: [spec])
    async with Client(_mock_server()) as client:
        result = await client.call_tool("boom", {})
    assert result.is_error is True
    assert "upstream exploded" in result.content[0].text


async def test_unknown_tool_name_is_a_tool_error_not_a_crash():
    async with Client(_mock_server()) as client:
        result = await client.call_tool("no_such_tool", {})
    assert result.is_error is True
    assert "no_such_tool" in result.content[0].text


# --- fatal error: KeyMismatchError raises MCPError, no ordinary result ---

async def _raise_key_mismatch(client: object, args: dict) -> None:
    raise KeyMismatchError("infobase key does not match")


def _fatal_spec() -> ToolSpec:
    return ToolSpec(
        "boom",
        "raises KeyMismatchError for the test",
        {"type": "object", "properties": {}, "required": []},
        _raise_key_mismatch,
    )


async def test_key_mismatch_raises_mcp_error_not_a_result(monkeypatch):
    monkeypatch.setattr(
        "mcbp_mcp_server.registry.tool_specs", lambda **kwargs: [_fatal_spec()]
    )
    server = _mock_server()
    # pytest.raises scoped to the single await, not the whole `async with` — letting Client's
    # own __aexit__ run against a task group that already settled cleanly, rather than one still
    # unwinding the propagating exception (which anyio reports as an ExceptionGroup, not MCPError).
    async with Client(server) as client:
        with pytest.raises(MCPError):
            await client.call_tool("boom", {})


# --- Cyrillic round-trip ---

async def test_cyrillic_values_come_back_readable_not_escaped():
    async with Client(_mock_server()) as client:
        result = await client.call_tool("search_catalog", {"type": "Контрагенты"})
    text = result.content[0].text
    assert "Фурнітура" in text or "Контрагенты" in text
    assert "\\u04" not in text  # no \uXXXX escape of the Cyrillic block
