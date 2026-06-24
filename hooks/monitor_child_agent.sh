#!/usr/bin/env bash
# monitor_child_agent.sh - one-shot child-agent monitor and stop escalation.
#
# Usage:
#   monitor_child_agent.sh --child NAME --risk low|medium|high \
#     --resources CSV --parent NAME [--mode startup|steady|auto]
#
# Exit codes:
#   0  - continue
#   10 - completion detected
#   11 - tmux session disappeared; completion/error unknown
#   20 - warning only
#   30 - soft stop sent (Escape or C-c)
#   40 - process group frozen with SIGSTOP
#   50 - tmux session killed

set -euo pipefail

CHILD_NAME=""
RISK_LEVEL=""
RESOURCES=""
PARENT_NAME=""
MODE="startup"

while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --child) CHILD_NAME="$2"; shift 2 ;;
    --risk) RISK_LEVEL="$2"; shift 2 ;;
    --resources) RESOURCES="$2"; shift 2 ;;
    --parent) PARENT_NAME="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    *) echo "Unknown flag / 不明なフラグ: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$CHILD_NAME" || -z "$RISK_LEVEL" || -z "$PARENT_NAME" ]]; then
  echo "Usage / 使い方: monitor_child_agent.sh --child NAME --risk low|medium|high --resources CSV --parent NAME [--mode startup|steady|auto]" >&2
  exit 1
fi

RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
STATE_DIR="$RUNTIME_DIR/child-monitor"
mkdir -p "$STATE_DIR"
STATE_KEY=$(printf '%s' "$CHILD_NAME" | shasum -a 256 | awk '{print $1}')
STATE_FILE="${STATE_DIR}/${STATE_KEY}.json"
TMUX_TARGET="$CHILD_NAME"

read_state() {
  if [[ -f "$STATE_FILE" ]]; then
    cat "$STATE_FILE"
  else
    echo '{"last_pane_hash":"","stasis_count":0,"soft_stop_count":0,"last_stop_level":"none","last_seen_ts":""}'
  fi
}

write_state() {
  local json="$1"
  local tmp_file
  tmp_file=$(mktemp "${STATE_FILE}.XXXXXX")
  printf '%s\n' "$json" > "$tmp_file"
  mv -f "$tmp_file" "$STATE_FILE"
}

jq_get() {
  local json="$1"
  local key="$2"
  python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get(sys.argv[2],''))" "$json" "$key"
}

jq_set() {
  local json="$1"
  local key="$2"
  local value="$3"
  local value_type="${4:-string}"
  python3 -c '
import json
import sys
d = json.loads(sys.argv[1])
v = sys.argv[3]
if sys.argv[4] == "int":
    v = int(v)
d[sys.argv[2]] = v
print(json.dumps(d))
' "$json" "$key" "$value" "$value_type"
}

has_dangerous_exec() {
  local exec_lines="$1"
  if echo "$exec_lines" | grep -qE 'rsync[[:space:]].*--delete([[:alnum:]_-]*)([[:space:]=]|$)'; then
    return 0
  fi
  if echo "$exec_lines" | grep -qE 'rm[[:space:]].*((-[A-Za-z]*r[A-Za-z]*f|-[A-Za-z]*f[A-Za-z]*r)|((--recursive|-r|-R).*(--force|-f)|(--force|-f).*(--recursive|-r|-R)))([[:space:]]|$)'; then
    return 0
  fi
  if echo "$exec_lines" | grep -qE 'find[[:space:]].*[[:space:]]-delete([[:space:]]|$)'; then
    return 0
  fi
  if echo "$exec_lines" | grep -qE 'git[[:space:]]+clean[[:space:]].*-[[:alnum:]]*f[[:alnum:]]*d'; then
    return 0
  fi
  return 1
}

send_escape_stop() {
  tmux send-keys -t "$TMUX_TARGET" Escape 2>/dev/null || true
}

send_interrupt_stop() {
  tmux send-keys -t "$TMUX_TARGET" C-c 2>/dev/null || true
}

