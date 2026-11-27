#!/usr/bin/env bash
set -euo pipefail

bin="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_dir="$(cd "$bin/.." && pwd)"
if [[ ! -r "$install_dir/env.sh" ]]; then
  printf 'agentstack-install-cua-driver: installed environment is missing: %s/env.sh\n' "$install_dir" >&2
  exit 1
fi
unset AGENTSTACK_PYTHON
# shellcheck source=/dev/null
source "$install_dir/env.sh"
python="${AGENTSTACK_PYTHON:-}"
if [[ "$python" != /* || ! -x "$python" ]]; then
  printf 'agentstack-install-cua-driver: installed Python is unavailable; re-run install.sh\n' >&2
  exit 1
fi
exec "$python" "$bin/lib/cua_driver_install.py" "$@" --install "$install_dir" \
  --pins "$bin/lib/cua-driver-pins.json"
