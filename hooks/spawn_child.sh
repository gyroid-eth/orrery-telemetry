#!/bin/bash
# spawn_child.sh - launch a child agent (Claude / Codex) in a new tmux session
#
# Usage:
#   spawn_child.sh --resources "path1,path2" "<task>" [<workdir>]
#   spawn_child.sh --resources "docs/**" --codex "<task>"
#   spawn_child.sh --unsafe-no-resources "<task>"
#   spawn_child.sh --model opus --resources "path" "<task>"
#   spawn_child.sh --worktree --resources "path" "<task>"
#   spawn_child.sh --pre-registered <name> --child-token-file <path> "<task>"
#   spawn_child.sh --pre-registered <name> --child-token-file <path> --embed-task --task-file <path> [<workdir>]
#   spawn_child.sh --pre-registered <name> --child-token-file <path> --standalone "<task>"
#
# モデル指定（--model。Codex は gpt-5.6-sol 既定で旧 model 名も有効）:
#   --model 省略/opus    → claude-opus-5（200K。warm pool 対象）
#   --model opus[1m]     → claude-opus-4-8[1m]（legacy 1M。要シングルクォート: glob 回避）
#   --model opus-1m      → claude-opus-4-8[1m]（旧来の friendly 表記を正規化）
#   --model claude-opus-4-8 → 旧 200K Opus を明示指定（引き続き有効）
#   --model sonnet       → claude-sonnet-5（200K。warm pool 対象）
#   --model haiku/fable  → claude-haiku-4-5-20251001 / claude-fable-5
#   --codex --model 省略/sol → gpt-5.6-sol（terra / luna alias も利用可）
#   未知の形             → 明確なエラーで停止（claude-* 接頭の正式 ID は前方互換で素通り）
#   ※ 正規化は normalize_claude_model() / normalize_codex_model() が担当。warm pool は要求モデルが
#     事前起動モデル（opus=claude-opus-5/200K, sonnet=claude-sonnet-5/200K）と
#     完全一致するときだけ claim する（[1m]/fable 等は cold-start で正しく起動）。
#
# リソース管理:
#   --resources CSV       対象リソースパス（カンマ区切り、必須）
#   --resource-ttl SEC    reservation有効期限（デフォルト14400秒）
#   --unsafe-no-resources resource宣言なしの明示的opt-out
#
# 分離モード:
#   --worktree            子を独立した git worktree (別ブランチ・別ディレクトリ) で動かす
#                          - worktree dir: /tmp/cc-worktrees/<AGENT_NAME>
#                          - branch:       exp/<AGENT_NAME>
#                          - 子の tmux cwd は worktree dir
#                          - 元 source は WORK_DIR (引数 $2 / pre-registered モードは $3)
#                          - クリーンアップ: 子の作業完了後、親側から
#                              git -C <source> worktree remove /tmp/cc-worktrees/<NAME>
#                              git -C <source> branch -D exp/<NAME>
#   --worktree-base REV   --worktree と併用。worktree の起点 commit/branch/tag を明示指定。
#                          未指定時は spawn 実行時の HEAD (時間差で drift する可能性あり)。
#                          複数 sub-agent を同一 baseline で並列実行したい場合に使う。
#                          REV は git rev-parse で解決できる任意の参照 (例: main, 22f327b, v1.0)。
#
# 環境変数:
#   PARENT_AGENT  - 親エージェント名（省略時: tmuxセッション名）
#   PROJECT_KEY   - ORRERY Mail のプロジェクトキー（省略時: デフォルト）
#
# 終了コード:
#   0  - 成功
#   1  - 引数不正 / サーバー接続失敗 / worktree 作成失敗
#   2  - --resources も --unsafe-no-resources も未指定
#   21 - リソース競合（conflict検知）
#
# 出力（stdout）: 子エージェント名
# ログ（stderr）: 詳細ログ

set -euo pipefail

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/.agentstack/mail/.env}"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:18765/mcp}}"
HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
PROJECT_KEY="${PROJECT_KEY:-${AGENTSTACK_PROJECT_KEY:-}}"
TERMINAL_SETTING="${AGENTSTACK_TERMINAL:-auto}"
AGENTSTACK_HOME_DIR="${AGENTSTACK_HOME:-}"
if [[ -z "$AGENTSTACK_HOME_DIR" && -d "$HOOKS_DIR/.." ]]; then
    AGENTSTACK_HOME_DIR="$(cd "$HOOKS_DIR/.." && pwd)"
fi
REREGISTER_HELPER="${AGENTSTACK_HOME_DIR:+$AGENTSTACK_HOME_DIR/bin/agentstack-reregister}"

# Source the shared register lib early (function definitions only — no side
# effects) so the macOS TCC access guard is available in every launch path,
# including pre-registered mode which returns before the rest of the script.
if ! declare -F ags_warn_tcc_access >/dev/null 2>&1; then
    _ags_reglib="${AGENTSTACK_REGISTER_LIB:-$AGENTSTACK_HOME_DIR/bin/lib/agentstack-register.sh}"
    [[ -f "$_ags_reglib" ]] && . "$_ags_reglib" 2>/dev/null || true
fi

get_agentstack_token() {
    if [[ -n "${MCP_AGENT_MAIL_TOKEN:-}" ]]; then
        printf '%s' "$MCP_AGENT_MAIL_TOKEN"
        return 0
    fi
    if [[ -x "$HOOKS_DIR/get-mcp-agent-mail-token.sh" ]]; then
        bash "$HOOKS_DIR/get-mcp-agent-mail-token.sh" 2>/dev/null && return 0
    fi
    if command -v security >/dev/null 2>&1; then
        local keychain_token
        keychain_token=$(security find-generic-password -s "mcp-agent-mail" -a "HTTP_BEARER_TOKEN" -w 2>/dev/null || true)
        if [[ -n "$keychain_token" ]]; then
            printf '%s' "$keychain_token"
            return 0
        fi
    fi
    if [[ -f "$MAIL_ENV" ]]; then
        sed -n 's/^HTTP_BEARER_TOKEN=//p' "$MAIL_ENV" | tr -d '[:space:]'
        return 0
    fi
    return 1
}

legacy_http_bearer_enabled() {
    case "$HTTP_BEARER_MODE" in
        enabled|auto) return 0 ;;
        disabled) return 1 ;;
        *)
            echo "Invalid AGENTSTACK_MAIL_HTTP_BEARER_MODE: $HTTP_BEARER_MODE" >&2
            return 2
            ;;
    esac
}

mac_app_exists() {
    [[ -d "/Applications/$1" || -d "$HOME/Applications/$1" ]]
}

terminal_adapter() {
    local setting
    setting="$(printf '%s' "$TERMINAL_SETTING" | tr '[:upper:]' '[:lower:]')"
    case "$setting" in
        ""|auto)
            if [[ "$(uname -s 2>/dev/null)" != "Darwin" ]]; then
                echo "none"
            elif mac_app_exists "Ghostty.app" || command -v ghostty >/dev/null 2>&1; then
                echo "ghostty"
            elif mac_app_exists "iTerm.app" || mac_app_exists "iTerm2.app"; then
                echo "iterm"
            elif mac_app_exists "Terminal.app" || [[ -d "/System/Applications/Utilities/Terminal.app" ]]; then
                echo "terminal"
            else
                echo "none"
            fi
            ;;
        ghostty|iterm|terminal|none)
            echo "$setting"
            ;;
        *)
            echo "none"
            ;;
    esac
}

_open_child_terminal() {
    local child_name="$1"
    local adapter shell_child shell_cmd
    adapter="$(terminal_adapter)"
    [[ "$adapter" == "none" ]] && return 0

    printf -v shell_child '%q' "$child_name"
    shell_cmd="env -u TMUX -u TMUX_PANE tmux attach -t $shell_child"
    # Spawning a child is a background event: the window is opened so the user
    # CAN watch it, not so it steals what they are doing. `open -g` keeps it
    # behind the current app. Set AGENTSTACK_FOCUS_CHILD=1 to bring it forward.
    local open_bg=(-g)
    [[ "${AGENTSTACK_FOCUS_CHILD:-}" == "1" ]] && open_bg=()
    case "$adapter" in
        ghostty)
            if env -u TMUX -u TMUX_PANE open ${open_bg[@]+"${open_bg[@]}"} -na Ghostty.app --args --title="$child_name" -e tmux attach -t "$child_name" 2>/dev/null; then
                echo "[spawn_child] Opened terminal window (${child_name}, adapter: ghostty)" >&2
            fi
            ;;
        iterm)
            if command -v osascript >/dev/null 2>&1; then
                # `activate` is what pulls the app in front, so it is applied
                # only when the user asked for the child to take focus.
                local iterm_activate=""
                [[ "${AGENTSTACK_FOCUS_CHILD:-}" == "1" ]] && iterm_activate="activate"
                osascript -e 'on run argv
                  set cmd to item 1 of argv
                  tell application "iTerm2"
                    '"$iterm_activate"'
                    create window with default profile command cmd
                  end tell
                end run' "$shell_cmd" >/dev/null 2>&1 || true
            fi
            ;;
        terminal)
            if command -v osascript >/dev/null 2>&1; then
                local terminal_activate=""
                [[ "${AGENTSTACK_FOCUS_CHILD:-}" == "1" ]] && terminal_activate="activate"
                osascript -e 'on run argv
                  set cmd to item 1 of argv
                  tell application "Terminal"
                    '"$terminal_activate"'
                    do script cmd
                  end tell
                end run' "$shell_cmd" >/dev/null 2>&1 || true
            fi
            ;;
    esac
    return 0
}

# Terminal activation is an optional observer side effect, never part of child
# readiness. On headless macOS, `open` / `osascript` can wait indefinitely for
# a GUI application, which used to keep a successful spawn_child.sh call stuck
# after the tmux child was already alive. Detach it from the launcher's critical
# path; failures remain best-effort diagnostics from the worker above.
open_child_terminal() {
    (_open_child_terminal "$1") </dev/null >/dev/null 2>&1 &
    return 0
}

# フラグの処理
USE_CODEX=false
CLAUDE_MODEL=""
CODEX_EFFORT="xhigh"
RESOURCES=""
RESOURCE_TTL=14400
UNSAFE_NO_RESOURCES=false
PRE_REGISTERED=""
CHILD_TOKEN_FILE=""
STANDALONE=false
EMBED_TASK=false
TASK_FILE=""
USE_WORKTREE=false
WORKTREE_BASE="/tmp/cc-worktrees"
WORKTREE_BASE_REV=""   # --worktree-base で指定された起点 rev (空=HEAD)
WORKTREE_BASE_RESOLVED="" # rev-parse 後の commit hash (記録用)
WORKTREE_DIR=""        # 後で maybe_create_worktree がセット
WORKTREE_SOURCE=""     # worktree の元 git repo（クリーンアップ用）
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --codex)
            USE_CODEX=true
            shift
            ;;
        --model)
            CLAUDE_MODEL="$2"
            shift 2
            ;;
        --effort)
            CODEX_EFFORT="$2"
            shift 2
            ;;
        --resources)
            RESOURCES="$2"
            shift 2
            ;;
        --resource-ttl)
            RESOURCE_TTL="$2"
            shift 2
            ;;
        --unsafe-no-resources)
            UNSAFE_NO_RESOURCES=true
            shift
            ;;
        --pre-registered)
            PRE_REGISTERED="$2"
            shift 2
            ;;
        --child-token-file|--token-file)
            CHILD_TOKEN_FILE="$2"
            shift 2
            ;;
        --standalone)
            STANDALONE=true
            shift
            ;;
        --embed-task)
            EMBED_TASK=true
            shift
            ;;
        --task-file)
            if [[ $# -lt 2 || -z "${2:-}" ]]; then
                echo "Error: --task-file requires a path" >&2
                exit 1
            fi
            TASK_FILE="$2"
            shift 2
            ;;
        --worktree)
            USE_WORKTREE=true
            shift
            ;;
        --worktree-base)
            WORKTREE_BASE_REV="$2"
            shift 2
            ;;
        *)
            echo "Unknown flag: $1" >&2
            exit 1
            ;;
    esac
done

# --worktree-base は --worktree とのみ意味を持つ
if [[ -n "$WORKTREE_BASE_REV" && "$USE_WORKTREE" != true ]]; then
    echo "Error: --worktree-base requires --worktree" >&2
    exit 1
fi

if [[ "$STANDALONE" == true && -z "$PRE_REGISTERED" ]]; then
    echo "Error: --standalone requires --pre-registered" >&2
    exit 1
fi

if [[ "$EMBED_TASK" == true && -z "$PRE_REGISTERED" ]]; then
    echo "Error: --embed-task requires --pre-registered" >&2
    exit 1
fi

if [[ "$EMBED_TASK" == true && "$STANDALONE" == true ]]; then
    echo "Error: --embed-task cannot be combined with --standalone" >&2
    exit 1
fi

child_token_file_path() {
    local agent_name="$1" key
    key="$(printf '%s' "$agent_name" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '_')"
    [[ -n "$key" ]] || return 1
    printf '%s/agent_token_%s\n' "$RUNTIME_DIR" "$key"
}