freeze_process_group() {
  local pane_pid
  local pgid
  pane_pid=$(tmux list-panes -t "$TMUX_TARGET" -F '#{pane_pid}' 2>/dev/null | head -1)
  if [[ -n "$pane_pid" ]]; then
    pgid=$(ps -o pgid= -p "$pane_pid" 2>/dev/null | tr -d ' ')
    if [[ -n "$pgid" ]]; then
      kill -STOP -"$pgid" 2>/dev/null || true
      echo "[monitor] SIGSTOP sent to PGID $pgid / PGID $pgid に SIGSTOP を送信" >&2
    fi
  fi
}

if ! tmux has-session -t "$TMUX_TARGET" 2>/dev/null; then
  echo "[monitor] session '$CHILD_NAME' is missing; completion or error unknown / セッション '$CHILD_NAME' が見つかりません（完了または異常終了、要確認）" >&2
  exit 11
fi

PANE_TEXT=$(tmux capture-pane -t "$TMUX_TARGET" -p -S -40 2>/dev/null || true)
PANE_HASH=$(echo "$PANE_TEXT" | md5 -q 2>/dev/null || echo "$PANE_TEXT" | md5sum | cut -d' ' -f1)
NOW=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')

STATE=$(read_state)
LAST_HASH=$(jq_get "$STATE" "last_pane_hash")
STASIS_COUNT=$(jq_get "$STATE" "stasis_count")
SOFT_STOP_COUNT=$(jq_get "$STATE" "soft_stop_count")
LAST_STOP_LEVEL=$(jq_get "$STATE" "last_stop_level")

[[ -z "$STASIS_COUNT" ]] && STASIS_COUNT=0
[[ -z "$SOFT_STOP_COUNT" ]] && SOFT_STOP_COUNT=0

# Completion detection:
# - Claude Code may keep a UI input marker visible while still running.
# - Codex uses a `>`-like prompt marker in the REPL, so shell-return detection
#   only trusts the last non-empty line after removing known live TUI markers.
LAST_LINES=$(echo "$PANE_TEXT" | tail -5)
LAST_LINES_NORM=$(printf '%s' "$LAST_LINES" | LC_ALL=C sed $'s/\xc2\xa0/ /g')
if echo "$LAST_LINES_NORM" | grep -qE '(esc to interrupt|ctx: [0-9]+% used|Context [0-9]+% left|tokens\)|· ↓ ?[0-9]|working|thinking|press enter to continue)'; then
  LAST_NONEMPTY=""
else
  LAST_NONEMPTY=$(echo "$LAST_LINES_NORM" | grep -v '^[[:space:]]*$' | tail -1)
fi

if [[ -n "$LAST_NONEMPTY" ]] && echo "$LAST_NONEMPTY" | grep -qE '(\$ ?$|❯ ?$|% ?$)'; then
  echo "[monitor] '$CHILD_NAME' returned to a shell prompt; completion detected / '$CHILD_NAME' がシェルプロンプトに戻りました（完了検知）" >&2
  write_state "$(jq_set "$STATE" "last_seen_ts" "$NOW")"
  exit 10
fi

# Permission prompts are handled before stasis escalation because their screen
# output is intentionally stable.
if echo "$PANE_TEXT" | grep -qE '(Allow|Deny|Yes|No)\?' 2>/dev/null; then
  if [[ "$PANE_HASH" == "$LAST_HASH" && -n "$LAST_HASH" ]]; then
    echo "[monitor] repeated permission prompt detected / permission prompt の反復を検知" >&2
    STATE=$(jq_set "$STATE" "stasis_count" "0" "int")
    STATE=$(jq_set "$STATE" "last_pane_hash" "$PANE_HASH")
    STATE=$(jq_set "$STATE" "last_seen_ts" "$NOW")
    write_state "$STATE"
    exit 20
  fi
fi

# Dangerous command detection is passive by default. Enable with:
#   AGENTSTACK_MONITOR_DANGER_CHECK=1
if [[ "${AGENTSTACK_MONITOR_DANGER_CHECK:-0}" == "1" ]]; then
  EXEC_LINES=$(echo "$PANE_TEXT" | grep -E '^\s*(•\s*Ran|[$>]\s)' || true)
  if has_dangerous_exec "$EXEC_LINES"; then
    echo "[monitor] dangerous command detected; sending soft stop / 危険操作を検知したため soft-stop を送信" >&2
    send_escape_stop
    SOFT_STOP_COUNT=$((SOFT_STOP_COUNT + 1))
    STATE=$(jq_set "$STATE" "soft_stop_count" "$SOFT_STOP_COUNT" "int")
    STATE=$(jq_set "$STATE" "last_stop_level" "escape")
    STATE=$(jq_set "$STATE" "last_seen_ts" "$NOW")
    write_state "$STATE"
    exit 30
  fi
