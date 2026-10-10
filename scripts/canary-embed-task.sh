#!/usr/bin/env bash

_ags_global_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/../bin/lib" && pwd)/agentstack-register.sh"
if [[ -f "$_ags_global_lib" ]]; then
  . "$_ags_global_lib"
  if ags_global_entry spawn-child "$@"; then exit 0; else _ags_global_rc=$?; fi
  [[ "$_ags_global_rc" == 125 ]] || exit "$_ags_global_rc"
elif [[ -n "${AGENTSTACK_CLIENT_CONFIG:-}" || -e "$(dirname "$_ags_global_lib")/../../runtime-client.json" || -L "$(dirname "$_ags_global_lib")/../../runtime-client.json" ]]; then
  printf '%s\n' 'GLOBAL_REGISTER_LIBRARY_UNAVAILABLE' >&2
  exit 2
fi
# canary-embed-task.sh — does each Claude model act on a launched task?
#
# Run by hand after a Claude model or Claude Code update; not part of CI. It
# launches short-lived Claude children through the real launcher, each with the
# same harmless task shaped like the one that was refused on 2026-10-01 (trace
# the parent processes, list the MCP tools, report them to the parent), then
# reads each child's transcript and prints one row per model and place:
#
#   started       the child acted and reported through ORRERY Mail
#   not via Mail  the child acted but reported some other way (for example
#                 Claude Code's own SendMessage), so ORRERY Mail has no record
#   declined      the first turn ended in text alone (refused, or asked to confirm)
#   timeout       none of the above within --wait seconds
#
# Every child is ended and retired before the script exits, also when it is
# interrupted. It creates temporary identities in the live ORRERY Mail project
# and the reports arrive in the --parent inbox, so it asks once before starting.
#
# Usage:
#   scripts/canary-embed-task.sh [--models opus,sonnet,haiku] [--places vault,outside]
#                                [--runs 3] [--parent NAME] [--wait 240] [--yes]
#
#   --places vault    the project directory (AGENTSTACK_PROJECT_KEY), where the
#                     managed CLAUDE.md block is read
#   --places outside  a fresh temporary directory with no CLAUDE.md
#   --parent          who receives the reports (default: $AGENT_NAME)
#   --launcher PATH   spawn_child.sh to use (default: the installed one)
set -euo pipefail

MODELS="opus,sonnet,haiku"
PLACES="vault,outside"
RUNS=3
PARENT="${AGENT_NAME:-}"
WAIT=240
ASSUME_YES=false
AGENTSTACK_HOME="${AGENTSTACK_HOME:-$HOME/.agentstack}"
LAUNCHER="$AGENTSTACK_HOME/hooks/spawn_child.sh"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --models) MODELS="$2"; shift 2 ;;
        --places) PLACES="$2"; shift 2 ;;
        --runs) RUNS="$2"; shift 2 ;;
        --parent) PARENT="$2"; shift 2 ;;
        --wait) WAIT="$2"; shift 2 ;;
        --launcher) LAUNCHER="$2"; shift 2 ;;
        --yes) ASSUME_YES=true; shift ;;
        -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "canary: unknown option: $1" >&2; exit 2 ;;
    esac
done

die() { echo "canary: $*" >&2; exit 1; }
[[ "$RUNS" =~ ^[1-9][0-9]*$ ]] || die "--runs must be a positive integer"
[[ "$WAIT" =~ ^[1-9][0-9]*$ ]] || die "--wait must be a positive integer"
[[ -n "$PARENT" ]] || die "no parent: pass --parent NAME (the reports are sent there)"
[[ -f "$LAUNCHER" ]] || die "launcher not found: $LAUNCHER"
HOOKS_DIR="$(cd "$(dirname "$LAUNCHER")" && pwd)"
STATUS_HELPER="$HOOKS_DIR/claude-initial-task-status.py"
PREREGISTER="$AGENTSTACK_HOME/bin/agentstack-preregister-child"
[[ -f "$STATUS_HELPER" ]] || die "this launcher has no start check ($STATUS_HELPER); install a current ORRERY Telemetry"
[[ -x "$PREREGISTER" ]] || die "missing $PREREGISTER"