# Copy a child token from a 0600 file into the durable per-child runtime files.
# The secret is read inside Python and never appears in a process argv or tmux
# environment.  A dashboard handoff is one-shot, so its source is unlinked only
# after both durable files have been atomically installed.
adopt_child_token_file() {
    local agent_name="$1" project_key="$2" source_file="$3"
    local consume_source="${4:-false}" token_file state_file
    token_file="$(child_token_file_path "$agent_name")" || return 1
    state_file="$CHILD_STATE_DIR/$agent_name.json"
    python3 - "$agent_name" "$project_key" "$source_file" "$token_file" \
        "$state_file" "$consume_source" <<'PY'
import json
import os
import pathlib
import stat
import sys

agent_name, project_key, source, token_file, state_file, consume = sys.argv[1:7]
source_path = pathlib.Path(source)
token_path = pathlib.Path(token_file)
state_path = pathlib.Path(state_file)
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(source_path, flags)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("token handoff is not a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("token handoff permissions must be 0600")
    registration_token = os.read(fd, 4097).decode("utf-8").strip()
finally:
    os.close(fd)
if not registration_token or len(registration_token) > 4096:
    raise ValueError("token handoff is empty or too large")

token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(token_path.parent, 0o700)
state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(state_path.parent, 0o700)

token_tmp = token_path.with_name(token_path.name + f".tmp.{os.getpid()}")
state_tmp = state_path.with_name(state_path.name + f".tmp.{os.getpid()}")
with open(token_tmp, "x", encoding="utf-8") as f:
    f.write(registration_token)
    f.flush()
    os.fsync(f.fileno())
os.chmod(token_tmp, 0o600)
with open(state_tmp, "x", encoding="utf-8") as f:
    json.dump({
        "agent_name": agent_name,
        "project_key": project_key,
        "registration_token": registration_token,
    }, f)
    f.flush()
    os.fsync(f.fileno())
os.chmod(state_tmp, 0o600)
os.replace(token_tmp, token_path)
os.replace(state_tmp, state_path)
os.chmod(token_path, 0o600)
os.chmod(state_path, 0o600)
if consume == "true" and source_path != token_path:
    try:
        source_path.unlink()
    except Exception:
        token_path.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        raise
print(token_path)
PY
}

# Restore the canonical token file from an existing 0600 state file without
# exposing the token to the shell.  This is compatibility-only; new dashboard
# spawns always arrive through adopt_child_token_file's one-shot path.
restore_child_token_file_from_state() {
    local agent_name="$1" state_file token_file
    state_file="$CHILD_STATE_DIR/$agent_name.json"
    token_file="$(child_token_file_path "$agent_name")" || return 1
    [[ -f "$state_file" ]] || return 1
    python3 - "$state_file" "$token_file" <<'PY'
import json
import os
import pathlib
import stat
import sys

state_path = pathlib.Path(sys.argv[1])
token_path = pathlib.Path(sys.argv[2])
if stat.S_IMODE(state_path.stat().st_mode) & 0o077:
    raise PermissionError("child state permissions must be 0600")
data = json.loads(state_path.read_text(encoding="utf-8"))
token = data.get("registration_token")
if not isinstance(token, str) or not token:
    raise ValueError("child state has no registration token")
token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
tmp = token_path.with_name(token_path.name + f".tmp.{os.getpid()}")
with open(tmp, "x", encoding="utf-8") as handle:
    handle.write(token)
os.chmod(tmp, 0o600)
os.replace(tmp, token_path)
os.chmod(token_path, 0o600)
print(token_path)
PY
}

# --- Child model catalog -------------------------------------------------
# Keep defaults and warm-pool identities here. Both launch paths normalize
# through the functions below instead of carrying their own generation names.
CLAUDE_DEFAULT_MODEL="claude-opus-5"
CLAUDE_DEFAULT_SONNET_MODEL="claude-sonnet-5"
CLAUDE_CURRENT_OPUS_1M_MODEL="claude-opus-5[1m]"
CLAUDE_LEGACY_OPUS_MODEL="claude-opus-4-8"
CLAUDE_LEGACY_OPUS_1M_MODEL="claude-opus-4-8[1m]"
CLAUDE_LEGACY_SONNET_MODEL="claude-sonnet-4-6"
CLAUDE_LEGACY_SONNET_1M_MODEL="claude-sonnet-4-6[1m]"
CLAUDE_HAIKU_MODEL="claude-haiku-4-5-20251001"
CLAUDE_FABLE_MODEL="claude-fable-5"
CLAUDE_WARM_OPUS_MODEL="$CLAUDE_DEFAULT_MODEL"
CLAUDE_WARM_SONNET_MODEL="$CLAUDE_DEFAULT_SONNET_MODEL"
CODEX_DEFAULT_MODEL="gpt-5.6-sol"
CODEX_TERRA_MODEL="gpt-5.6-terra"
CODEX_LUNA_MODEL="gpt-5.6-luna"
CODEX_LEGACY_MODEL="gpt-5.5"

# --- Claude モデル名の正規化 ---
# friendly エイリアス / 略記を `claude --model` が受け付ける正式 model string に変換する。
#   - unqualified opus / sonnet track the current 200K generation and warm pool.
#   - generic 1M aliases remain on the known legacy 1M models; old explicit
#     model IDs remain valid for existing automation.
#   - 未知の形は stderr に明確なエラーを出して非ゼロで返す（set -e 下で呼び出し側が停止する）。
#     ただし claude-* 接頭の正式 ID は前方互換のため素通りさせる（新モデル ID 対応）。
# 注意: Codex 経路では呼ばない。
normalize_claude_model() {
    local raw="${1:-}"
    # 小文字化 + 空白除去で正規化キーを作る（出力は固定の正式文字列）
    local m
    m="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"

    case "$m" in
        ""|opus|opus-5|opus5|"$CLAUDE_DEFAULT_MODEL")
            printf '%s\n' "$CLAUDE_DEFAULT_MODEL" ;;
        opus-1m|opus1m|"opus[1m]"|claude-opus-4-8-1m|"$CLAUDE_LEGACY_OPUS_1M_MODEL")
            printf '%s\n' "$CLAUDE_LEGACY_OPUS_1M_MODEL" ;;
        opus-200k|opus200k|"$CLAUDE_LEGACY_OPUS_MODEL")
            printf '%s\n' "$CLAUDE_LEGACY_OPUS_MODEL" ;;
        opus-5-1m|opus51m|"opus-5[1m]"|"opus5[1m]"|"$CLAUDE_CURRENT_OPUS_1M_MODEL")
            printf '%s\n' "$CLAUDE_CURRENT_OPUS_1M_MODEL" ;;
        sonnet|sonnet-5|sonnet5|"$CLAUDE_DEFAULT_SONNET_MODEL")
            printf '%s\n' "$CLAUDE_DEFAULT_SONNET_MODEL" ;;
        sonnet-4-6|sonnet46|"$CLAUDE_LEGACY_SONNET_MODEL")
            printf '%s\n' "$CLAUDE_LEGACY_SONNET_MODEL" ;;
        sonnet-1m|sonnet1m|"sonnet[1m]"|"$CLAUDE_LEGACY_SONNET_1M_MODEL")
            printf '%s\n' "$CLAUDE_LEGACY_SONNET_1M_MODEL" ;;
        haiku|claude-haiku-4-5|"$CLAUDE_HAIKU_MODEL")
            printf '%s\n' "$CLAUDE_HAIKU_MODEL" ;;
        fable|"$CLAUDE_FABLE_MODEL")
            printf '%s\n' "$CLAUDE_FABLE_MODEL" ;;
        *)
            if [[ "$m" == claude-* ]]; then
                # 正式 ID は前方互換で素通り（新モデル ID 対応）
                printf '%s\n' "$m"
            else
                echo "Error: unknown model '$raw'. Valid forms: opus / opus[1m] / opus-5[1m] / claude-opus-4-8 / sonnet / sonnet-4-6 / haiku / fable / claude-<id>" >&2
                return 1
            fi
            ;;
    esac
    return 0
}

normalize_codex_model() {
    local raw="${1:-}"
    local model
    model="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"

    case "$model" in
        ""|sol|gpt-5.6|"$CODEX_DEFAULT_MODEL")
            printf '%s\n' "$CODEX_DEFAULT_MODEL" ;;
        terra|"$CODEX_TERRA_MODEL")
            printf '%s\n' "$CODEX_TERRA_MODEL" ;;
        luna|"$CODEX_LUNA_MODEL")
            printf '%s\n' "$CODEX_LUNA_MODEL" ;;
        gpt-*)
            printf '%s\n' "$model" ;;
        *)
            echo "Error: unknown Codex model '$raw'. Valid forms: sol / terra / luna / gpt-<id>" >&2
            return 1 ;;
    esac
}

validate_codex_effort() {
    local model="$1"
    local raw_effort="${2:-xhigh}"
    local effort
    effort="$(printf '%s' "$raw_effort" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"

    case "$effort" in
        low|medium|high|xhigh|max|ultra) ;;
        *)
            echo "Error: unknown Codex reasoning effort '$raw_effort'. Valid values: low / medium / high / xhigh / max / ultra" >&2
            return 1 ;;
    esac
    case "$model:$effort" in
        "${CODEX_LUNA_MODEL}:ultra")
            echo "Error: $CODEX_LUNA_MODEL does not support ultra reasoning effort" >&2
            return 1 ;;
        "${CODEX_LEGACY_MODEL}:max"|"${CODEX_LEGACY_MODEL}:ultra")
            echo "Error: $CODEX_LEGACY_MODEL supports reasoning effort only through xhigh" >&2
            return 1 ;;
    esac
    printf '%s\n' "$effort"
}

# 子用に独立した git worktree を作って WORK_DIR を上書きするヘルパー。
# 呼び出し前に CHILD_NAME と WORK_DIR が確定している必要がある。
# 成功時: WORKTREE_DIR / WORKTREE_SOURCE をセットし、return 0
# 失敗時: stderr にエラーを吐いて return 1
maybe_create_worktree() {
    local child_name="$1"
    local source_dir="$2"

    if [[ "$USE_WORKTREE" != true ]]; then
        return 0
    fi

    if ! git -C "$source_dir" rev-parse --git-dir > /dev/null 2>&1; then
        echo "Error: --worktree requires source_dir to be a git repository: $source_dir" >&2
        return 1
    fi

    local worktree_dir="${WORKTREE_BASE}/${child_name}"
    local branch_name="exp/${child_name}"

    # Keep generated worktrees outside common synced/vault folders to avoid
    # sync conflicts.
    case "$worktree_dir" in
        *Syncthing*|*Obsidian*)
            echo "Error: worktree path must be outside synced/vault folders: $worktree_dir" >&2
            return 1
            ;;
    esac

    mkdir -p "$WORKTREE_BASE"

    if [[ -e "$worktree_dir" ]]; then
        echo "Error: worktree dir already exists: $worktree_dir (delete it or pick another name)" >&2
        return 1
    fi

    if git -C "$source_dir" show-ref --verify --quiet "refs/heads/$branch_name"; then
        echo "Error: branch $branch_name already exists in $source_dir (delete it first: git -C $source_dir branch -D $branch_name)" >&2
        return 1
    fi

    # --worktree-base 指定時: rev を rev-parse で解決して起点 commit を確定
    local base_args=()
    local base_label="HEAD"
    if [[ -n "$WORKTREE_BASE_REV" ]]; then
        local resolved
        if ! resolved=$(git -C "$source_dir" rev-parse --verify "${WORKTREE_BASE_REV}^{commit}" 2>/dev/null); then
            echo "Error: cannot resolve --worktree-base '$WORKTREE_BASE_REV' (commit/branch/tag not found)" >&2
            return 1
        fi
        WORKTREE_BASE_RESOLVED="$resolved"
        base_args=("$resolved")
        base_label="${WORKTREE_BASE_REV} (${resolved:0:8})"
    fi

    echo "[spawn_child] Creating git worktree: $worktree_dir (branch: $branch_name, base: $base_label)" >&2
    # set -u 下で空配列展開を許容する慣用句: ${base_args[@]+"${base_args[@]}"}
    if ! git -C "$source_dir" worktree add "$worktree_dir" -b "$branch_name" ${base_args[@]+"${base_args[@]}"} >&2; then
        echo "Error: git worktree add failed" >&2
        return 1
    fi

    WORKTREE_DIR="$worktree_dir"
    WORKTREE_SOURCE="$source_dir"
    return 0
}

# worktree クリーンアップ (失敗時 rollback 用)
cleanup_worktree() {
    if [[ -n "${WORKTREE_DIR:-}" && -d "$WORKTREE_DIR" && -n "${WORKTREE_SOURCE:-}" ]]; then
        echo "[spawn_child] cleanup: removing worktree $WORKTREE_DIR" >&2
        git -C "$WORKTREE_SOURCE" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || true
        if [[ -n "${CHILD_NAME:-}" ]]; then
            git -C "$WORKTREE_SOURCE" branch -D "exp/${CHILD_NAME}" 2>/dev/null || true
        fi
    fi
}

# --- Codex launch helpers -------------------------------------------------
# Kept here (before both launch paths) so the pre-registered and normal paths
# share one definition of "which flags does this codex accept" and "is the
# child actually ready".

# Approval/sandbox flags for the installed Codex CLI.
#
# `--full-auto` was removed from the Codex CLI: 0.144.6 answers
# "error: unexpected argument '--full-auto' found" and exits 2, so the child
# died instantly and the launcher then sat through its full readiness timeout.
# Probe --help instead of pinning a version, so both old and new CLIs work.
# Printed as one line and handed to the child through the tmux environment, so
# the inner zsh can word-split it with ${=AGENTSTACK_CODEX_APPROVAL}.
#
# The policy comes from AGENTSTACK_CODEX_CHILD_APPROVAL (installer setting,
# default `never`): a spawned child works unattended, so every "may I run this"
# prompt is a stall nobody is watching. The probe resolves the binary the same
# way the child will (PATH plus ~/.local/bin, or AGENTSTACK_CODEX_BIN); when
# the dashboard's minimal launchd PATH cannot find codex at all, the modern flag
# is emitted instead of nothing — an empty result silently reverted every child
# to Codex's own `on-request` default (2026-09-04).
codex_approval_flags() {
    local help_text codex_bin policy
    policy="${AGENTSTACK_CODEX_CHILD_APPROVAL:-never}"
    codex_bin="${AGENTSTACK_CODEX_BIN:-}"
    if [[ -z "$codex_bin" ]]; then
        codex_bin="$(PATH="$PATH:$HOME/.local/bin" command -v codex 2>/dev/null || true)"
    fi
    if [[ -z "$codex_bin" ]]; then
        printf '%s\n' "--ask-for-approval $policy"
        return 0
    fi
    help_text="$("$codex_bin" --help 2>/dev/null || true)"
    if printf '%s' "$help_text" | grep -q -- "--ask-for-approval"; then
        printf '%s\n' "--ask-for-approval $policy"
    elif printf '%s' "$help_text" | grep -q -- "--full-auto"; then
        printf '%s\n' "--full-auto"
    fi
    # Neither flag: emit nothing and let codex use its own defaults rather than
    # passing an argument this build will reject.
}

