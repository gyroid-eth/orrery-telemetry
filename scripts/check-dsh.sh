#!/bin/bash
set -euo pipefail
check_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${AGENTSTACK_CHECK_PYTHON:-python3}" "$check_root/integrations/dsh/check.py" "$@"
