"""The base's "AI data change allowed" switch (`/health` -> `allow_data_change`) gating write
tools on top of `MCP_ALLOW_WRITE`. Mock `MCBPClient` + the SDK's in-memory `Client`, no live BAS."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import DataChangeDisabledError
from mcbp_core.tools import ToolSpec, tool_specs
from mcp import Client, types
from mcp.server import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler

from mcbp_mcp_server.data_change import DataChangeGate
from mcbp_mcp_server.registry import make_on_call_tool, make_on_list_tools
from mcbp_mcp_server.server import AppContext, _probe_health, create_server

_WRITE_NAMES = {s.name for s in tool_specs(surface="mcp", include_write=True)} - {
    s.name for s in tool_specs(surface="mcp")
}


def _server(allow_write: bool, allow_data_change: bool | None) -> Server[AppContext]:
    @asynccontextmanager
    async def _lifespan(server: Server[AppContext]) -> AsyncIterator[AppContext]:
        client = MCBPClient(ConnectionConfig(mock=True, mock_allow_data_change=allow_data_change))
        await client.startup()
        gate = DataChangeGate(client)
        gate.update(await _probe_health(client))
        try:
            yield AppContext(client=client, allow_write=allow_write, data_change=gate)
        finally:
            await client.shutdown()

    return Server(
        "mcbp-ai-test",
        lifespan=_lifespan,
        on_list_tools=make_on_list_tools(allow_write),
        on_call_tool=make_on_call_tool(allow_write),
    )


async def _names(allow_write: bool, allow_data_change: bool | None) -> set[str]:
    async with Client(_server(allow_write, allow_data_change)) as client:
        return {t.name for t in (await client.list_tools()).tools}


async def test_list_tools_hides_write_tools_when_base_disallows() -> None:
    names = await _names(allow_write=True, allow_data_change=False)
    assert names == {s.name for s in tool_specs(surface="mcp")}
    assert not names & _WRITE_NAMES
    assert "exchange_result" in names


@pytest.mark.parametrize("allow_data_change", [True, None])
async def test_list_tools_shows_write_tools_when_base_allows_or_unknown(
    allow_data_change: bool | None,
) -> None:
    names = await _names(allow_write=True, allow_data_change=allow_data_change)
    assert _WRITE_NAMES <= names


async def test_mcp_allow_write_off_still_hides_write_tools_when_base_allows() -> None:
    assert not await _names(allow_write=False, allow_data_change=True) & _WRITE_NAMES


async def test_call_write_tool_refused_without_calling_1c(monkeypatch: Any) -> None:
    calls: list[dict] = []

    async def _never(client: object, args: dict) -> dict:
        calls.append(args)
        return {}

    spec = ToolSpec("write_thing", "test write tool",
                    {"type": "object", "properties": {}, "required": []}, _never,
                    read_only=False)
    monkeypatch.setattr("mcbp_mcp_server.registry.tool_specs", lambda **kwargs: [spec])
    async with Client(_server(allow_write=True, allow_data_change=False)) as client:
        result = await client.call_tool("write_thing", {})
    assert calls == []
    assert result.is_error is True
    text = result.content[0].text
    assert text.startswith("DATA_CHANGE_DISABLED: Data changes are disabled in this base")


async def test_call_read_tool_unaffected_when_base_disallows() -> None:
    async with Client(_server(allow_write=True, allow_data_change=False)) as client:
        result = await client.call_tool("search_catalog", {"type": "Контрагенты"})
    assert result.is_error is not True


async def test_call_write_tool_runs_when_base_allows() -> None:
    async with Client(_server(allow_write=True, allow_data_change=True)) as client:
        result = await client.call_tool(
            "write_object", {"type": "Контрагенты", "payload": {"Description": "X"}},
        )
    assert result.is_error is not True


async def test_upstream_data_change_disabled_is_a_tool_error_and_closes_the_gate(
    monkeypatch: Any,
) -> None:
    async def _refused(client: object, args: dict) -> dict:
        raise DataChangeDisabledError("Data change by AI is disabled in this base")

    spec = ToolSpec("write_thing", "test write tool",
                    {"type": "object", "properties": {}, "required": []}, _refused,
                    read_only=False)
    monkeypatch.setattr("mcbp_mcp_server.registry.tool_specs", lambda **kwargs: [spec])
    async with Client(_server(allow_write=True, allow_data_change=True)) as client:
        result = await client.call_tool("write_thing", {})
        after = await client.list_tools()
    assert result.is_error is True
    assert "DATA_CHANGE_DISABLED" in result.content[0].text
    assert after.tools == []  # the only tool was a write tool: now hidden


# --- the gate itself: TTL refresh, never one health call per tool call ---

class _CountingHealth:
    def __init__(self, values: list[Any]) -> None:
        self.values = values
        self.calls = 0

    async def health(self) -> dict[str, Any]:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return {"status": "ok", "allow_data_change": value}


async def test_gate_refreshes_only_after_ttl() -> None:
    now = [0.0]
    client = _CountingHealth([True, False])
    gate = DataChangeGate(client, ttl_s=60.0, clock=lambda: now[0])
    assert await gate.allowed() is True
    now[0] = 59.0
    assert await gate.allowed() is True
    assert client.calls == 1
    now[0] = 60.0
    assert await gate.allowed() is False
    assert client.calls == 2


async def test_gate_keeps_last_value_when_refresh_fails() -> None:
    now = [0.0]
    client = _CountingHealth([False, RuntimeError("down")])
    gate = DataChangeGate(client, ttl_s=60.0, clock=lambda: now[0])
    assert await gate.allowed() is False
    now[0] = 61.0
    assert await gate.allowed() is False
    now[0] = 62.0
    assert await gate.allowed() is False
    assert client.calls == 2  # the failed refresh restarted the TTL


async def test_concurrent_allowed_after_ttl_shares_one_health_call() -> None:
    class _SlowHealth(_CountingHealth):
        async def health(self) -> dict[str, Any]:
            await asyncio.sleep(0.01)  # suspends, so the other callers really overlap
            return await super().health()

    now = [0.0]
    client = _SlowHealth([False])
    gate = DataChangeGate(client, ttl_s=60.0, clock=lambda: now[0])
    gate.update({"allow_data_change": True})
    now[0] = 61.0
    results = await asyncio.gather(*(gate.allowed() for _ in range(5)))
    assert client.calls == 1
    assert results == [False] * 5


# --- tools/list_changed: announced on a flip, from the request that observed it ---

def _gated_server(gate: DataChangeGate) -> Server[AppContext]:
    @asynccontextmanager
    async def _lifespan(server: Server[AppContext]) -> AsyncIterator[AppContext]:
        client = MCBPClient(ConnectionConfig(mock=True))
        await client.startup()
        try:
            yield AppContext(client=client, allow_write=True, data_change=gate)
        finally:
            await client.shutdown()

    bus = InMemorySubscriptionBus()
    return Server(
        "mcbp-ai-test", lifespan=_lifespan,
        on_list_tools=make_on_list_tools(True, bus), on_call_tool=make_on_call_tool(True, bus),
        on_subscriptions_listen=ListenHandler(bus),
    )


def _flipping_gate(first: bool, second: bool) -> tuple[DataChangeGate, list[float]]:
    now = [0.0]
    gate = DataChangeGate(_CountingHealth([second]), ttl_s=60.0, clock=lambda: now[0])
    gate.update({"allow_data_change": first})  # the startup seed: never a change
    return gate, now


async def _list_twice_across_ttl(client: Client, now: list[float]) -> None:
    await client.list_tools()
    now[0] = 61.0
    await client.list_tools()
    await client.list_tools()


async def _legacy_list_changed_count(first: bool, second: bool) -> int:
    """Handshake-era client: a `notifications/tools/list_changed` on the session."""
    gate, now = _flipping_gate(first, second)
    seen: list[object] = []

    async def _on_message(message: object) -> None:
        if isinstance(message, types.ToolListChangedNotification):
            seen.append(message)

    async with Client(_gated_server(gate), mode="legacy", message_handler=_on_message) as client:
        await _list_twice_across_ttl(client, now)
    return len(seen)


async def _listen_list_changed_count(first: bool, second: bool) -> int:
    """2026-07-28 client: a `ToolsListChanged` event on its `subscriptions/listen` stream."""
    gate, now = _flipping_gate(first, second)
    seen: list[object] = []
    async with Client(_gated_server(gate)) as client:
        async with client.listen(tools_list_changed=True) as sub:
            await _list_twice_across_ttl(client, now)
            with anyio.move_on_after(0.3):
                async for event in sub:
                    seen.append(event)
    return len(seen)


@pytest.mark.parametrize("count", [_legacy_list_changed_count, _listen_list_changed_count])
@pytest.mark.parametrize(("first", "second"), [(True, False), (False, True)])
async def test_switch_flip_announces_tools_list_changed_once(
    count: Any, first: bool, second: bool,
) -> None:
    assert await count(first, second) == 1


@pytest.mark.parametrize("count", [_legacy_list_changed_count, _listen_list_changed_count])
async def test_no_flip_announces_no_tools_list_changed(count: Any) -> None:
    assert await count(True, True) == 0


def test_gate_none_to_true_is_not_a_tool_list_change() -> None:
    gate = DataChangeGate(_CountingHealth([True]))
    gate.update({"status": "ok"})
    gate.update({"allow_data_change": True})
    assert gate.take_change() is False
    gate.mark_disabled()
    assert gate.take_change() is True
    assert gate.take_change() is False


def test_server_declares_tools_list_changed(monkeypatch: Any) -> None:
    monkeypatch.setenv("MCP_ALLOW_WRITE", "1")
    server = create_server()
    caps = server.create_initialization_options().capabilities  # handshake era
    assert caps.tools is not None and caps.tools.list_changed is True
    modern = server.get_capabilities(protocol_version="2026-07-28")  # listen-derived
    assert modern.tools is not None and modern.tools.list_changed is True


def test_gate_treats_missing_or_non_bool_field_as_unknown() -> None:
    gate = DataChangeGate(_CountingHealth([True]))
    gate.update({"status": "ok"})
    assert gate._value is None
    gate.update({"allow_data_change": "false"})
    assert gate._value is None
