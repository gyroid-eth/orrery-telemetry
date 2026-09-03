#!/bin/bash
# PreToolUse(file reservation tools): invalidate sleeping release workers for
# paths being reserved again. Without this, an older Edit worker can release a
# brand-new reservation because the server release operation is path-based.
# Never blocks the reservation tool.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reservation-common.sh"

TOOL_INPUT=$(cat)
STATE_DIR="$RUNTIME_DIR/file_release_debounce"
[ -d "$STATE_DIR" ] || exit 0

reservation_extract_session_id "$TOOL_INPUT"
AGENT_RESULT="$(resolve_agent_name)"
AGENT_SRC="${AGENT_RESULT%%|*}"
AGENT="${AGENT_RESULT#*|}"
[ "$AGENT_SRC" != "identity-conflict" ] || exit 0
[ -n "$AGENT" ] || exit 0

QUERY_DOCUMENT="$TOOL_INPUT" QUERY_STATE_DIR="$STATE_DIR" \
    QUERY_AGENT="$AGENT" QUERY_ROOTS="$PROTECTED_ROOTS" QUERY_CWD="$(pwd)" \
    QUERY_HOME="$HOME" python3 - <<'PY' >/dev/null 2>&1 || true
import hashlib
import json
import os
import unicodedata
from pathlib import Path

try:
    document = json.loads(os.environ["QUERY_DOCUMENT"])
except Exception:
    raise SystemExit(0)
tool_input = document.get("tool_input") or {}
raw_paths = tool_input.get("paths") or []
if isinstance(raw_paths, str):
    try:
        raw_paths = json.loads(raw_paths)
    except Exception:
        raw_paths = [raw_paths]
if not isinstance(raw_paths, list):
    raise SystemExit(0)

home = os.environ["QUERY_HOME"]
cwd = os.environ["QUERY_CWD"]
roots = []
for raw_root in os.environ.get("QUERY_ROOTS", "").split(":"):
    if raw_root.startswith("~/"):
        raw_root = os.path.join(home, raw_root[2:])
    raw_root = raw_root.rstrip("/") if raw_root != "/" else raw_root
    if raw_root:
        roots.append(raw_root)

agent = os.environ["QUERY_AGENT"]
state_dir = Path(os.environ["QUERY_STATE_DIR"])
for raw_path in raw_paths:
    if not isinstance(raw_path, str) or not raw_path:
        continue
    if raw_path.startswith("/"):
        candidates = [raw_path]
    elif raw_path.startswith("~/"):
        candidates = [os.path.join(home, raw_path[2:])]
    else:
        # Reservation tool paths are project-relative. Also try cwd-relative to
        # mirror the Edit guard exactly; the two strings can differ on macOS
        # when /var and /private/var name the same temporary directory.
        candidates = [os.path.join(root, raw_path) for root in roots]
        candidates.append(os.path.join(cwd, raw_path))
    for absolute in candidates:
        for root in roots:
            if absolute == root:
                relative = os.path.basename(absolute)
            elif absolute.startswith(root + "/"):
                relative = absolute[len(root) + 1 :]
            else:
                continue
            # New workers use NFC. NFD is included so an upgraded install also
            # invalidates state armed by the older handwritten hook.
            for form in ("NFC", "NFD"):
                normalized = unicodedata.normalize(form, relative)
                key = hashlib.sha1((agent + "\0" + normalized).encode("utf-8")).hexdigest()
                try:
                    (state_dir / key).unlink()
                except OSError:
                    pass
PY

exit 0
