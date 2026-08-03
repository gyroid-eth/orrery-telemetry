# Launcher と identity

> English version: planned.

[前: インストール](install.md) · [README に戻る](../README.md) · [次: 委任と child agent](delegation.md)

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

top-level launcher の新規 identity は要求時 `Adjective-Scientist`（ハイフン形、たとえば `Windy-Fermi`）です。**実際の登録名は `register_agent` 応答の read-back が正**で、サーバーが separator を除去したり別名へ coerce する環境もあります。要求名と read-back が違う場合、launcher は差し替えを明示的に報告し、まだ起動前の top-level session は read-back 名へ揃えます。既に task・token・inbox が結び付いた reserved / resumed identity は別名を採用せず停止します。

- adjective は `bin/lib/agentstack-scientists.sh` の134語
- scientist は `dashboard/scientist_portraits.json`
- scientist suffix が portrait key
- ASCII alphabetic の scientist だけを候補にする

134語は agent-mail の正典 `SIMPLE_ADJECTIVES` Round 3 と逐語同期しています。strict agent-mail deployment は正典で生成名を検証するため、AgentStack 側だけへ独自語を追加してはいけません。

launcher、dashboard catalog、suggestion API、child preregistration は同じ adjective / scientist source を共有します。命名 source を重複させないことで、portrait、登録名、server-side validation の drift を防ぎます。

## Name availability と fail-closed

候補の利用可否は三値です。

| 状態 | 意味 |
| --- | --- |
| `available` | project 内に同名 identity がないと確認済み |
| `occupied` | 同名 identity が存在 |
| `unknown` | transport failure、auth error、timeout、DB unavailable などで確認不能 |

`unknown` は空き名として扱いません。launcher の availability probe は既定で `unknown` が3回続くと停止します。通信障害時に衝突しうる identity を取得しない fail-closed 設計です。

dashboard spawn は scientist rail の空き判定後、`/api/suggest-name` で完全名を再検証します。指定名から `-` を除いて正規化し、exact status が `available` でない場合は拒否します。詳しくは [Dashboard](dashboard.md#new-agent) と [API](api.md#post-apispawn) を参照してください。

## Identity 登録

launcher は CLI を起動する前に agent-mail へ identity を登録します。

1. stale な `AGENT_NAME`、`PARENT_AGENT`、token、reserved marker を削除
2. candidate name を生成
3. agent-mail health を確認
4. project key、program、model、task metadata で登録
5. 要求名と返された canonical name を比較。不一致なら top-level は明示して tmux session を返却名へ rename、reserved identity は停止
6. managed agent list と clipboard を更新

`AGENTSTACK_PROJECT_KEY` が未設定、または agent-mail が到達不能でも CLI 自体は preselected name で起動します。ただし mail、reservation、project-scoped dashboard 機能は使えません。

Claude Code hook は session 内登録も記録します。Codex は Claude Code の hook system を持たないため、`agentstack-codex-bootstrap` が起動前の登録と tmux rename を担当します。

## Registration token

既存 identity を再登録するには、その identity の `registration_token` が必要です。top-level token は mode `0600` で保存されます。

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/agent_token_<name>
```

delegated child はさらに:

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/child-agents/<name>.json
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

## Skills（2件）と file reservation

installer は次の skill を正本の `~/.agentstack/skills` へ配置し、Claude Code の標準 discovery path `~/.claude/skills/<name>` から各正本への絶対 symlink を作ります。

- [`/delegate`](../skills/delegate/SKILL.md): resource を宣言・予約し、Claude / Codex child、任意 model、worktree を起動して監視
- [`/log`](../skills/log/SKILL.md): session の決定、変更、検証、次 action を再利用できる Markdown log に整理

install 前から開いていた Claude Code session は追加された skill を認識しません。一度 `/exit` し、新しい terminal から `agent-start <project>` で起動し直してください。

### `/delegate`

`/delegate` は child を起動するだけの shortcut ではありません。

AgentStack の委譲は、必ず先頭の slash を付けて `/delegate ...` と入力します。`delegate ...` は通常の prompt であり、この skill の呼び出しではありません。Claude が組み込み subagent / Agent tool で処理した場合、成果物ができても AgentStack の identity、reservation、専用 tmux session、dashboard telemetry には載りません。AgentStack で監視する child を作る目的では、組み込み Agent tool を `/delegate` の代わりに使わないでください。

| 項目 | 内容 |
| --- | --- |
| トリガー | child への委譲、subagent 起動、並列作業を依頼されたとき |
| 基本形 | `/delegate "<task>" [--dir <path>] [--codex] [--model <model>] [--worktree] [--worktree-base <rev>]` |
| 必須前提 | 親の agent-mail identity と正本 project key。編集 task では対象 resource 宣言と reservation |
| 任意前提 | `--worktree` には git repository、dashboard annotation には dashboard service |

親 agent は task を渡して終了せず、scope と risk の決定、reservation、monitoring、成果物の検証に責任を持ちます。`--codex` で Codex child、`--model` で許可済み model、`--dir` で child の cwd を選びます。

1. 対象 resource、排他性、失敗点、可逆性から risk と監視頻度を決める
2. `agentstack-preregister-child` で child-owned token と canonical name を作る
3. file reservation、contact、正本 task message を準備する
4. `spawn_child.sh` で Claude / Codex、model、worktree を起動する
5. agent-mail の完了報告と `monitor_child_agent.sh` を読み、自分で成果物を検証する
6. reservation を release してから親の結果として報告する

worktree child の cwd は `/tmp/cc-worktrees/<name>` に変わりますが、agent-mail project は変わりません。task には必ず `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY` を正本として明記します。`--worktree-base <rev>` を使うと複数 child の baseline を固定できます。

monitor の danger command 検知は既定では passive です。`AGENTSTACK_MONITOR_DANGER_CHECK=1` で有効にすると一致時に soft stop します。出力が変わらない stasis の反復時は設定にかかわらず soft stop、`C-c`、process group freeze、session kill の順に段階化します。exit code の意味は skill 本文を参照してください。

### `/log`

| 項目 | 内容 |
| --- | --- |
| トリガー | session log の作成、現在作業の要約、決定・変更・検証の保存を依頼されたとき |
| 基本形 | `/log <theme> [project]` |
| 必須前提 | theme。project が自明でなく、安全に推定できない場合だけ確認 |
| 任意前提 | Obsidian mode には `AGENTSTACK_OBSIDIAN_APP` と vault 内を指す `AGENTSTACK_PROJECT_KEY` |

`/log` は `AGENTSTACK_OBSIDIAN_APP` と `AGENTSTACK_PROJECT_KEY` が揃い、project が vault 内にある場合だけ Obsidian mode を使います。既存の project `logs/` と daily note 規約があればそこへ接続し、規約が見つからなければ private な directory 構成を推測しません。

それ以外は:

```text
<git-root-or-cwd>/logs/LOG_<YYYY-MM-DDTHHmm> <Theme>.md
```

へ書きます。log は transcript ではなく、Goal、Decisions、Work Performed、Verification、Related Notes、Next Actions を中心にします。

### Hook と reservation enforcement

Claude Code は `check-file-reservation.sh` の PreToolUse hook で `Edit` / `Write` を hard block します。Codex には同等 hook がないため、managed `~/.codex/AGENTS.md` が reserve / renew / release discipline を指示します。registry は共通なので、Claude と Codex の reservation は相互に見えます。

repository にある11件の hook / helper の発火タイミング、caller、block 条件、cleanup lifecycle は [Hooks と運用 helper](hooks.md)を参照してください。

## 関連文書

- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [Dashboard](dashboard.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
