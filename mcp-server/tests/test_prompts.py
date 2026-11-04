"""`setup_exchange` prompt: listed by `create_server()` with its arguments, rendered with the
required behaviour, and refused without `object_type`. No network — the SDK's in-memory
`Client` against the low-level `Server`; `create_server()` itself needs no `ONEC_*` env."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcbp_core.client import ConnectionConfig, MCBPClient
from mcp import Client, MCPError
from mcp.server import Server

from mcbp_mcp_server.prompts import make_on_get_prompt, make_on_list_prompts
from mcbp_mcp_server.server import AppContext, create_server


def _prompt_server() -> Server[AppContext]:
    @asynccontextmanager
    async def _test_lifespan(server: Server[AppContext]) -> AsyncIterator[AppContext]:
        client = MCBPClient(ConnectionConfig(mock=True))
        yield AppContext(client=client, allow_write=False)

    return Server("mcbp-ai-test", lifespan=_test_lifespan,
                  on_list_prompts=make_on_list_prompts(), on_get_prompt=make_on_get_prompt())


def test_create_server_registers_prompt_handlers():
    caps = create_server().create_initialization_options().capabilities
    assert caps.prompts is not None


async def test_setup_exchange_is_listed_with_its_arguments():
    async with Client(_prompt_server()) as client:
        result = await client.list_prompts()
    prompts = {p.name: p for p in result.prompts}
    assert "setup_exchange" in prompts
    args = {a.name: a.required for a in prompts["setup_exchange"].arguments}
    assert args == {"object_type": True, "target_base": False}


async def test_setup_exchange_text_carries_the_required_steps():
    async with Client(_prompt_server()) as client:
        result = await client.get_prompt(
            "setup_exchange",
            {"object_type": "Document.РеализацияТоваровУслуг", "target_base": "DemoBASTrade"},
        )
    text = result.messages[0].content.text
    assert "Document.РеализацияТоваровУслуг" in text and "DemoBASTrade" in text
    assert "TWO" in text or "two MCP servers" in text
    assert "STOP" in text and "confirm" in text
    assert "get_object" in text and "before -> after" in text
    assert "Never map them to a constant" in text
    assert "excluded" in text
    assert "exchange_enqueue" in text and "exchange_run" in text and "exchange_result" in text
    assert "Final report" in text


async def test_setup_exchange_without_target_base():
    async with Client(_prompt_server()) as client:
        result = await client.get_prompt("setup_exchange", {"object_type": "Catalog.Номенклатура"})
    assert "node to be found in step 1" in result.messages[0].content.text


async def test_setup_exchange_requires_object_type():
    async with Client(_prompt_server()) as client:
        with pytest.raises(MCPError):
            await client.get_prompt("setup_exchange", {})


async def test_unknown_prompt_is_an_error():
    async with Client(_prompt_server()) as client:
        with pytest.raises(MCPError):
            await client.get_prompt("nope", {})
