# Deploying `mcbp-ai` as a remote MCP server

Runs the same server the stdio entry point runs, over Streamable HTTP, behind nginx. Everything
here is stage 1 of the plan: **network-level protection only, no authentication**.

## 0. Prerequisite that decides everything

Claude Desktop and claude.ai do **not** connect from the user's machine — hosted surfaces are
served by Anthropic infrastructure, whose egress is `160.79.104.0/21`. The endpoint therefore
needs a public hostname and a publicly valid certificate. Claude Code is the exception: it
connects from the user's own machine, so a private endpoint still works there.

## 1. Install

```bash
sudo useradd --system --home /opt/mcbp-mcp --shell /usr/sbin/nologin mcbp
sudo mkdir -p /opt/mcbp-mcp && sudo chown mcbp:mcbp /opt/mcbp-mcp
sudo -u mcbp python3.11 -m venv /opt/mcbp-mcp/.venv
sudo -u mcbp /opt/mcbp-mcp/.venv/bin/pip install "mcbp[http]"
```

Keep this venv separate from the web backend's. Installing from PyPI is what makes the box
self-contained — no checkout, no editable installs, no source tree to keep in sync.

Before `mcbp` is on PyPI, install from the checkout instead — `mcbp-core` **first** and editable,
or the venv silently keeps a snapshot copy and later edits do nothing:

```bash
sudo -u mcbp /opt/mcbp-mcp/.venv/bin/pip install -e /srv/mcbp-mcp-server/mcbp-core
sudo -u mcbp /opt/mcbp-mcp/.venv/bin/pip install -e "/srv/mcbp-mcp-server/mcp-server[http]"
```

## 2. Configure

```bash
sudo cp mcbp-mcp.env.example /etc/mcbp-mcp.env
sudo chmod 600 /etc/mcbp-mcp.env && sudo chown root:root /etc/mcbp-mcp.env
sudo -e /etc/mcbp-mcp.env
```

Read the comments in that file — three of them mark mistakes that have already cost a live
round-trip each (`/ai/v1` suffix, absent `ONEC_PASSWORD`, empty host allow-list).

## 3. Run

```bash
sudo cp mcbp-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mcbp-mcp
curl -s localhost:8010/healthz          # -> ok  (process is up; says nothing about BAS)
journalctl -u mcbp-mcp -n 30            # startup logs the BAS base_url and the write flag
```

Then add the `location` blocks from `nginx-mcp.conf` to the TLS server block and reload nginx.

## 4. Verify from outside

`/healthz` reports the process, not the integration. The endpoint itself is the real check —
it needs a session, so a bare GET is expected to fail:

```bash
curl -sS https://mcp.example.com/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

A `serverInfo` with a non-empty `version` means the transport is live. Note that the IP
allow-list will reject this call from anywhere but Anthropic's range — comment `allow`/`deny`
out while testing, or run the curl from the server itself against `127.0.0.1:8010`.

Then register the URL as a connector and confirm the client lists 10 tools.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `307` to `/mcp/` | Not from this server — check for a proxy rewriting the path; the route matches `/mcp` exactly. |
| `421 Invalid Host header` | `MCP_HTTP_ALLOWED_HOSTS` disagrees with what nginx forwards. Keep `proxy_set_header Host $host`. |
| `400` on POST | Missing `mcp-session-id`, or `accept` without `text/event-stream`. |
| Response hangs, then arrives all at once | `proxy_buffering` is still on. |
| Every tool 403 `KEY_MISMATCH` while `health` works | Infobase key does not match the publication — a BAS-side issue; startup logs a warning for it. |
| `404` on every tool, `health` fine | `ONEC_BASE_URL` still carries the `/ai/v1` suffix. |
