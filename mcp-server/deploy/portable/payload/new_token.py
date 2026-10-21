"""Issue one Bearer token for the portable bundle: verify a BAS account, append it to the
`MCP_TOKENS_FILE` registry, print the `claude mcp add` line. Launched by `new-token.cmd`.

Order matters: the credentials are checked against BAS BEFORE anything is written, and the
registry is re-read with the server's own loader afterwards — a wrong password or a broken file
must surface here, not on the client's first tool call.
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import re
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Embeddable Python runs in isolated mode (its ._pth file), so the script's own directory is
# not on sys.path and `import serve` would fail without this.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from serve import read_env_file  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_AUTH = 2
EXIT_ABORTED = 3

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n",
                 "\f": "\\f", "\r": "\\r"}


class ConfigError(Exception):
    pass


def toml_string(value: str) -> str:
    """A TOML basic string. Non-ASCII stays literal (the file is UTF-8); control characters and
    DEL must be escaped or `tomllib` rejects the file."""
    out = []
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def tokens_path(env: dict[str, str], here: Path) -> Path:
    """Relative to the bundle directory — `start.cmd` runs the server from there."""
    raw = env.get("MCP_TOKENS_FILE", "").strip()
    if not raw:
        raise ConfigError(
            "MCP_TOKENS_FILE is not set in server.env, so the server would ignore any token. "
            "Add the line MCP_TOKENS_FILE=tokens.toml to server.env and run this again."
        )
    p = Path(raw)
    return p if p.is_absolute() else here / p


def server_url(env: dict[str, str]) -> str:
    from mcbp_mcp_server.http import load_http_settings

    http = load_http_settings(env)
    tls = env.get("MCP_HTTP_SSL_CERTFILE", "").strip() and env.get(
        "MCP_HTTP_SSL_KEYFILE", "").strip()
    scheme = "https" if tls else "http"
    return f"{scheme}://127.0.0.1:{http.port}{http.path}"


def load_registry(path: Path):
    from mcbp_mcp_server.http import load_token_registry

    return load_token_registry({"MCP_TOKENS_FILE": str(path)})


def append_entry(path: Path, token: str, user: str, password: str, label: str) -> None:
    lines = ["[[users]]", f"token = {toml_string(token)}", f"onec_user = {toml_string(user)}",
             f"onec_password = {toml_string(password)}"]
    if label:
        lines.append(f"label = {toml_string(label)}")
    block = "\n".join(lines) + "\n"
    existing = path.read_bytes() if path.exists() else b""
    if existing:
        block = ("\n" if existing.endswith(b"\n") else "\n\n") + block
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(block)


async def check_credentials(env: dict[str, str], user: str, password: str) -> tuple[str, str]:
    """One GET /ai/v1/health as `user`. Returns ("ok"|"auth"|"unreachable", detail)."""
    from mcbp_core.client import ConnectionConfig, MCBPClient
    from mcbp_core.errors import AuthError, MCBPError

    from mcbp_mcp_server.server import load_settings

    s = load_settings({**env, "ONEC_USER": user, "ONEC_PASSWORD": password})
    client = MCBPClient(ConnectionConfig(base_url=s.base_url, user=user, password=password,
                                         timeout_s=s.timeout, pool_max=1, mock=False,
                                         verify=s.verify_ssl))
    await client.startup()
    try:
        await client.health()
    except AuthError:
        return "auth", ""
    except MCBPError as e:
        return "unreachable", str(e)[:200]
    finally:
        await client.shutdown()
    return "ok", ""


def _ask(prompt: str, input_fn: Callable[[str], str]) -> str:
    return input_fn(prompt).strip()


def main(here: Path = HERE, *, input_fn: Callable[[str], str] = input,
         getpass_fn: Callable[[str], str] = getpass.getpass) -> int:
    # mcbp_core logs every failed call at WARNING; the prompt below says it once, cleanly.
    logging.basicConfig(level=logging.ERROR)
    try:
        from mcbp_mcp_server.server import SettingsError
    except ImportError as e:
        print(f"ERROR: package mcbp[http] is not installed in the bundled Python ({e}).")
        return EXIT_CONFIG

    env_file = here / "server.env"
    try:
        env = read_env_file(env_file)
        path = tokens_path(env, here)
        url = server_url(env)
        if not env.get("ONEC_BASE_URL", "").strip():
            raise ConfigError("ONEC_BASE_URL is empty in server.env")
        if path.exists():
            load_registry(path)
    except FileNotFoundError:
        print(f"ERROR: {env_file} not found — keep this script next to server.env.")
        return EXIT_CONFIG
    except (ConfigError, SettingsError) as e:
        print(f"ERROR: {e}")
        return EXIT_CONFIG

    print(f"BAS:         {env['ONEC_BASE_URL']}")
    print(f"Tokens file: {path}")
    print()
    user = ""
    while not user:
        user = _ask("BAS user name: ", input_fn)
    password = getpass_fn("BAS password (input hidden, may be empty): ")

    print("Checking the account against BAS ...")
    status, detail = asyncio.run(check_credentials(env, user, password))
    if status == "auth":
        print(f"REJECTED: BAS did not accept user {user!r} with this password (401/403).")
        print("Nothing was written.")
        return EXIT_AUTH
    if status == "unreachable":
        print(f"Could not verify the account: {detail}")
        if _ask("Write the entry anyway, unverified? [y/N]: ", input_fn).lower() not in ("y", "yes"):
            print("Nothing was written.")
            return EXIT_ABORTED
    else:
        print("OK: BAS accepted the account.")

    label = _ask("Label for logs (optional, Enter to skip): ", input_fn)
    name = ""
    while not name:
        name = _ask("Server name in Claude Code [mcbp-ai]: ", input_fn) or "mcbp-ai"
        if not _SERVER_NAME_RE.match(name):
            print("Use letters, digits, '.', '_' or '-' only.")
            name = ""

    token = secrets.token_urlsafe(32)
    before = path.read_bytes() if path.exists() else None
    append_entry(path, token, user, password, label)
    try:
        registry = load_registry(path)
        entry = registry.lookup(token)
        if entry is None or entry.credentials.user != user:
            raise ConfigError("the new entry is not found after re-reading the file")
    except (ConfigError, SettingsError) as e:
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
        print(f"ERROR: the tokens file did not load after the append ({e}); change rolled back.")
        return EXIT_CONFIG

    print()
    print(f"Added [[users]] entry #{len(registry)} for {entry.display_name} to {path}.")
    print("The token is shown ONCE. Run this on the client machine:")
    print()
    print(f'claude mcp add --scope local --transport http {name} {url} '
          f'--header "Authorization: Bearer {token}"')
    print()
    if env.get("MCP_HTTP_HOST", "127.0.0.1").strip() not in ("", "127.0.0.1", "localhost"):
        print("On another machine replace 127.0.0.1 with this server's name or address.")
    print("Next: restart start.cmd — the server reads the tokens file only at startup.")
    print("Then start a NEW Claude Code session: `claude mcp add` is picked up on session start.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
