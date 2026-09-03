#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$HOME/.agentstack/integrations/codex_app}"
ENV_FILE="$INSTALL_DIR/env.sh"

# The installed env.sh belongs to the Codex App bridge and uses plain `export`,
# so sourcing it would clobber values the CALLER set. spawn_child.sh launches
# this runner per child with that child's identity, project and endpoint in the
# environment; letting a machine-wide file win would silently bind the child to
# the bridge's project and endpoint instead of its own. Caller wins: snapshot
# what was passed in, source the file for anything still unset, restore.
_AGS_CALLER_KEYS=(
  AGENTSTACK_PROXY_AGENT_NAME
  AGENTSTACK_PROXY_TOKEN_FILE
  AGENTSTACK_PROXY_PROGRAM
  AGENTSTACK_PROJECT_KEY
  AGENTSTACK_MCP_URL
  AGENTSTACK_MAIL_ENV
  AGENTSTACK_MAIL_HTTP_BEARER_MODE
  AGENTSTACK_RUNTIME_DIR
  AGENTSTACK_CODEX_APP_RUNTIME_DIR
  MCP_AGENT_MAIL_TOKEN
)
_ags_saved_names=()
_ags_saved_values=()
for _ags_key in "${_AGS_CALLER_KEYS[@]}"; do
  if [[ -n "${!_ags_key:-}" ]]; then
    _ags_saved_names+=("$_ags_key")
    _ags_saved_values+=("${!_ags_key}")
  fi
done

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

_ags_i=0
while [[ $_ags_i -lt ${#_ags_saved_names[@]} ]]; do
  export "${_ags_saved_names[$_ags_i]}=${_ags_saved_values[$_ags_i]}"
  _ags_i=$((_ags_i + 1))
done
unset _ags_key _ags_i _ags_saved_names _ags_saved_values _AGS_CALLER_KEYS

HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
if [[ "$HTTP_BEARER_MODE" == "disabled" ]]; then
  unset MCP_AGENT_MAIL_TOKEN
elif [[ "$HTTP_BEARER_MODE" != "auto" && "$HTTP_BEARER_MODE" != "enabled" ]]; then
  echo "invalid AGENTSTACK_MAIL_HTTP_BEARER_MODE: $HTTP_BEARER_MODE" >&2
  exit 1
elif [[ -z "${MCP_AGENT_MAIL_TOKEN:-}" && -f "${AGENTSTACK_MAIL_ENV:-}" ]]; then
  # A service env without HTTP_BEARER_TOKEN (bearer disabled) is normal: the
  # proxy then authenticates with the owner token only. Under `set -e`, a
  # `read` that hits EOF returned 1 and ended the proxy before it answered
  # initialize, which Codex reported as "connection closed" (2026-09-03).
  _ags_bearer_token="$(
    python3 - "${AGENTSTACK_MAIL_ENV}" <<'PY'
import pathlib
import sys

for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and key.strip() == "HTTP_BEARER_TOKEN":
        print(value.strip().strip("\"'"))
        break
PY
  )" || _ags_bearer_token=""
  if [[ -n "$_ags_bearer_token" ]]; then
    export MCP_AGENT_MAIL_TOKEN="$_ags_bearer_token"
  else
    unset MCP_AGENT_MAIL_TOKEN
  fi
  unset _ags_bearer_token
fi

SOURCE_ROOT="$PLUGIN_ROOT/src"
if [[ ! -d "$SOURCE_ROOT/agentstack_codex_app" ]]; then
  SOURCE_ROOT="$(cd "$PLUGIN_ROOT/../src" && pwd)"
fi
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
exec "$PYTHON_BIN" "$SOURCE_ROOT/agentstack_codex_app/mcp_server.py"
