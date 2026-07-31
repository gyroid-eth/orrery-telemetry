# Launcher と identity

> English version: planned.

[前: インストール](install.md) · [README に戻る](../README.md) · [次: Dashboard](dashboard.md)

## 起動コマンド

```bash
export PATH="$HOME/.agentstack/bin:$PATH"

agent-start ~/code/my-project
agent-start-codex ~/code/my-project
```

- `agent-start`: Claude Code
- `agent-start-codex`: Codex CLI

directory 引数を省略すると、`fzf` があれば `AGENTSTACK_BASE_DIR` 以下を選択できます。なければ現在 directory を使います。

```bash
export AGENTSTACK_BASE_DIR="$HOME/Obsidian/MyVault"
agent-start
```

優先順位は明示引数、`fzf` picker、現在 directory の順です。

## tmux session

tmux 外から起動すると、新しい named session を作って現在の terminal tab を置き換えます。tmux 内からは current session を rename し、その場で CLI を `exec` します。

session 名を agent-mail identity と一致させることで、次の照合が一意になります。

- dashboard の click-to-jump
- inbox signal の配送先
- transcript / history
- token recovery
- graceful EXIT / RESUME

terminal process が終了した後も shell を残すため、調査や scrollback を続けられます。

## 科学者名

top-level launcher の新規 identity は `AdjectiveScientist`、たとえば `WindyFermi` です。

- adjective は `bin/lib/agentstack-scientists.sh` の内蔵 list
- scientist は `dashboard/scientist_portraits.json`
- scientist suffix が portrait key
- ASCII alphabetic の scientist だけを候補にする

launcher、dashboard API、child preregistration は同じ adjective / scientist source を共有します。命名 source を重複させないことで、portrait と登録名の drift を防ぎます。

## Name availability と fail-closed

候補の利用可否は三値です。

| 状態 | 意味 |
| --- | --- |
| `available` | project 内に同名 identity がないと確認済み |
| `occupied` | 同名 identity が存在 |
| `unknown` | transport failure、auth error、timeout、DB unavailable などで確認不能 |

`unknown` は空き名として扱いません。launcher の availability probe は既定で `unknown` が3回続くと停止します。通信障害時に衝突しうる identity を取得しない fail-closed 設計です。

dashboard spawn は指定名から `-` を除いて正規化し、確認結果が `available` でない場合は拒否します。spawn v2 の移行状況は [Dashboard](dashboard.md#new-agent) と [API](api.md#post-apispawn) を参照してください。

## Identity 登録

launcher は CLI を起動する前に agent-mail へ identity を登録します。

1. stale な `AGENT_NAME`、`PARENT_AGENT`、token、reserved marker を削除
2. candidate name を生成
3. agent-mail health を確認
4. project key、program、model、task metadata で登録
5. 返された canonical name を tmux session と `AGENT_NAME` に設定
6. managed agent list と clipboard を更新

`AGENTSTACK_PROJECT_KEY` が未設定、または agent-mail が到達不能でも CLI 自体は preselected name で起動します。ただし mail、reservation、project-scoped dashboard 機能は使えません。

Claude Code hook は session 内登録も記録します。Codex は Claude Code の hook system を持たないため、`agentstack-codex-bootstrap` が起動前の登録と tmux rename を担当します。

## Registration token

既存 identity を再登録するには、その identity の `registration_token` が必要です。top-level token は mode `0600` で保存されます。

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/agent_token_<name>
```

delegated child はさらに:

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/child-agents/<name>.json
```

に child-owned state を持ちます。

pre-registered child へ親 token は渡しません。dashboard spawn は child 専用 token を生成し、mode `0600` の一時 token file 経由で `spawn_child.sh --pre-registered` へ渡します。token を transcript、command-line argument、dashboard response に表示しないためです。

`CHILD_REGISTRATION_TOKEN` は歴史的な変数名ですが、top-level identity の再認証でも使われます。

## 再登録

```bash
AGENTSTACK_PROJECT_KEY=/path/to/project \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

helper は owner token を runtime state から読み、同名 identity を復元します。同名登録に失敗しても別名を作らないでください。別名は inbox、thread、reservation、監査履歴を分断します。

## `CLAUDECODE` guard

launcher と child spawner は tmux session ごとの environment に:

```text
CLAUDECODE=1
```

を設定します。interactive shell の exit hook が tmux server 全体を連鎖 kill する事故を防ぐ guard です。

値は session 作成時の `tmux new-session -e` で設定し、他 session の identity と混ざらないよう tmux server global environment には置きません。

## Codex 固有の起動

`agent-start-codex` は次を行います。

- `agentstack-codex-bootstrap` を source して登録と rename
- `codex -C <dir>` で working directory を固定
- `--sandbox ${AGENTSTACK_CODEX_SANDBOX:-workspace-write}`
- `--ask-for-approval ${AGENTSTACK_CODEX_APPROVAL:-on-request}`
- `AGENTSTACK_VAULT` が存在するときだけ `--add-dir`
- `OPENAI_API_KEY` を除去し、ChatGPT OAuth を優先

API key が環境にあると OAuth を上書きすることがあるため、Codex subprocess だけから除去します。

## Mail watcher と REPL 注入

mail watcher は agent-mail signal を見つけると、対応する tmux session の Claude / Codex REPL へ通知文を注入します。

text と submit は別操作です。

```bash
tmux send-keys -t "$session" -l "$text"
sleep 0.2
tmux send-keys -t "$session" C-m
```

Codex では `Enter` keysym が submit にならない場合があるため `C-m` を使います。watcher は bare shell への誤注入を避け、tmux call を timeout 付き worker で実行します。

## Skills と file reservation

installer は次の skill を `~/.agentstack/skills` へ配置します。

- `/delegate`: resource を宣言・予約し、Claude / Codex child、任意 model、worktree を起動して監視
- `/log`: Obsidian vault なら `05_Agents` と Daily Note へ接続した log、なければ local `logs/`

Claude Code は `check-file-reservation.sh` の PreToolUse hook で `Edit` / `Write` を hard block します。Codex には同等 hook がないため、managed `~/.codex/AGENTS.md` が reserve / renew / release discipline を指示します。registry は共通なので、Claude と Codex の reservation は相互に見えます。

## 関連文書

- [Dashboard](dashboard.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
