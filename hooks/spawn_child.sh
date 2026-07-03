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
#
# モデル指定（--model, Claude 子のみ。Codex は常に gpt-5.5）:
#   --model 省略         → claude-opus-4-8[1m]（Opus 4.8 1M。保存既定と一致・プラン同梱で無料）
#   --model opus         → claude-opus-4-8[1m]（[1m] 自動付与）
#   --model opus[1m]     → claude-opus-4-8[1m]（friendly エイリアス。要シングルクォート: glob 回避）
#   --model opus-1m      → claude-opus-4-8[1m]（旧来の無効 ID を正規化）
#   --model claude-opus-4-8 → そのまま 200K Opus（warm pool 事前起動と一致するので warm を claim）
#   --model sonnet       → claude-sonnet-4-6（200K）
#   --model haiku/fable  → claude-haiku-4-5-20251001 / claude-fable-5
#   未知の形             → 明確なエラーで停止（claude-* 接頭の正式 ID は前方互換で素通り）
#   ※ 正規化は normalize_claude_model() が担当。warm pool は要求モデルが
#     事前起動モデル（opus=claude-opus-4-8/200K, sonnet=claude-sonnet-4-6/200K）と
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
#   PROJECT_KEY   - mcp-agent-mailのプロジェクトキー（省略時: デフォルト）
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
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$HOME/.claude/managed_agents.txt}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/mcp_agent_mail/.env}"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:8765/mcp}}"
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

open_child_terminal() {
    local child_name="$1"
    local adapter shell_child shell_cmd
    adapter="$(terminal_adapter)"
    [[ "$adapter" == "none" ]] && return 0

    printf -v shell_child '%q' "$child_name"
    shell_cmd="env -u TMUX -u TMUX_PANE tmux attach -t $shell_child"
    case "$adapter" in
        ghostty)
            if env -u TMUX -u TMUX_PANE open -na Ghostty.app --args --title="$child_name" -e tmux attach -t "$child_name" 2>/dev/null; then
                echo "[spawn_child] Opened terminal window (${child_name}, adapter: ghostty)" >&2
            fi
            ;;
        iterm)
            if command -v osascript >/dev/null 2>&1; then
                osascript -e 'on run argv
                  set cmd to item 1 of argv
                  tell application "iTerm2"
                    activate
                    create window with default profile command cmd
                  end tell
                end run' "$shell_cmd" >/dev/null 2>&1 || true
            fi
            ;;
        terminal)
            if command -v osascript >/dev/null 2>&1; then
                osascript -e 'on run argv
                  set cmd to item 1 of argv
                  tell application "Terminal"
                    activate
                    do script cmd
                  end tell
                end run' "$shell_cmd" >/dev/null 2>&1 || true
            fi
            ;;
    esac
    return 0
}

# フラグの処理
USE_CODEX=false
CLAUDE_MODEL=""
RESOURCES=""
RESOURCE_TTL=14400
UNSAFE_NO_RESOURCES=false
PRE_REGISTERED=""
CHILD_TOKEN_FILE=""
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

load_child_state_token() {
    # Split declaration: bash 3.2 (macOS system bash) does not make an
    # earlier name in the same `local` statement visible to a later
    # initializer, so a combined line trips `set -u` (agent_name: unbound).
    local agent_name="$1"
    local state_file="$CHILD_STATE_DIR/$agent_name.json"
    [[ -f "$state_file" ]] || return 1
    python3 - "$state_file" <<'PY'
import json
import sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
token = data.get("registration_token")
if not isinstance(token, str) or not token:
    sys.exit(1)
print(token)
PY
}

read_token_file() {
    local token_file="$1" token
    [[ -n "$token_file" && -f "$token_file" ]] || return 1
    IFS= read -r token < "$token_file" || true
    [[ -n "$token" ]] || return 1
    printf '%s\n' "$token"
}

