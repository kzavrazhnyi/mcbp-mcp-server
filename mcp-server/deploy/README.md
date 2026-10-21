# Deploying `mcbp-ai` as a remote MCP server

Runs the same server the stdio entry point runs, over Streamable HTTP. One process per BAS base.

Two stages, and only the first is being done now:

| | Stage 1 — internal network | Stage 2 — external server, public IP |
|---|---|---|
| Client reaches | the process, `http://`, directly | nginx, `https://` |
| Process listens on | the LAN interface | `127.0.0.1` |
| Access control | the network boundary, nothing else | TLS + token or IP allow-list in nginx |
| Files used | `mcbp-mcp@.service`, `mcbp-mcp.env.example` | the same two + `nginx-mcp.conf` |

Stage 2 is a config delta on top of stage 1, not a redeployment: same venv, same unit, same env
files with three values changed. It is written out in section 6.

## 0. Which clients can reach an internal endpoint

The rule is **where the MCP client process runs**, not which model is behind it.

- **Runs on a machine inside the network → works in stage 1.** Claude Code is the case at hand;
  so is any in-house application that speaks MCP itself, whatever model it drives (OpenAI
  included).
- **Hosted connector surfaces → cannot work in stage 1.** claude.ai, Claude Desktop and ChatGPT
  connectors do not connect from the user's machine: the provider's infrastructure does, from the
  public internet (Anthropic's egress is `160.79.104.0/21`). Such a client cannot see an internal
  host at all. That is architecture, not configuration — it changes only in stage 2.

---

# Stage 1: internal network

## 1. Install

**Quick path (Ubuntu, verified on 20.04): `install-ubuntu.sh`.** Sections 1–3 in one command —
service user, Python 3.11, venv with pinned packages, env file, token registry, systemd unit:

```bash
sudo ./install-ubuntu.sh partner              # instance name = base; --port 8011, --bind 127.0.0.1
sudo -e /etc/mcbp-mcp/partner.env             # ONEC_BASE_URL, ONEC_USER, ONEC_PASSWORD
sudo -e /etc/mcbp-mcp/partner.tokens.toml     # one [[users]] entry per person
sudo systemctl start mcbp-mcp@partner
```

Re-running it updates the packages and keeps the env file and the token registry. It exists
because Ubuntu 20.04 ships Python 3.8 and the package needs 3.11+: the script installs a
standalone 3.11 with `uv` under `/opt/mcbp-mcp/python` and leaves the system Python alone.
It writes `MCP_REQUIRE_AUTH=1` and strips CR from the env template — a Windows checkout gives it
CRLF, and `MCP_HTTP_PATH=/mcp\r` answers every request with 404 while `/healthz` stays `ok`.

The manual steps below are what the script does.

```bash
sudo useradd --system --home /opt/mcbp-mcp --shell /usr/sbin/nologin mcbp
sudo mkdir -p /opt/mcbp-mcp && sudo chown mcbp:mcbp /opt/mcbp-mcp
sudo -u mcbp python3.11 -m venv /opt/mcbp-mcp/.venv
```

The distribution is named `mcbp` and is published on **PyPI** (current: `mcbp 0.2.5`,
`mcbp-core 0.1.18`).

```bash
sudo -u mcbp /opt/mcbp-mcp/.venv/bin/pip install "mcbp[http]==0.2.5" "mcbp-core==0.1.18"
```

- **Pin both versions.** A bare `mcbp[http]` resolves to whatever is newest, which is not always
  what was tested, and an upgrade of `mcbp` alone leaves an older `mcbp-core` in place whenever it
  still satisfies the range. 0.1.0 predates the HTTP transport and gives a stdio-only server.

**Checking a release before it reaches PyPI — TestPyPI.** Every release goes to TestPyPI first.
To install that candidate, `install-ubuntu.sh --testpypi`, or by hand:

```bash
sudo -u mcbp /opt/mcbp-mcp/.venv/bin/pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  "mcbp[http]==0.2.5" "mcbp-core==0.1.18"
```

- **`--extra-index-url https://pypi.org/simple/` is mandatory.** The runtime dependencies —
  httpx, mcp, starlette, uvicorn — are not on TestPyPI, so resolution fails without it. That is
  a property of TestPyPI, not a defect of the package.
- **Dependency confusion.** With `--extra-index-url` pip may take httpx or starlette from
  TestPyPI if someone uploaded a higher version there. `install-ubuntu.sh --testpypi` avoids it:
  it downloads only our two wheels from TestPyPI and resolves everything else from PyPI.
- **TestPyPI is a sandbox.** It promises no durability and its packages can be pruned — use it to
  check a release, not to run a deployment.

**Fallback — install from the checkout.** For a machine with no access to PyPI, or to test a
change that is not released yet:

```bash
sudo -u mcbp /opt/mcbp-mcp/.venv/bin/pip install -e /srv/mcbp-mcp-server/mcbp-core
sudo -u mcbp /opt/mcbp-mcp/.venv/bin/pip install -e "/srv/mcbp-mcp-server/mcp-server[http]"
```

Here — and only here — `mcbp-core` must go **first and editable (`-e`)**. A plain `pip install`
leaves a snapshot copy in `site-packages`, and later edits to the checkout then silently do
nothing. That failure has already happened on this box, in the backend's venv (19.08).

**Keep this venv separate from the backend's** at `/opt/mcbp-ai/backend/.venv`. The two install
the same packages at different versions and share a machine, nothing else.

One venv serves every base: updating is one `pip install` plus
`sudo systemctl restart 'mcbp-mcp@*'`.

## 2. One env file per base

```bash
sudo mkdir -p /etc/mcbp-mcp
sudo cp mcbp-mcp.env.example /etc/mcbp-mcp/prod.env
sudo chmod 600 /etc/mcbp-mcp/prod.env && sudo chown root:root /etc/mcbp-mcp/prod.env
sudo -e /etc/mcbp-mcp/prod.env
```

The file name is the systemd instance name: `/etc/mcbp-mcp/prod.env` → `mcbp-mcp@prod`. Repeat
per base, changing `ONEC_BASE_URL`, the credentials and — mandatory — `MCP_HTTP_PORT`.

For stage 1 set `MCP_HTTP_HOST` to the LAN address (or `0.0.0.0`) and leave
`MCP_HTTP_ALLOWED_HOSTS` **empty**.

Read the comments in that file. Three of them mark mistakes that have already cost a live
round-trip each: the `/ai/v1` suffix, the absent `ONEC_PASSWORD`, the empty host allow-list.

## 3. Run

```bash
sudo cp mcbp-mcp@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mcbp-mcp@prod mcbp-mcp@demo
```

One unit file, any number of bases — the instance name after `@` picks the env file.

```bash
systemctl status 'mcbp-mcp@*'
journalctl -u mcbp-mcp@prod -n 30    # startup logs the BAS base_url and the write flag
```

Bring the first base up alone and verify it end to end before adding the rest.

## 4. Verify

Two checks that answer different questions.

**a) The process is alive.** Says nothing about BAS — it is a static string:

