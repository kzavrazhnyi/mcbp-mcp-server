#!/usr/bin/env bash
# Installs the mcbp-ai MCP server as a systemd service on Ubuntu (tested on 20.04).
#
#   sudo ./install-ubuntu.sh <instance> [--port 8011] [--bind 127.0.0.1] [--testpypi] [--wheels DIR]
#
# <instance> names the BAS base: it becomes /etc/mcbp-mcp/<instance>.env and the unit
# mcbp-mcp@<instance>. Re-running is safe: the venv is refreshed, an existing env file and token
# registry are never overwritten, so the script doubles as the update command.
#
# Why uv and not apt: the package needs Python 3.11+, Ubuntu 20.04 ships 3.8, and the system
# Python must not be replaced — apt and other system tools depend on it. uv puts a standalone
# 3.11 under /opt/mcbp-mcp/python, touching nothing else.
#
# --bind 127.0.0.1 (default) is for the internal-HTTPS layout: the existing web server of the
# base (Apache or nginx) terminates TLS and proxies /mcp to this port — see apache-mcp.conf.
# --bind 0.0.0.0 lets clients dial the port directly over plain http.
#
# Packages come from PyPI. --testpypi takes our two wheels from TestPyPI instead (a release being
# checked before it is published); --wheels DIR installs wheels already on disk (offline).
set -euo pipefail

MCBP_VERSION="0.2.5"
CORE_VERSION="0.1.28"
TESTPYPI="https://test.pypi.org/simple/"

PREFIX="/opt/mcbp-mcp"
ETC="/etc/mcbp-mcp"
SVC_USER="mcbp"
HERE="$(cd "$(dirname "$0")" && pwd)"

INSTANCE="${1:-}"
PORT="8011"
BIND="127.0.0.1"
USE_TESTPYPI=0
WHEELS=""

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ -n "$INSTANCE" ] && [ "${INSTANCE#-}" = "$INSTANCE" ] || usage
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --testpypi) USE_TESTPYPI=1; shift ;;
    --pypi) USE_TESTPYPI=0; shift ;;
    --wheels) WHEELS="$(cd "$2" && pwd)"; shift 2 ;;
    *) usage ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

step() { printf '\n==> %s\n' "$*"; }

step "service user and directories"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$PREFIX" "$ETC"
chown "$SVC_USER:$SVC_USER" "$PREFIX"

step "uv"
if ! command -v uv >/dev/null 2>&1; then
  command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv --version

as_svc() {
  runuser -u "$SVC_USER" -- env HOME="$PREFIX" UV_CACHE_DIR="$PREFIX/.cache/uv" \
    UV_PYTHON_INSTALL_DIR="$PREFIX/python" "$@"
}

step "Python 3.11 and venv in $PREFIX/.venv"
as_svc uv venv --allow-existing --python 3.11 --seed "$PREFIX/.venv"
PY="$PREFIX/.venv/bin/python"

step "mcbp $MCBP_VERSION + mcbp-core $CORE_VERSION"
# Our two packages come from one place and everything else from PyPI — never mixed in one
# resolution. Letting TestPyPI answer for httpx or starlette would install whatever anyone
# uploaded there under that name.
if [ "$USE_TESTPYPI" -eq 0 ] && [ -z "$WHEELS" ]; then
  as_svc uv pip install --python "$PY" "mcbp[http]==$MCBP_VERSION" "mcbp-core==$CORE_VERSION"
else
  if [ -z "$WHEELS" ]; then
    WHEELS="$PREFIX/.cache/wheels"
    as_svc rm -rf "$WHEELS"
    as_svc "$PY" -m pip download --quiet --no-deps --index-url "$TESTPYPI" --dest "$WHEELS" \
      "mcbp==$MCBP_VERSION" "mcbp-core==$CORE_VERSION"
  fi
  MCBP_WHL="$(ls "$WHEELS"/mcbp-"$MCBP_VERSION"-*.whl)"
  CORE_WHL="$(ls "$WHEELS"/mcbp_core-"$CORE_VERSION"-*.whl)"
  as_svc uv pip install --python "$PY" "$CORE_WHL" "$MCBP_WHL[http]"
fi
as_svc "$PY" -c "import mcbp_core, mcbp_mcp_server; print('installed:', mcbp_core.__version__, mcbp_mcp_server.__version__)"

ENV_FILE="$ETC/$INSTANCE.env"
TOKENS_FILE="$ETC/$INSTANCE.tokens.toml"

step "env file $ENV_FILE"
if [ -e "$ENV_FILE" ]; then
  echo "exists, left untouched"
else
  sed -e "s|^MCP_HTTP_HOST=.*|MCP_HTTP_HOST=$BIND|" \
      -e "s|^MCP_HTTP_PORT=.*|MCP_HTTP_PORT=$PORT|" \
      -e "s|^MCP_TOKENS_FILE=.*|MCP_TOKENS_FILE=$TOKENS_FILE|" \
      -e "s|^MCP_REQUIRE_AUTH=.*|MCP_REQUIRE_AUTH=1|" \
      "$HERE/mcbp-mcp.env.example" | tr -d '\r' > "$ENV_FILE"
  chown root:root "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "created — set ONEC_BASE_URL, ONEC_USER and ONEC_PASSWORD before starting"
fi

step "token registry $TOKENS_FILE"
if [ -e "$TOKENS_FILE" ]; then
  echo "exists, left untouched"
else
  TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > "$TOKENS_FILE" <<EOF
# One [[users]] entry per person: the token a client sends as "Authorization: Bearer <token>"
# and the BAS account its calls run under. Read at startup — restart the unit after editing.
# New token: $PY -c 'import secrets; print(secrets.token_urlsafe(32))'

[[users]]
token = "$TOKEN"
onec_user = "REPLACE-WITH-BAS-USER"
onec_password = ""
label = "first user"
EOF
  chown root:"$SVC_USER" "$TOKENS_FILE"
  chmod 640 "$TOKENS_FILE"
  echo "created with one generated token — put a real BAS account into it"
fi

step "systemd unit"
if [ -d /run/systemd/system ]; then
  install -m 644 "$HERE/mcbp-mcp@.service" /etc/systemd/system/mcbp-mcp@.service
  systemctl daemon-reload
  systemctl enable "mcbp-mcp@$INSTANCE" >/dev/null 2>&1
  if systemctl is-active --quiet "mcbp-mcp@$INSTANCE"; then
    systemctl restart "mcbp-mcp@$INSTANCE"
    echo "restarted mcbp-mcp@$INSTANCE"
  else
    echo "enabled, NOT started: edit $ENV_FILE and $TOKENS_FILE, then"
    echo "  sudo systemctl start mcbp-mcp@$INSTANCE"
  fi
else
  echo "no systemd here (container?) — run by hand:"
  echo "  bash -c 'set -a; . $ENV_FILE; exec runuser -u $SVC_USER -- $PREFIX/.venv/bin/mcbp-http'"
fi

cat <<EOF

Check after start:
  curl -s http://127.0.0.1:$PORT/healthz            # -> ok
  journalctl -u mcbp-mcp@$INSTANCE -n 30
EOF
