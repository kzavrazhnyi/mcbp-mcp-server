# mcbp-core

Async HTTP client and shared tool registry for the **BAS / 1C `MCBP_AI`** service (`/ai/v1`).

This is the engine shared by two front-ends: the [`mcbp`](https://pypi.org/project/mcbp/)
MCP server and a FastAPI web chat. It holds everything that knows about BAS — routes, filters,
response shapes, typed errors — so those front-ends hold none of it.

> **Requires a server-side component.** `mcbp-core` talks to the `MCBP_AI` HTTP service, a BSL
> module installed into a BAS/1C configuration through Конфігуратор. That module is licensed
> separately and is not part of this package. Without it there is nothing to connect to.

The MCBP+ configuration and the `MCBP_AI` service module are proprietary works of
**[MCBP.PLUS](https://mcbp.plus)** and are distributed separately. This MIT licence covers the
Python package only.

## What it gives you

- **`MCBPClient`** — one pooled `httpx.AsyncClient` over the BAS publication: HTTP Basic auth,
  retries on `503`, an in-memory metadata cache, and an offline mock mode for tests.
- **Typed errors** — the `{"error": {"code", "message"}}` envelope BAS returns is mapped to
  exception classes (`ParameterError`, `NotFoundError`, `KeyMismatchError`, `PlusRequiredError`, …),
  so callers branch on type instead of re-parsing text.
- **A tool registry** — 13 `ToolSpec`s (JSON Schema + async executor) covering catalogs, documents,
  registers, metadata introspection and the gated write routes. Both front-ends register from this
  one list, so their surfaces cannot drift apart.

## Usage

```python
from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.tools import tool_specs

client = MCBPClient(ConnectionConfig(
    base_url="http://host/base/hs/mcbp_ai",   # without the /ai/v1 suffix — the client adds it
    user="ai_service", password="***", mock=False,
))
await client.startup()
try:
    rows = await client.list_catalog(type_="Номенклатура", cursor=None, limit=50, q="кабель")
finally:
    await client.shutdown()
```

Selecting the tools a front-end should expose:

```python
read_only = tool_specs(surface="mcp")                        # 10 specs
everything = tool_specs(surface="mcp", include_write=True)   # 13 specs
```

Tool descriptions are Ukrainian by default; `ToolSpec.description_en` carries the English variant
used by the MCP surface.

## Requirements

Python 3.11+.

## License

MIT — see `LICENSE`.