# Writable roots for a Codex child, ':'-separated, handed to the child through
# the tmux environment (AGENTSTACK_CODEX_ADD_DIRS_RESOLVED). The child turns each
# entry into `--add-dir`; a ':' list survives spaces in directory names where a
# word-split flag string would not.
#
# Order and membership mirror what an unattended child actually touches: the
# project, every NEW AGENT launch preset and typeahead root, the install dir
# (runtime state, spool, tokens), the worktree base, the user's Claude and
# Codex homes, the child's own CODEX_HOME, then anything the operator added
# with AGENTSTACK_CODEX_ADD_DIRS. Missing directories are dropped so codex does
# not refuse to start on a path that is not there yet.
codex_child_add_dirs() {
    local child_codex_home="$1" raw entry expanded resolved
    local -a candidates=() seen=()
    raw="${AGENTSTACK_PROJECT_KEY:-$PROJECT_KEY}"
    raw="$raw:${AGENTSTACK_SPAWN_DIRS:-}:${AGENTSTACK_SPAWN_ROOTS:-}"
    raw="$raw:${AGENTSTACK_HOME_DIR:-$HOME/.agentstack}:$WORKTREE_BASE"
    raw="$raw:$HOME/.claude:$HOME/.codex:$child_codex_home"
    raw="$raw:${AGENTSTACK_CODEX_ADD_DIRS:-}"
    IFS=':' read -r -a candidates <<< "$raw"
    for entry in "${candidates[@]}"; do
        [[ -n "$entry" ]] || continue
        expanded="$entry"
        if [[ "$entry" == "~" ]]; then
            expanded="$HOME"
        elif [[ "$entry" == "~/"* ]]; then
            expanded="$HOME/${entry#\~/}"
        fi
        [[ -d "$expanded" ]] || continue
        # Codex compares realpaths (macOS /tmp -> /private/tmp), so hand it the
        # resolved form and use that for de-duplication too.
        resolved="$(cd "$expanded" 2>/dev/null && pwd -P)" || continue
        local dup=false
        local kept
        for kept in ${seen[@]+"${seen[@]}"}; do
            [[ "$kept" == "$resolved" ]] && { dup=true; break; }
        done
        [[ "$dup" == true ]] && continue
        seen+=("$resolved")
    done
    local IFS=':'
    printf '%s\n' "${seen[*]-}"
}

# Sandbox network flag for a Codex child. workspace-write blocks the network
# by default, which turns every curl / git fetch / ssh into an approval prompt
# (or a hard failure under `never`). AGENTSTACK_CODEX_NETWORK (installer
# setting, default on) controls it.
codex_network_flags() {
    case "${AGENTSTACK_CODEX_NETWORK:-1}" in
        0|off|false|no) ;;
        *) printf '%s\n' "-c sandbox_workspace_write.network_access=true" ;;
    esac
}

# True while the child's tmux session still exists. A child that died (bad
# flag, crash, sign-in failure) must fail fast instead of burning the whole
# readiness timeout.
codex_session_alive() {
    tmux has-session -t "=$1" 2>/dev/null
}

# Last N non-blank lines of a pane capture. `capture-pane` pads the output to
# the window height, so a plain `tail` on a tall window sees only blank lines
# and every footer-based readiness check fails (2026-09-03: Codex 0.153 timed
# out for 90s with a ready REPL on screen).
pane_nonblank_tail() {
    local count="$1"
    awk '{ lines[NR] = $0 }
         END {
             n = NR
             while (n > 0 && lines[n] ~ /^[[:space:]]*$/) n--
             for (i = 1; i <= n; i++) print lines[i]
         }' | tail -n "$count"
}

# Dialog detectors read the visible screen only: the Codex polls below capture
# without scrollback, because a capture that included history kept showing an
# already-accepted dialog and the launcher pressed Enter on every poll.
codex_trust_dialog_present() {
    printf '%s' "$1" | grep -qi "Do you trust"
}

# Claude Code 2.1.259 replaced "Do you trust ..." with a "Quick safety check"
# whose default row is "No, exit". Both wordings are a modal, never readiness.
claude_trust_dialog_present() {
    printf '%s' "$1" | grep -qiE "Do you trust|Quick safety check|Yes, I trust this folder"
}

# Accept Codex's untrusted-directory prompt.  tmux's symbolic `Enter` did not
# submit this dialog on Codex 0.144.x; the carriage return key `C-m` does.
# Bound repeated detections so a future dialog change fails through the normal
# pre-registration cleanup path instead of hanging the dashboard request.
codex_accept_trust_dialog() {
    local session_name="$1"
    local attempt="$2"
    local max_attempts="$3"
    local log_prefix="${4:-spawn_child}"
    if (( attempt > max_attempts )); then
        echo "[$log_prefix] Trust dialog persisted after ${max_attempts} attempts; aborting" >&2
        return 1
    fi
    echo "[$log_prefix] Trust dialog detected; accepting with C-m (${attempt}/${max_attempts})" >&2
    tmux send-keys -t "$session_name" C-m
}

# Claude Code shows the same trust gate on a directory it has never opened.
# A fresh ~/code/<project> therefore cannot reach the normal input prompt until
# this is accepted. Keep the detector separate from readiness so task text is
# never injected into a modal dialog.
claude_pane_ready() {
    local pane_text="$1" last_lines
    claude_trust_dialog_present "$pane_text" && return 1
    last_lines="$(printf '%s' "$pane_text" | pane_nonblank_tail 8)"
    printf '%s' "$last_lines" | grep -qE 'for shortcuts' && return 0
    # Only an empty input row counts. A selected dialog row also starts with
    # the cursor glyph ("❯ No, exit") and must not read as ready.
    printf '%s' "$last_lines" | grep -qE '^[[:space:]]*❯[[:space:]]*$' && return 0
    return 1
}

claude_accept_trust_dialog() {
    local session_name="$1"
    local attempt="$2"
    local max_attempts="$3"
    local log_prefix="${4:-spawn_child}"
    if (( attempt > max_attempts )); then
        echo "[$log_prefix] Claude trust dialog persisted after ${max_attempts} attempts; aborting" >&2
        return 1
    fi
    local pane_text
    pane_text="$(tmux capture-pane -t "$session_name" -p 2>/dev/null || true)"
    if printf '%s' "$pane_text" | grep -q "Yes, I trust this folder"; then
        # New dialog: the default row is "No, exit", so a bare Enter ends the
        # child (observed 2026-09-03: the session lived 4 seconds). Move to the
        # Yes row and confirm it is selected before confirming.
        if ! printf '%s' "$pane_text" | grep -qE '❯[[:space:]]*Yes, I trust'; then
            tmux send-keys -t "$session_name" Down
            sleep 1
            pane_text="$(tmux capture-pane -t "$session_name" -p 2>/dev/null || true)"
        fi
        if printf '%s' "$pane_text" | grep -qE '❯[[:space:]]*Yes, I trust'; then
            echo "[$log_prefix] Claude trust dialog detected; selecting 'Yes, I trust this folder' (${attempt}/${max_attempts})" >&2
            tmux send-keys -t "$session_name" C-m
        else
            echo "[$log_prefix] Claude trust dialog detected but the Yes row is not selected; not pressing Enter (${attempt}/${max_attempts})" >&2
        fi
        return 0
    fi
    echo "[$log_prefix] Claude trust dialog detected; accepting with C-m (${attempt}/${max_attempts})" >&2
    tmux send-keys -t "$session_name" C-m
}

# Record prompt-delivery evidence outside the launcher's stderr. Dashboard and
# hook callers commonly trim command output, so stderr alone is not durable
# enough for a child that started successfully but never received its task.
SPAWN_INCIDENT_LOG="${AGENTSTACK_SPAWN_INCIDENT_LOG:-$RUNTIME_DIR/spawn_incidents.log}"
INJECTION_VERIFIED=false
SPAWN_TRAP_SESSION=""

spawn_note() {
    local message="$1"
    echo "[spawn_child] $message" >&2
    mkdir -p "$(dirname "$SPAWN_INCIDENT_LOG")" 2>/dev/null || true
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$message" \
        >> "$SPAWN_INCIDENT_LOG" 2>/dev/null || true
}

# Claude can accept a submit while startup is still settling but leave it as a
# queued message. With no active turn, that queue never drains and the child
# waits forever. An empty submit creates the turn that flushes the queued task.
flush_queued_prompt() {
    local session_name="$1"
    local waited=0
    local pane_text
    while (( waited < 20 )); do
        pane_text="$(tmux capture-pane -t "$session_name" -p 2>/dev/null || true)"
        if ! printf '%s' "$pane_text" | grep -qF "edit queued messages"; then
            return 0
        fi
        echo "[spawn_child] Prompt is still queued; submitting an empty turn to flush it ($session_name)" >&2
        tmux send-keys -t "$session_name" C-m
        sleep 3
        waited=$((waited + 3))
    done
    spawn_note "WARNING: prompt did not leave the queued-message state ($session_name); press Enter once in the child session"
    return 1
}

# Verify delivery against pane scrollback, not only the visible viewport. Long
# embedded tasks push their first line off screen immediately. Normalize line
# wrapping and spaces before matching a short literal prefix of the prompt.
verify_injection() {
    local session_name="$1"
    local prompt_text="$2"
    local needle
    local waited=0
    local pane_text
    needle="$(printf '%s' "${prompt_text:0:40}" | tr -d '\n ')"
    [[ -n "$needle" ]] || return 0
    while (( waited < 10 )); do
        pane_text="$(tmux capture-pane -t "$session_name" -p -S -1000 2>/dev/null || true)"
        if printf '%s' "$pane_text" | tr -d '\n ' | grep -qF -- "$needle"; then
            INJECTION_VERIFIED=true
            spawn_note "injected ok ($session_name)"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    spawn_note "WARNING: injection FAILED ($session_name): the REPL is ready but the task text was not found; resend it or close the child session"
    return 1
}

# Existing failure cleanup terminates a half-started child. Preserve that
# stronger repository contract, while leaving durable evidence that prompt
# delivery was never verified before cleanup ran.
warn_if_uninjected() {
    [[ -n "$SPAWN_TRAP_SESSION" ]] || return 0
    [[ "$INJECTION_VERIFIED" == true ]] && return 0
    spawn_note "WARNING: session $SPAWN_TRAP_SESSION ended before prompt injection was verified; launcher cleanup will remove the incomplete child"
}

# --- Authenticated per-child MCP connection ------------------------------
# Writes a child-scoped --mcp-config that points orrery-mail at the local
# stdio proxy instead of the shared HTTP endpoint. The proxy holds the child's
# owner token and authenticates every call on its behalf, so the child can read
# its OWN inbox (and nobody else's) without the token ever entering its context.
#
# Prints the config path, or nothing when the proxy is unavailable — callers
# fall back to the shared endpoint rather than failing the spawn.
write_child_mcp_config() {
    local child_name="$1" token_file="$2"
    local runner="${AGENTSTACK_MCP_PROXY:-${AGENTSTACK_HOME_DIR:-$HOME/.agentstack}/integrations/codex_app/plugin/scripts/run-mcp.sh}"
    [[ -n "$token_file" && -f "$token_file" && -x "$runner" ]] || return 0

    local config_dir="$RUNTIME_DIR/child-agents"
    local config_path="$config_dir/${child_name}.mcp.json"
    mkdir -p "$config_dir" || return 0
    python3 - "$config_path" "$runner" "$child_name" "$PROJECT_KEY" "$token_file" \
        "$MCP_URL" "$MAIL_ENV" "$RUNTIME_DIR" \
        "${AGENTSTACK_CLAUDE_JSON:-$HOME/.claude.json}" "$HTTP_BEARER_MODE" <<'PY' || return 0
# NOTE: no line here may start with "}" in column 0 — the shell function is
# extracted by "up to the first line that is just a closing brace".
import json
import os
import sys

path, runner, child, project_key, token_file, mcp_url, mail_env, runtime_dir, claude_json, bearer_mode = sys.argv[1:11]
server_env = dict(
    AGENTSTACK_PROXY_AGENT_NAME=child,
    AGENTSTACK_PROXY_TOKEN_FILE=token_file,
    AGENTSTACK_PROXY_PROGRAM="claude-code",
    AGENTSTACK_PROJECT_KEY=project_key,
    AGENTSTACK_MCP_URL=mcp_url,
    AGENTSTACK_MAIL_ENV=mail_env,
    # The MCP client starts the proxy with this table only, not the child's
    # shell environment. Without the bearer mode the proxy defaulted to
    # "auto", found no HTTP_BEARER_TOKEN in a bearer-disabled service env
    # and exited before answering initialize (2026-09-03).
    AGENTSTACK_MAIL_HTTP_BEARER_MODE=bearer_mode,
    AGENTSTACK_RUNTIME_DIR=runtime_dir,
)
server = dict(command=runner, args=[], env=server_env)


def looks_like_agent_mail(name):
    normalized = name.replace("-", "").replace("_", "").lower()
    return normalized in {"agentmail", "mcpagentmail", "agentstackmail", "orrerymail"}


# The child inherits the user's own MCP servers, including their DIRECT
# agent-mail connection. Publishing the proxy under a NEW name just adds a
# second agent-mail, and the model reaches for the name it knows — the direct,
# unauthenticated one. --mcp-config overrides a same-named server (measured),
# so claim the names the user already uses as compatibility aliases as well as
# the canonical product key.
names = set()
try:
    with open(claude_json, encoding="utf-8") as handle:
        settings = json.load(handle)
except Exception:
    settings = dict()
scopes = [settings.get("mcpServers")]
projects = settings.get("projects")
if isinstance(projects, dict):
    for scope in projects.values():
        if isinstance(scope, dict):
            scopes.append(scope.get("mcpServers"))
for scope in scopes:
    if isinstance(scope, dict):
        names.update(name for name in scope if looks_like_agent_mail(name))
if not names:
    names = {"orrery-mail"}
else:
    names.add("orrery-mail")

config = dict(mcpServers=dict((name, server) for name in sorted(names)))
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
os.chmod(path, 0o600)
PY
    printf '%s\n' "$config_path"
}