```bash
curl -s 127.0.0.1:8011/healthz     # -> ok
```

**b) The transport really speaks MCP.** A bare `GET /mcp` is expected to fail (400, no session),
so the check is an `initialize` round-trip. In stage 1 there is no TLS and no allow-list, so this
runs unchanged from the server or from a workstation — swap the host:

```bash
curl -sS http://<server-lan-address>:8011/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

A `serverInfo` with a non-empty `version` means the transport is live. It still says nothing
about BAS: the connection to the base is made lazily, on the first tool call.

**c) BAS is actually reachable.** Only a real tool call proves that. Register the server with a
client and confirm `tools/list` returns 10 tools (13 if `MCP_ALLOW_WRITE=1`), then call `health`.

## 5. Connect a client

Claude Code, from a machine on the same network, connecting as the user's OWN BAS account:

```bash
claude mcp add --transport http mcbp-prod http://<server-lan-address>:8011/mcp   --header "Authorization: Basic $(printf '%s' 'user:password' | base64)"
```

Each base is a separate entry with its own port.

**What the header does.** Every tool call from that client runs under the named BAS account: BAS
rights decide what it may read or write, and the BAS registration log names that person rather
than the service account. One process still serves one base — the header carries credentials
only, never a base selector.

**Without the header** the client falls back to the process's `ONEC_USER` (section 2). That is
still supported, and it is what stdio always does — but it puts every action under the one
service identity.

**A malformed header is rejected, never downgraded.** A non-Basic scheme, undecodable base64, or
a value that is not `user:password` answers `400 BAD_AUTHORIZATION`. A silent fallback to the
service account is exactly the failure this feature removes. A *wrong* password is not this case:
it reaches BAS and comes back as the normal BAS authentication failure.

**Where the password ends up.** In the client's own MCP config file, in clear text (base64 is not
encryption) — `~/.claude.json` for Claude Code. This differs from the web backend, where the
password is typed into a login form and lives only in that session's memory. Treat the config
file accordingly, and prefer a per-person BAS account over a shared one.

## 5a. Internal HTTPS through the publication's web server

Plain `http` over a VPN works — Claude Code and Codex both connect with a Bearer token (Codex
verified against an Ubuntu 20.04 install, 08.10.2026) — but the token and the data travel in clear
text between the VPN gateway and the server. When the BAS publication already runs HTTPS (often
with a self-signed certificate), reuse it instead of opening a new port:

1. Install with `--bind 127.0.0.1` (the default): the instance is reachable only on loopback.
2. Add the `<Location "/mcp">` block from `apache-mcp.conf` to the publication's HTTPS virtual
   host (for nginx, the `location` block from `nginx-mcp.conf`).
3. Clients use `https://<server>/mcp` and trust that certificate once on their machine. The
   certificate must carry `subjectAltName` for the address they dial.

