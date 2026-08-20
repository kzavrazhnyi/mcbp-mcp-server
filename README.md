<!-- mcp-name: io.github.kzavrazhnyi/mcbp-ai -->

# mcbp-ai — MCP server for BAS / 1C

Read your **BAS (1C)** database directly from Claude Desktop, Claude Code, or any MCP client.
Catalogs, documents, register balances and records, and the full configuration metadata tree —
exposed as MCP tools over the `MCBP_AI` HTTP service.

**Read-only by default.** None of the tools available out of the box modifies your data.

> ### ⚠ Requires a server-side component that is not in this repository
>
> These packages are a *client*. They talk to the **`MCBP_AI` HTTP service** — a BSL module (a
> common module plus an HTTP service definition) installed into a BAS/1C configuration through
> Конфігуратор. **That module is proprietary and is not part of this repository.**
>
> Without it the server starts, connects, and every route answers `404`. To run it against your own
> base, contact [MCBP.PLUS](https://mcbp.plus) to obtain the `MCBP_AI` module.

The MCBP+ configuration and the `MCBP_AI` service module are proprietary works of
[MCBP.PLUS](https://mcbp.plus). The MIT licence in this repository covers the Python packages only.

## Packages

| Directory | PyPI package | What it is |
|---|---|---|
| [`mcp-server/`](mcp-server) | `mcbp-mcp-server` | The MCP server: stdio transport, tool registration, error mapping |
| [`mcbp-core/`](mcbp-core) | `mcbp-core` | The shared engine: async HTTP client, typed errors, tool registry |

They are split because `mcbp-core` is also consumed by a separate FastAPI web chat. Everything that
knows about BAS — routes, filters, response shapes, JSON schemas — lives in `mcbp-core`; the MCP
server holds none of it and is a thin adapter of roughly 200 lines. That is deliberate: two
front-ends registering from one registry cannot drift apart.

## Quick start

```bash
pip install mcbp-mcp-server
```

Claude Desktop — `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "mcbp-ai": {
      "command": "python",
      "args": ["-m", "mcbp_mcp_server"],
      "env": {
        "ONEC_BASE_URL": "http://host/base/hs/mcbp_ai",
        "ONEC_USER": "ai_service",
        "ONEC_PASSWORD": "***"
      }
    }
  }
}
```

Note that `ONEC_BASE_URL` stops at the service name — the client appends `/ai/v1/...` itself.

The full tool list, every environment variable, security notes and troubleshooting:
**[mcp-server/README.md](mcp-server/README.md)**.

## Tools

10 read tools are always registered; three write tools appear only when `MCP_ALLOW_WRITE=1`.

```
list_metadata → describe_metadata → search_catalog / filter_catalog / get_documents → get_object
```

That is the cycle the tool descriptions steer a model through. Errors are deliberately not
swallowed: naming a field that does not exist returns `BAD_PARAMETER` **with that field named**, so
the model can call `describe_metadata` and correct its own request.

## Development

Both packages live here and are developed together:

```bash
pip install -e ./mcbp-core
pip install -e ./mcp-server

python -m pytest mcbp-core/tests -q
python -m pytest mcp-server/tests -q
```

Run the two suites as **separate commands** — a single pytest invocation over both fails at
collection, because each package carries its own `[tool.pytest.ini_options]` and rootdir.

Tests never touch a live base: `mcbp-core` ships an offline mock mode
(`ConnectionConfig(mock=True)`), and the MCP layer is exercised through the SDK's in-memory client.

## Licence

MIT — see [LICENSE](LICENSE).
