"""`ToolSpec` -> MCP tool registration for the low-level `Server`.

One loop over `mcbp_core.tools.tool_specs(surface="mcp", ...)`, no per-tool `async def` and no
hardcoded tool list — a spec added to the shared registry is exposed here with zero edits.
`spec.parameters` is passed through as `input_schema` UNCHANGED (that is the whole reason the
skeleton is built on the low-level `Server` rather than `MCPServer` — see
`.claude/skills/mcbp-mcp-server/SKILL.md`).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcbp_core.errors import MCBPError
from mcbp_core.tools import ToolSpec, tool_specs
from mcp import types
from mcp.server.context import ServerRequestContext

from mcbp_mcp_server.errors import is_fatal, to_mcp_error, to_tool_error

if TYPE_CHECKING:
    from mcbp_mcp_server.server import AppContext

log = logging.getLogger("mcbp_mcp_server")

_OnListTools = Callable[
    ["ServerRequestContext[AppContext, Any]", "types.PaginatedRequestParams | None"],
    Awaitable[types.ListToolsResult],
]
_OnCallTool = Callable[
    ["ServerRequestContext[AppContext, Any]", types.CallToolRequestParams],
    Awaitable[types.CallToolResult],
]


def _annotations(spec: ToolSpec) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        read_only_hint=spec.read_only,
        destructive_hint=True if not spec.read_only else None,
    )


def _to_tool(spec: ToolSpec) -> types.Tool:
    return types.Tool(
        name=spec.name,
        description=spec.description_en or spec.description,
        input_schema=spec.parameters,  # already JSON Schema — passed through untouched
        annotations=_annotations(spec),
    )


def make_on_list_tools(allow_write: bool) -> _OnListTools:
    # Built once at registration time so `tools/list` is deterministic and cheap — the registry
    # order from `tool_specs()` is preserved, never sorted.
    tools = [_to_tool(spec) for spec in tool_specs(surface="mcp", include_write=allow_write)]

    async def on_list_tools(
        ctx: ServerRequestContext[AppContext, Any], params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    return on_list_tools


def make_on_call_tool(allow_write: bool) -> _OnCallTool:
    specs: dict[str, ToolSpec] = {
        spec.name: spec for spec in tool_specs(surface="mcp", include_write=allow_write)
    }

    async def on_call_tool(
        ctx: ServerRequestContext[AppContext, Any], params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        spec = specs.get(params.name)
        if spec is None:
            return types.CallToolResult(
                is_error=True,
                content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")],
            )
        client = ctx.lifespan_context.client
        arguments = params.arguments or {}
        try:
            result = await spec.executor(client, arguments)
        except MCBPError as exc:
            if is_fatal(exc):
                raise to_mcp_error(exc) from exc
            log.warning("%s -> %s: %s", spec.name, exc.code, exc.message)
            return to_tool_error(exc)
        # Cyrillic is the overwhelming majority of BAS field values — ensure_ascii=False keeps
        # the response readable and avoids bloating the token count with \uXXXX escapes.
        text = json.dumps(result, ensure_ascii=False)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return on_call_tool
