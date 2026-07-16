#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFERRED_INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$INFERRED_INSTALL_DIR}"
ENV_FILE="$INSTALL_DIR/env.sh"
[[ -f "$ENV_FILE" ]] || {
  echo "missing Codex App integration env: $ENV_FILE" >&2
  exit 1
}
# shellcheck disable=SC1090
. "$ENV_FILE"

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

PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
export PYTHONPATH="$INSTALL_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -m agentstack_codex_app.daemon
