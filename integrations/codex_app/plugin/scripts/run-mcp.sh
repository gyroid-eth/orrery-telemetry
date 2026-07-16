#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$HOME/.agentstack/integrations/codex_app}"
ENV_FILE="$INSTALL_DIR/env.sh"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

if [[ -z "${MCP_AGENT_MAIL_TOKEN:-}" && -f "${AGENTSTACK_MAIL_ENV:-}" ]]; then
  export MCP_AGENT_MAIL_TOKEN
  IFS= read -r MCP_AGENT_MAIL_TOKEN < <(
    python3 - "${AGENTSTACK_MAIL_ENV}" <<'PY'
import pathlib
import sys

for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and key.strip() == "HTTP_BEARER_TOKEN":
        print(value.strip().strip("\"'"))
        break
PY
  )
fi

SOURCE_ROOT="$PLUGIN_ROOT/src"
if [[ ! -d "$SOURCE_ROOT/agentstack_codex_app" ]]; then
  SOURCE_ROOT="$(cd "$PLUGIN_ROOT/../src" && pwd)"
fi
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
exec "$PYTHON_BIN" "$SOURCE_ROOT/agentstack_codex_app/mcp_server.py"