write_child_state() {
    # Split declaration (see load_child_state_token): bash 3.2 can't reference
    # agent_name from a later initializer in the same `local` statement.
    local agent_name="$1" project_key="$2" registration_token="$3"
    local state_file="$CHILD_STATE_DIR/$agent_name.json"
    mkdir -p "$CHILD_STATE_DIR"
    python3 - "$agent_name" "$project_key" "$registration_token" "$state_file" <<'PY'
import json
import os
import pathlib
import sys

agent_name, project_key, registration_token, state_file = sys.argv[1:5]
path = pathlib.Path(state_file)
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(path.name + ".tmp")
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({
        "agent_name": agent_name,
        "project_key": project_key,
        "registration_token": registration_token,
    }, f)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
}

# --- Claude モデル名の正規化 ---
# friendly エイリアス / 略記を `claude --model` が受け付ける正式 model string に変換する。
# 設計判断（2026-06-13 OrangeGauss）:
#   - --model 省略時は保存既定と同じ Opus 4.8 1M を既定にする（従来は claude-sonnet-4-6 に
#     降格していた＝ユーザーの保存既定 Opus 4.8 1M を無視していた）。Opus 4.8 1M はプラン同梱で無料。
#   - opus → claude-opus-4-8[1m]（[1m] を自動付与）。opus[1m] / opus-1m 等の friendly 表記も同じに正規化。
#   - 明示的に 200K opus が欲しい場合だけ claude-opus-4-8（warm pool 事前起動と完全一致）。
#   - 未知の形は stderr に明確なエラーを出して非ゼロで返す（set -e 下で呼び出し側が停止する）。
#     ただし claude-* 接頭の正式 ID は前方互換のため素通りさせる（新モデル ID 対応）。
# 注意: Codex 経路では呼ばない（CHILD_MODEL は gpt-5.5 に固定されるため）。
normalize_claude_model() {
    local raw="${1:-}"
    # 小文字化 + 空白除去で正規化キーを作る（出力は固定の正式文字列）
    local m
    m="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"

    case "$m" in
        ""|opus|opus-1m|opus1m|"opus[1m]"|claude-opus-4-8-1m|"claude-opus-4-8[1m]")
            echo "claude-opus-4-8[1m]" ;;
        opus-200k|opus200k|claude-opus-4-8)
            echo "claude-opus-4-8" ;;
        sonnet|claude-sonnet-4-6)
            echo "claude-sonnet-4-6" ;;
        sonnet-1m|sonnet1m|"sonnet[1m]"|"claude-sonnet-4-6[1m]")
            echo "claude-sonnet-4-6[1m]" ;;
        haiku|claude-haiku-4-5|claude-haiku-4-5-20251001)
            echo "claude-haiku-4-5-20251001" ;;
        fable|claude-fable-5)
            echo "claude-fable-5" ;;
        *)
            if [[ "$m" == claude-* ]]; then
                # 正式 ID は前方互換で素通り（新モデル ID 対応）
                printf '%s\n' "$m"
            else
                echo "Error: unknown model '$raw'. Valid forms: opus / opus[1m] / claude-opus-4-8[1m] / sonnet / haiku / fable / claude-<id>" >&2
                return 1
            fi
            ;;
    esac
    return 0
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

TASK="${1:-}"
WORK_DIR="${2:-$(pwd)}"
CHILD_STATE_DIR="$RUNTIME_DIR/child-agents"

if [[ -z "$PROJECT_KEY" ]]; then
    echo "Error: AGENTSTACK_PROJECT_KEY or PROJECT_KEY is required" >&2
    echo "  Set it to the shared mcp-agent-mail project key before spawning a child." >&2
    echo "  For delegated children this may differ from the child workdir or git repo cwd." >&2
    exit 1
fi

