#!/bin/bash
# Installed entry point for the persistent-agent implementation.

set -euo pipefail

PROG="agentstack-persistent"
fail() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_HOME="${AGENTSTACK_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="$STACK_HOME/env.sh"
IMPLEMENTATION="$SCRIPT_DIR/agentstack-persistent.py"

[[ -r "$ENV_FILE" ]] || fail "installed environment is unavailable: $ENV_FILE"
# An inherited value must not make a missing assignment in the installed env
# look valid. install.sh selected and version-checked this interpreter; it is
# the authority for the installed entry point.
unset AGENTSTACK_PYTHON
set +e
# shellcheck disable=SC1090
source "$ENV_FILE"
env_status=$?
set -e
if [[ "$env_status" -ne 0 ]]; then
  fail "could not load installed environment: $ENV_FILE"
fi
PYTHON_BIN="${AGENTSTACK_PYTHON:-}"
[[ -n "$PYTHON_BIN" ]] || \
  fail "installed Python is unavailable; re-run install.sh"
[[ "$PYTHON_BIN" == /* ]] || \
  fail "installed Python path is not absolute; re-run install.sh"
[[ -x "$PYTHON_BIN" ]] || \
  fail "installed Python is unavailable; re-run install.sh"
[[ -r "$IMPLEMENTATION" ]] || \
  fail "installed implementation is unavailable: $IMPLEMENTATION"

exec "$PYTHON_BIN" "$IMPLEMENTATION" "$@"