# Ready = the REPL is accepting input. Do not depend on a single footer string:
# the footer varies with terminal width, model and configuration. The reported
# 90s hang came from a pane showing "gpt-5.5 xhigh · ~/obsidian", which has no
# "% left" segment at all.
codex_pane_ready() {
    local pane_text="$1" last_lines
    printf '%s' "$pane_text" | grep -qF "Use existing model" && return 1
    last_lines="$(printf '%s' "$pane_text" | pane_nonblank_tail 5)"
    printf '%s' "$last_lines" | grep -qE '% (left|context)' && return 0
    printf '%s' "$last_lines" | grep -qE 'for shortcuts' && return 0
    # Footer form "<model> <effort> · <cwd>" (no context segment).
    printf '%s' "$last_lines" | grep -qE '(gpt|codex|o[0-9])[^ ]* .*·' && return 0
    # Bare input prompt.
    printf '%s' "$last_lines" | grep -qE '^[[:space:]]*[▌❯>][[:space:]]*$' && return 0
    return 1
}

# Codex has no per-session --mcp-config equivalent: `-c mcp_servers...` replaces
# the whole table (dropping the transport keys, which fails config load) and its
# effect cannot be inspected, so a child gets its own CODEX_HOME instead. Only
# config.toml is rewritten; everything else (auth.json, sessions, plugins) is
# symlinked to the real home, and `CODEX_HOME=<dir> codex mcp get orrery-mail`
# shows exactly what the child will use.
#
# Prints the directory, or nothing when the proxy or token is unavailable.
write_child_codex_home() {
    local child_name="$1" token_file="$2"
    local runner="${AGENTSTACK_MCP_PROXY:-${AGENTSTACK_HOME_DIR:-$HOME/.agentstack}/integrations/codex_app/plugin/scripts/run-mcp.sh}"
    local source_home="${CODEX_HOME:-$HOME/.codex}"
    [[ -n "$token_file" && -f "$token_file" && -x "$runner" && -d "$source_home" ]] || return 0

    local home_dir="$RUNTIME_DIR/child-agents/${child_name}.codex-home"
    python3 - "$home_dir" "$source_home" "$runner" "$child_name" "$PROJECT_KEY" \
        "$token_file" "$MCP_URL" "$MAIL_ENV" "$RUNTIME_DIR" "$HTTP_BEARER_MODE" <<'PY' || return 0
import os
import pathlib
import re
import sys

home, source, runner, child, project_key, token_file, mcp_url, mail_env, runtime_dir, bearer_mode = sys.argv[1:11]
home_path = pathlib.Path(home)
source_path = pathlib.Path(source)
home_path.mkdir(parents=True, exist_ok=True)

# Everything except config.toml stays shared with the real home, so the child
# keeps its login and writes its session history where the user expects it.
for entry in source_path.iterdir():
    if entry.name == "config.toml":
        continue
    link = home_path / entry.name
    if link.is_symlink() or link.exists():
        if link.is_symlink():
            link.unlink()
        else:
            continue
    os.symlink(entry, link)

def looks_like_agent_mail(name):
    normalized = name.replace("-", "").replace("_", "").replace('"', "").lower()
    return normalized in {"agentmail", "mcpagentmail", "agentstackmail", "orrerymail"}


def toml_string(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def plugin_name(header):
    match = re.match(r'^plugins\.(?:"([^"]+)"|([A-Za-z0-9_-]+))', header)
    return (match.group(1) or match.group(2)) if match else ""


# `--ask-for-approval never` governs model-generated shell commands, not MCP
# calls. Codex otherwise prompts for these tools even though this proxy is
# already child-bound and exposes only this fixed coordination surface.
proxy_tools = (
    "bootstrap",
    "fetch_inbox",
    "send_message",
    "acknowledge_message",
    "reserve_files",
    "renew_reservations",
    "release_reservations",
    "runtime_status",
)


# Strip EVERY agent-mail server the user has, not just one spelling. A child
# that still sees the direct connection will use it — the model reaches for the
# name it knows — and that connection is not authenticated as the child.
lines = []
skipping = False
claimed = []
plugin_ids = []
config_source = source_path / "config.toml"
text = config_source.read_text(encoding="utf-8") if config_source.exists() else ""
for line in text.splitlines():
    stripped = line.strip()
    if stripped.startswith("["):
        header = stripped.strip("[]").strip()
        server = ""
        if header.startswith("mcp_servers."):
            server = header[len("mcp_servers."):].split(".")[0]
        name = server.strip('"')
        plugin_id = plugin_name(header)
        if plugin_id.startswith("agentstack-codex-app@"):
            if plugin_id not in plugin_ids:
                plugin_ids.append(plugin_id)
            plugin_prefix = "plugins." + toml_string(plugin_id)
            skipping = header.startswith(plugin_prefix + ".mcp_servers.agentstack")
        else:
            skipping = False
        if name and (looks_like_agent_mail(name) or name == "agentstack"):
            skipping = True
        if skipping:
            if name and name not in claimed:
                claimed.append(name)
    if not skipping:
        lines.append(line)
if not claimed:
    claimed = ["orrery-mail"]
elif "orrery-mail" not in claimed:
    claimed.append("orrery-mail")
# Codex's deferred tool registry identifies this proxy by serverInfo.name
# (`agentstack`), while direct MCP calls use the configured server key. Claim
# both so the same per-tool policy applies through either path.
if "agentstack" not in claimed:
    claimed.append("agentstack")

lines.append("")
lines.append("# Written by spawn_child.sh: this child talks to ORRERY Mail through the")
lines.append("# local proxy, which authenticates every call with the child's own token.")
lines.append("# The proxy claims the same server name(s) the user's own config used,")
lines.append("# so the model's habitual call lands on the authenticated connection.")
for plugin_id in plugin_ids:
    lines.append("")
    lines.append("[plugins." + toml_string(plugin_id) + ".mcp_servers.agentstack]")
    lines.append("enabled = false")
for name in claimed:
    key = "mcp_servers." + toml_string(name)
    lines.append("")
    lines.append("[" + key + "]")
    lines.append("command = " + toml_string(runner))
    lines.append("args = []")
    lines.append("")
    lines.append("[" + key + ".env]")
    lines.append("AGENTSTACK_PROXY_AGENT_NAME = " + toml_string(child))
    lines.append("AGENTSTACK_PROXY_TOKEN_FILE = " + toml_string(token_file))
    lines.append("AGENTSTACK_PROXY_PROGRAM = " + toml_string("codex"))
    lines.append("AGENTSTACK_PROJECT_KEY = " + toml_string(project_key))
    lines.append("AGENTSTACK_MCP_URL = " + toml_string(mcp_url))
    lines.append("AGENTSTACK_MAIL_ENV = " + toml_string(mail_env))
    # Codex starts the proxy with this env table only; see the Claude writer.
    lines.append("AGENTSTACK_MAIL_HTTP_BEARER_MODE = " + toml_string(bearer_mode))
    lines.append("AGENTSTACK_RUNTIME_DIR = " + toml_string(runtime_dir))
    # run-mcp.sh also reads the machine-wide Codex App env for missing values.
    # Pin its state inside this child-owned home so a bridge install cannot
    # redirect the sandboxed child back into the live bridge runtime.
    lines.append("AGENTSTACK_CODEX_APP_RUNTIME_DIR = " + toml_string(
        os.fspath(home_path / "proxy-runtime")
    ))
    for tool_name in proxy_tools:
        lines.append("")
        lines.append("[" + key + ".tools." + toml_string(tool_name) + "]")
        lines.append("approval_mode = \"approve\"")

target = home_path / "config.toml"
target.write_text("\n".join(lines) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
PY
    printf '%s\n' "$home_dir"
}

# Generate the direct-spawn registration token in a 0600 one-shot file.  The
# token itself never crosses a shell argument boundary.
generate_child_token_file() {
    local token_file="$1"
    mkdir -p "$(dirname "$token_file")"
    chmod 700 "$(dirname "$token_file")" 2>/dev/null || true
    python3 - "$token_file" <<'PY'
import os
import secrets
import sys

path = sys.argv[1]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags, 0o600)
try:
    os.write(fd, secrets.token_urlsafe(32).encode("utf-8"))
    os.fsync(fd)
finally:
    os.close(fd)
os.chmod(path, 0o600)
PY
}

# Persist the token actually returned by register_agent (some servers replace
# the caller-supplied value), falling back to the sent one-shot token.  The MCP
# response is supplied on stdin and the secret remains file-only.
adopt_registered_token_response() {
    local agent_name="$1" project_key="$2" sent_token_file="$3"
    local token_file state_file
    token_file="$(child_token_file_path "$agent_name")" || return 1
    state_file="$CHILD_STATE_DIR/$agent_name.json"
    python3 -c '
import json
import os
import pathlib
import sys

agent_name, project_key, sent_file, token_file, state_file = sys.argv[1:6]
response = json.load(sys.stdin)

def candidate_tokens(obj):
    if isinstance(obj, dict):
        value = obj.get("registration_token")
        if isinstance(value, str) and value:
            yield value

token = ""
objects = [response]
result = response.get("result") if isinstance(response, dict) else None
objects.append(result)
if isinstance(result, dict):
    objects.append(result.get("structuredContent"))
    for part in result.get("content") or []:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            continue
        try:
            objects.append(json.loads(part["text"]))
        except Exception:
            pass
for obj in objects:
    token = next(candidate_tokens(obj), "")
    if token:
        break
if not token:
    token = pathlib.Path(sent_file).read_text(encoding="utf-8").strip()
if not token:
    raise ValueError("register_agent returned no usable registration token")

token_path = pathlib.Path(token_file)
state_path = pathlib.Path(state_file)
token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(token_path.parent, 0o700)
os.chmod(state_path.parent, 0o700)
token_tmp = token_path.with_name(token_path.name + f".tmp.{os.getpid()}")
state_tmp = state_path.with_name(state_path.name + f".tmp.{os.getpid()}")
with open(token_tmp, "x", encoding="utf-8") as handle:
    handle.write(token)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(token_tmp, 0o600)
with open(state_tmp, "x", encoding="utf-8") as handle:
    json.dump({
        "agent_name": agent_name,
        "project_key": project_key,
        "registration_token": token,
    }, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(state_tmp, 0o600)
os.replace(token_tmp, token_path)
os.replace(state_tmp, state_path)
os.chmod(token_path, 0o600)
os.chmod(state_path, 0o600)
pathlib.Path(sent_file).unlink(missing_ok=True)
print(token_path)
' "$agent_name" "$project_key" "$sent_token_file" "$token_file" "$state_file"
}

build_embedded_task_prompt() {
    local child_name="$1"
    local parent_name="$2"
    local spawned_at="$3"
    local project_key="$4"
    local task_text="$5"
    printf 'あなたは %s（親: %s）。この起動は --embed-task mode です。ORRERY Mail への登録は親が完了済み・儀式不要です。ensure_project・register_agent・fetch_inbox は実行しないでください。現在時刻: %s。project_key は %s。以下のタスクが正本です。直ちに開始し、完了したら send_message で %s に報告してください:\n\n%s' \
        "$child_name" "$parent_name" "$spawned_at" "$project_key" \
        "$parent_name" "$task_text"
}

TASK="${1:-}"
WORK_DIR="${2:-$(pwd)}"
if [[ -n "$TASK_FILE" ]]; then
    if [[ ! -f "$TASK_FILE" || ! -r "$TASK_FILE" ]]; then
        echo "Error: --task-file not readable: $TASK_FILE" >&2
        exit 1
    fi
    TASK="$(cat "$TASK_FILE")"
    # With no positional TASK, the first positional argument is the workdir.
    # If both positionals are present, TASK_FILE overrides $1 and $2 remains
    # the workdir, preserving the existing positional contract.
    if [[ -d "${1:-}" && -z "${2:-}" ]]; then
        WORK_DIR="$1"
    fi
fi
CHILD_STATE_DIR="$RUNTIME_DIR/child-agents"

if [[ -z "$PROJECT_KEY" ]]; then
    echo "Error: AGENTSTACK_PROJECT_KEY or PROJECT_KEY is required" >&2
    echo "  Set it to the shared ORRERY Mail project key before spawning a child." >&2
    echo "  For delegated children this may differ from the child workdir or git repo cwd." >&2
    exit 1
fi

# --- Pre-registered mode ---
# 親エージェントが MCP 経由で事前に register_agent / file_reservation_paths を
# 済ませてから呼ぶモード。通常は task mail を正本にし、--embed-task 使用時は
# task mail を送らず起動 prompt を正本にする。
# Usage: spawn_child.sh --pre-registered <CHILD_NAME> --child-token-file <path> "<タスク>" [<作業ディレクトリ>]
if [[ -n "$PRE_REGISTERED" ]]; then
    CHILD_NAME="$PRE_REGISTERED"
    # Both providers share the catalog/normalizers above in every launch path.
    if [[ "$USE_CODEX" == true ]]; then
        CHILD_MODEL="$(normalize_codex_model "$CLAUDE_MODEL")"
        CODEX_EFFORT="$(validate_codex_effort "$CHILD_MODEL" "$CODEX_EFFORT")"
    else
        CHILD_MODEL="$(normalize_claude_model "$CLAUDE_MODEL")"
    fi
    if [[ "$STANDALONE" == true ]]; then
        PARENT_NAME=""
    else
        PARENT_NAME="${PARENT_AGENT:-$(tmux display-message -p '#S' 2>/dev/null || echo unknown)}"
        if [[ "$PARENT_NAME" == "unknown" || -z "$PARENT_NAME" ]]; then
            echo "Error: parent agent name required unless --standalone is set" >&2
            exit 1
        fi
    fi

    if [[ -z "$TASK" ]]; then
        echo "Usage: spawn_child.sh --pre-registered <CHILD_NAME> --child-token-file <path> \"<task>\" [workdir]" >&2
        exit 1
    fi
    if [[ ! -d "$WORK_DIR" ]]; then
        echo "Error: workdir does not exist: $WORK_DIR" >&2
        exit 1
    fi

    EMBEDDED_TASK_PROMPT=""
    if [[ "$EMBED_TASK" == true ]]; then
        SPAWNED_AT="$(date '+%Y-%m-%dT%H:%M %Z')"
        EMBEDDED_TASK_PROMPT="$(
            build_embedded_task_prompt "$CHILD_NAME" "$PARENT_NAME" \
                "$SPAWNED_AT" "$PROJECT_KEY" "$TASK"
        )"
        echo "[spawn_child/embed-task] WARNING: the launch prompt is canonical; do not send task mail to $CHILD_NAME (a second task source would split authority)." >&2
    fi

    # Pre-registered children must use their own token. Never inherit the
    # caller's ambient CHILD_REGISTRATION_TOKEN here; that may be the parent's
    # owner token. Adopt the 0600 one-shot into durable child-owned files and
    # unlink the handoff only after both writes succeed.
    PRE_REGISTERED_TOKEN_CREATED=false
    PRE_REGISTERED_SESSION_STARTED=false
    PRE_REGISTERED_SUCCESS=false
    cleanup_preregister_failure() {
        if [[ "$PRE_REGISTERED_SUCCESS" == true ]]; then
            return
        fi
        warn_if_uninjected
        if [[ "$PRE_REGISTERED_SESSION_STARTED" == true ]]; then
            tmux kill-session -t "=$CHILD_NAME" >/dev/null 2>&1 || true
        fi
        if [[ "$PRE_REGISTERED_TOKEN_CREATED" == true ]]; then
            rm -f "$CHILD_TOKEN_FILE" "$CHILD_STATE_DIR/$CHILD_NAME.json"
        fi
        cleanup_worktree
        if [[ -f "$MANAGED_FILE" ]]; then
            python3 - "$MANAGED_FILE" "$CHILD_NAME" <<'PY' 2>/dev/null || true
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except OSError:
    raise SystemExit(0)
path.write_text(
    "\n".join(line for line in lines if line != name) + "\n",
    encoding="utf-8",
)
PY
        fi
    }
    trap cleanup_preregister_failure EXIT

    if [[ -n "$CHILD_TOKEN_FILE" ]]; then
        ONE_SHOT_TOKEN_FILE="$CHILD_TOKEN_FILE"
        if ! CHILD_TOKEN_FILE="$(
            adopt_child_token_file "$CHILD_NAME" "$PROJECT_KEY" \
                "$ONE_SHOT_TOKEN_FILE" true
        )"; then
            echo "Error: --child-token-file is unreadable, insecure, or empty: $ONE_SHOT_TOKEN_FILE" >&2
            exit 1
        fi
        PRE_REGISTERED_TOKEN_CREATED=true
    else
        CHILD_TOKEN_FILE="$(child_token_file_path "$CHILD_NAME")"
        if [[ ! -s "$CHILD_TOKEN_FILE" ]]; then
            if ! CHILD_TOKEN_FILE="$(
                restore_child_token_file_from_state "$CHILD_NAME"
            )"; then
                echo "Error: pre-registered child token is required for $CHILD_NAME" >&2
                echo "  Generate/register the child with a child-owned token, then pass --child-token-file <path>." >&2
                echo "  Existing state fallback: $CHILD_STATE_DIR/$CHILD_NAME.json" >&2
                exit 1
            fi
        fi
    fi

    # --worktree が指定されていれば worktree を作って WORK_DIR を上書き
    if [[ "$USE_WORKTREE" == true ]]; then
        if ! maybe_create_worktree "$CHILD_NAME" "$WORK_DIR"; then
        echo "[spawn_child/pre-reg] Worktree creation failed; aborting spawn." >&2
        exit 1
    fi
        WORK_DIR="$WORKTREE_DIR"
        echo "[spawn_child/pre-reg] WORK_DIR overridden to worktree: $WORK_DIR" >&2
    fi

    if ! grep -qxF "$CHILD_NAME" "$MANAGED_FILE" 2>/dev/null; then
        mkdir -p "$(dirname "$MANAGED_FILE")"
        echo "$CHILD_NAME" >> "$MANAGED_FILE"
    fi

    # Warn (do not block) if the child's workdir is a macOS privacy-protected
    # folder this process can't read — turns an undiagnosable EPERM into advice.
    declare -F ags_warn_tcc_access >/dev/null 2>&1 && ags_warn_tcc_access "$WORK_DIR"

    # Create tmux session and optionally open a terminal window.
    # CLAUDECODE=1 guards the child session's interactive shell against destructive
    # shell exit hooks (e.g. a ~/.zshrc zshexit / bash trap that runs `tmux
    # kill-session`): without it, exiting this session can cascade-kill the whole
    # tmux server. Requires tmux >= 3.0.
    TMUX_ENV_ARGS=(-e "CLAUDECODE=1" -e "AGENTSTACK_RESERVED_IDENTITY=1" -e "AGENT_NAME=$CHILD_NAME" -e "PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_HOOKS_DIR=$HOOKS_DIR" -e "AGENTSTACK_RUNTIME_DIR=$RUNTIME_DIR" -e "AGENTSTACK_MCP_URL=$MCP_URL" -e "AGENTSTACK_MAIL_ENV=$MAIL_ENV" -e "AGENTSTACK_MAIL_HTTP_BEARER_MODE=$HTTP_BEARER_MODE" -e "AGENTSTACK_TERMINAL=$TERMINAL_SETTING" -e "AGENTSTACK_CODEX_APPROVAL=$(codex_approval_flags)" -e "AGENTSTACK_CODEX_NETWORK_FLAGS=$(codex_network_flags)")
    if [[ "$STANDALONE" != true ]]; then
        TMUX_ENV_ARGS+=(-e "PARENT_AGENT=$PARENT_NAME")
    fi
    if [[ -n "$AGENTSTACK_HOME_DIR" ]]; then
        TMUX_ENV_ARGS+=(-e "AGENTSTACK_HOME=$AGENTSTACK_HOME_DIR")
    fi
    if [[ "$USE_CODEX" == true ]]; then
        # Codex startup (--pre-registered mode).
        TMUX_ENV_ARGS+=(-e "AGENTSTACK_CODEX_MODEL=$CHILD_MODEL" -e "AGENTSTACK_CODEX_EFFORT=$CODEX_EFFORT")
        CHILD_CODEX_HOME="$(write_child_codex_home "$CHILD_NAME" "$CHILD_TOKEN_FILE")"
        TMUX_ENV_ARGS+=(-e "AGENTSTACK_CODEX_ADD_DIRS_RESOLVED=$(codex_child_add_dirs "$CHILD_CODEX_HOME")")
        if [[ -n "$CHILD_CODEX_HOME" ]]; then
            echo "[spawn_child/pre-reg] Child CODEX_HOME with authenticated agent-mail: $CHILD_CODEX_HOME" >&2
            TMUX_ENV_ARGS+=(
                -e "CODEX_HOME=$CHILD_CODEX_HOME"
                -e "CODEX_SHARED_CODEX_DIR=$CHILD_CODEX_HOME"
            )
        else
            echo "[spawn_child/pre-reg] No MCP proxy available; Codex child uses the shared agent-mail endpoint" >&2
        fi
        if [[ "$STANDALONE" == true ]]; then
            CODEX_PROMPT="You are ${CHILD_NAME}, a standalone agent with no parent. The name ${CHILD_NAME} is already reserved; do not register another identity. This prompt is the canonical task. Start it immediately:

${TASK}"
        elif [[ "$EMBED_TASK" == true ]]; then
            CODEX_PROMPT="$EMBEDDED_TASK_PROMPT"
        else
            CODEX_PROMPT="You are ${CHILD_NAME}. The parent agent is ${PARENT_NAME}. The child name ${CHILD_NAME} is already reserved, so do not register under another name. The canonical task is in your ORRERY Mail inbox. First, if ${REREGISTER_HELPER:-agentstack-reregister} exists, run PROJECT_KEY=${PROJECT_KEY} ${REREGISTER_HELPER:-agentstack-reregister} ${CHILD_NAME}; when that succeeds, skip register_agent and fetch_inbox for ${CHILD_NAME}. The helper reads the child-owned 0600 token file; never request or print its token. Do not infer the task from this prompt; treat the inbox request as authoritative."
        fi
        tmux new-session -d -s "$CHILD_NAME" \
            -c "$WORK_DIR" \
            "${TMUX_ENV_ARGS[@]}" \
            '/bin/zsh -lc '"'"'
                export PATH="$HOME/.local/bin:$PATH";
                # The child never sources a user-side bootstrap: identity comes
                # from the reserved name and token file, and a failing script
                # under set -e would take the whole session with it (2026-09-03).
                # The product decides sandbox, approval, network and writable
                # roots itself; a user-side launcher (~/.codex/bin/...) is never
                # consulted. Handing off to one silently replaced `never` with
                # its `on-request` default and dropped the network flag and the
                # extra roots (2026-09-04).
                EXTRA_ARGS=()
                for d in ${(s.:.)AGENTSTACK_CODEX_ADD_DIRS_RESOLVED}; do
                    [[ -d "$d" ]] && EXTRA_ARGS+=(--add-dir "$d")
                done
                env -u OPENAI_API_KEY codex -C "$PWD" --sandbox workspace-write ${=AGENTSTACK_CODEX_APPROVAL} ${=AGENTSTACK_CODEX_NETWORK_FLAGS} \
                    "${EXTRA_ARGS[@]}" --model "$AGENTSTACK_CODEX_MODEL" -c "model_reasoning_effort=$AGENTSTACK_CODEX_EFFORT"
                /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"
            '"'"''
        PRE_REGISTERED_SESSION_STARTED=true
        SPAWN_TRAP_SESSION="$CHILD_NAME"

        echo "[spawn_child/pre-reg] Waiting for Codex REPL..." >&2
        WAITED=0
        WAIT_MAX=90
        READY=false
        DIED=false
        TRUST_FAILED=false
        TRUST_ATTEMPTS=0
        TRUST_MAX=10
        while [[ $WAITED -lt $WAIT_MAX ]]; do
            sleep 3
            WAITED=$((WAITED + 3))
            PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p 2>/dev/null || true)
            if echo "$PANE_TEXT" | grep -qF "Use existing model"; then
                echo "[spawn_child/pre-reg] Model selection dialog detected; choosing existing model" >&2
                tmux send-keys -t "$CHILD_NAME" Down Enter
                sleep 5
                continue
            fi
            # Trust ダイアログ: "Do you trust the contents of this directory?"
            if codex_trust_dialog_present "$PANE_TEXT"; then
                TRUST_ATTEMPTS=$((TRUST_ATTEMPTS + 1))
                if ! codex_accept_trust_dialog \
                    "$CHILD_NAME" "$TRUST_ATTEMPTS" "$TRUST_MAX" "spawn_child/pre-reg"; then
                    TRUST_FAILED=true
                    break
                fi
                sleep 3
                continue
            fi
            if echo "$PANE_TEXT" | grep -qi "Press enter to continue"; then
                echo "[spawn_child/pre-reg] Sign-in prompt detected; pressing Enter" >&2
                tmux send-keys -t "$CHILD_NAME" Enter
                sleep 3
                continue
            fi
            if codex_pane_ready "$PANE_TEXT"; then
                READY=true
                break
            fi
            if ! codex_session_alive "$CHILD_NAME"; then
                echo "[spawn_child/pre-reg] Codex session '$CHILD_NAME' died after ${WAITED}s; last pane output:" >&2
                printf '%s\n' "$PANE_TEXT" | tail -15 >&2
                DIED=true
                break
            fi
        done

        if [[ "$TRUST_FAILED" == true ]]; then
            echo "[spawn_child/pre-reg] Aborting: unable to accept the Codex trust dialog." >&2
            exit 1
        elif [[ "$DIED" == true ]]; then
            echo "[spawn_child/pre-reg] Aborting: the child exited before becoming ready (check the codex flags above)." >&2
            exit 1
        elif [[ "$READY" == true ]]; then
            sleep 2
            echo "[spawn_child/pre-reg] Waited ${WAITED}s (+2s); injecting prompt" >&2
        else
            echo "[spawn_child/pre-reg] Codex readiness timeout (${WAIT_MAX}s); refusing to inject the task into an unknown screen state." >&2
            printf '%s\n' "$PANE_TEXT" | tail -15 >&2
            exit 1
        fi

        if [[ "$STANDALONE" == true || "$EMBED_TASK" == true ]]; then
            tmux send-keys -t "$CHILD_NAME" -l "$(printf '\033[200~')${CODEX_PROMPT}$(printf '\033[201~')"
        else
            tmux send-keys -t "$CHILD_NAME" -l "$CODEX_PROMPT"
        fi
        sleep 0.5
        tmux send-keys -t "$CHILD_NAME" C-m
        verify_injection "$CHILD_NAME" "$CODEX_PROMPT" || true
    else
        # Claude Code startup (--pre-registered mode).
        WARM_POOL="$HOOKS_DIR/warm_pool.sh"
        # warm pool は current 200K opus / sonnet generation で
        # 事前起動している。要求モデル（正規化済み CHILD_MODEL）が warm の事前起動モデルと
        # 完全一致するときだけ claim する。それ以外（legacy [1m] / fable / haiku /
        # sonnet[1m] 等）は __skip_warm__ で cold-start し、$CLAUDE_CHILD_MODEL を尊重する。
        # 旧実装は部分一致（*opus* + *[1m]* skip）だったため、opus[1m] は skip できても
        # fable 等の非デフォルトモデルが warm-sonnet に握り潰されていた（RainyKepler 事例）。
        # exact-match に広げて [1m] 以外の降格も塞ぐ。
        if [[ "$STANDALONE" == true ]]; then
            # A claimed warm session may retain a parent environment. Cold
            # start standalone children so PARENT_AGENT is guaranteed absent.
            WARM_TYPE="__skip_warm__"
        else
            case "$CHILD_MODEL" in
                "$CLAUDE_WARM_OPUS_MODEL")   WARM_TYPE="opus" ;;
                "$CLAUDE_WARM_SONNET_MODEL") WARM_TYPE="sonnet" ;;
                *)                              WARM_TYPE="__skip_warm__" ;;
            esac
        fi

        WARM_CLAIMED=false
        WARM_STATUS=$(bash "$WARM_POOL" status 2>/dev/null || true)
        if [[ -f "$WARM_POOL" ]] && echo "$WARM_STATUS" | grep -q "${WARM_TYPE}.*ready"; then
            echo "[spawn_child/pre-reg] Claiming warm pool session ($WARM_TYPE)..." >&2
            if CLAIMED_NAME=$(bash "$WARM_POOL" claim "$WARM_TYPE" "$CHILD_NAME" 2>/dev/null); then
                WARM_CLAIMED=true
                PRE_REGISTERED_SESSION_STARTED=true
                SPAWN_TRAP_SESSION="$CHILD_NAME"
                echo "[spawn_child/pre-reg] Warm session claimed -> $CHILD_NAME" >&2
            fi
        fi

        if [[ "$WARM_CLAIMED" == false ]]; then
            # Cold start（フォールバック）
            echo "[spawn_child/pre-reg] Cold start..." >&2
            CHILD_MCP_CONFIG="$(write_child_mcp_config "$CHILD_NAME" "$CHILD_TOKEN_FILE")"
            if [[ -n "$CHILD_MCP_CONFIG" ]]; then
                echo "[spawn_child/pre-reg] Child MCP proxy config: $CHILD_MCP_CONFIG" >&2
            else
                echo "[spawn_child/pre-reg] No MCP proxy available; child uses the shared agent-mail endpoint" >&2
            fi
            tmux new-session -d -s "$CHILD_NAME" \
                -c "$WORK_DIR" \
                "${TMUX_ENV_ARGS[@]}" \
                -e "CLAUDE_CHILD_MODEL=$CHILD_MODEL" \
                -e "CLAUDE_CHILD_MCP_CONFIG=$CHILD_MCP_CONFIG" \
                '/bin/zsh -lc '"'"'export PATH="$HOME/.local/bin:$PATH"; MCP_ARGS=(); [[ -n "$CLAUDE_CHILD_MCP_CONFIG" ]] && MCP_ARGS=(--mcp-config "$CLAUDE_CHILD_MCP_CONFIG" --strict-mcp-config); claude --model "$CLAUDE_CHILD_MODEL" "${MCP_ARGS[@]}"; /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"'"'"''
            PRE_REGISTERED_SESSION_STARTED=true
            SPAWN_TRAP_SESSION="$CHILD_NAME"

            WAITED=0
            READY=false
            CLAUDE_EXITED=false
            TRUST_FAILED=false
            TRUST_ATTEMPTS=0
            TRUST_MAX=5
            while [[ $WAITED -lt 60 ]]; do
                sleep 2
                WAITED=$((WAITED + 2))
                PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p 2>/dev/null || true)
                if claude_trust_dialog_present "$PANE_TEXT"; then
                    TRUST_ATTEMPTS=$((TRUST_ATTEMPTS + 1))
                    if ! claude_accept_trust_dialog \
                        "$CHILD_NAME" "$TRUST_ATTEMPTS" "$TRUST_MAX" "spawn_child/pre-reg"; then
                        TRUST_FAILED=true
                        break
                    fi
                    sleep 1
                    continue
                fi
                if claude_pane_ready "$PANE_TEXT"; then
                    READY=true
                    break
                fi
                if ! tmux has-session -t "=$CHILD_NAME" 2>/dev/null; then
                    echo "[spawn_child/pre-reg] Claude session '$CHILD_NAME' died after ${WAITED}s; last pane output:" >&2
                    printf '%s\n' "$PANE_TEXT" | tail -15 >&2
                    CLAUDE_EXITED=true
                    break
                fi
            done
            if [[ "$TRUST_FAILED" == true ]]; then
                echo "[spawn_child/pre-reg] Aborting: unable to accept the Claude trust dialog." >&2
                exit 1
            elif [[ "$CLAUDE_EXITED" == true ]]; then
                echo "[spawn_child/pre-reg] Aborting: Claude terminated before readiness." >&2
                exit 1
            elif [[ "$READY" != true ]]; then
                echo "[spawn_child/pre-reg] Claude readiness timeout (60s); refusing to inject the task into an unknown screen state." >&2
                printf '%s\n' "$PANE_TEXT" | tail -15 >&2
                exit 1
            fi
            sleep 1
        fi

        if ! tmux has-session -t "=$CHILD_NAME" 2>/dev/null; then
            echo "[spawn_child/pre-reg] Claude session '$CHILD_NAME' is not alive" >&2
            exit 1
        fi
        if [[ "$STANDALONE" == true ]]; then
            CHILD_PROMPT="You are ${CHILD_NAME}, a standalone agent with no parent. The name ${CHILD_NAME} is already reserved; do not register another identity. This prompt is the canonical task. Start it immediately:

