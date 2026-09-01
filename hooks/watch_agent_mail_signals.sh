#!/usr/bin/env bash
# watch_agent_mail_signals.sh - Watch mcp-agent-mail signal files and inject
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

MAIL_HOME="${AGENTSTACK_MAIL_HOME:-$HOME/.mcp_agent_mail}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-$MAIL_HOME/signals}"
POLL_INTERVAL=2  # seconds (fallback if fswatch unavailable)
WATCHER_LOCK_DIR="${AGENTSTACK_MAIL_WATCHER_LOCK_DIR:-/tmp/mcp-agent-mail-watcher.lock}"
WATCHER_PIDFILE="${AGENTSTACK_MAIL_WATCHER_PIDFILE:-${WATCHER_LOCK_DIR}/watcher.pid}"
WATCHER_HEARTBEAT="${AGENTSTACK_MAIL_WATCHER_HEARTBEAT:-${WATCHER_LOCK_DIR}/heartbeat}"
WATCH_FIFO=""
WATCH_BACKEND_PID=""
LOCK_ACQUIRED=0

# 通知として割り込ませる下限。low|normal|high|urgent。既定 low = 従来どおり全通。
NOTIFY_MIN_IMPORTANCE="${AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE:-low}"

# importance を順序に写す。未知の値は normal 扱い: agent-mail は importance を
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
    python3 - "$1" <<'PY'
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
PY
}

state_should_attempt() {
    python3 - "$STATE_FILE" "$1" "$2" "$RETRY_COOLDOWN" <<'PY'
import json, sys, time
path, agent, msg_key, cooldown = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
compound = f"{agent}:{msg_key}"
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
    python3 - "$STATE_FILE" "$1" "$2" "$3" "$4" <<'PY'
import fcntl, json, sys, time
from pathlib import Path
path, agent, msg_key, result, source = sys.argv[1:6]
compound = f"{agent}:{msg_key}"
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
    local agent="$1"
    local msg_key="$2"
    local lease_path="${LEASE_DIR}/${agent}-${msg_key}.lock"
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
    rm -rf "${LEASE_DIR}/$1-$2.lock" 2>/dev/null || true
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
    rm -f "$WATCHER_PIDFILE"
    rmdir "$WATCHER_LOCK_DIR" 2>/dev/null || true
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
    {
        IFS= read -r msg_id
        IFS= read -r from
        IFS= read -r subject
        IFS= read -r importance
        IFS= read -r mtime
        IFS= read -r body_snippet
        IFS= read -r body_truncated
    } < <(read_signal_meta "$signal_file")
    msg_id="${msg_id:-}"
    from="${from:-unknown}"
    subject="${subject:-(no subject)}"
    importance="${importance:-normal}"
    mtime="${mtime:-0}"
    body_snippet="${body_snippet:-}"
    body_truncated="${body_truncated:-0}"
    msg_key="${msg_id:-mtime-${mtime}}"

    # `set -e` 下で `|| return` だと return が直前の exit code を継承して
    # 関数が non-zero で抜け、呼び出し元の while ループが止まる。
    # 早期スキップは `return 0` を明示してスクリプト継続を保証する。
    state_should_attempt "$agent_name" "$msg_key" || return 0

    # 割り込みの閾値。既定は low = 従来どおり全部通す。
    #
    # 通知は相手の入力欄に直接タイプされるので、人間が親と会話している最中に子の
    # 進捗報告が挟まる。「子を何体も抱えている親」ほど会話が細切れになる、という
    # 報告がテスターから届いた。ここで落としても**メールは消えない**: signal を
    # 消費しないまま state に記録するだけなので、次に fetch_inbox を呼べば普通に
    # 読める。奪うのは割り込む権利であって、届く権利ではない。
    if ! importance_at_least "$importance" "$NOTIFY_MIN_IMPORTANCE"; then
        state_mark_result "$agent_name" "$msg_key" "below_min_importance" "watcher"
        return 0
    fi

    acquire_delivery_lease "$agent_name" "$msg_key" || return 0

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
    deliver_worker "$signal_file" "$agent_name" "$msg_key" "$from" "$subject" "$importance" "$per_msg_file" "$body_snippet" "$body_truncated" &
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
    local signal_file="$1" agent_name="$2" msg_key="$3"
    local from="$4" subject="$5" importance="$6" per_msg_file="$7"
    local body_snippet="${8:-}" body_truncated="${9:-0}"
    local session_name="$agent_name"

    if ! run_to "$TMUX_TIMEOUT" tmux has-session -t "$session_name" 2>/dev/null; then
        state_mark_result "$agent_name" "$msg_key" "session_not_found" "watcher"
        release_delivery_lease "$agent_name" "$msg_key"
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
    last_lines=$(run_to "$TMUX_TIMEOUT" tmux capture-pane -t "$session_name" -p -S -5 2>/dev/null) || rc=$?
    if [ "$rc" -ne 0 ]; then
        # capture が時間内に返らない = server stall。inject せず後で再試行。
        state_mark_result "$agent_name" "$msg_key" "capture_timeout" "watcher"
        release_delivery_lease "$agent_name" "$msg_key"
        return 0
    fi
    if echo "$last_lines" | grep -qE '(\$ ?$|% ?$)' && \
       ! echo "$last_lines" | grep -qE '(❯|Claude|claude|ctx:|Sonnet|Opus|Haiku|›)'; then
        state_mark_result "$agent_name" "$msg_key" "bare_shell" "watcher"
        release_delivery_lease "$agent_name" "$msg_key"
        return 0
    fi

    local prompt
    if [[ -n "$body_snippet" && "$body_truncated" == "0" ]]; then
        prompt="AgentStack mail notification: message from ${from} [${importance}]: ${subject}. Body (complete; no inbox fetch needed): ${body_snippet}"
    elif [[ -n "$body_snippet" ]]; then
        prompt="AgentStack mail notification: message from ${from} [${importance}]: ${subject}. Body preview: ${body_snippet} ... Fetch inbox to read the rest."
    else
        prompt="mcp-agent-mail notification: message from ${from} [${importance}]: ${subject}. Please call fetch_inbox to read it."
    fi

    if ! run_to "$TMUX_TIMEOUT" tmux send-keys -t "$session_name" -l "$prompt" 2>/dev/null; then
        state_mark_result "$agent_name" "$msg_key" "inject_failed" "watcher"
        release_delivery_lease "$agent_name" "$msg_key"
        return 0
    fi
    sleep 0.2
    # submit は Enter keysym ではなく C-m（Ctrl+M=CR）を使う。spawn_child.sh が
    # Claude/Codex 両方の prompt 注入で C-m を使っており（proven-universal）、Codex
    # REPL では Enter が submit されないことがある（2026-06-05 WildCurie が Enter で
    # 固まった件）。Claude Code は Enter/C-m 両方 submit するので C-m に統一しても無回帰
    # （捨て子 Claude で実測確認）。
    if ! run_to "$TMUX_TIMEOUT" tmux send-keys -t "$session_name" C-m 2>/dev/null; then
        state_mark_result "$agent_name" "$msg_key" "submit_failed" "watcher"
        release_delivery_lease "$agent_name" "$msg_key"
        return 0
    fi

    state_mark_result "$agent_name" "$msg_key" "success" "watcher"
    release_delivery_lease "$agent_name" "$msg_key"
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
    WATCH_FIFO="$(mktemp -u "/tmp/mcp-agent-mail-fswatch.XXXXXX")"
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
