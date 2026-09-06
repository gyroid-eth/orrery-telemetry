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
  AGENTSTACK_PYTHON
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

# AGENTSTACK_MAIL_ENV is already the canonical env-file handoff for this
# proxy. Read it once and recover both values that may be absent from the
# caller, rather than resolving a second AgentStack env file independently.
_ags_mail_python=""
_ags_mail_bearer_token=""
if [[ -f "${AGENTSTACK_MAIL_ENV:-}" ]]; then
  _ags_mail_values="$(
    python3 - "${AGENTSTACK_MAIL_ENV}" <<'PYENV'
import pathlib
import sys

values = {"AGENTSTACK_PYTHON": "", "HTTP_BEARER_TOKEN": ""}
for raw_line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    key, separator, value = line.partition("=")
    if not separator:
        continue
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if key in values:
        values[key] = value.strip().strip("\"'")
print(values["AGENTSTACK_PYTHON"])
print(values["HTTP_BEARER_TOKEN"])
PYENV
  )" || _ags_mail_values=""
  _ags_mail_python="$(printf '%s\n' "$_ags_mail_values" | sed -n '1p')"
  _ags_mail_bearer_token="$(printf '%s\n' "$_ags_mail_values" | sed -n '2p')"
  if [[ -z "${AGENTSTACK_PYTHON:-}" && -n "$_ags_mail_python" ]]; then
    export AGENTSTACK_PYTHON="$_ags_mail_python"
  fi
fi

HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
if [[ "$HTTP_BEARER_MODE" == "disabled" ]]; then
  unset MCP_AGENT_MAIL_TOKEN
elif [[ "$HTTP_BEARER_MODE" != "auto" && "$HTTP_BEARER_MODE" != "enabled" ]]; then
  echo "invalid AGENTSTACK_MAIL_HTTP_BEARER_MODE: $HTTP_BEARER_MODE" >&2
  exit 1
elif [[ -z "${MCP_AGENT_MAIL_TOKEN:-}" ]]; then
  if [[ -n "$_ags_mail_bearer_token" ]]; then
    export MCP_AGENT_MAIL_TOKEN="$_ags_mail_bearer_token"
  else
    unset MCP_AGENT_MAIL_TOKEN
  fi
fi
unset _ags_mail_values _ags_mail_python _ags_mail_bearer_token

SOURCE_ROOT="$PLUGIN_ROOT/src"
if [[ ! -d "$SOURCE_ROOT/agentstack_codex_app" ]]; then
  SOURCE_ROOT="$(cd "$PLUGIN_ROOT/../src" && pwd)"
fi
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
exec "$PYTHON_BIN" "$SOURCE_ROOT/agentstack_codex_app/mcp_server.py"
