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

MAIL_HOME="${AGENTSTACK_MAIL_HOME:-$HOME/.agentstack/mail}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-$MAIL_HOME/signals}"
POLL_INTERVAL=2  # seconds (fallback if fswatch unavailable)
# 既定は利用者ごと（$HOME 配下）。1 台の Mac に複数利用者がいても lock dir が
# 衝突しない。AGENTSTACK_MAIL_WATCHER_LOCK_DIR / _PIDFILE / _HEARTBEAT で明示した
# path が他人の物（以前の /tmp 既定を踏襲した設定など）を指すときは、奪いにも
# 別の場所へ切り替えにも行かず、何も書かずに止まって直し方を出す（2026-10-05 の
# fresh install で 2 人目の利用者の watcher が共通 /tmp lock を取れず無限リトライ
# し続けた件）。
DEFAULT_WATCHER_LOCK_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/mail-watcher.lock"
WATCHER_LOCK_DIR="${AGENTSTACK_MAIL_WATCHER_LOCK_DIR:-$DEFAULT_WATCHER_LOCK_DIR}"
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
# periodic scan で取りこぼし救済。Linux の fswatch（inotify）は再帰 watch を自前で
# 足すので、新しくできた受信者フォルダの watch が付く前に書かれた signal は通知
# されない（inotify(7) の既知の制約）。フォルダは配送のたびに消えて次の1通で作り
# 直されるため、WSL ではほぼ毎回この scan 待ち（30秒）になっていた（2026-09-29）。
# Linux では scan を2秒にして救済を早める。macOS（FSEvents）は従来どおり30秒。
# 上書きは AGENTSTACK_MAIL_WATCHER_SCAN_INTERVAL（1 以上の整数秒）。
case "$(uname -s 2>/dev/null || true)" in
    Linux) SCAN_INTERVAL_DEFAULT=2 ;;
    *) SCAN_INTERVAL_DEFAULT=30 ;;
esac
SCAN_INTERVAL="${AGENTSTACK_MAIL_WATCHER_SCAN_INTERVAL:-$SCAN_INTERVAL_DEFAULT}"
case "$SCAN_INTERVAL" in
    ''|*[!0-9]*) SCAN_INTERVAL="$SCAN_INTERVAL_DEFAULT" ;;