# --- Pre-registered mode ---
# 親エージェントが MCP 経由で事前に register_agent / file_reservation_paths / send_message
# を済ませてから呼ぶモード。spawn_child.sh は tmux セッション作成のみ行う。
# Usage: spawn_child.sh --pre-registered <CHILD_NAME> --child-token-file <path> "<タスク>" [<作業ディレクトリ>]
if [[ -n "$PRE_REGISTERED" ]]; then
    CHILD_NAME="$PRE_REGISTERED"
    TASK="${1:-}"
    WORK_DIR="${2:-$(pwd)}"
    # Claude 子はモデル名を正規化（省略時 Opus 4.8 1M 既定）。Codex 子は後段で gpt-5.5 に上書き。
    if [[ "$USE_CODEX" == true ]]; then
        CHILD_MODEL="gpt-5.5"
    else
        CHILD_MODEL="$(normalize_claude_model "$CLAUDE_MODEL")"
    fi
    PARENT_NAME="${PARENT_AGENT:-$(tmux display-message -p '#S' 2>/dev/null || echo unknown)}"

    if [[ -z "$TASK" ]]; then
        echo "Usage: spawn_child.sh --pre-registered <CHILD_NAME> --child-token-file <path> \"<task>\" [workdir]" >&2
        exit 1
    fi

    # Pre-registered children must use their own token. Never inherit the
    # caller's ambient CHILD_REGISTRATION_TOKEN here; that may be the parent's
    # owner token and would both fail strict auth and leak a secret to the child.
    PRE_REGISTERED_CHILD_TOKEN=""
    if [[ -n "$CHILD_TOKEN_FILE" ]]; then
        if ! PRE_REGISTERED_CHILD_TOKEN="$(read_token_file "$CHILD_TOKEN_FILE")"; then
            echo "Error: --child-token-file is unreadable or empty: $CHILD_TOKEN_FILE" >&2
            exit 1
        fi
    else
        PRE_REGISTERED_CHILD_TOKEN="$(load_child_state_token "$CHILD_NAME" 2>/dev/null || true)"
    fi
    if [[ -z "$PRE_REGISTERED_CHILD_TOKEN" ]]; then
        echo "Error: pre-registered child token is required for $CHILD_NAME" >&2
        echo "  Generate/register the child with a child-owned token, then pass --child-token-file <path>." >&2
        echo "  Existing state fallback: $CHILD_STATE_DIR/$CHILD_NAME.json" >&2
        exit 1
    fi
    write_child_state "$CHILD_NAME" "$PROJECT_KEY" "$PRE_REGISTERED_CHILD_TOKEN"

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
    TMUX_ENV_ARGS=(-e "CLAUDECODE=1" -e "AGENT_NAME=$CHILD_NAME" -e "PARENT_AGENT=$PARENT_NAME" -e "PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_HOOKS_DIR=$HOOKS_DIR" -e "AGENTSTACK_RUNTIME_DIR=$RUNTIME_DIR" -e "AGENTSTACK_MCP_URL=$MCP_URL" -e "AGENTSTACK_MAIL_ENV=$MAIL_ENV" -e "AGENTSTACK_TERMINAL=$TERMINAL_SETTING")
    if [[ -n "$AGENTSTACK_HOME_DIR" ]]; then
        TMUX_ENV_ARGS+=(-e "AGENTSTACK_HOME=$AGENTSTACK_HOME_DIR")
    fi
    TMUX_ENV_ARGS+=(-e "CHILD_REGISTRATION_TOKEN=$PRE_REGISTERED_CHILD_TOKEN")

    if [[ "$USE_CODEX" == true ]]; then
        # Codex startup (--pre-registered mode).
        CHILD_MODEL="gpt-5.5"
        TOKEN=$(get_agentstack_token 2>/dev/null || true)
        CODEX_PROMPT="You are ${CHILD_NAME}. The parent agent is ${PARENT_NAME}. The child name ${CHILD_NAME} is already reserved, so do not register under another name. The canonical task is in your mcp-agent-mail inbox. First, if ${REREGISTER_HELPER:-agentstack-reregister} exists, run PROJECT_KEY=${PROJECT_KEY} ${REREGISTER_HELPER:-agentstack-reregister} ${CHILD_NAME}; when that succeeds, skip register_agent and fetch_inbox for ${CHILD_NAME}. If the helper is unavailable, ensure_project with human_key ${PROJECT_KEY}, then register_agent with name ${CHILD_NAME} and registration_token only if CHILD_REGISTRATION_TOKEN is visible, then fetch_inbox. Do not infer the task from this prompt; treat the inbox request as authoritative."
        tmux new-session -d -s "$CHILD_NAME" \
            -c "$WORK_DIR" \
            "${TMUX_ENV_ARGS[@]}" \
            -e "MCP_AGENT_MAIL_TOKEN=$TOKEN" \
            '/bin/zsh -lc '"'"'
                if [[ -f "$HOME/.codex/bin/codex_agent_bootstrap.sh" ]]; then
                    source "$HOME/.codex/bin/codex_agent_bootstrap.sh" "$PWD"
                fi
                if [[ -f "$HOME/.codex/bin/launch_codex_workspace.sh" ]]; then
                    env -u OPENAI_API_KEY /bin/bash "$HOME/.codex/bin/launch_codex_workspace.sh" "$PWD" --model gpt-5.5 -c model_reasoning_effort=xhigh
                else
                    EXTRA_ARGS=()
                    if [[ -n "${AGENTSTACK_PROJECT_KEY:-}" && -d "$AGENTSTACK_PROJECT_KEY" ]]; then
                        EXTRA_ARGS+=(--add-dir "$AGENTSTACK_PROJECT_KEY")
                    fi
                    [[ -d "$HOME/.claude" ]] && EXTRA_ARGS+=(--add-dir "$HOME/.claude")
                    [[ -d "$HOME/.codex" ]] && EXTRA_ARGS+=(--add-dir "$HOME/.codex")
                    env -u OPENAI_API_KEY codex -C "$PWD" --sandbox workspace-write --full-auto \
                        "${EXTRA_ARGS[@]}" --model gpt-5.5 -c model_reasoning_effort=xhigh
                fi
                /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"
            '"'"''

        echo "[spawn_child/pre-reg] Waiting for Codex REPL..." >&2
        WAITED=0
        WAIT_MAX=90
        READY=false
        while [[ $WAITED -lt $WAIT_MAX ]]; do
            sleep 3
            WAITED=$((WAITED + 3))
            PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p -S -30 2>/dev/null || true)
            if echo "$PANE_TEXT" | grep -qF "Use existing model"; then
                echo "[spawn_child/pre-reg] Model selection dialog detected; choosing existing model" >&2
                tmux send-keys -t "$CHILD_NAME" Down Enter
                sleep 5
                continue
            fi
            # Trust ダイアログ: "Do you trust the contents of this directory?"
            if echo "$PANE_TEXT" | grep -qi "Do you trust"; then
                echo "[spawn_child/pre-reg] Trust dialog detected; pressing Enter" >&2
                tmux send-keys -t "$CHILD_NAME" Enter
                sleep 3
                continue
            fi
            if echo "$PANE_TEXT" | grep -qi "Press enter to continue"; then
                echo "[spawn_child/pre-reg] Sign-in prompt detected; pressing Enter" >&2
                tmux send-keys -t "$CHILD_NAME" Enter
                sleep 3
                continue
            fi
            LAST_LINES=$(echo "$PANE_TEXT" | tail -5)
            if echo "$LAST_LINES" | grep -qE '% (left|context)'; then
                if ! echo "$PANE_TEXT" | grep -qF "Use existing model"; then
                    READY=true
                    break
                fi
            fi
        done

        if [[ "$READY" == true ]]; then
            sleep 2
            echo "[spawn_child/pre-reg] Waited ${WAITED}s (+2s); injecting prompt" >&2
        else
            echo "[spawn_child/pre-reg] Timeout (${WAIT_MAX}s); injecting prompt anyway" >&2
            sleep 2
        fi

        tmux send-keys -t "$CHILD_NAME" -l "$CODEX_PROMPT"
        sleep 0.5
        tmux send-keys -t "$CHILD_NAME" C-m
    else
        # Claude Code startup (--pre-registered mode).
        WARM_POOL="$HOOKS_DIR/warm_pool.sh"
        # warm pool は opus を `claude-opus-4-8`(200K)、sonnet を `claude-sonnet-4-6`(200K) で
        # 事前起動している。要求モデル（正規化済み CHILD_MODEL）が warm の事前起動モデルと
        # 完全一致するときだけ claim する。それ以外（claude-opus-4-8[1m] / fable / haiku /
        # sonnet[1m] 等）は __skip_warm__ で cold-start し、$CLAUDE_CHILD_MODEL を尊重する。
        # 旧実装は部分一致（*opus* + *[1m]* skip）だったため、opus[1m] は skip できても
        # fable 等の非デフォルトモデルが warm-sonnet に握り潰されていた（RainyKepler 事例）。
        # exact-match に広げて [1m] 以外の降格も塞ぐ。
        case "$CHILD_MODEL" in
            claude-opus-4-8)   WARM_TYPE="opus" ;;
            claude-sonnet-4-6) WARM_TYPE="sonnet" ;;
            *)                 WARM_TYPE="__skip_warm__" ;;
        esac

        WARM_CLAIMED=false
        WARM_STATUS=$(bash "$WARM_POOL" status 2>/dev/null || true)
        if [[ -f "$WARM_POOL" ]] && echo "$WARM_STATUS" | grep -q "${WARM_TYPE}.*ready"; then
            echo "[spawn_child/pre-reg] Claiming warm pool session ($WARM_TYPE)..." >&2
            if CLAIMED_NAME=$(bash "$WARM_POOL" claim "$WARM_TYPE" "$CHILD_NAME" 2>/dev/null); then
                WARM_CLAIMED=true
                echo "[spawn_child/pre-reg] Warm session claimed -> $CHILD_NAME" >&2
            fi
        fi

        if [[ "$WARM_CLAIMED" == false ]]; then
            # Cold start（フォールバック）
            echo "[spawn_child/pre-reg] Cold start..." >&2
            tmux new-session -d -s "$CHILD_NAME" \
                -c "$WORK_DIR" \
                "${TMUX_ENV_ARGS[@]}" \
                -e "CLAUDE_CHILD_MODEL=$CHILD_MODEL" \
                '/bin/zsh -lc '"'"'claude --model "$CLAUDE_CHILD_MODEL"; /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"'"'"''

            WAITED=0
            while [[ $WAITED -lt 60 ]]; do
                sleep 2
                WAITED=$((WAITED + 2))
                PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p 2>/dev/null || true)
                if echo "$PANE_TEXT" | grep -qE '(for shortcuts|^❯ )'; then break; fi
            done
            sleep 1
        fi

        CHILD_PROMPT="Child agent startup. AGENT_NAME=${CHILD_NAME}; parent=${PARENT_NAME}. Follow the child-agent startup procedure in CLAUDE.md and start the task immediately."
        tmux send-keys -t "$CHILD_NAME" -l "$CHILD_PROMPT"
        sleep 0.3
        tmux send-keys -t "$CHILD_NAME" C-m
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

