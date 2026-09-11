"""Maps `mcbp_core.errors.MCBPError` subclasses to the two channels a tool call on the
low-level `Server` can take.

EMPIRICALLY VERIFIED against the installed `mcp==2.0.0` (probe: raise a plain exception from
`on_call_tool`, call through the SDK's in-memory `Client`) — the low-level `Server` has NO
wrapper around `on_call_tool` the way `MCPServer`'s `@tool()` decorator did. An exception that
escapes the handler is NOT turned into `CallToolResult(is_error=True, ...)` automatically: it
propagates through the JSON-RPC dispatcher, which recognizes only `MCPError` and pydantic
`ValidationError` and maps everything else to a GENERIC `MCPError(code=INTERNAL_ERROR,
message="Internal server error")` — the original message is DISCARDED. So:

- `KeyMismatchError` / `AuthError` (fatal configuration, the model cannot fix it): build our own
  `MCPError` here and `raise` it from `on_call_tool` — that is the one exception type the
  dispatcher passes through with its message intact, so this is the only way to get a
  *meaningful* protocol-level error to the host instead of the generic swallowed one.
- Every other `MCBPError` (`ParameterError`, `NotFoundError`, `PlusRequiredError`,
  `UpstreamError`, …): must NOT be allowed to escape `on_call_tool` — `registry.py` catches it and
  calls `to_tool_error()` to build a `CallToolResult(is_error=True, ...)` explicitly, which is the
  only way the model sees the message (e.g. `BAD_PARAMETER` naming the unknown field verbatim)
  instead of a swallowed "Internal server error".
"""
from __future__ import annotations

from mcbp_core.errors import AuthError, KeyMismatchError, LicenseRequiredError, MCBPError
from mcp import MCPError, types

_FATAL: tuple[type[MCBPError], ...] = (KeyMismatchError, AuthError, LicenseRequiredError)


def is_fatal(exc: MCBPError) -> bool:
    """True for the error types that are a fatal configuration issue, not a recoverable tool-call
    failure — the model cannot self-correct a key mismatch, bad credentials or a missing licence.

    A licence covers the whole service: every remaining tool would fail identically, so surfacing
    it as an ordinary tool error would have the model walk the registry one call at a time."""
    return isinstance(exc, _FATAL)


def to_mcp_error(exc: MCBPError) -> MCPError:
    """`KeyMismatchError`/`AuthError` only. `code`/`message` are carried verbatim, not reworded —
    the host surfaces this as a JSON-RPC error, never a normal tool result."""
    return MCPError(code=types.INTERNAL_ERROR, message=f"{exc.code}: {exc.message}")


def to_tool_error(exc: MCBPError) -> types.CallToolResult:
    """Every other `MCBPError`. `code`/`message` are carried verbatim (never wrapped or
    reworded) — `BAD_PARAMETER` naming the unknown field is exactly what lets the model recover
    via `describe_metadata`."""
    return types.CallToolResult(
        is_error=True,
        content=[types.TextContent(type="text", text=f"{exc.code}: {exc.message}")],
    )
