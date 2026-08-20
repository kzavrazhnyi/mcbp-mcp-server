"""mcbp-core — shared BAS MCBP_AI HTTP client, typed errors, and tool registry.

No dependency on any specific host application (backend FastAPI app, mcp-server, ...).
Consumers build a `ConnectionConfig` from their own settings and pass it to `MCBPClient`;
`mcbp_core.tools.tool_specs()` gives the same 11-entry `ToolSpec` registry to every caller.
"""

__version__ = "0.1.0"