# --- Bearer Token (Keychain first, .env fallback) ---
TOKEN=$(get_agentstack_token 2>/dev/null || true)
if [[ -z "$TOKEN" ]]; then
    echo "Error: could not read HTTP_BEARER_TOKEN from env, Keychain, or .env" >&2
    exit 1
fi

# JSON-RPC呼び出しヘルパー（http.clientベース — urllib はSSEストリームでハングするため）
call_mcp() {
    local method="$1"
    local args_json="$2"
    python3 - "$method" "$args_json" "$MCP_URL" "$TOKEN" <<'PYEOF'
import sys, json, http.client
from urllib.parse import urlparse

method = sys.argv[1]
args = json.loads(sys.argv[2])
url = sys.argv[3]
token = sys.argv[4]

parsed = urlparse(url)
payload = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": method, "arguments": args}
}).encode()

conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
conn.request("POST", parsed.path, body=payload, headers={
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Authorization": f"Bearer {token}",
    "Connection": "close"
})
resp = conn.getresponse()
print(resp.read().decode())
conn.close()
PYEOF
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

child_agent_exists() {
    local agent_name="$1" args_json response
    args_json=$(python3 -c "
import json, sys
print(json.dumps({'project_key': sys.argv[1], 'agent_name': sys.argv[2]}))
" "$PROJECT_KEY" "$agent_name")
    response="$(call_mcp "whois" "$args_json" 2>/dev/null || true)"
    [[ -n "$response" ]] || return 1
    if printf '%s' "$response" | mcp_response_has_error; then
        return 1
    fi
    [[ -n "$(printf '%s' "$response" | mcp_extract_agent_name)" ]]
}

pick_available_child_agent_name() {
    local attempts="${AGENTSTACK_AGENT_NAME_ATTEMPTS:-75}"
    local candidate adjective scientist i

    load_agent_name_helpers || return 1

    for ((i = 0; i < attempts; i++)); do
        candidate="$(ags_pick_adjective_scientist_name)" || return 1
        if ! child_agent_exists "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    for ((i = 2; i < attempts + 200; i++)); do
        adjective="$(ags_pick_adjective)" || return 1
        scientist="$(ags_pick_scientist)" || return 1
        candidate="${adjective}-${i}-${scientist}"
        if ! child_agent_exists "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

retire_agent_with_registration_token() {
    local agent_name="$1"
    local registration_token="$2"
    local retire_args
    retire_args=$(python3 -c "
import json, sys
print(json.dumps({
    'project_key': sys.argv[1],
    'agent_name': sys.argv[2],
    'registration_token': sys.argv[3],
}))
" "$PROJECT_KEY" "$agent_name" "$registration_token")
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
    echo "Error: cannot connect to mcp-agent-mail server at $MCP_URL" >&2
    exit 1
fi

# --- 2. 子エージェントを事前登録 ---
TASK_SHORT="${TASK:0:80}"
if [[ "$USE_CODEX" == true ]]; then
    CHILD_PROGRAM="codex"
    CHILD_MODEL="gpt-5.5"
else
    CHILD_PROGRAM="claude-code"
    # Claude 子はモデル名を正規化（省略時 Opus 4.8 1M 既定）
    CHILD_MODEL="$(normalize_claude_model "$CLAUDE_MODEL")"
fi

CHILD_REGISTRATION_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
if ! CHILD_NAME_CANDIDATE="$(pick_available_child_agent_name)"; then
    echo "Error: failed to generate an available child agent name" >&2
    exit 1
fi

REGISTER_ARGS=$(python3 -c "
import json, sys
args = {
    'project_key': sys.argv[1],
    'program': sys.argv[2],
    'model': sys.argv[3],
    'task_description': sys.argv[4],
    'registration_token': sys.argv[5],
    'name': sys.argv[6],
}
print(json.dumps(args))
" "$PROJECT_KEY" "$CHILD_PROGRAM" "$CHILD_MODEL" "$TASK_SHORT" "$CHILD_REGISTRATION_TOKEN" "$CHILD_NAME_CANDIDATE")

REGISTER_RESULT=$(call_mcp "register_agent" "$REGISTER_ARGS")
CHILD_NAME=$(python3 -c "
import json, sys
r = json.loads(sys.stdin.read())
data = json.loads(r['result']['content'][0]['text'])
print(data['name'])
" <<< "$REGISTER_RESULT")

if [[ -z "$CHILD_NAME" ]]; then
    echo "Error: failed to read child agent name" >&2
    echo "$REGISTER_RESULT" >&2
    exit 1
fi

mkdir -p "$CHILD_STATE_DIR"
python3 -c "
import json, pathlib, sys
path = pathlib.Path(sys.argv[4])
path.write_text(json.dumps({
    'agent_name': sys.argv[1],
    'project_key': sys.argv[2],
    'registration_token': sys.argv[3],
}), encoding='utf-8')
" "$CHILD_NAME" "$PROJECT_KEY" "$CHILD_REGISTRATION_TOKEN" "$CHILD_STATE_DIR/$CHILD_NAME.json"

# --- 失敗時cleanup trap ---
# tmux セッション生成前に異常終了した場合だけ、登録済みの子エージェントと予約を解放する
CHILD_SESSION_STARTED=false
cleanup_on_failure() {
    if [[ "$CHILD_SESSION_STARTED" == true ]]; then
        return
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
        if [[ -n "${CHILD_REGISTRATION_TOKEN:-}" ]]; then
            retire_agent_with_registration_token "$CHILD_NAME" "$CHILD_REGISTRATION_TOKEN" > /dev/null 2>&1 || true
        fi
    fi
    # worktree も作っていれば撤去
    cleanup_worktree
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
        if [[ -n "${CHILD_REGISTRATION_TOKEN:-}" ]]; then
            retire_agent_with_registration_token "$CHILD_NAME" "$CHILD_REGISTRATION_TOKEN" > /dev/null 2>&1 || true
        fi
        echo "[spawn_child] Released reservations and retired $CHILD_NAME" >&2
        CHILD_SESSION_STARTED=true  # suppress cleanup trap; cleanup already done
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
- **Use \`${PROJECT_KEY}\` as the mcp-agent-mail project_key**, not the current working directory. This is especially important in worktree mode. The tmux \$PROJECT_KEY env var has the same value. If you call ensure_project(human_key=cwd) from outside the project root, you will create a different project and will not be able to read this inbox.
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
TMUX_ENV_ARGS=(-e "CLAUDECODE=1" -e "AGENT_NAME=$CHILD_NAME" -e "PARENT_AGENT=$PARENT_NAME" -e "PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_PROJECT_KEY=$PROJECT_KEY" -e "AGENTSTACK_HOOKS_DIR=$HOOKS_DIR" -e "AGENTSTACK_RUNTIME_DIR=$RUNTIME_DIR" -e "AGENTSTACK_MCP_URL=$MCP_URL" -e "AGENTSTACK_MAIL_ENV=$MAIL_ENV" -e "AGENTSTACK_TERMINAL=$TERMINAL_SETTING")
if [[ -n "$AGENTSTACK_HOME_DIR" ]]; then
    TMUX_ENV_ARGS+=(-e "AGENTSTACK_HOME=$AGENTSTACK_HOME_DIR")
fi
if [[ -n "$RESOURCES" ]]; then
    TMUX_ENV_ARGS+=(-e "CHILD_RESOURCES=$RESOURCES")
fi
if [[ -n "${CHILD_REGISTRATION_TOKEN:-}" ]]; then
    TMUX_ENV_ARGS+=(-e "CHILD_REGISTRATION_TOKEN=$CHILD_REGISTRATION_TOKEN")
fi

if [[ "$USE_CODEX" == true ]]; then
    # Codex startup: inject a bootstrap prompt that points the child to inbox.
    CODEX_PROMPT="You are ${CHILD_NAME}. The parent agent is ${PARENT_NAME}. The child name ${CHILD_NAME} is already reserved, so do not register under another name. The canonical task is in your mcp-agent-mail inbox. First, if ${REREGISTER_HELPER:-agentstack-reregister} exists, run PROJECT_KEY=${PROJECT_KEY} ${REREGISTER_HELPER:-agentstack-reregister} ${CHILD_NAME}; when that succeeds, skip register_agent and fetch_inbox for ${CHILD_NAME}. If the helper is unavailable, ensure_project with human_key ${PROJECT_KEY}, then register_agent with name ${CHILD_NAME} and registration_token only if CHILD_REGISTRATION_TOKEN is visible, then fetch_inbox. Do not infer the task from this prompt; treat the inbox request as authoritative."
    tmux new-session -d -s "$CHILD_NAME" \
        -c "$WORK_DIR" \
        "${TMUX_ENV_ARGS[@]}" \
        -e "MCP_AGENT_MAIL_TOKEN=$TOKEN" \
        '/bin/zsh -lc '"'"'
            if [[ -f "$HOME/.codex/bin/codex_agent_bootstrap.sh" ]]; then
                source "$HOME/.codex/bin/codex_agent_bootstrap.sh" "$PWD"
            fi
            if [[ -f "$HOME/.codex/bin/launch_codex_workspace.sh" ]]; then
                env -u OPENAI_API_KEY /bin/bash "$HOME/.codex/bin/launch_codex_workspace.sh" "$PWD" --model gpt-5.5 -c model_reasoning_effort=xhigh
            else
                EXTRA_ARGS=()
                if [[ -n "${AGENTSTACK_PROJECT_KEY:-}" && -d "$AGENTSTACK_PROJECT_KEY" ]]; then
                    EXTRA_ARGS+=(--add-dir "$AGENTSTACK_PROJECT_KEY")
                fi
                [[ -d "$HOME/.claude" ]] && EXTRA_ARGS+=(--add-dir "$HOME/.claude")
                [[ -d "$HOME/.codex" ]] && EXTRA_ARGS+=(--add-dir "$HOME/.codex")
                env -u OPENAI_API_KEY codex -C "$PWD" --sandbox workspace-write --full-auto \
                    "${EXTRA_ARGS[@]}" --model gpt-5.5 -c model_reasoning_effort=xhigh
            fi
            /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"
        '"'"''
    CHILD_SESSION_STARTED=true

    # Codex REPL起動待機
    # 注意: モデルアップグレードダイアログやサインインプロンプトが
    # 表示されることがある。これらを自動スキップしてから入力待ちを検知する。
    echo "[spawn_child] Waiting for Codex REPL..." >&2
    WAITED=0
    WAIT_MAX=90
    READY=false
    while [[ $WAITED -lt $WAIT_MAX ]]; do
        sleep 3
        WAITED=$((WAITED + 3))
        PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p -S -30 2>/dev/null || true)

        # モデルアップグレードダイアログ: "Use existing model" を選択
        if echo "$PANE_TEXT" | grep -qF "Use existing model"; then
            echo "[spawn_child] Model selection dialog detected; choosing existing model" >&2
            tmux send-keys -t "$CHILD_NAME" Down Enter
            sleep 5
            continue
        fi

        # Trust ダイアログ: "Do you trust the contents of this directory?"
        if echo "$PANE_TEXT" | grep -qi "Do you trust"; then
            echo "[spawn_child] Trust dialog detected; pressing Enter" >&2
            tmux send-keys -t "$CHILD_NAME" Enter
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

        # Codex の入力待ち検知: "% left" が最終行付近にある = REPL ready
        # "? for shortcuts" が表示されてかつモデル選択が終わっていれば ready
        LAST_LINES=$(echo "$PANE_TEXT" | tail -5)
        if echo "$LAST_LINES" | grep -qE '% (left|context)'; then
            # モデル選択がまだ表示されていないことを確認
            if ! echo "$PANE_TEXT" | grep -qF "Use existing model"; then
                READY=true
                break
            fi
        fi
    done

    if [[ "$READY" == true ]]; then
        sleep 2
        echo "[spawn_child] Waited ${WAITED}s (+2s); injecting prompt" >&2
    else
        echo "[spawn_child] Timeout (${WAIT_MAX}s); injecting prompt anyway" >&2
        sleep 2
    fi

    # Codex にはタスク概要を含むプロンプトを注入
    # 注意: テキストと Enter は分離して送信する。
    # 長いテキスト + C-m を同一コールで送ると C-m が落ちることがある。
    tmux send-keys -t "$CHILD_NAME" -l "$CODEX_PROMPT"
    sleep 0.5
    tmux send-keys -t "$CHILD_NAME" C-m
else
    # Claude Code 起動（モデル指定付き）
    tmux new-session -d -s "$CHILD_NAME" \
        -c "$WORK_DIR" \
        "${TMUX_ENV_ARGS[@]}" \
        -e "CLAUDE_CHILD_MODEL=$CHILD_MODEL" \
        '/bin/zsh -lc '"'"'claude --model "$CLAUDE_CHILD_MODEL"; /bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"'"'"''
    CHILD_SESSION_STARTED=true

    # Claude REPL起動待機
    echo "[spawn_child] Waiting for Claude REPL..." >&2
    WAITED=0
    WAIT_MAX=60
    while [[ $WAITED -lt $WAIT_MAX ]]; do
        sleep 2
        WAITED=$((WAITED + 2))
        PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p 2>/dev/null || true)
        if echo "$PANE_TEXT" | grep -qE '(for shortcuts|^❯ )'; then
            break
        fi
    done
    sleep 1
    echo "[spawn_child] Waited ${WAITED}s (+1s); injecting prompt" >&2

    CHILD_PROMPT="Child agent startup. AGENT_NAME=${CHILD_NAME}; parent=${PARENT_NAME}. Follow the child-agent startup procedure in CLAUDE.md and start the task immediately."
    tmux send-keys -t "$CHILD_NAME" -l "$CHILD_PROMPT"
    sleep 0.3
    tmux send-keys -t "$CHILD_NAME" C-m
fi

open_child_terminal "$CHILD_NAME"

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
