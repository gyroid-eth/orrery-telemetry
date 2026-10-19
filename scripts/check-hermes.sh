#!/bin/bash
set -eu
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -B "$repo_root/integrations/hermes/check.py" "$@"