${TASK}"
        elif [[ "$EMBED_TASK" == true ]]; then
            CHILD_PROMPT="$EMBEDDED_TASK_PROMPT"
        else
            CHILD_PROMPT="Child agent startup. AGENT_NAME=${CHILD_NAME}; parent=${PARENT_NAME}. Follow the child-agent startup procedure in CLAUDE.md and start the task immediately."
        fi
        if [[ "$STANDALONE" == true || "$EMBED_TASK" == true ]]; then
            tmux send-keys -t "$CHILD_NAME" -l "$(printf '\033[200~')${CHILD_PROMPT}$(printf '\033[201~')"
        else
            tmux send-keys -t "$CHILD_NAME" -l "$CHILD_PROMPT"
        fi
        sleep 0.3
        tmux send-keys -t "$CHILD_NAME" C-m
        sleep 2
        flush_queued_prompt "$CHILD_NAME" || true
        verify_injection "$CHILD_NAME" "$CHILD_PROMPT" || true
    fi

    open_child_terminal "$CHILD_NAME"

    if [[ "$USE_WORKTREE" == true ]]; then
        if [[ -n "$WORKTREE_BASE_RESOLVED" ]]; then
            echo "[spawn_child/pre-reg] worktree: $WORKTREE_DIR (branch: exp/${CHILD_NAME}, base: $WORKTREE_BASE_REV / ${WORKTREE_BASE_RESOLVED:0:12}, source: $WORKTREE_SOURCE)" >&2
        else
            echo "[spawn_child/pre-reg] worktree: $WORKTREE_DIR (branch: exp/${CHILD_NAME}, base: HEAD, source: $WORKTREE_SOURCE)" >&2
        fi
        echo "[spawn_child/pre-reg] cleanup: git -C $WORKTREE_SOURCE worktree remove $WORKTREE_DIR && git -C $WORKTREE_SOURCE branch -D exp/${CHILD_NAME}" >&2
    fi

    PRE_REGISTERED_SUCCESS=true
    echo "$CHILD_NAME"
    exit 0
fi
# --- Argument validation ---
if [[ -z "$TASK" ]]; then
    echo "Usage: spawn_child.sh --resources \"path1,path2\" \"<task>\" [<workdir>]" >&2
    exit 1
fi

if [[ ! -d "$WORK_DIR" ]]; then
    echo "Error: workdir does not exist: $WORK_DIR" >&2
    exit 1
fi

# --- Resource declaration validation ---
if [[ -z "$RESOURCES" && "$UNSAFE_NO_RESOURCES" == false ]]; then
    echo "Error: --resources or --unsafe-no-resources is required" >&2
    echo "  --resources \"path1,path2\"  : declare target resource paths" >&2
    echo "  --unsafe-no-resources       : force spawn without resource declaration" >&2
    exit 2
fi

# --- 親エージェント名の取得 ---
if [[ -n "${PARENT_AGENT:-}" ]]; then
    PARENT_NAME="$PARENT_AGENT"
elif [[ -n "${TMUX:-}" ]]; then
    PARENT_NAME=$(tmux display-message -p '#S' 2>/dev/null || echo "unknown")
else
    PARENT_NAME="unknown"
fi

# 親名の妥当性チェック（send_message失敗を事前に防止）
if [[ "$PARENT_NAME" == "unknown" || -z "$PARENT_NAME" ]]; then
    echo "Error: parent agent name is unknown. Set PARENT_AGENT or run inside a tmux session" >&2
    exit 1
fi

# --- Legacy transport bearer (native AgentStack Mail deliberately has none) ---
if legacy_http_bearer_enabled; then
    TOKEN=$(get_agentstack_token 2>/dev/null || true)
    bearer_status=0
else
    bearer_status=$?
    TOKEN=""
fi
if [[ "$bearer_status" == "2" ]]; then
    exit 1
fi
if [[ "$bearer_status" == "0" && -z "$TOKEN" ]]; then
    echo "Error: could not read HTTP_BEARER_TOKEN from env, Keychain, or .env" >&2
    exit 1
fi

# JSON-RPC呼び出しヘルパー（http.clientベース — urllib はSSEストリームでハングするため）
call_mcp() {
    local method="$1"
    local args_json="$2"
    # Both the owner token embedded in args_json and the HTTP bearer travel over
    # stdin.  Neither secret is visible in ps(1) argv or a child environment.
    printf '%s\0%s' "$args_json" "$TOKEN" | python3 -c '
import sys, json, http.client
from urllib.parse import urlparse

method = sys.argv[1]
url = sys.argv[2]
args_raw, token = sys.stdin.buffer.read().split(b"\0", 1)
args = json.loads(args_raw)
token = token.decode("utf-8")

parsed = urlparse(url)
payload = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": method, "arguments": args}
}).encode()

conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Connection": "close"
}
if token:
    headers["Authorization"] = f"Bearer {token}"
conn.request("POST", parsed.path, body=payload, headers=headers)
resp = conn.getresponse()
print(resp.read().decode())
conn.close()
' "$method" "$MCP_URL"
}

load_agent_name_helpers() {
    if declare -F ags_pick_adjective_scientist_name >/dev/null 2>&1; then
        return 0
    fi

    local lib_path="${AGENTSTACK_SCIENTISTS_LIB:-}"
    if [[ -z "$lib_path" ]]; then
        lib_path="$HOOKS_DIR/../bin/lib/agentstack-scientists.sh"
    fi
    if [[ ! -f "$lib_path" ]]; then
        echo "Error: missing agent name helper: $lib_path" >&2
        return 1
    fi
    # shellcheck disable=SC1090
    source "$lib_path"
}

mcp_response_has_error() {
    python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(data, dict) and data.get("error") else 1)
'
}

mcp_extract_agent_name() {
    python3 -c '
import json
import sys

def candidate_names(obj):
    if isinstance(obj, dict):
        for key in ("name", "agent_name"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                yield value

try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)

for name in candidate_names(data):
    print(name)
    sys.exit(0)

result = data.get("result") if isinstance(data, dict) else None
for name in candidate_names(result):
    print(name)
    sys.exit(0)
if isinstance(result, dict):
    structured = result.get("structuredContent")
    for name in candidate_names(structured):
        print(name)
        sys.exit(0)
    content = result.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            for name in candidate_names(obj):
                print(name)
                sys.exit(0)
print("")
'
}

# Three-valued: available | occupied | unknown. See ags_agent_name_status in
# bin/lib/agentstack-register.sh — whois reports "not found" through the error
# channel, so the error text decides, and anything we cannot classify is
# 'unknown' (never treated as a free name).
child_agent_name_status() {
    local agent_name="$1" args_json response
    args_json=$(python3 -c "
import json, sys
print(json.dumps({'project_key': sys.argv[1], 'agent_name': sys.argv[2]}))
" "$PROJECT_KEY" "$agent_name")
    response="$(call_mcp "whois" "$args_json" 2>/dev/null || true)"
    if [[ -z "$response" ]]; then
        printf 'unknown\n'
        return 0
    fi
    if printf '%s' "$response" | mcp_response_has_error; then
        if printf '%s' "$response" | grep -qiE "requires[ _]registration_token|already authenticated"; then
            printf 'occupied\n'
        elif printf '%s' "$response" | grep -qiE "not found|does not exist|no such agent|unknown agent"; then
            printf 'available\n'
        else
            printf 'unknown\n'
        fi
        return 0
    fi
    if [[ -n "$(printf '%s' "$response" | mcp_extract_agent_name)" ]]; then
        printf 'occupied\n'
    else
        printf 'available\n'
    fi
}

child_agent_exists() {
    [[ "$(child_agent_name_status "$1")" == "occupied" ]]
}

pick_available_child_agent_name() {
    local attempts="${AGENTSTACK_AGENT_NAME_ATTEMPTS:-75}"
    local unknown_limit="${AGENTSTACK_NAME_UNKNOWN_LIMIT:-3}"
    local candidate adjective scientist i name_status
    local unknowns=0

    load_agent_name_helpers || return 1

    for ((i = 0; i < attempts; i++)); do
        candidate="$(ags_pick_adjective_scientist_name)" || return 1
        name_status="$(child_agent_name_status "$candidate")"
        case "$name_status" in
            available) printf '%s\n' "$candidate"; return 0 ;;
            occupied)  unknowns=0 ;;
            *)
                unknowns=$((unknowns + 1))
                if (( unknowns >= unknown_limit )); then
                    echo "[spawn_child] name availability checks failed $unknowns times in a row; refusing to pick a child name that may already be in use." >&2
                    return 1
                fi
                ;;
        esac
    done

    for ((i = 2; i < attempts + 200; i++)); do
        adjective="$(ags_pick_adjective)" || return 1
        scientist="$(ags_pick_scientist)" || return 1
        candidate="${adjective}-${i}-${scientist}"
        name_status="$(child_agent_name_status "$candidate")"
        case "$name_status" in
            available) printf '%s\n' "$candidate"; return 0 ;;
            occupied)  unknowns=0 ;;
            *)
                unknowns=$((unknowns + 1))
                if (( unknowns >= unknown_limit )); then
                    echo "[spawn_child] name availability checks failed $unknowns times in a row; refusing to pick a child name that may already be in use." >&2
                    return 1
                fi
                ;;
        esac
    done

    return 1
}

