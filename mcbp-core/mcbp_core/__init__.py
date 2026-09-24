"""mcbp-core — shared BAS MCBP_AI HTTP client, typed errors, and tool registry.

No dependency on any specific host application (backend FastAPI app, mcp-server, ...).
Consumers build a `ConnectionConfig` from their own settings and pass it to `MCBPClient`;
`mcbp_core.tools.tool_specs()` gives every caller the same `ToolSpec` registry: 13 tools, of
which the 10 read-only ones are returned by default. `include_write=True` adds `write_object`,
`save_context` and `patch_object`; `surface=` narrows the list to one front-end.
"""

__version__ = "0.1.5"
