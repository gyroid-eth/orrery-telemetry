#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/codex-app-integration.sh
. "$SCRIPT_DIR/lib/codex-app-integration.sh"
agentstack_codex_app_apply "$@"