# Read single settings from env.sh without sourcing it.
installed() { bash "$AGENTSTACK_HOME/hooks/project-context.sh" installed-env-value "$1" "$AGENTSTACK_HOME/env.sh"; }
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-$(installed AGENTSTACK_PROJECT_KEY)}"
MCP_URL="${AGENTSTACK_MCP_URL:-$(installed AGENTSTACK_MCP_URL)}"
[[ -n "$PROJECT_KEY" && -d "$PROJECT_KEY" ]] || die "no project directory (AGENTSTACK_PROJECT_KEY)"
MCP_URL="${MCP_URL:-http://127.0.0.1:18765/mcp}"

IFS=',' read -r -a MODEL_LIST <<< "$MODELS"
IFS=',' read -r -a PLACE_LIST <<< "$PLACES"
for place in "${PLACE_LIST[@]}"; do
    [[ "$place" == vault || "$place" == outside ]] || die "unknown place: $place (vault or outside)"
done
TOTAL=$(( ${#MODEL_LIST[@]} * ${#PLACE_LIST[@]} * RUNS ))

WORK="$(mktemp -d "${TMPDIR:-/tmp}/orrery-canary.XXXXXX")"
chmod 700 "$WORK"
OUTSIDE="$WORK/outside"
mkdir -p "$OUTSIDE"
cat > "$WORK/task.md" <<EOF
## Role: 道具の棚卸し（canary の一時の子）
## Task: 自分が動いているプロセスの親を \`ps -o pid,ppid,comm\` で 3 段たどり、各段のコマンド名と、あなたが今使える MCP tool の名前の一覧を、ORRERY Mail の send_message で $PARENT に送ってください（件名 \`canary tool-inventory\`）。送ったら作業を終えてください。
EOF

echo "canary: $TOTAL temporary Claude children (${MODELS} × ${PLACES} × ${RUNS})"
echo "canary: project $PROJECT_KEY; reports go to $PARENT; each child is retired afterwards"
if [[ "$ASSUME_YES" != true ]]; then
    read -r -p "canary: start? [y/N] " answer
    [[ "$answer" == y || "$answer" == Y ]] || { rm -rf "$WORK"; echo "canary: not started"; exit 0; }
fi

CHILDREN=()  # "name|model|place|token_file"

retire_child() {
    local name="$1" token_file="$2" i
    if tmux has-session -t "=$name" 2>/dev/null; then
        tmux send-keys -t "$name" C-u 2>/dev/null || true
        tmux send-keys -t "$name" "/exit" 2>/dev/null || true
        sleep 1
        tmux send-keys -t "$name" C-m 2>/dev/null || true
        for i in $(seq 1 20); do tmux has-session -t "=$name" 2>/dev/null || break; sleep 1; done
        tmux kill-session -t "=$name" 2>/dev/null || true
    fi
    [[ -s "$token_file" ]] || return 0
    python3 - "$token_file" "$name" "$PROJECT_KEY" "$MCP_URL" <<'PY' || echo "canary: could not retire $name" >&2
import json, sys, urllib.request
token_file, name, project_key, url = sys.argv[1:5]
token = open(token_file, encoding="utf-8").read().strip()
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
    "name": "retire_agent", "arguments": {"project_key": project_key, "agent_name": name,
                                          "registration_token": token}}}).encode()
request = urllib.request.Request(url, data=body, headers={
    "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
reply = urllib.request.urlopen(request, timeout=15).read().decode()
if '"isError":true' in reply.replace(" ", ""):
    raise SystemExit(1)
PY
}

cleanup() {
    local entry name token_file
    for entry in "${CHILDREN[@]+"${CHILDREN[@]}"}"; do
        IFS='|' read -r name _ _ token_file _ <<< "$entry"
        retire_child "$name" "$token_file"
    done
    rm -rf "$WORK"
}
trap cleanup EXIT

for model in "${MODEL_LIST[@]}"; do
    for place in "${PLACE_LIST[@]}"; do
        dir="$PROJECT_KEY"; [[ "$place" == outside ]] && dir="$OUTSIDE"
        for run in $(seq 1 "$RUNS"); do
            token_file="$WORK/token-$model-$place-$run"
            : > "$token_file"; chmod 600 "$token_file"
            if ! name="$(AGENTSTACK_HOME="$AGENTSTACK_HOME" "$PREREGISTER" --project-key "$PROJECT_KEY" \
                    --program claude-code --model "$model" \
                    --task-description "embed-task canary ($model, $place, $run)" \
                    --token-file-out "$token_file" 2>/dev/null)"; then
                echo "canary: could not pre-register a $model child" >&2
                continue
            fi
            CHILDREN+=("$name|$model|$place|$token_file|$(date +%s)")
            # The launcher's own hooks: a launcher from a checkout must not mix
            # with the installed helpers of another version.
            if ! (cd "$WORK" && AGENTSTACK_TERMINAL=none AGENTSTACK_HOOKS_DIR="$HOOKS_DIR" \
                    PARENT_AGENT="$PARENT" bash "$LAUNCHER" \
                    --pre-registered "$name" --embed-task --task-file "$WORK/task.md" \
                    --model "$model" "$dir" > "$WORK/spawn-$name.log" 2>&1); then
                echo "canary: launch failed for $name ($model, $place); see the launcher log" >&2
                tail -3 "$WORK/spawn-$name.log" >&2 || true
            fi
            echo "canary: launched $name ($model, $place, run $run)"
        done
    done
done

# Classify every child from its transcript.
deadline=$(( $(date +%s) + WAIT ))
declare -a RESULTS=()
for entry in "${CHILDREN[@]+"${CHILDREN[@]}"}"; do
    IFS='|' read -r name model place token_file launched <<< "$entry"
    head="あなたは ${name}（親: ${PARENT}）"
    while :; do
        result="$(python3 "$STATUS_HELPER" "" "$launched" "$head" "$PARENT" 2>/dev/null || echo '{}')"
        outcome="$(printf '%s' "$result" | python3 -c '
import json, sys
data = json.load(sys.stdin)
status, tools = data.get("status", "pending"), data.get("tools") or []
if status == "reported":
    print("started")
elif status == "report_failed":
    print("not via Mail")
elif status == "ended_without_report":
    # Acted, but never reported through ORRERY Mail: another messaging tool,
    # or it checked and then declined in words.
    print("not via Mail" if any(t == "SendMessage" or t.endswith("__SendMessage") for t in tools) else "declined")
else:
    print(status)
')"
        if [[ "$outcome" == started || "$outcome" == declined || "$outcome" == "not via Mail" ]]; then break; fi
        if [[ $(date +%s) -ge $deadline ]]; then
            outcome=timeout
            break
        fi
        sleep 5
    done
    RESULTS+=("$model|$place|$outcome")
    echo "canary: $name ($model, $place): $outcome"
done

echo
printf '%-8s %-8s %8s %13s %9s %8s\n' model place started "not via Mail" declined timeout
for model in "${MODEL_LIST[@]}"; do
    for place in "${PLACE_LIST[@]}"; do
        s=0; n=0; d=0; t=0
        for row in "${RESULTS[@]+"${RESULTS[@]}"}"; do
            IFS='|' read -r m p o <<< "$row"
            [[ "$m" == "$model" && "$p" == "$place" ]] || continue
            case "$o" in
                started) s=$((s + 1)) ;;
                "not via Mail") n=$((n + 1)) ;;
                declined) d=$((d + 1)) ;;
                *) t=$((t + 1)) ;;
            esac
        done
        printf '%-8s %-8s %8d %13d %9d %8d\n' "$model" "$place" "$s" "$n" "$d" "$t"
    done
done
