"""MCP prompts for the low-level `Server` (`prompts/list`, `prompts/get`).

`setup_exchange` walks an agent that is connected to TWO instances of this server (the source base
and the target base) through setting up the exchange of one metadata object: read-only analysis,
a numbered plan the user confirms, journalled writes, then a test exchange through
`exchange_enqueue -> exchange_run -> exchange_result`. The prompt only produces text — every
write still goes through the tools and their `MCP_ALLOW_WRITE` gate.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp import MCPError, types
from mcp.server.context import ServerRequestContext

if TYPE_CHECKING:
    from mcbp_mcp_server.server import AppContext

_OnListPrompts = Callable[
    ["ServerRequestContext[AppContext, Any]", "types.PaginatedRequestParams | None"],
    Awaitable[types.ListPromptsResult],
]
_OnGetPrompt = Callable[
    ["ServerRequestContext[AppContext, Any]", types.GetPromptRequestParams],
    Awaitable[types.GetPromptResult],
]

SETUP_EXCHANGE = types.Prompt(
    name="setup_exchange",
    title="Налаштувати обмін",
    description=(
        "Налаштувати обмін: set up and test the exchange of one metadata object between two "
        "bases, for an agent connected to two MCP servers (source and target base)."
    ),
    arguments=[
        types.PromptArgument(
            name="object_type",
            description="Source metadata object to exchange, e.g. Document.РеализацияТоваровУслуг",
            required=True,
        ),
        types.PromptArgument(
            name="target_base",
            description="Identifier of the exchange node (MCBP_InformationBases) in the source "
                        "base; omit to find it during the analysis",
            required=False,
        ),
    ],
)

_SETUP_EXCHANGE_TEXT = """\
Set up the exchange of `{object_type}` from the SOURCE base to the TARGET base ({target}).
You are connected to two MCP servers: one for the source base, one for the target base. Always \
say which base each call goes to.

1. Analyse, read-only. Source: the registration rules for `{object_type}` and the exchange node \
(the MCBP_InformationBases item{node_hint}). Target: the conversion settings and data sources \
for the incoming object. Find every dependent catalog (references in the header and tabular \
sections, e.g. organisations, warehouses, counterparties, items, units). Use \
list_metadata/describe_metadata/search_catalog/filter_catalog/get_object/get_register_records.

2. Plan. Give a numbered list; each step: base, action, object, new values, reason. Include the \
steps for the dependent catalogs. Then STOP and wait for the user's confirmation in chat. Do not \
write anything before it.

3. Apply only the steps the user confirmed. Before every write call get_object (or \
get_register_records) on the target of that write. Keep a journal in your reply: step, base, \
object, before -> after.

4. Match organisations, warehouses and similar catalogs by name or code through a data-source \
search in the other base. Never map them to a constant value.

5. An attribute that does not exist in the target base is excluded from the mapping; name it in \
the report.

6. Test exchange: exchange_enqueue one existing `{object_type}` object -> exchange_run -> \
exchange_result. exchange_run sends the node's whole queue, not only this object. In the result, \
`processed: true` means delivered (`receiver_id` is its id in the target); `processed: false` \
with a `result` text is the error. Report delivered, or the error text verbatim.

7. Final report: what was changed (the journal), excluded attributes, the test exchange outcome, \
and anything left for the user to do.
"""


def setup_exchange_text(object_type: str, target_base: str | None = None) -> str:
    target = f"node `{target_base}`" if target_base else "node to be found in step 1"
    node_hint = f" `{target_base}`" if target_base else ""
    return _SETUP_EXCHANGE_TEXT.format(object_type=object_type, target=target,
                                       node_hint=node_hint)


def make_on_list_prompts() -> _OnListPrompts:
    async def on_list_prompts(
        ctx: ServerRequestContext[AppContext, Any], params: types.PaginatedRequestParams | None,
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[SETUP_EXCHANGE])

    return on_list_prompts


def make_on_get_prompt() -> _OnGetPrompt:
    async def on_get_prompt(
        ctx: ServerRequestContext[AppContext, Any], params: types.GetPromptRequestParams,
    ) -> types.GetPromptResult:
        if params.name != SETUP_EXCHANGE.name:
            raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown prompt: {params.name}")
        args = params.arguments or {}
        object_type = (args.get("object_type") or "").strip()
        if not object_type:
            raise MCPError(code=types.INVALID_PARAMS,
                           message="setup_exchange: missing required argument object_type")
        text = setup_exchange_text(object_type, (args.get("target_base") or "").strip() or None)
        return types.GetPromptResult(
            description=SETUP_EXCHANGE.description,
            messages=[types.PromptMessage(role="user",
                                          content=types.TextContent(type="text", text=text))],
        )

    return on_get_prompt