retire_agent_with_token_file() {
    local agent_name="$1"
    local token_file="$2"
    local retire_args
    [[ -s "$token_file" ]] || return 1
    retire_args=$(python3 -c '
import json
import pathlib
import sys
print(json.dumps({
    "project_key": sys.argv[1],
    "agent_name": sys.argv[2],
    "registration_token": pathlib.Path(sys.argv[3]).read_text(
        encoding="utf-8").strip(),
}))
' "$PROJECT_KEY" "$agent_name" "$token_file")
    call_mcp "retire_agent" "$retire_args"
}

parse_resource_paths_json() {
    python3 -c "
import csv, json, sys
reader = csv.reader([sys.argv[1]], skipinitialspace=True)
paths = [p.strip() for p in next(reader, []) if p.strip()]
print(json.dumps(paths))
" "$1"
}

# --- 1. サーバー稼働確認 ---
if ! call_mcp "health_check" "{}" > /dev/null 2>&1; then
    echo "Error: cannot connect to ORRERY Mail server at $MCP_URL" >&2
    exit 1
fi

# --- 2. 子エージェントを事前登録 ---
TASK_SHORT="${TASK:0:80}"
if [[ "$USE_CODEX" == true ]]; then
    CHILD_PROGRAM="codex"
    CHILD_MODEL="$(normalize_codex_model "$CLAUDE_MODEL")"
    CODEX_EFFORT="$(validate_codex_effort "$CHILD_MODEL" "$CODEX_EFFORT")"
else
    CHILD_PROGRAM="claude-code"
    # Claude 子は model catalog の current generation へ正規化する。
    CHILD_MODEL="$(normalize_claude_model "$CLAUDE_MODEL")"
fi

if ! CHILD_NAME_CANDIDATE="$(pick_available_child_agent_name)"; then
    echo "Error: failed to generate an available child agent name" >&2
    exit 1
fi

TOKEN_HANDOFF_DIR="$RUNTIME_DIR/spawn-tokens"
TOKEN_NONCE="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
DIRECT_ONE_SHOT_TOKEN_FILE="$TOKEN_HANDOFF_DIR/direct.$$.${TOKEN_NONCE}.token"
generate_child_token_file "$DIRECT_ONE_SHOT_TOKEN_FILE"
REGISTER_ARGS=$(python3 -c '
import json
import pathlib
import sys
args = {
    "project_key": sys.argv[1],
    "program": sys.argv[2],
    "model": sys.argv[3],
    "task_description": sys.argv[4],
    "registration_token": pathlib.Path(sys.argv[5]).read_text(
        encoding="utf-8").strip(),
    "name": sys.argv[6],
}
print(json.dumps(args))
' "$PROJECT_KEY" "$CHILD_PROGRAM" "$CHILD_MODEL" "$TASK_SHORT" \
    "$DIRECT_ONE_SHOT_TOKEN_FILE" "$CHILD_NAME_CANDIDATE")

if ! REGISTER_RESULT=$(call_mcp "register_agent" "$REGISTER_ARGS"); then
    rm -f "$DIRECT_ONE_SHOT_TOKEN_FILE"
    echo "Error: register_agent request failed" >&2
    exit 1
fi
if printf '%s' "$REGISTER_RESULT" | mcp_response_has_error; then
    rm -f "$DIRECT_ONE_SHOT_TOKEN_FILE"
    echo "Error: register_agent returned an error" >&2
    exit 1
fi
CHILD_NAME="$(printf '%s' "$REGISTER_RESULT" | mcp_extract_agent_name)"

if [[ -z "$CHILD_NAME" || ! "$CHILD_NAME" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "Error: register_agent returned no valid child agent name" >&2
    rm -f "$DIRECT_ONE_SHOT_TOKEN_FILE"
    exit 1
fi
if [[ "$CHILD_NAME" != "$CHILD_NAME_CANDIDATE" ]]; then
    echo "[spawn_child] register_agent normalized '$CHILD_NAME_CANDIDATE' to actual identity '$CHILD_NAME'" >&2
fi

# Adopt the token the server persisted, not the one we sent. Legacy servers
# may ignore the client-supplied registration_token and mint their
# own, returning it in the response; keeping our sent token would leave the
# child holding a token the server never stored, so its reregister/fetch_inbox
# all fail with "Invalid registration_token". The response and sent fallback
# are consumed inside Python and persisted only in 0600 files.
if ! CHILD_TOKEN_FILE="$(
    printf '%s' "$REGISTER_RESULT" |
        adopt_registered_token_response "$CHILD_NAME" "$PROJECT_KEY" \
            "$DIRECT_ONE_SHOT_TOKEN_FILE"
)"; then
    rm -f "$DIRECT_ONE_SHOT_TOKEN_FILE"
    echo "Error: failed to persist the registered child token" >&2
    exit 1
fi

# --- 失敗時cleanup trap ---
# Launcher が完全に readiness/prompt injection を終える前に異常終了したら、
# 一瞬 tmux session が作られていても child credentials と予約を回収する。
SPAWN_COMPLETED=false
CHILD_SESSION_STARTED=false
cleanup_on_failure() {
    if [[ "$SPAWN_COMPLETED" == true ]]; then
        return
    fi
    warn_if_uninjected
    if [[ "$CHILD_SESSION_STARTED" == true && -n "${CHILD_NAME:-}" ]]; then
        tmux kill-session -t "=$CHILD_NAME" >/dev/null 2>&1 || true
    fi
    if [[ -n "${CHILD_NAME:-}" ]]; then
        echo "[spawn_child] cleanup: retiring $CHILD_NAME and releasing reservations" >&2
        # 予約解放
        if [[ -n "${RESOURCES:-}" ]]; then
            local release_args
            release_args=$(python3 -c "
import json, sys
print(json.dumps({'project_key': sys.argv[1], 'agent_name': sys.argv[2]}))
" "$PROJECT_KEY" "$CHILD_NAME") 2>/dev/null || true
            call_mcp "release_file_reservations" "$release_args" > /dev/null 2>&1 || true
        fi
        # エージェント retire
        if [[ -s "${CHILD_TOKEN_FILE:-}" ]]; then
            retire_agent_with_token_file "$CHILD_NAME" "$CHILD_TOKEN_FILE" > /dev/null 2>&1 || true
        fi
    fi
    # worktree も作っていれば撤去
    cleanup_worktree
    rm -f "${CHILD_TOKEN_FILE:-}" "$CHILD_STATE_DIR/${CHILD_NAME:-}.json"
    if [[ -n "${CHILD_NAME:-}" && -f "$MANAGED_FILE" ]]; then
        python3 - "$MANAGED_FILE" "$CHILD_NAME" <<'PY' 2>/dev/null || true
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except OSError:
    raise SystemExit(0)
path.write_text(
    "\n".join(line for line in lines if line != name) + "\n",
    encoding="utf-8",
)
PY
    fi
}
trap cleanup_on_failure EXIT

# --- 2b. リソース予約 ---
if [[ -n "$RESOURCES" ]]; then
    echo "[spawn_child] Reserving resources: $RESOURCES (TTL: ${RESOURCE_TTL}s)" >&2

    # CSVとしてパースし、カンマを含むパスはクォートで表現可能にする
    PATHS_JSON=$(parse_resource_paths_json "$RESOURCES")

    RESERVE_ARGS=$(python3 -c "
import json, sys
args = {
    'project_key': sys.argv[1],
    'agent_name': sys.argv[2],
    'paths': json.loads(sys.argv[3]),
    'ttl_seconds': int(sys.argv[4]),
    'exclusive': True,
    'reason': 'spawn_child: ' + sys.argv[5][:60]
}
print(json.dumps(args))
" "$PROJECT_KEY" "$CHILD_NAME" "$PATHS_JSON" "$RESOURCE_TTL" "$TASK")

    RESERVE_RESULT=$(call_mcp "file_reservation_paths" "$RESERVE_ARGS")

    # 競合チェック
    HAS_CONFLICT=$(python3 -c "
import json, sys
r = json.loads(sys.stdin.read())
data = json.loads(r['result']['content'][0]['text'])
conflicts = data.get('conflicts', [])
if conflicts:
    for c in conflicts:
        holders = ', '.join(h.get('agent_name', '?') for h in c.get('holders', []))
        sys.stderr.write(f'  CONFLICT: {c[\"path\"]} held by: {holders}\n')
    print('yes')
else:
    granted = data.get('granted', [])
    for g in granted:
        sys.stderr.write(f'  GRANTED: {g[\"path_pattern\"]} (expires: {g.get(\"expires_ts\", \"?\")})\n')
    print('no')
" <<< "$RESERVE_RESULT")

    if [[ "$HAS_CONFLICT" == "yes" ]]; then
        echo "Error: resource conflict detected; aborting spawn." >&2
        # クリーンアップ: 部分成功した予約を解放 + 子エージェントを retire
        RELEASE_ARGS=$(python3 -c "
import json, sys
print(json.dumps({'project_key': sys.argv[1], 'agent_name': sys.argv[2]}))
" "$PROJECT_KEY" "$CHILD_NAME")
        call_mcp "release_file_reservations" "$RELEASE_ARGS" > /dev/null 2>&1 || true
        if [[ -s "${CHILD_TOKEN_FILE:-}" ]]; then
            retire_agent_with_token_file "$CHILD_NAME" "$CHILD_TOKEN_FILE" > /dev/null 2>&1 || true
        fi
        echo "[spawn_child] Released reservations and retired $CHILD_NAME" >&2
        rm -f "$CHILD_TOKEN_FILE" "$CHILD_STATE_DIR/$CHILD_NAME.json"
        SPAWN_COMPLETED=true  # cleanup already completed explicitly above
        exit 21
    fi
fi

# --- 2c. --worktree 指定時: 先に worktree を作って WORK_DIR を上書き ---
# タスクメッセージに worktree path / base commit を含めるため、message 送信より先に行う
if [[ "$USE_WORKTREE" == true ]]; then
    if ! maybe_create_worktree "$CHILD_NAME" "$WORK_DIR"; then
        echo "[spawn_child] Worktree creation failed; aborting spawn." >&2
        exit 1
    fi
    WORK_DIR="$WORKTREE_DIR"
    echo "[spawn_child] WORK_DIR overridden to worktree: $WORK_DIR" >&2
fi

# --- 3. タスクメッセージを子エージェントに送信 ---
SUBJECT="Task request: ${TASK:0:50}"
RESOURCE_NOTE=""
if [[ -n "$RESOURCES" ]]; then
    RESOURCE_NOTE="
- Reserved resources: ${RESOURCES}
- Do not modify paths outside the reserved resources above."
fi

WORKTREE_NOTE=""
if [[ "$USE_WORKTREE" == true ]]; then
    WORKTREE_NOTE="
- Isolated worktree: ${WORKTREE_DIR}
- worktree branch: exp/${CHILD_NAME}
- source repo: ${WORKTREE_SOURCE}"
    if [[ -n "$WORKTREE_BASE_RESOLVED" ]]; then
        WORKTREE_NOTE="${WORKTREE_NOTE}
- worktree base: ${WORKTREE_BASE_REV} (${WORKTREE_BASE_RESOLVED:0:12})"
    else
        WORKTREE_NOTE="${WORKTREE_NOTE}
- worktree base: HEAD (parent HEAD at spawn time; --worktree-base was not set)"
    fi
fi

BODY_MD="## Task

${TASK}

## Context

- Parent agent: ${PARENT_NAME}
- Working directory: ${WORK_DIR}${RESOURCE_NOTE}${WORKTREE_NOTE}
- **Use \`${PROJECT_KEY}\` as the ORRERY Mail project_key**, not the current working directory. This is especially important in worktree mode. The tmux \$PROJECT_KEY env var has the same value. If you call ensure_project(human_key=cwd) from outside the project root, you will create a different project and will not be able to read this inbox.
- File reservation TTL: ${RESOURCE_TTL} seconds
- The parent pre-reserved the resources above under your agent name. Do not call macro_file_reservation_cycle or file_reservation_paths again for the same paths; use the existing reservations.
- If you are worried about remaining TTL, prefer renew_file_reservations rather than acquiring the same paths again.
- Split large changes into smaller Edit/Update operations instead of one huge Write.
- Acquire new reservations only when you need additional unreserved paths.
- Reply to the parent agent when the task is complete."

SEND_ARGS=$(python3 -c "
import json, sys
args = {
    'project_key': sys.argv[1],
    'sender_name': sys.argv[2],
    'to': [sys.argv[3]],
    'subject': sys.argv[4],
    'body_md': sys.argv[5],
    'importance': 'high'
}
print(json.dumps(args))
" "$PROJECT_KEY" "$PARENT_NAME" "$CHILD_NAME" "$SUBJECT" "$BODY_MD")

call_mcp "send_message" "$SEND_ARGS" > /dev/null

# --- 4. managed_agents.txt に追加 ---
if ! grep -qxF "$CHILD_NAME" "$MANAGED_FILE" 2>/dev/null; then
    mkdir -p "$(dirname "$MANAGED_FILE")"
    echo "$CHILD_NAME" >> "$MANAGED_FILE"
fi

# Warn (do not block) if the child's workdir is a macOS privacy-protected folder
# this process can't read — turns an undiagnosable EPERM into actionable advice.
declare -F ags_warn_tcc_access >/dev/null 2>&1 && ags_warn_tcc_access "$WORK_DIR"

# --- 5. 新しいtmuxセッションで子エージェントを起動 ---
# CLAUDECODE=1 guards the child session's interactive shell against destructive
# shell exit hooks (e.g. a ~/.zshrc zshexit / bash trap that runs `tmux
# kill-session`): without it, exiting this session can cascade-kill the tmux
# server. Requires tmux >= 3.0.
TMUX_ENV_ARGS=(-e "CLAUDECODE=1" -e "AGENTSTACK_RESERVED_IDENTITY=1" -e "AGENT_NAME=$CHILD_NAME" -e "PARENT_AGENT=$PARENT_NAME" -e "PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_HOOKS_DIR=$HOOKS_DIR" -e "AGENTSTACK_RUNTIME_DIR=$RUNTIME_DIR" -e "AGENTSTACK_MCP_URL=$MCP_URL" -e "AGENTSTACK_MAIL_ENV=$MAIL_ENV" -e "AGENTSTACK_MAIL_HTTP_BEARER_MODE=$HTTP_BEARER_MODE" -e "AGENTSTACK_TERMINAL=$TERMINAL_SETTING" -e "AGENTSTACK_CODEX_APPROVAL=$(codex_approval_flags)" -e "AGENTSTACK_CODEX_NETWORK_FLAGS=$(codex_network_flags)")
if [[ -n "$AGENTSTACK_HOME_DIR" ]]; then
    TMUX_ENV_ARGS+=(-e "AGENTSTACK_HOME=$AGENTSTACK_HOME_DIR")
fi
if [[ -n "$RESOURCES" ]]; then
    TMUX_ENV_ARGS+=(-e "CHILD_RESOURCES=$RESOURCES")
fi
if [[ "$USE_CODEX" == true ]]; then
    CHILD_CODEX_HOME="$(write_child_codex_home "$CHILD_NAME" "$CHILD_TOKEN_FILE")"
    TMUX_ENV_ARGS+=(-e "AGENTSTACK_CODEX_MODEL=$CHILD_MODEL" -e "AGENTSTACK_CODEX_EFFORT=$CODEX_EFFORT")
    TMUX_ENV_ARGS+=(-e "AGENTSTACK_CODEX_ADD_DIRS_RESOLVED=$(codex_child_add_dirs "$CHILD_CODEX_HOME")")
    if [[ -n "$CHILD_CODEX_HOME" ]]; then
        echo "[spawn_child] Child CODEX_HOME with authenticated agent-mail: $CHILD_CODEX_HOME" >&2
        TMUX_ENV_ARGS+=(
            -e "CODEX_HOME=$CHILD_CODEX_HOME"
            -e "CODEX_SHARED_CODEX_DIR=$CHILD_CODEX_HOME"
        )
    else
        echo "[spawn_child] No MCP proxy available; Codex child uses the shared agent-mail endpoint" >&2
    fi
    # Codex startup: inject a bootstrap prompt that points the child to inbox.
    CODEX_PROMPT="You are ${CHILD_NAME}. The parent agent is ${PARENT_NAME}. The child name ${CHILD_NAME} is already reserved, so do not register under another name. The canonical task is in your ORRERY Mail inbox. First, if ${REREGISTER_HELPER:-agentstack-reregister} exists, run PROJECT_KEY=${PROJECT_KEY} ${REREGISTER_HELPER:-agentstack-reregister} ${CHILD_NAME}; when that succeeds, skip register_agent and fetch_inbox for ${CHILD_NAME}. The helper reads the child-owned 0600 token file; never request or print its token. Do not infer the task from this prompt; treat the inbox request as authoritative."
    tmux new-session -d -s "$CHILD_NAME" \
        -c "$WORK_DIR" \
        "${TMUX_ENV_ARGS[@]}" \
        '/bin/zsh -lc '"'"'
                export PATH="$HOME/.local/bin:$PATH";
            # See the pre-registered path: no user-side bootstrap is sourced.
            # See the pre-registered path: the product owns the launch flags and
            # never hands off to a user-side launcher.
            EXTRA_ARGS=()
            for d in ${(s.:.)AGENTSTACK_CODEX_ADD_DIRS_RESOLVED}; do
                [[ -d "$d" ]] && EXTRA_ARGS+=(--add-dir "$d")
            done
            env -u OPENAI_API_KEY codex -C "$PWD" --sandbox workspace-write ${=AGENTSTACK_CODEX_APPROVAL} ${=AGENTSTACK_CODEX_NETWORK_FLAGS} \
                "${EXTRA_ARGS[@]}" --model "$AGENTSTACK_CODEX_MODEL" -c "model_reasoning_effort=$AGENTSTACK_CODEX_EFFORT"
            /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"
        '"'"''
    CHILD_SESSION_STARTED=true
    SPAWN_TRAP_SESSION="$CHILD_NAME"
    # Codex REPL起動待機
    # 注意: モデルアップグレードダイアログやサインインプロンプトが
    # 表示されることがある。これらを自動スキップしてから入力待ちを検知する。
    echo "[spawn_child] Waiting for Codex REPL..." >&2
    WAITED=0
    WAIT_MAX=90
    READY=false
    DIED=false
    TRUST_FAILED=false
    TRUST_ATTEMPTS=0
    TRUST_MAX=10
    while [[ $WAITED -lt $WAIT_MAX ]]; do
        sleep 3
        WAITED=$((WAITED + 3))
        PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p 2>/dev/null || true)

        # モデルアップグレードダイアログ: "Use existing model" を選択
        if echo "$PANE_TEXT" | grep -qF "Use existing model"; then
            echo "[spawn_child] Model selection dialog detected; choosing existing model" >&2
            tmux send-keys -t "$CHILD_NAME" Down Enter
            sleep 5
            continue
        fi

        # Trust ダイアログ: "Do you trust the contents of this directory?"
        if codex_trust_dialog_present "$PANE_TEXT"; then
            TRUST_ATTEMPTS=$((TRUST_ATTEMPTS + 1))
            if ! codex_accept_trust_dialog \
                "$CHILD_NAME" "$TRUST_ATTEMPTS" "$TRUST_MAX" "spawn_child"; then
                TRUST_FAILED=true
                break
            fi
            sleep 3
            continue
        fi

        # サインインプロンプト: Enter で続行
        if echo "$PANE_TEXT" | grep -qi "Press enter to continue"; then
            echo "[spawn_child] Sign-in prompt detected; pressing Enter" >&2
            tmux send-keys -t "$CHILD_NAME" Enter
            sleep 3
            continue
        fi

        if codex_pane_ready "$PANE_TEXT"; then
            READY=true
            break
        fi
        if ! codex_session_alive "$CHILD_NAME"; then
            echo "[spawn_child] Codex session '$CHILD_NAME' died after ${WAITED}s; last pane output:" >&2
            printf '%s\n' "$PANE_TEXT" | tail -15 >&2
            DIED=true
            break
        fi
    done

    if [[ "$TRUST_FAILED" == true ]]; then
        echo "[spawn_child] Aborting: unable to accept the Codex trust dialog." >&2
        exit 1
    elif [[ "$DIED" == true ]]; then
        echo "[spawn_child] Aborting: the child exited before becoming ready (check the codex flags above)." >&2
        exit 1
    elif [[ "$READY" == true ]]; then
        sleep 2
        echo "[spawn_child] Waited ${WAITED}s (+2s); injecting prompt" >&2
    else
        echo "[spawn_child] Codex readiness timeout (${WAIT_MAX}s); refusing to inject the task into an unknown screen state." >&2
        printf '%s\n' "$PANE_TEXT" | tail -15 >&2
        exit 1
    fi

    # Codex にはタスク概要を含むプロンプトを注入
    # 注意: テキストと Enter は分離して送信する。
    # 長いテキスト + C-m を同一コールで送ると C-m が落ちることがある。
    tmux send-keys -t "$CHILD_NAME" -l "$CODEX_PROMPT"
    sleep 0.5
    tmux send-keys -t "$CHILD_NAME" C-m
    verify_injection "$CHILD_NAME" "$CODEX_PROMPT" || true
else
    # Claude Code 起動（モデル指定付き）
    CHILD_MCP_CONFIG="$(write_child_mcp_config "$CHILD_NAME" "$CHILD_TOKEN_FILE")"
    tmux new-session -d -s "$CHILD_NAME" \
        -c "$WORK_DIR" \
        "${TMUX_ENV_ARGS[@]}" \
        -e "CLAUDE_CHILD_MODEL=$CHILD_MODEL" \
        -e "CLAUDE_CHILD_MCP_CONFIG=$CHILD_MCP_CONFIG" \
        '/bin/zsh -lc '"'"'export PATH="$HOME/.local/bin:$PATH"; MCP_ARGS=(); [[ -n "$CLAUDE_CHILD_MCP_CONFIG" ]] && MCP_ARGS=(--mcp-config "$CLAUDE_CHILD_MCP_CONFIG" --strict-mcp-config); claude --model "$CLAUDE_CHILD_MODEL" "${MCP_ARGS[@]}"; /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"'"'"''
    CHILD_SESSION_STARTED=true
    SPAWN_TRAP_SESSION="$CHILD_NAME"
    # Claude REPL起動待機
    echo "[spawn_child] Waiting for Claude REPL..." >&2
    WAITED=0
    WAIT_MAX=60
    READY=false
    CLAUDE_EXITED=false
    TRUST_FAILED=false
    TRUST_ATTEMPTS=0
    TRUST_MAX=5
    while [[ $WAITED -lt $WAIT_MAX ]]; do
        sleep 2
        WAITED=$((WAITED + 2))
        PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p 2>/dev/null || true)
        if claude_trust_dialog_present "$PANE_TEXT"; then
            TRUST_ATTEMPTS=$((TRUST_ATTEMPTS + 1))
            if ! claude_accept_trust_dialog \
                "$CHILD_NAME" "$TRUST_ATTEMPTS" "$TRUST_MAX" "spawn_child"; then
                TRUST_FAILED=true
                break
            fi
            sleep 1
            continue
        fi
        if claude_pane_ready "$PANE_TEXT"; then
            READY=true
            break
        fi
        if ! tmux has-session -t "=$CHILD_NAME" 2>/dev/null; then
            echo "[spawn_child] Claude session '$CHILD_NAME' died after ${WAITED}s; last pane output:" >&2
            printf '%s\n' "$PANE_TEXT" | tail -15 >&2
            CLAUDE_EXITED=true
            break
        fi
    done
    if [[ "$TRUST_FAILED" == true ]]; then
        echo "[spawn_child] Aborting: unable to accept the Claude trust dialog." >&2
        exit 1
    elif [[ "$CLAUDE_EXITED" == true ]]; then
        echo "[spawn_child] Aborting: Claude terminated before readiness." >&2
        exit 1
    elif [[ "$READY" != true ]]; then
        echo "[spawn_child] Claude readiness timeout (${WAIT_MAX}s); refusing to inject the task into an unknown screen state." >&2
        printf '%s\n' "$PANE_TEXT" | tail -15 >&2
        exit 1
    fi
    sleep 1
    echo "[spawn_child] Waited ${WAITED}s (+1s); injecting prompt" >&2

    CHILD_PROMPT="Child agent startup. AGENT_NAME=${CHILD_NAME}; parent=${PARENT_NAME}. Follow the child-agent startup procedure in CLAUDE.md and start the task immediately."
    tmux send-keys -t "$CHILD_NAME" -l "$CHILD_PROMPT"
    sleep 0.3
    tmux send-keys -t "$CHILD_NAME" C-m
    sleep 2
    flush_queued_prompt "$CHILD_NAME" || true
    verify_injection "$CHILD_NAME" "$CHILD_PROMPT" || true
fi

open_child_terminal "$CHILD_NAME"
SPAWN_COMPLETED=true

# --- Complete: stdout contains only child agent name ---
echo "$CHILD_NAME"
echo "[spawn_child] Started '$CHILD_NAME' in tmux session '$CHILD_NAME'" >&2
echo "[spawn_child] Task: $TASK" >&2
echo "[spawn_child] Parent: $PARENT_NAME / directory: $WORK_DIR" >&2
echo "[spawn_child] Agent type: $CHILD_PROGRAM" >&2
if [[ -n "$RESOURCES" ]]; then
    echo "[spawn_child] Reserved resources: $RESOURCES (TTL: ${RESOURCE_TTL}s)" >&2
fi
if [[ "$USE_WORKTREE" == true ]]; then
    if [[ -n "$WORKTREE_BASE_RESOLVED" ]]; then
        echo "[spawn_child] worktree: $WORKTREE_DIR (branch: exp/${CHILD_NAME}, base: $WORKTREE_BASE_REV / ${WORKTREE_BASE_RESOLVED:0:12}, source: $WORKTREE_SOURCE)" >&2
    else
        echo "[spawn_child] worktree: $WORKTREE_DIR (branch: exp/${CHILD_NAME}, base: HEAD, source: $WORKTREE_SOURCE)" >&2
    fi
    echo "[spawn_child] cleanup: git -C $WORKTREE_SOURCE worktree remove $WORKTREE_DIR && git -C $WORKTREE_SOURCE branch -D exp/${CHILD_NAME}" >&2
fi
