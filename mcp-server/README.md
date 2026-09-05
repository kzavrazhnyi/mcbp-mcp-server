<!-- mcp-name: io.github.kzavrazhnyi/mcbp-ai -->

# mcbp-ai — MCP server for BAS / 1C (MCBP+)

Read your BAS (1C) database from any MCP client. Catalogs, documents, register balances and records,
and the full configuration metadata tree — exposed as MCP tools over the `MCBP_AI` HTTP service.

**Read-only by default.** None of the tools available out of the box modifies your data.

**Model-agnostic.** MCP is a vendor-neutral standard, so this server works with **Claude Desktop /
Code, ChatGPT desktop, Gemini, Microsoft and GitHub Copilot, Cursor, Windsurf, VS Code and Zed** —
it serves `tools/list` and executes `tools/call` without knowing which model is asking. The
transport is **stdio**, so the client launches the process locally; browser-based clients would
need a remote HTTP server, which this one does not expose.

> ### ⚠ This server needs a server-side component that is not in this repository
>
> `mcbp-ai` is a client for the **`MCBP_AI` HTTP service** — a BSL module (a common module plus an
> HTTP service definition) that must be installed into your BAS/1C configuration through
> Конфігуратор. **That module is licensed separately and is not part of this repository.**
>
> Without it this server starts, connects, and every route answers `404`. If you want to run it
> against your own base, contact MCBP.PLUS to obtain the `MCBP_AI` module.
>
> The Python code here — the MCP adapter and the shared BAS client — is open source and complete.

The MCBP+ configuration and the `MCBP_AI` service module are proprietary works of
**[MCBP.PLUS](https://mcbp.plus)** and are distributed separately from this repository. This MIT
licence covers the Python packages only.

## Why this exists

BAS/1C holds the data, but it is not reachable from an LLM: the platform speaks its own query
language, metadata names are inherited Russian identifiers while synonyms are Ukrainian, and a raw
OData feed is both heavy and hostile to a model (opaque errors, no self-correction path).

`MCBP_AI` solves that on the BAS side — canonical English field names in list rows, compact
responses, and errors that name the offending field verbatim so a model can fix its own call. This
server is the thin MCP adapter in front of it.

## Tools

10 read tools are always registered. Three write tools appear only when `MCP_ALLOW_WRITE` is on.

| Tool | What it does |
|---|---|
| `list_metadata` | Every object of a metadata kind (names + synonyms) — start here |
| `describe_metadata` | Full attribute tree of one object, incl. tabular sections |
| `search_catalog` | Substring search over a catalog by name |
| `filter_catalog` | Filter/sort/aggregate a catalog by any field (`agg`, `groupby`) |
| `get_documents` | Documents over a period, with filters, sorting and aggregation |
| `get_schema` | Data structure of one document type |
| `get_object` | **All** attribute values of one record + its tabular sections |
| `get_register_balance` | Accumulation-register balance (native query) |
| `get_register_records` | Raw rows of an information or accumulation register |
| `health` | Service ping — reports whether the infobase key matches |
| `write_object` *(gated)* | Create/update an object through MCBP Plus conversion rules |
| `patch_object` *(gated)* | Update header attributes of an existing object natively |
| `save_context` *(gated)* | Push a conversation turn upstream (skeleton) |

The model is expected to walk `list_metadata → describe_metadata → search/filter → get_object`.
Errors are deliberately not swallowed: an unknown field comes back as `BAD_PARAMETER` naming that
field, which is how the model corrects itself.

## Install

Requires **Python 3.11+**.

```bash
pip install mcbp
```

## Configure

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
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

### Claude Code

```bash
claude mcp add mcbp-ai --env ONEC_BASE_URL=http://host/base/hs/mcbp_ai \
                       --env ONEC_USER=ai_service \
                       --env ONEC_PASSWORD=*** \
                       -- python -m mcbp_mcp_server
```

## Environment

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ONEC_BASE_URL` | yes | — | `http(s)://<host>/<base>/hs/mcbp_ai` — see the note below |
| `ONEC_USER` | yes | — | HTTP Basic user of the BAS publication |
| `ONEC_PASSWORD` | yes | — | Password. **May be empty**, but the variable must be present |
| `ONEC_TIMEOUT` | no | `30` | Request timeout, seconds |
| `ONEC_POOL_MAX` | no | `10` | Connection pool size |
| `ONEC_VERIFY_SSL` | no | `1` | `0` for self-signed intranet publications |
| `MCP_ALLOW_WRITE` | no | `0` | `1` enables the three write tools |

### `ONEC_BASE_URL` — leave off the `/ai/v1`

The client appends `/ai/v1/...` itself, so the base URL stops at the service name:

```
http://localhost/mybase/hs/mcbp_ai          ← correct
http://localhost/mybase/hs/mcbp_ai/ai/v1/   ← also accepted (normalized away)
```

Note that the published path genuinely repeats the segment: `rootUrl` is `mcbp_ai` in `default.vrd`,
and the service's own URL templates start with `ai/v1`.

## Security

- Read-only unless you deliberately set `MCP_ALLOW_WRITE=1`. The write tools are not merely hidden
  — they are never registered, so they cannot be invoked.
- Credentials live only in the MCP client's env block; nothing is written to disk by this server.
- The BAS side adds its own checks: the publication's user rights, plus an infobase-key check. If
  the key does not match, `health` reports `key: false` and every other route answers
  `403 KEY_MISMATCH`.
- `write_object` additionally requires the MCBP Plus extension and a configured conversion rule;
  without one it returns `422 CONVERSION_NOT_CONFIGURED` and writes nothing.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every route `404`, `health` also fails | `ONEC_BASE_URL` wrong, or the `MCBP_AI` module is not installed in the base |
| Every route `403 KEY_MISMATCH` | Infobase key mismatch — check `health`, fix the publication key |
| `ONEC_PASSWORD is required` | The variable is absent. An empty value is fine; an absent one is not |
| `501 PLUS_REQUIRED` | Only `write_object` raises it — that base has no MCBP Plus |
| Model says a register is unreadable | Information registers have no `Ref`; use `get_register_records` |

## License

See `LICENSE`.