For the instance's own connection to BAS on the same server, prefer
`ONEC_BASE_URL=http://127.0.0.1/<base>/hs/mcbp_ai` when the web server also listens on port 80
locally: the traffic never leaves the machine. Over HTTPS with a self-signed certificate, point
`SSL_CERT_FILE` in the env file at that certificate rather than setting `ONEC_VERIFY_SSL=0`.

## Security property of stage 1 — read this before opening the port

**The endpoint itself authenticates nobody.** It accepts every connection; what it does NOT do
is grant every connection the same power.

- **With an `Authorization` header** (section 5) the caller acts as their own BAS account. BAS
  rights are then the access control, and the registration log names the person. The header is
  passed through to BAS and verified there — this server never checks a password itself, so it
  cannot be tricked into accepting one.
- **Without a header** the caller acts as the process's `ONEC_USER`, and every such action looks
  identical in the BAS log. Keep that account low-privilege for exactly this reason.

`MCP_ALLOW_WRITE` remains a property of the PROCESS, not of the caller: a read-only instance
exposes the 10 read tools to everyone who reaches it, whatever account they present. Write
permission is therefore decided twice — by the instance, then by BAS rights.

What is still missing: **nothing stops an unauthorized person from reaching the port** and
falling back to the service account. Handing out "only the bases you are allowed" is a
convenience, not a control: ports are predictable, and someone inside the network who learns a
neighbouring base's URL simply adds it. So **the network boundary is still the control over WHO
connects** — the header changes what they can do once connected, not whether they may connect.
Both must be replaced before the endpoint gets a public IP.

---

# Stage 2: external server with a public IP

What changes. Nothing is rebuilt.

1. **nginx in front, terminating TLS.** Take the `location` blocks from `nginx-mcp.conf` — one
   per instance, mapping a public path to that instance's port — into the TLS `server { }` block,
   then reload. Do not drop `proxy_buffering off` or the 310 s timeouts: the first breaks SSE
   streaming, the second lets nginx cut a long tool call.
2. **`MCP_HTTP_HOST=127.0.0.1`** in every env file, then restart. The process must be reachable
   only through the proxy; leaving it on the LAN interface would publish an unauthenticated
   endpoint next to the guarded one.
3. **`MCP_HTTP_ALLOWED_HOSTS=<public hostname>`** in every env file. This turns on DNS-rebinding
   protection. It must agree with what nginx forwards, which is why the config keeps
   `proxy_set_header Host $host`.
4. **Access control, chosen deliberately** — the header of `nginx-mcp.conf` lists the options
   (per-base token in a custom header via an nginx `map`, source-IP allow-list, OAuth) and what
   each one is and is not good for. Whatever is chosen there decides WHO may connect; the BAS
   account in the caller's `Authorization` header decides what they may then do. That header is
   taken: nginx must forward it untouched and must not run `auth_basic` on these locations.
5. **A publicly valid certificate** if hosted connector surfaces are to be clients (section 0).
6. **Install from the production PyPI**, not TestPyPI (the default of `install-ubuntu.sh`).
   TestPyPI makes no durability promise; a public deployment must not depend on a sandbox index.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Connection refused from a workstation, fine from the server | `MCP_HTTP_HOST` is still `127.0.0.1`. |
| `421 Invalid Host header` | `MCP_HTTP_ALLOWED_HOSTS` is set and disagrees with the Host the client sends. In stage 1 it should be empty; behind nginx, keep `proxy_set_header Host $host`. |
| `307` to `/mcp/` | Not from this server — the route matches `/mcp` exactly. Look for a proxy rewriting the path. |
| `400` on POST | Missing `mcp-session-id`, or `accept` without `text/event-stream`. |
| `400 BAD_AUTHORIZATION` | The `Authorization` header is not `Basic <base64 user:password>`. Rebuild it; the server deliberately does not fall back to the service account. |
| Tools answer with a BAS auth error | The credentials in the header are wrong for this base. The header reached BAS — that is the base rejecting them, not the transport. |
| Actions show as the service account in the BAS log | The client sends no `Authorization` header. |
| Response hangs, then arrives all at once | `proxy_buffering` is still on (stage 2 only). |
| Two instances, one won't start | Same `MCP_HTTP_PORT` in both env files. `journalctl` shows the bind error. |
| Every tool 403 `KEY_MISMATCH` while `health` works | Infobase key does not match the publication — a BAS-side issue; startup logs a warning for it. |
| `404` on every tool, `health` fine | `ONEC_BASE_URL` still carries the `/ai/v1` suffix. |
| `No matching distribution found for httpx` (or mcp/starlette/uvicorn) | The TestPyPI install is missing `--extra-index-url https://pypi.org/simple/`. |
| `mcbp-http: command not found` | Version 0.1.0 got installed — it predates the HTTP transport. Pin `==0.2.5`. |
| Every `/mcp` request is `404 Not Found` (plain text), `/healthz` is `ok`, even without a token | The env file has CRLF line endings, so `MCP_HTTP_PATH` is `/mcp\r`. `sed -i 's/\r$//' /etc/mcbp-mcp/<base>.env` and restart. |
| Edits to the checkout have no effect | Fallback install only: `mcbp-core` went in without `-e`. |
