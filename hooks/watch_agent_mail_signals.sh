#!/usr/bin/env bash
# watch_agent_mail_signals.sh - Watch ORRERY Mail signal files and inject
# prompts into the target agent's tmux session.
#
# Uses macOS fswatch (or fallback polling) to monitor signal files.
# When a signal file is created/updated, reads the JSON metadata and
# sends a notification prompt to the agent's Claude Code tmux session.
#
# Usage:
#   bash ~/.agentstack/hooks/watch_agent_mail_signals.sh &
#   # Or run in a dedicated tmux session:
#   tmux new-session -d -s mail-watcher 'bash ~/.agentstack/hooks/watch_agent_mail_signals.sh'

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_CONTEXT_HELPER="$SCRIPT_DIR/project-context.sh"
if [[ -f "$PROJECT_CONTEXT_HELPER" ]]; then
    # shellcheck disable=SC1090
    source "$PROJECT_CONTEXT_HELPER"
fi

MAIL_HOME="${AGENTSTACK_MAIL_HOME:-$HOME/.agentstack/mail}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-$MAIL_HOME/signals}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:18765/mcp}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
POLL_INTERVAL=2  # seconds (fallback if fswatch unavailable)
WATCHER_LOCK_DIR="${AGENTSTACK_MAIL_WATCHER_LOCK_DIR:-/tmp/orrery-mail-watcher.lock}"
WATCHER_PIDFILE="${AGENTSTACK_MAIL_WATCHER_PIDFILE:-${WATCHER_LOCK_DIR}/watcher.pid}"
WATCHER_HEARTBEAT="${AGENTSTACK_MAIL_WATCHER_HEARTBEAT:-${WATCHER_LOCK_DIR}/heartbeat}"
WATCH_FIFO=""
WATCH_BACKEND_PID=""
LOCK_ACQUIRED=0

# 通知として割り込ませる下限。low|normal|high|urgent。既定 low = 従来どおり全通。
NOTIFY_MIN_IMPORTANCE="${AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE:-low}"

# importance を順序に写す。未知の値は normal 扱い: ORRERY Mail は importance を
# 自由文字列として受けるので、知らない語を落とすと配送が黙って止まる。
importance_rank() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        low) printf '0' ;;
        high) printf '2' ;;
        urgent) printf '3' ;;
        *) printf '1' ;;
    esac
}

importance_at_least() {
    [ "$(importance_rank "$1")" -ge "$(importance_rank "$2")" ]
}

# 2026-05-20 SilverEuler 設計の non-destructive 通知パイプライン:
#   - signal file は server-owned dirty bit (rename/delete しない)
#   - notify-state.json で「(agent, msg_id) → 配送結果」を永続キャッシュ
#   - notify-locks/ で短命 lease lock (重複 inject 防止、watcher/daemon dual で必須)
#   - 失敗時は state に記録するだけで signal は残し、後で再試行可能
STATE_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
STATE_FILE="${STATE_DIR}/notify-state.json"
LEASE_DIR="${STATE_DIR}/notify-locks"
SCAN_INTERVAL=30      # periodic scan で取りこぼし救済
RETRY_COOLDOWN=30     # 同一 (agent, msg) の再試行間隔
LEASE_TTL=120         # lease 失効時間（古い lease は強制取り直し）
# 2026-05-22 JollyTesla hang 根治: tmux 呼び出しは server stall 時に同期ブロック
# し、単一スレッドの本体ループ全体を凍結させる (本日 game2 で2回 hang)。全 tmux
# 呼び出しを run_to で時間制限し、配送本体は background worker に切り離す。
TMUX_TIMEOUT="${TMUX_TIMEOUT:-5}"   # tmux 1 コールの上限秒
MAX_WORKERS="${MAX_WORKERS:-12}"    # 同時 delivery worker 上限 (server stall 時の暴走防止)
mkdir -p "$STATE_DIR" "$LEASE_DIR"

log() { echo "[mail-watcher $(date '+%H:%M:%S')] $*"; }

watcher_normalize_project_key() {
    local project_key="${1:-}"
    if type agentstack_normalize_project_key >/dev/null 2>&1; then
        agentstack_normalize_project_key "$project_key"
    else
        # Missing-helper mode cannot safely infer path aliases. Preserve the
        # literal so mismatches remain fail-closed rather than guessed.
        printf '%s\n' "$project_key"
    fi
}

