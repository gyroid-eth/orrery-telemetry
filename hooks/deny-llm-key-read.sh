#!/bin/bash
# Only the model's shell tool goes through this hook, never a launcher process.
GUARD="$(cd "$(dirname "$0")" && pwd)/deny-llm-key-read.py"
PAYLOAD="$(cat)"
if printf '%s' "$PAYLOAD" | "${AGENTSTACK_PYTHON:-python3}" "$GUARD"; then
  exit 0
else
  RC=$?
fi
[[ "$RC" == 2 ]] && exit 2
# A broken interpreter/partial install must not disable all agent shell work.
case "$PAYLOAD" in
  *find-generic-password*-w*|*find-generic-password*-g*|*secret-tool*lookup*)
    echo 'LOCAL_MODEL_KEY_READ_UNAVAILABLE: credential-read inspection failed.' >&2
    exit 2 ;;
esac
exit 0
