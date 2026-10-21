"""`deploy/portable/payload/new_token.py`: the operator's token-issuing script.

Offline: `respx` stands in for BAS. The script is not part of the package — it ships as a file
in the portable bundle — so it is imported from the payload directory.
"""
from __future__ import annotations

import base64
import shutil
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
import respx

from mcbp_mcp_server.credentials import Credentials

PAYLOAD = Path(__file__).resolve().parents[1] / "deploy" / "portable" / "payload"
sys.path.insert(0, str(PAYLOAD))

import new_token  # noqa: E402

BASE = "http://bas.test/base/hs/mcbp_ai"
HEALTH = f"{BASE}/ai/v1/health"
USER = 'Директор-Корнієнко "Головний" \\ Зам'
PASSWORD = 'p"a\\ss Пароль'


def _bundle(tmp_path: Path, extra: str = "", tokens: str | None = None) -> Path:
    (tmp_path / "server.env").write_text(
        "# comment line\n"
        f"ONEC_BASE_URL={BASE}/ai/v1\n"
        "ONEC_USER=service\n"
        "ONEC_PASSWORD=\n"
        "MCP_HTTP_PORT=8123\n"
        "MCP_TOKENS_FILE=tokens.toml\n"
        f"{extra}",
        encoding="utf-8",
    )
    if tokens is not None:
        (tmp_path / "tokens.toml").write_text(tokens, encoding="utf-8")
    return tmp_path


def _answers(*values: str):
    it = iter(values)
    return lambda _prompt: next(it)


def test_server_env_parse_and_url(tmp_path):
    _bundle(tmp_path, "MCP_HTTP_PATH=\n# MCP_HTTP_PORT=9999\n")
    env = new_token.read_env_file(tmp_path / "server.env")
    assert env["ONEC_PASSWORD"] == ""
    assert new_token.tokens_path(env, tmp_path) == tmp_path / "tokens.toml"
    assert new_token.server_url({**env, "MCP_HTTP_PATH": "/mcp"}) == "http://127.0.0.1:8123/mcp"


def test_https_when_both_cert_settings_present():
    env = {"MCP_HTTP_SSL_CERTFILE": "certs\\a.pem", "MCP_HTTP_SSL_KEYFILE": "certs\\k.pem"}
    assert new_token.server_url(env) == "https://127.0.0.1:8010/mcp"
    assert new_token.server_url({"MCP_HTTP_SSL_CERTFILE": "x"}).startswith("http://")


def test_unset_tokens_file_is_a_config_error(tmp_path):
    with pytest.raises(new_token.ConfigError, match="MCP_TOKENS_FILE"):
        new_token.tokens_path({"MCP_TOKENS_FILE": " "}, tmp_path)


@pytest.mark.parametrize("value", [USER, PASSWORD, "", "tab\there\nnl\r\x01\x7f", "Іван Петрів"])
def test_toml_string_round_trips(value):
    assert tomllib.loads(f"k = {new_token.toml_string(value)}")["k"] == value


def test_append_preserves_content_and_loads(tmp_path):
    path = tmp_path / "tokens.toml"
    shutil.copy(PAYLOAD / "tokens.example.toml", path)
    original = path.read_bytes()
    new_token.append_entry(path, "tok-new", USER, PASSWORD, "Склад \"2\"")
    assert path.read_bytes().startswith(original)
    registry = new_token.load_registry(path)
    assert len(registry) == 3
    entry = registry.lookup("tok-new")
    assert entry.credentials == Credentials(USER, PASSWORD)
    assert entry.label == 'Склад "2"'


def test_append_creates_file_and_handles_missing_trailing_newline(tmp_path):
    path = tmp_path / "tokens.toml"
    new_token.append_entry(path, "t1", "u1", "", "")
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    new_token.append_entry(path, "t2", "u2", "", "")
    registry = new_token.load_registry(path)
    assert registry.lookup("t1").credentials == Credentials("u1", "")
    assert registry.lookup("t2").label == ""


@respx.mock
def test_rejected_credentials_write_nothing(tmp_path, capsys):
    respx.get(HEALTH).mock(return_value=httpx.Response(401, text="<html>401</html>"))
    _bundle(tmp_path)
    rc = new_token.main(tmp_path, input_fn=_answers(USER), getpass_fn=lambda _p: PASSWORD)
    assert rc == new_token.EXIT_AUTH
    assert not (tmp_path / "tokens.toml").exists()
    assert PASSWORD not in capsys.readouterr().out


@respx.mock
def test_unreachable_bas_default_answer_writes_nothing(tmp_path):
    respx.get(HEALTH).mock(side_effect=httpx.ConnectError("refused"))
    tokens = "# keep me\n"
    _bundle(tmp_path, tokens=tokens)
    rc = new_token.main(tmp_path, input_fn=_answers(USER, ""), getpass_fn=lambda _p: PASSWORD)
    assert rc == new_token.EXIT_ABORTED
    assert (tmp_path / "tokens.toml").read_text(encoding="utf-8") == tokens


@respx.mock
def test_accepted_credentials_append_and_print_command(tmp_path, capsys):
    route = respx.get(HEALTH).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    _bundle(tmp_path, tokens='[[users]]\ntoken = "old"\nonec_user = "a"\nonec_password = ""\n')
    rc = new_token.main(tmp_path, input_fn=_answers(USER, "Директор", ""),
                        getpass_fn=lambda _p: PASSWORD)
    assert rc == new_token.EXIT_OK
    sent = route.calls.last.request.headers["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(sent).decode() == f"{USER}:{PASSWORD}"
    registry = new_token.load_registry(tmp_path / "tokens.toml")
    assert len(registry) == 2
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("claude mcp add"))
    token = line.rsplit("Bearer ", 1)[1].rstrip('"')
    assert registry.lookup(token).credentials == Credentials(USER, PASSWORD)
    assert line.startswith(
        "claude mcp add --scope local --transport http mcbp-ai http://127.0.0.1:8123/mcp "
        '--header "Authorization: Bearer ')
    assert PASSWORD not in out