# run_to <secs> <cmd...> : cmd を最大 secs 秒で実行。超過したら TERM→KILL で
# 強制終了し非ゼロを返す。macOS には timeout(1)/gtimeout が無く、watcher は
# bash 3.2 で動くため、background + watchdog で自前実装する。これにより
# tmux のブロックが本体ループへ波及しなくなる。
run_to() {
    local secs="$1"; shift
    "$@" &
    local cmd_pid=$!
    # watchdog: fd を /dev/null へ向ける ($(run_to ...) が watchdog の stdout 保持で
    # ハングしないように)。cmd 側は呼び出し元の stdout を保持し続ける。
    ( sleep "$secs"; kill -TERM "$cmd_pid" 2>/dev/null; sleep 0.3; kill -KILL "$cmd_pid" 2>/dev/null ) >/dev/null 2>&1 &
    local wd_pid=$!
    local rc=0
    wait "$cmd_pid" 2>/dev/null || rc=$?
    kill "$wd_pid" 2>/dev/null || true
    wait "$wd_pid" 2>/dev/null || true
    return "$rc"
}

read_signal_meta() {
    "$PYTHON_BIN" - "$1" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}
msg = data.get("message") or {}
st = os.stat(path)
print(msg.get("id") or "")
print(msg.get("from") or "unknown")
print((msg.get("subject") or "(no subject)")[:80])
print(msg.get("importance") or "normal")
print(int(st.st_mtime))
# A short body is included by newer mail servers. Keep it on one line before
# passing it to tmux: an embedded newline would submit an incomplete prompt.
snippet = (msg.get("body_snippet") or "").replace("\r", " ").replace("\n", " ⏎ ")
print(snippet[:500])
print("1" if msg.get("body_truncated") else "0")
print(data.get("project_key") or "")
print(data.get("project") or "")
PY
}

resolve_signal_project_key() {
    local project_slug="$1"
    [[ -n "$project_slug" ]] || return 1
    "$PYTHON_BIN" - "$MCP_URL" "$project_slug" <<'PY' 2>/dev/null
import json
import os
import sys
import urllib.request

url, slug = sys.argv[1:3]
payload = json.dumps({
    "jsonrpc": "2.0",
    "id": "agentstack-watcher-project",
    "method": "resources/read",
    "params": {"uri": f"resource://project/{slug}"},
}).encode()
headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
token = os.environ.get("MCP_AGENT_MAIL_TOKEN", "").strip()
if token:
    headers["Authorization"] = f"Bearer {token}"
request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
with urllib.request.urlopen(request, timeout=3) as response:
    raw = response.read().decode("utf-8", errors="replace")
for line in raw.splitlines():
    if line.startswith("data:"):
        raw = line[5:].strip()
        break
body = json.loads(raw)
result = body.get("result") or {}
contents = result.get("contents") or []
if not contents:
    raise SystemExit(1)
text = contents[0].get("text") or "{}"
data = json.loads(text)
human_key = data.get("human_key")
if not isinstance(human_key, str) or not human_key:
    raise SystemExit(1)
print(human_key)
PY
}

durable_agent_project_key() {
    "$PYTHON_BIN" - "$STATE_DIR" "$1" <<'PY' 2>/dev/null
import json
import pathlib
import re
import sys

runtime = pathlib.Path(sys.argv[1])
name = sys.argv[2]
projects = set()

def project_key(value):
    if not isinstance(value, str) or not value:
        return ""
    path = pathlib.Path(value)
    if path.is_absolute() or path.is_dir():
        try:
            return str(path.resolve())
        except (OSError, RuntimeError):
            return ""
    return value

token_key = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
sidecar = runtime / f"agent_token_{token_key}.project"
try:
    value = sidecar.read_text(encoding="utf-8").strip()
    if value:
        projects.add(project_key(value))
except OSError:
    pass

for path in (
    runtime / "child-agents" / f"{name}.json",
    runtime / "name-bindings" / f"{name.replace('-', '').lower()}.json",
):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if path.parent.name == "name-bindings":
        bound_name = data.get("agent_name")
        if not isinstance(bound_name, str) or bound_name.replace("-", "").lower() != name.replace("-", "").lower():
            continue
    value = data.get("project_key")
    if isinstance(value, str) and value:
        projects.add(project_key(value))

index_dir = runtime / "session_index"
if index_dir.is_dir():
    for path in index_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("agent_name") == name:
            value = data.get("project_key")
            if isinstance(value, str) and value:
                projects.add(project_key(value))

projects.discard("")

if len(projects) == 1:
    print(projects.pop())
elif len(projects) > 1:
    print("__AGENTSTACK_AMBIGUOUS__")
PY
}