fi

if [[ "$PANE_HASH" == "$LAST_HASH" && -n "$LAST_HASH" ]]; then
  STASIS_COUNT=$((STASIS_COUNT + 1))
else
  STASIS_COUNT=0
fi

STASIS_THRESHOLD=3
[[ "$RISK_LEVEL" == "low" ]] && STASIS_THRESHOLD=5

if [[ $STASIS_COUNT -ge $STASIS_THRESHOLD ]]; then
  echo "[monitor] hang/stasis detected: ${STASIS_COUNT} unchanged checks (risk: $RISK_LEVEL) / ハング検知: ${STASIS_COUNT}回連続無変化 (risk: $RISK_LEVEL)" >&2

  if [[ "$LAST_STOP_LEVEL" == "none" || "$LAST_STOP_LEVEL" == "" ]]; then
    send_escape_stop
    STATE=$(jq_set "$STATE" "last_stop_level" "escape")
    SOFT_STOP_COUNT=$((SOFT_STOP_COUNT + 1))
    STATE=$(jq_set "$STATE" "soft_stop_count" "$SOFT_STOP_COUNT" "int")
    STATE=$(jq_set "$STATE" "stasis_count" "$STASIS_COUNT" "int")
    STATE=$(jq_set "$STATE" "last_pane_hash" "$PANE_HASH")
    STATE=$(jq_set "$STATE" "last_seen_ts" "$NOW")
    write_state "$STATE"
    exit 30
  elif [[ "$LAST_STOP_LEVEL" == "escape" ]]; then
    send_interrupt_stop
    STATE=$(jq_set "$STATE" "last_stop_level" "interrupt")
    STATE=$(jq_set "$STATE" "stasis_count" "$STASIS_COUNT" "int")
    STATE=$(jq_set "$STATE" "last_pane_hash" "$PANE_HASH")
    STATE=$(jq_set "$STATE" "last_seen_ts" "$NOW")
    write_state "$STATE"
    exit 30
  elif [[ "$LAST_STOP_LEVEL" == "interrupt" ]]; then
    freeze_process_group
    STATE=$(jq_set "$STATE" "last_stop_level" "freeze")
    STATE=$(jq_set "$STATE" "stasis_count" "$STASIS_COUNT" "int")
    STATE=$(jq_set "$STATE" "last_pane_hash" "$PANE_HASH")
    STATE=$(jq_set "$STATE" "last_seen_ts" "$NOW")
    write_state "$STATE"
    exit 40
  else
    tmux kill-session -t "$TMUX_TARGET" 2>/dev/null || true
    echo "[monitor] tmux session killed: $CHILD_NAME / tmux セッションを kill: $CHILD_NAME" >&2
    STATE=$(jq_set "$STATE" "last_stop_level" "killed")
    STATE=$(jq_set "$STATE" "last_seen_ts" "$NOW")
    write_state "$STATE"
    exit 50
  fi
fi

if [[ "$LAST_STOP_LEVEL" != "none" && "$LAST_STOP_LEVEL" != "" && "$PANE_HASH" != "$LAST_HASH" ]]; then
  echo "[monitor] output changed; resetting stop level / 出力変化を検知、停止レベルをリセット" >&2
  STATE=$(jq_set "$STATE" "last_stop_level" "none")
  STASIS_COUNT=0
fi

STATE=$(jq_set "$STATE" "stasis_count" "$STASIS_COUNT" "int")
STATE=$(jq_set "$STATE" "last_pane_hash" "$PANE_HASH")
STATE=$(jq_set "$STATE" "last_seen_ts" "$NOW")
write_state "$STATE"

echo "[monitor] $CHILD_NAME: healthy (mode: $MODE, stasis: $STASIS_COUNT) / 正常 (mode: $MODE, stasis: $STASIS_COUNT)" >&2
exit 0
