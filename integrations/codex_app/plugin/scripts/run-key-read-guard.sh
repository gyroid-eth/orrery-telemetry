#!/bin/bash
CORE_HOME="${AGENTSTACK_HOME:-$HOME/.agentstack}"
GUARD="$CORE_HOME/hooks/deny-llm-key-read.sh"
if [[ -f "$GUARD" ]]; then
  exec /bin/bash "$GUARD"
fi
# No usable core guard: do not disable ordinary shell commands.
PAYLOAD="$(cat)"
case "$PAYLOAD" in
  *find-generic-password*-w*|*find-generic-password*-g*|*secret-tool*lookup*)
    echo 'LOCAL_MODEL_KEY_READ_UNAVAILABLE: refresh the core installation and review /hooks.' >&2
    exit 2 ;;
esac
exit 0