tmux_session_env_value() {
    local session="$1" variable="$2" raw=""
    raw="$(run_to "$TMUX_TIMEOUT" tmux show-environment -t "=$session" "$variable" 2>/dev/null || true)"
    case "$raw" in
        "$variable="*) printf '%s\n' "${raw#"$variable="}" ;;
    esac
}

session_context_is_consistent() {
    local signal_project="$1" session="$2" durable_project="$3"
    local session_repository="$4" session_work_dir="$5" session_cwd="$6"
    local reference_repository="" candidate_repository="" value=""

    signal_project="$(watcher_normalize_project_key "$signal_project")"
    if [[ -n "$durable_project" && "$durable_project" != "__AGENTSTACK_AMBIGUOUS__" ]]; then
        durable_project="$(watcher_normalize_project_key "$durable_project")"
    fi

    [[ -z "$durable_project" || "$durable_project" == "$signal_project" ]] || return 1
    if ! type agentstack_repository_key >/dev/null 2>&1; then
        # No repository-bearing tuple may be accepted without the shared,
        # selector-sanitizing resolver. Pure legacy non-Git evidence remains
        # usable when its durable project identity agrees.
        [[ ! -d "$signal_project" && ! -d "$session_repository" ]] || return 1
        return 0
    fi
    for value in "$signal_project" "$session_repository" "$session_work_dir" "$session_cwd"; do
        [[ -n "$value" && -d "$value" ]] || continue
        candidate_repository="$(agentstack_repository_key "$value" 2>/dev/null || true)"
        [[ -n "$candidate_repository" ]] || continue
        if [[ -n "$reference_repository" && "$candidate_repository" != "$reference_repository" ]]; then
            return 1
        fi
        reference_repository="$candidate_repository"
    done

    # For a non-Git pinned tuple, a live cwd outside its established work_dir
    # is contradictory even though neither path has a Git repository identity.
    if [[ -n "$session_work_dir" && -d "$session_work_dir" && -n "$session_cwd" && -d "$session_cwd" ]]; then
        local work_physical="" cwd_physical=""
        work_physical="$(agentstack_physical_dir "$session_work_dir" 2>/dev/null || true)"
        cwd_physical="$(agentstack_physical_dir "$session_cwd" 2>/dev/null || true)"
        if [[ -n "$work_physical" && -n "$cwd_physical" ]]; then
            case "$cwd_physical" in
                "$work_physical"|"$work_physical"/*) ;;
                *) return 1 ;;
            esac
        fi
    fi
    return 0
}

state_should_attempt() {
    "$PYTHON_BIN" - "$STATE_FILE" "$1" "$2" "$3" "$RETRY_COOLDOWN" <<'PY'
import json, sys, time
path, project, agent, msg_key = sys.argv[1:5]
cooldown = int(sys.argv[5])
compound = f"{project}:{agent}:{msg_key}"
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}
entry = data.get(compound)
if not entry:
    sys.exit(0)
if entry.get("last_result") == "success":
    sys.exit(1)
last_attempt = int(entry.get("last_attempt_epoch", 0) or 0)
if int(time.time()) - last_attempt >= cooldown:
    sys.exit(0)
sys.exit(1)
PY
}

state_mark_result() {
    "$PYTHON_BIN" - "$STATE_FILE" "$1" "$2" "$3" "$4" "$5" <<'PY'
import fcntl, json, sys, time
from pathlib import Path
path, project, agent, msg_key, result, source = sys.argv[1:7]
compound = f"{project}:{agent}:{msg_key}"
p = Path(path)
p.parent.mkdir(parents=True, exist_ok=True)
# 配送 worker を background 化したため複数プロセスが同時に state を read-modify-
# write する。flock で直列化しないと key の lost-update が起きる (success が消え
# て二重 inject)。lock は同一 fd close / プロセス死で自動解放されるので hang し
# ない。
lock = open(str(p) + ".lock", "w")
fcntl.flock(lock, fcntl.LOCK_EX)
try:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    now = int(time.time())
    entry = data.get(compound, {})
    entry.update({
        "agent": agent,
        "project_key": project,
        "msg_key": msg_key,
        "last_result": result,
        "last_attempt_epoch": now,
        "last_attempt_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source": source,
    })
    if result == "success":
        entry["last_success_epoch"] = now
        entry["last_success_ts"] = entry["last_attempt_ts"]
    data[compound] = entry
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)  # atomic swap so a reader never sees a half-written file
finally:
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
PY
}

acquire_delivery_lease() {
    local project="$1" agent="$2" msg_key="$3" scope
    scope="$("$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])' "$project")"
    local lease_path="${LEASE_DIR}/${scope}-${agent}-${msg_key}.lock"
    local now
    now=$(date +%s)

    if mkdir "$lease_path" 2>/dev/null; then
        printf '%s\n' "$now" > "$lease_path/ts"
        return 0
    fi

    local ts="0"
    [[ -f "$lease_path/ts" ]] && ts=$(<"$lease_path/ts")
    if (( now - ts > LEASE_TTL )); then
        rm -rf "$lease_path" 2>/dev/null || true
        if mkdir "$lease_path" 2>/dev/null; then
            printf '%s\n' "$now" > "$lease_path/ts"
            return 0
        fi
    fi
    return 1
}

release_delivery_lease() {
    local project="$1" agent="$2" msg_key="$3" scope
    scope="$("$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])' "$project")"
    rm -rf "${LEASE_DIR}/${scope}-${agent}-${msg_key}.lock" 2>/dev/null || true
}

is_pid_running() {
    local pid="${1:-}"
    [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

write_heartbeat() {
    mkdir -p "$(dirname "$WATCHER_HEARTBEAT")"
    : > "$WATCHER_HEARTBEAT"
}

acquire_lock() {
    local existing_pid=""

    if mkdir "$WATCHER_LOCK_DIR" 2>/dev/null; then
        mkdir -p "$(dirname "$WATCHER_PIDFILE")"
        printf '%s\n' "$$" > "$WATCHER_PIDFILE"
        LOCK_ACQUIRED=1
        write_heartbeat
        return 0
    fi

    if [[ -f "$WATCHER_PIDFILE" ]]; then
        existing_pid=$(<"$WATCHER_PIDFILE")
    fi

    if is_pid_running "$existing_pid"; then
        log "Watcher already running (PID ${existing_pid}); exiting duplicate instance"
        exit 0
    fi

    log "Stale watcher lock detected; taking ownership"
    # A watcher that died without running its EXIT trap leaves the heartbeat
    # (and possibly the pidfile) inside the lock dir. `rmdir` refuses a
    # non-empty dir, the following `mkdir` then fails, and under `set -e` the
    # process exits 1 - which the service manager restarts every few seconds,
    # forever. Clear the whole dir so takeover cannot wedge on leftovers.
    rm -f "$WATCHER_PIDFILE" "$WATCHER_HEARTBEAT"
    rm -rf "$WATCHER_LOCK_DIR"
    mkdir "$WATCHER_LOCK_DIR"
    mkdir -p "$(dirname "$WATCHER_PIDFILE")"
    printf '%s\n' "$$" > "$WATCHER_PIDFILE"
    LOCK_ACQUIRED=1
    write_heartbeat
}

cleanup() {
    if is_pid_running "$WATCH_BACKEND_PID"; then
        kill "$WATCH_BACKEND_PID" 2>/dev/null || true
        kill -KILL "$WATCH_BACKEND_PID" 2>/dev/null || true
    fi
    if [[ -n "$WATCH_FIFO" && -p "$WATCH_FIFO" ]]; then
        rm -f "$WATCH_FIFO"
    fi
    if [[ "$LOCK_ACQUIRED" -eq 1 ]]; then
        rm -f "$WATCHER_PIDFILE" "$WATCHER_HEARTBEAT"
        rmdir "$WATCHER_LOCK_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# Ensure signals directory exists
mkdir -p "$SIGNALS_DIR"
acquire_lock

handle_signal_file() {
    local signal_file="$1"
    local agent_name parent_dir per_msg_file=0

    if [[ -z "$signal_file" || ! -f "$signal_file" ]]; then
        return
    fi

    # Per-message layout: agents/{agent_name}/{msg_id}.signal
    # Legacy layout:      agents/{agent_name}.signal
    parent_dir=$(basename "$(dirname "$signal_file")")
    if [[ "$parent_dir" == "agents" ]]; then
        agent_name=$(basename "$signal_file" .signal)
    else
        agent_name="$parent_dir"
        per_msg_file=1
    fi

    if [[ -z "$agent_name" ]]; then
        return
    fi

    # signal は server-owned dirty bit。client は rename/delete しない。
    # 重複処理防止は state_should_attempt + acquire_delivery_lease で行う。
    # bash 3.2 (macOS system) 互換: mapfile を使わず逐次 read する。
    local msg_id from subject importance mtime body_snippet body_truncated msg_key
    local signal_project_key signal_project_slug
    {
        IFS= read -r msg_id
        IFS= read -r from
        IFS= read -r subject
        IFS= read -r importance
        IFS= read -r mtime
        IFS= read -r body_snippet
        IFS= read -r body_truncated
        IFS= read -r signal_project_key
        IFS= read -r signal_project_slug
    } < <(read_signal_meta "$signal_file")
    msg_id="${msg_id:-}"
    from="${from:-unknown}"
    subject="${subject:-(no subject)}"
    importance="${importance:-normal}"
    mtime="${mtime:-0}"
    body_snippet="${body_snippet:-}"
    body_truncated="${body_truncated:-0}"
    signal_project_key="${signal_project_key:-}"
    signal_project_slug="${signal_project_slug:-}"
    if [[ -z "$signal_project_key" && -n "$signal_project_slug" ]]; then
        signal_project_key="$(resolve_signal_project_key "$signal_project_slug" || true)"
    fi
    signal_project_key="$(watcher_normalize_project_key "$signal_project_key")"
    if [[ -z "$signal_project_key" ]]; then
        log "Skipping signal for '$agent_name': canonical project key unavailable"
        return 0
    fi
    msg_key="${msg_id:-mtime-${mtime}}"

    # `set -e` 下で `|| return` だと return が直前の exit code を継承して
    # 関数が non-zero で抜け、呼び出し元の while ループが止まる。
    # 早期スキップは `return 0` を明示してスクリプト継続を保証する。
    state_should_attempt "$signal_project_key" "$agent_name" "$msg_key" || return 0

    # 割り込みの閾値。既定は low = 従来どおり全部通す。
    #
    # 通知は相手の入力欄に直接タイプされるので、人間が親と会話している最中に子の
    # 進捗報告が挟まる。「子を何体も抱えている親」ほど会話が細切れになる、という
    # 報告がテスターから届いた。ここで落としても**メールは消えない**: signal を
    # 消費しないまま state に記録するだけなので、次に fetch_inbox を呼べば普通に
    # 読める。奪うのは割り込む権利であって、届く権利ではない。
    if ! importance_at_least "$importance" "$NOTIFY_MIN_IMPORTANCE"; then
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "below_min_importance" "watcher"
        return 0
    fi

    acquire_delivery_lease "$signal_project_key" "$agent_name" "$msg_key" || return 0

    log "Signal: ${agent_name} ← ${from} [${importance}]: ${subject}"

    # 配送 (tmux 操作) は background worker に切り離す。tmux が server stall で
    # ブロックしても本体ループは即座に次の signal へ進めるため、健全な pane への
    # 配送が止まらない (= hang しない)。worker は run_to で各 tmux 呼び出しを時間
    # 制限し、state 記録 + lease 解放 + signal 削除まで自己完結する。
    # server stall 時の worker 暴走を防ぐため同時数を MAX_WORKERS で制限する。
    # worker は run_to により有限時間で必ず終了するので、この待ちは有界 (最悪
    # TMUX_TIMEOUT 程度) であり恒久 deadlock しない。
    while [ "$(jobs -p 2>/dev/null | wc -l | tr -d ' ')" -ge "$MAX_WORKERS" ]; do
        sleep 0.1
    done
    deliver_worker "$signal_file" "$signal_project_key" "$agent_name" "$msg_key" "$from" "$subject" "$importance" "$per_msg_file" "$body_snippet" "$body_truncated" &
}

# deliver_worker: 1 signal の配送を完結させる background ジョブ。すべての tmux
# 呼び出しを run_to で時間制限するため、ここがブロックしても本体ループには波及
# しない。
#
# tmux session 名 = エージェント名の規約に従い exact match のみ。過去にあった
# 「ペイン text に agent_name が含まれていれば fallback resolve」は、別エージェン
# トのペインに偶然名前が現れた場合に誤配する構造的バグの元 (2026-05-20
# BoldLeeuwenhoek の古い signal が SwiftFaraday へ誤配)。session 不在は誤配より
# 安全な skip として扱う。
deliver_worker() {
    local signal_file="$1" signal_project_key="$2" agent_name="$3" msg_key="$4"
    local from="$5" subject="$6" importance="$7" per_msg_file="$8"
    local body_snippet="${9:-}" body_truncated="${10:-0}"
    local session_name="$agent_name"

    if ! run_to "$TMUX_TIMEOUT" tmux has-session -t "=$session_name" 2>/dev/null; then
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "session_not_found" "watcher"
        release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
        return 0
    fi

    local session_project="" session_repository="" session_work_dir="" session_cwd="" durable_project=""
    session_project="$(tmux_session_env_value "$session_name" AGENTSTACK_PROJECT_KEY)"
    if [[ -z "$session_project" ]]; then
        session_project="$(tmux_session_env_value "$session_name" PROJECT_KEY)"
    fi
    durable_project="$(durable_agent_project_key "$agent_name" || true)"
    if [[ -z "$session_project" ]]; then
        session_project="$durable_project"
    fi
    session_project="$(watcher_normalize_project_key "$session_project")"
    if [[ -n "$durable_project" && "$durable_project" != "__AGENTSTACK_AMBIGUOUS__" ]]; then
        durable_project="$(watcher_normalize_project_key "$durable_project")"
    fi
    if [[ -z "$session_project" || "$session_project" != "$signal_project_key" ]]; then
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "project_mismatch" "watcher"
        release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
        return 0
    fi
    session_repository="$(tmux_session_env_value "$session_name" AGENTSTACK_PROJECT_REPOSITORY)"
    session_work_dir="$(tmux_session_env_value "$session_name" AGENTSTACK_PROJECT_WORK_DIR)"
    session_cwd="$(run_to "$TMUX_TIMEOUT" tmux display-message -t "=$session_name" -p '#{pane_current_path}' 2>/dev/null || true)"
    if ! session_context_is_consistent "$signal_project_key" "$session_name" \
        "$durable_project" "$session_repository" "$session_work_dir" "$session_cwd"; then
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "project_mismatch" "watcher"
        release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
        return 0
    fi

    # bare shell には inject しない (Claude REPL でないため)。
    # busy 判定はあえて行わない: 2026-05-22 に busy-skip を入れたところ、busy な
    # agent (特に game 進行役の NavyMaxwell は "Running scheduled task" 等で常時
    # busy 表示) へ通知が届かず game が止まる副作用が出た。hang は run_to timeout +
    # worker 切り離しで構造的に防げており、busy pane への send-keys はもう安全
    # (Claude が input をキューし、ターン完了後に処理する = むしろ望ましい挙動)。
    # したがって busy でも inject する (旧 watcher と同じ配送方針に戻す)。
    local last_lines rc=0
    last_lines=$(run_to "$TMUX_TIMEOUT" tmux capture-pane -t "=$session_name" -p -S -5 2>/dev/null) || rc=$?
    if [ "$rc" -ne 0 ]; then
        # capture が時間内に返らない = server stall。inject せず後で再試行。
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "capture_timeout" "watcher"
        release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
        return 0
    fi
    if echo "$last_lines" | grep -qE '(\$ ?$|% ?$)' && \
       ! echo "$last_lines" | grep -qE '(❯|Claude|claude|ctx:|Sonnet|Opus|Haiku|›)'; then
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "bare_shell" "watcher"
        release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
        return 0
    fi

    local prompt
    if [[ -n "$body_snippet" && "$body_truncated" == "0" ]]; then
        prompt="AgentStack mail notification: message from ${from} [${importance}]: ${subject}. Body (complete; no inbox fetch needed): ${body_snippet}"
    elif [[ -n "$body_snippet" ]]; then
        prompt="AgentStack mail notification: message from ${from} [${importance}]: ${subject}. Body preview: ${body_snippet} ... Fetch inbox to read the rest."
    else
        prompt="ORRERY Mail notification: message from ${from} [${importance}]: ${subject}. Please call fetch_inbox to read it."
    fi

    if ! run_to "$TMUX_TIMEOUT" tmux send-keys -t "=$session_name" -l "$prompt" 2>/dev/null; then
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "inject_failed" "watcher"
        release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
        return 0
    fi
    sleep 0.2
    # submit は Enter keysym ではなく C-m（Ctrl+M=CR）を使う。spawn_child.sh が
    # Claude/Codex 両方の prompt 注入で C-m を使っており（proven-universal）、Codex
    # REPL では Enter が submit されないことがある（2026-06-05 WildCurie が Enter で
    # 固まった件）。Claude Code は Enter/C-m 両方 submit するので C-m に統一しても無回帰
    # （捨て子 Claude で実測確認）。
    if ! run_to "$TMUX_TIMEOUT" tmux send-keys -t "=$session_name" C-m 2>/dev/null; then
        state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "submit_failed" "watcher"
        release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
        return 0
    fi

    state_mark_result "$signal_project_key" "$agent_name" "$msg_key" "success" "watcher"
    release_delivery_lease "$signal_project_key" "$agent_name" "$msg_key"
    log "  Injected notification into '$agent_name' (session: $session_name)"
    # Per-message files are watcher-owned (each represents one delivery); unlink
    # them on success so identical msg_ids never re-fire. Legacy single-file
    # signals remain server-owned and are cleared by fetch_inbox.
    if (( per_msg_file == 1 )); then
        rm -f "$signal_file" 2>/dev/null || true
        # Try to remove the per-agent dir if it's now empty (best-effort).
        rmdir "$(dirname "$signal_file")" 2>/dev/null || true
    fi
    return 0
}

process_existing_signals() {
    # Match both legacy (agents/{name}.signal) and per-message
    # (agents/{name}/{msg_id}.signal) layouts. find -name globs file names so
    # both layouts surface here; handle_signal_file disambiguates by parent dir.
    while IFS= read -r -d '' signal_file; do
        write_heartbeat
        handle_signal_file "$signal_file"
    done < <(find "$SIGNALS_DIR" -name "*.signal" -type f -print0 2>/dev/null)
}

if command -v fswatch &>/dev/null; then
    log "Starting fswatch on $SIGNALS_DIR"
    process_existing_signals
    WATCH_FIFO="$(mktemp -u "/tmp/orrery-mail-fswatch.XXXXXX")"
    mkfifo "$WATCH_FIFO"
    fswatch -r --event Created --event Updated "$SIGNALS_DIR" > "$WATCH_FIFO" &
    WATCH_BACKEND_PID=$!
    # fswatch + 30 秒ごとの periodic scan の二段構え。fswatch イベント
    # 取りこぼし時の救済 + state cooldown 経過後の再試行を担う。
    exec 3<>"$WATCH_FIFO"
    last_scan=$(date +%s)
    while true; do
        write_heartbeat
        if read -r -t 1 filepath <&3; then
            if [[ "$filepath" == *.signal ]]; then
                sleep 0.1
                handle_signal_file "$filepath"
            fi
        fi
        now=$(date +%s)
        if (( now - last_scan >= SCAN_INTERVAL )); then
            process_existing_signals
            last_scan=$now
        fi
    done
else
    log "fswatch not found, using polling (${POLL_INTERVAL}s interval)"
    while true; do
        write_heartbeat
        process_existing_signals
        sleep "$POLL_INTERVAL"
    done
fi