esac
SCAN_INTERVAL=$((10#$SCAN_INTERVAL))  # "08" は8進数として読まない
if (( SCAN_INTERVAL < 1 )); then
    SCAN_INTERVAL="$SCAN_INTERVAL_DEFAULT"
fi
RETRY_COOLDOWN=30     # 同一 (agent, msg) の再試行間隔（scan 間隔とは別。短い scan でも変えない）
# 2026-05-22 JollyTesla hang 根治: tmux 呼び出しは server stall 時に同期ブロック
# し、単一スレッドの本体ループ全体を凍結させる (本日 game2 で2回 hang)。全 tmux
# 呼び出しを run_to で時間制限し、配送本体は background worker に切り離す。
TMUX_TIMEOUT="${TMUX_TIMEOUT:-5}"   # tmux 1 コールの上限秒
MAX_WORKERS="${MAX_WORKERS:-12}"    # 同時 delivery worker 上限 (server stall 時の暴走防止)
HEADLESS_REPLY_TIMEOUT="${AGENTSTACK_HEADLESS_REPLY_TIMEOUT:-300}"
case "$HEADLESS_REPLY_TIMEOUT" in
    ''|*[!0-9]*) HEADLESS_REPLY_TIMEOUT=300 ;;
esac
if (( HEADLESS_REPLY_TIMEOUT < 1 || HEADLESS_REPLY_TIMEOUT > 300 )); then
    HEADLESS_REPLY_TIMEOUT=300
fi
# A worker retains its lease throughout the bridge wait. The lease must never
# expire first or the periodic scan can start a duplicate bot delivery.
LEASE_TTL=$((HEADLESS_REPLY_TIMEOUT + 30))
PERSISTENT_DELIVER="${AGENTSTACK_PERSISTENT_DELIVER:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bin/agentstack-persistent-deliver}"
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

# 1行目は ok / invalid。Mail server は signal を直接 write_text するので、書きかけ
# （空や途中で切れた JSON）を読むことがある。それを空の metadata として配送すると
# 件名なしの通知を1回送って success にし、signal を消してしまう。invalid なら
# 呼び出し側は何も記録せず signal を残し、次の scan で読み直す。
read_signal_meta() {
    python3 - "$1" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    st = os.stat(path)
except Exception:
    data = None
if not isinstance(data, dict) or not isinstance(data.get("message") or {}, dict):
    # Still print every line the caller reads: a short read would fail under
    # the watcher's set -e and end the whole watcher.
    print("invalid" + "\n" * 8, end="")
    raise SystemExit(0)
msg = data.get("message") or {}
print("ok")
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
    local guard_path="${lease_path}.guard"
    python3 - "$lease_path" "$guard_path" "$LEASE_TTL" <<'PY'
import fcntl
import os
from pathlib import Path
import secrets
import shutil
import sys
import time

lease = Path(sys.argv[1])
guard_path = Path(sys.argv[2])
ttl = int(sys.argv[3])
now = int(time.time())
owner = f"{os.getpid()}-{secrets.token_hex(16)}"
guard = open(guard_path, "a+", encoding="utf-8")
os.chmod(guard_path, 0o600)
fcntl.flock(guard, fcntl.LOCK_EX)
try:
    if lease.exists():
        try:
            timestamp = int((lease / "ts").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            timestamp = 0
        if now - timestamp <= ttl:
            raise SystemExit(1)
        shutil.rmtree(lease)
    lease.mkdir(mode=0o700)
    (lease / "ts").write_text(f"{now}\n", encoding="utf-8")
    (lease / "owner").write_text(f"{owner}\n", encoding="utf-8")
    os.chmod(lease / "ts", 0o600)
    os.chmod(lease / "owner", 0o600)
finally:
    fcntl.flock(guard, fcntl.LOCK_UN)
    guard.close()
print(owner)
PY
}

release_delivery_lease() {
    local lease_path="${LEASE_DIR}/$1-$2.lock"
    local guard_path="${lease_path}.guard"
    local owner="$3"
    python3 - "$lease_path" "$guard_path" "$owner" <<'PY'
import fcntl
import os
from pathlib import Path
import shutil
import sys

lease = Path(sys.argv[1])
guard_path = Path(sys.argv[2])
owner = sys.argv[3]
guard = open(guard_path, "a+", encoding="utf-8")
os.chmod(guard_path, 0o600)
fcntl.flock(guard, fcntl.LOCK_EX)
try:
    try:
        current = (lease / "owner").read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if current == owner and lease.is_dir():
        shutil.rmtree(lease)
finally:
    fcntl.flock(guard, fcntl.LOCK_UN)
    guard.close()
PY
}

is_pid_running() {
    local pid="${1:-}"
    [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

# stat(1) の所有者 UID の取り方は GNU (Linux/WSL, BusyBox も含む) の `-c` と
# BSD (macOS) の `-f` で違う。GNU の `-f` は「ファイルシステム情報」を意味し、
# 未知の directive (%u) でも先にファイルシステム情報を stdout へ出してから
# 失敗する。`-f` を先に試すと、2>/dev/null で消えるのは stderr だけなので、
# その出力が後続の `-c` の結果に混ざり、UID の比較が常に不一致になる
# （2026-10-05 WSL・GNU coreutils 9.4 実機で確認、ext4）。`-c` を先に試す:
# GNU/BusyBox ではそのまま正しい UID を返し、BSD では「unknown option」を
# stderr にだけ出して stdout は空のまま失敗するので、`-f` へ安全に落ちる。
lock_owner_uid() {
    # $2 = L: symlink を辿った先の所有者（既定は path 自体。symlink なら link の持ち主）
    if [[ "${2:-}" == L ]]; then
        stat -L -c '%u' "$1" 2>/dev/null || stat -L -f '%u' "$1" 2>/dev/null || true
    else
        stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1" 2>/dev/null || true
    fi
}

write_heartbeat() {
    mkdir -p "$(dirname "$WATCHER_HEARTBEAT")"
    : > "$WATCHER_HEARTBEAT"
}

# True only for a path that exists and, itself or (for a symlink) through its
# target, belongs to a different UID. A path that does not exist yet is not
# foreign: we are about to create it.
path_is_foreign() {
    local target="$1" owner me
    [[ -e "$target" || -L "$target" ]] || return 1
    me="$(id -u)"
    owner="$(lock_owner_uid "$target")"
    [[ -n "$owner" && "$owner" != "$me" ]] && return 0
    owner="$(lock_owner_uid "$target" L)"
    [[ -n "$owner" && "$owner" != "$me" ]]
}

# A pid counts as "an old watcher of mine" only if it is alive, is this user's
# own process, and its command line is this script. A pidfile left behind by a
# dead watcher can name a pid since reused by something unrelated; treating
# that as a live watcher would refuse to start and tell the user to kill it.
is_own_watcher_process() {
    local pid="${1:-}" cmd uid
    is_pid_running "$pid" || return 1
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$cmd" == *watch_agent_mail_signals.sh* ]] || return 1
    uid="$(ps -p "$pid" -o uid= 2>/dev/null | tr -d '[:space:]' || true)"
    [[ -n "$uid" && "$uid" == "$(id -u)" ]]
}

# The legacy, pre-2026-10-05 default: shared by every account on the Mac. Only
# ever consulted to avoid starting a second watcher while one from before the
# per-user default still runs there (see the LEGACY block in acquire_lock).
LEGACY_WATCHER_LOCK_DIR="/tmp/orrery-mail-watcher.lock"

acquire_lock() {
    local existing_pid=""

    # The default lock is per-user (under $HOME), so the only way any of
    # these three paths can belong to someone else is an explicit override
    # (AGENTSTACK_MAIL_WATCHER_LOCK_DIR / _PIDFILE / _HEARTBEAT) that still
    # points at another account's path (e.g. the pre-2026-10-05 shared /tmp
    # default, carried over by hand). Taking such a path over either fails
    # forever with "Permission denied" (the 2026-10-05 bug: "Stale watcher
    # lock detected" retried every 5s and never started) or, worse, could
    # succeed and corrupt or kill someone else's watcher. There is no safe
    # fallback here either: a dashboard or agent-start started with the same
    # override would keep reading the overridden path, not whatever this
    # process quietly switched to, and would report the watcher as down while
    # it was actually running elsewhere. So: touch nothing, say which path and
    # whose it is, and stop.
    local culprit owner_uid
    for culprit in "$WATCHER_LOCK_DIR" "$(dirname "$WATCHER_PIDFILE")" "$(dirname "$WATCHER_HEARTBEAT")" \
        "$WATCHER_PIDFILE" "$WATCHER_HEARTBEAT"; do
        if path_is_foreign "$culprit"; then
            owner_uid="$(lock_owner_uid "$culprit")"
            [[ -n "$owner_uid" && "$owner_uid" != "$(id -u)" ]] || owner_uid="$(lock_owner_uid "$culprit" L)"
            log "${culprit} belongs to another user (uid ${owner_uid}); not writing there. Remove the override (AGENTSTACK_MAIL_WATCHER_LOCK_DIR / _PIDFILE / _HEARTBEAT) or point it at a path only this user can write, then run this again."
            exit 1
        fi
    done

    # A watcher started before 2026-10-05 (or by hand per this file's own
    # Usage comment, which install.sh's service restart cannot stop) may still
    # be running on the old shared default. Since it is a different directory
    # now, acquiring our own lock would not catch it, and both would deliver
    # the same mail. Defer to it rather than risk a second instance.
    if [[ "$WATCHER_LOCK_DIR" == "$DEFAULT_WATCHER_LOCK_DIR" && "$LEGACY_WATCHER_LOCK_DIR" != "$WATCHER_LOCK_DIR" ]] \
        && [[ -f "${LEGACY_WATCHER_LOCK_DIR}/watcher.pid" ]] \
        && ! path_is_foreign "$LEGACY_WATCHER_LOCK_DIR"; then
        local legacy_pid
        legacy_pid="$(<"${LEGACY_WATCHER_LOCK_DIR}/watcher.pid")"
        if is_own_watcher_process "$legacy_pid"; then
            log "A watcher from before 2026-10-05 is still running on the old shared lock (${LEGACY_WATCHER_LOCK_DIR}, PID ${legacy_pid}); not starting a second one. Stop it (kill ${legacy_pid}, or its tmux/background job) and run this again."
            exit 1
        fi
    fi

    if mkdir "$WATCHER_LOCK_DIR" 2>/dev/null; then
        mkdir -p "$(dirname "$WATCHER_PIDFILE")"
        printf '%s\n' "$$" > "$WATCHER_PIDFILE"
        LOCK_ACQUIRED=1
        write_heartbeat
        return 0
    fi

    # The dir already exists and (checked above) is ours.
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
    local meta_status msg_id from subject importance mtime body_snippet body_truncated msg_key
    {
        IFS= read -r meta_status
        IFS= read -r msg_id
        IFS= read -r from
        IFS= read -r subject
        IFS= read -r importance
        IFS= read -r mtime
        IFS= read -r body_snippet
        IFS= read -r body_truncated
    } < <(read_signal_meta "$signal_file")
    if [[ "${meta_status:-}" != "ok" ]]; then
        # 書きかけか壊れた signal。記録も削除もせず、次の scan に回す。
        return 0
    fi
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

    # 配送 (tmux 操作) は background worker に切り離す。tmux が server stall で
    # ブロックしても本体ループは即座に次の signal へ進めるため、健全な pane への
    # 配送が止まらない (= hang しない)。worker は run_to で各 tmux 呼び出しを時間
    # 制限し、state 記録 + lease 解放 + signal 削除まで自己完結する。
    # server stall 時の worker 暴走を防ぐため同時数を MAX_WORKERS で制限する。
    # headless worker は返信 deadline まで生きるため、この待ちは
    # HEADLESS_REPLY_TIMEOUT 近くになり得る。待ち時間で lease の有効期間を消費
    # しないよう、空き枠を得て state を再確認してから lease を取得する。
    while [ "$(jobs -p 2>/dev/null | wc -l | tr -d ' ')" -ge "$MAX_WORKERS" ]; do
        sleep 0.1
    done
    # 枠待ちの間に別配送経路が成功していれば二重配送しない。
    state_should_attempt "$agent_name" "$msg_key" || return 0
    local lease_owner
    lease_owner=$(acquire_delivery_lease "$agent_name" "$msg_key") || return 0
    # 直前の確認から lease 取得までの間に、前の worker が success を書いて lease を
    # 手放していることがある。その lease を取ると同じ msg の worker がもう1つ走るので、
    # 取った後にもう一度確かめ、不要なら自分の lease を返して終える。
    if ! state_should_attempt "$agent_name" "$msg_key"; then
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
        return 0
    fi

    log "Signal: ${agent_name} ← ${from} [${importance}]: ${subject}"

    deliver_worker "$signal_file" "$agent_name" "$msg_key" "$from" "$subject" "$importance" "$per_msg_file" "$body_snippet" "$body_truncated" "$lease_owner" &
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
    local lease_owner="${10:-}"
    local session_name="$agent_name"

    # A persistent headless profile has no REPL pane to inject. Its wrapper
    # publishes a mode-0600 runtime manifest and execs a bridge that owns the
    # referenced Unix socket. The delivery helper returns success only after
    # that bridge confirms notification -> bot handoff -> Mail reply. Once a
    # headless manifest claims this identity, failure is retained for retry;
    # falling through to tmux could wake an unrelated same-name pane.
    local headless_rc=10
    if [[ -x "$PERSISTENT_DELIVER" ]]; then
        if "$PERSISTENT_DELIVER" \
            --runtime-dir "$STATE_DIR" \
            --agent-name "$agent_name" \
            --signal-file "$signal_file" \
            --timeout "$HEADLESS_REPLY_TIMEOUT"; then
            state_mark_result "$agent_name" "$msg_key" "success" "persistent-headless-replied"
            release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
            log "  Headless bridge replied for '$agent_name'"
            if (( per_msg_file == 1 )); then
                rm -f "$signal_file" 2>/dev/null || true
                rmdir "$(dirname "$signal_file")" 2>/dev/null || true
            fi
            return 0
        else
            headless_rc=$?
        fi
        if [[ "$headless_rc" -ne 10 ]]; then
            state_mark_result "$agent_name" "$msg_key" "headless_reply_failed" "persistent-headless"
            release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
            return 0
        fi
    fi

    # Leave the server-owned signal unread; conversation-only sessions cannot
    # authenticate Mail and must never be prompted to claim/register an identity.
    local mail_disabled
    mail_disabled=$(run_to "$TMUX_TIMEOUT" tmux show-environment -t "=$session_name" AGENTSTACK_MAIL_DISABLED 2>/dev/null) || mail_disabled=""
    if [[ "$mail_disabled" == "AGENTSTACK_MAIL_DISABLED=1" ]]; then
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
        return 0
    fi

    if ! run_to "$TMUX_TIMEOUT" tmux has-session -t "$session_name" 2>/dev/null; then
        state_mark_result "$agent_name" "$msg_key" "session_not_found" "watcher"
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
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
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
        return 0
    fi
    if echo "$last_lines" | grep -qE '(\$ ?$|% ?$)' && \
       ! echo "$last_lines" | grep -qE '(❯|Claude|claude|ctx:|Sonnet|Opus|Haiku|›)'; then
        state_mark_result "$agent_name" "$msg_key" "bare_shell" "watcher"
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
        return 0
    fi

    local prompt
    if [[ -n "$body_snippet" && "$body_truncated" == "0" ]]; then
        prompt="ORRERY Mail notification: message from ${from} [${importance}]: ${subject}. Body (complete; no inbox fetch needed): ${body_snippet}"
    elif [[ -n "$body_snippet" ]]; then
        prompt="ORRERY Mail notification: message from ${from} [${importance}]: ${subject}. Body preview: ${body_snippet} ... Fetch inbox to read the rest."
    else
        prompt="ORRERY Mail notification: message from ${from} [${importance}]: ${subject}. Please call fetch_inbox to read it."
    fi

    # 本文は bracketed paste で渡す。send-keys -l は 1 文字ずつの打鍵として届くため、
    # 受け側の読み取りが遅れる（機械の飽和など）と本文と C-m が同じ読み取りに入り、
    # C-m が貼り付けの一部として扱われて submit されない。本文が入力欄に残り、
    # 人が Enter を押すまで止まる。2026-09-27 に Codex と Claude Code の両方で、
    # REPL を SIGSTOP した間に送って再現し、paste-buffer -p なら submit されることを確認。
    # run_to は背景実行で stdin が /dev/null になるので、本文はファイル経由で渡す。
    # The session may have been replaced during capture. Recheck immediately
    # before constructing/injecting a notification into the resumed conversation.
    mail_disabled=$(run_to "$TMUX_TIMEOUT" tmux show-environment -t "=$session_name" AGENTSTACK_MAIL_DISABLED 2>/dev/null) || mail_disabled=""
    if [[ "$mail_disabled" == "AGENTSTACK_MAIL_DISABLED=1" ]]; then
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
        return 0
    fi
    local paste_file paste_buf="agentstack-notify-$$-${RANDOM}"
    paste_file=$(mktemp "${TMPDIR:-/tmp}/agentstack-notify.XXXXXX") || paste_file=""
    if [[ -z "$paste_file" ]] || ! printf '%s' "$prompt" > "$paste_file" \
       || ! run_to "$TMUX_TIMEOUT" tmux load-buffer -b "$paste_buf" "$paste_file" 2>/dev/null \
       || ! run_to "$TMUX_TIMEOUT" tmux paste-buffer -p -d -b "$paste_buf" -t "$session_name" 2>/dev/null; then
        [[ -n "$paste_file" ]] && rm -f "$paste_file"
        run_to "$TMUX_TIMEOUT" tmux delete-buffer -b "$paste_buf" 2>/dev/null || true
        state_mark_result "$agent_name" "$msg_key" "inject_failed" "watcher"
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
        return 0
    fi
    rm -f "$paste_file"
    sleep 0.2
    # submit は Enter keysym ではなく C-m（Ctrl+M=CR）を使う。spawn_child.sh が
    # Claude/Codex 両方の prompt 注入で C-m を使っており（proven-universal）、Codex
    # REPL では Enter が submit されないことがある（2026-06-05 WildCurie が Enter で
    # 固まった件）。Claude Code は Enter/C-m 両方 submit するので C-m に統一しても無回帰
    # （捨て子 Claude で実測確認）。
    if ! run_to "$TMUX_TIMEOUT" tmux send-keys -t "$session_name" C-m 2>/dev/null; then
        state_mark_result "$agent_name" "$msg_key" "submit_failed" "watcher"
        release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
        return 0
    fi

    # 保険: それでも入力欄に本文が残っていれば C-m を 1 回だけ送り直す。入力欄は
    # 画面上で最後にある ›（Codex）/ ❯（Claude Code）の行。履歴の同じ文面は
    # それより上にあるので誤判定しない。長い貼り付けは Claude Code が
    # [Pasted text …] に畳むので、それも残留とみなす。
    sleep 1
    local input_line
    input_line=$(run_to "$TMUX_TIMEOUT" tmux capture-pane -t "$session_name" -p 2>/dev/null \
        | grep -E '^[[:space:]]*(›|❯)' | tail -1) || input_line=""
    if [[ -n "$input_line" ]] && { [[ "$input_line" == *"ORRERY Mail notification: message from ${from}"* ]] \
        || [[ "$input_line" == *"[Pasted text"* ]]; }; then
        log "  Notification still in the input box of '$agent_name'; resending C-m"
        run_to "$TMUX_TIMEOUT" tmux send-keys -t "$session_name" C-m 2>/dev/null || true
    fi

    state_mark_result "$agent_name" "$msg_key" "success" "watcher"
    release_delivery_lease "$agent_name" "$msg_key" "$lease_owner"
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
    log "Starting fswatch on $SIGNALS_DIR (recovery scan every ${SCAN_INTERVAL}s)"
    process_existing_signals
    WATCH_FIFO="$(mktemp -u "/tmp/orrery-mail-fswatch.XXXXXX")"
    mkfifo "$WATCH_FIFO"
    fswatch -r --event Created --event Updated "$SIGNALS_DIR" > "$WATCH_FIFO" &
    WATCH_BACKEND_PID=$!
    # fswatch + periodic scan（SCAN_INTERVAL 秒、Linux 2 / macOS 30）の二段構え。
    # fswatch イベント取りこぼし時の救済 + state cooldown 経過後の再試行を担う。
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
