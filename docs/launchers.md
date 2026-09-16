# Launcher と identity

> English version: [launchers.en.md](launchers.en.md)

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

session 名を ORRERY Mail identity と一致させることで、次の照合が一意になります。

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

134語は ORRERY Mail の正典 `SIMPLE_ADJECTIVES` Round 3 と逐語同期しています。strict ORRERY Mail deployment は正典で生成名を検証するため、ORRERY Telemetry 側だけへ独自語を追加してはいけません。

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

launcher は CLI を起動する前に ORRERY Mail へ identity を登録します。

1. stale な `AGENT_NAME`、`PARENT_AGENT`、token、reserved marker を削除
2. candidate name を生成
3. ORRERY Mail health を確認
4. project key、program、model、task metadata で登録
5. 要求名と返された canonical name を比較。不一致なら top-level は明示して tmux session を返却名へ rename、reserved identity は停止
6. Codex CLI では数値 agent ID を含む今回の launch expectation を記録
7. managed agent list と clipboard を更新

`AGENTSTACK_PROJECT_KEY` が未設定、または ORRERY Mail が到達不能でも CLI 自体は preselected name で起動します。ただし mail、reservation、project-scoped dashboard 機能は使えません。

Claude Code hook は session 内登録も記録します。Codex CLI は `agentstack-codex-bootstrap` が起動前の登録と tmux rename に加えて fresh `launch_id` を作り、公式 SessionStart hook が runtime の `session_id` と rollout header を確認して receipt を完成させます。receipt が無ければ Codex History は推測へ fallback しません。

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

pre-registered child へ親 token は渡しません。dashboard spawn は child 専用 token を生成し、mode `0600` の一時 token file 経由で `spawn_child.sh --pre-registered` へ渡します。Codex では正式な登録応答の数値 ID・name・project・program を非秘密の `.binding.json` sidecar に添えます。launcher は token と sidecar を検証し、CLI の起動と fresh launch expectation の作成が成功した後にだけ一時 handoff を消費します。token を transcript、command-line argument、dashboard response に表示しません。

`agentstack-preregister-child` は Codex の正式な登録応答を、一時 handoff だけでなく上記の canonical token と child state にも mode `0600` で保存します。そのため `spawn_child.sh --pre-registered <name> --codex ...` は `--child-token-file` を省略しても、同じ登録に由来する token・数値 ID・name・project・program から fresh expectation を作れます。canonical token が無くても完全な child state からは復元できますが、token-only の旧 state、破損 metadata、project/name/provider の不一致、token と state の世代不一致は推測で補いません。`agentstack-preregister-child` を同じ project で再実行するか、一時 token と対応する `.binding.json` を `--child-token-file` で渡す必要があります。登録済み Codex child は fresh expectation を永続化できなければ CLI 起動前に停止します。

`/delegate` の既定経路は `--pre-registered --embed-task --task-file <path>` です。親が mode `0600` の一時ファイルへタスク全文を書き、launcher が child 名、親名、spawn 時刻、project key、完了時の `send_message` 指示とともに Claude / Codex の最初の prompt へ埋め込みます。登録・再登録・`fetch_inbox` の起動儀式は不要です。この prompt が唯一の正本なので、同じ child へ task mail を別送してはいけません。`--task-file` は位置引数の task より優先し、backtick や `$()` を shell に解釈させず渡すための境界でもあります。

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

dashboard の Codex resume も installer が配る同じ `agentstack-codex-bootstrap` を必ず source し、reserved identity を再登録して fresh resume launch を作ってから `codex resume` を exec します。個人用 `~/.codex/bin` wrapper には依存せず、bootstrap / prepare が失敗すれば resume 自体を開始しません。

launcher が child 専用 `CODEX_HOME` を作っていた場合、dashboard resume は正式な project・agent ID・name と private child state / token、さらに `config.toml` 内の local ORRERY proxy identity を照合してから、同じ `CODEX_HOME` / `CODEX_SHARED_CODEX_DIR` と writable root を復元します。state の無い既存 session は従来どおり既存 / 既定設定を使い、state は正しいのに home が無い旧・`inherit` 互換経路も継続します。後者は「home を作らなかった」と「後から消えた」を現 metadata では区別できないため、resume の detail と backend warning に「子専用設定なし・Mail 接続未確認」を残します。state が無いのに同名 child home だけ残る場合や、proxy identity が違う場合は、名前から推測して採用せず resume を停止します。

`codex-cli 0.154.0` の interactive TUI では、startup / resume の `SessionStart` hook は composer を開いて idle の間ではなく、最初の user message を submit した後に発火することを実測しています。そのため resume 直後は history が未確認でもよく、最初の submit 後に公式 payload と rollout header が fresh resume launch に一致して初めて bound になります。この発火時点は 0.154.0 の実測範囲であり、他 version / mode の一般保証ではありません。

API key が環境にあると OAuth を上書きすることがあるため、Codex subprocess だけから除去します。

## Mail watcher と REPL 注入

mail watcher は ORRERY Mail signal を見つけると、対応する tmux session の Claude / Codex REPL へ通知文を注入します。

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

ORRERY Telemetry の委譲は、必ず先頭の slash を付けて `/delegate ...` と入力します。`delegate ...` は通常の prompt であり、この skill の呼び出しではありません。Claude が組み込み subagent / Agent tool で処理した場合、成果物ができても ORRERY Telemetry の identity、reservation、専用 tmux session、dashboard telemetry には載りません。ORRERY Telemetry で監視する child を作る目的では、組み込み Agent tool を `/delegate` の代わりに使わないでください。

| 項目 | 内容 |
| --- | --- |
| トリガー | child への委譲、subagent 起動、並列作業を依頼されたとき |
| 基本形 | `/delegate "<task>" [--dir <path>] [--codex] [--model <model>] [--codex-mcp <inherit\|orrery-only>] [--worktree] [--worktree-base <rev>]` |
| 必須前提 | 親の ORRERY Mail identity と正本 project key。編集 task では対象 resource 宣言と reservation |
| 任意前提 | `--worktree` には git repository、dashboard annotation には dashboard service |

親 agent は task を渡して終了せず、scope と risk の決定、reservation、monitoring、成果物の検証に責任を持ちます。`--codex` で Codex child、`--model` で許可済み model、`--dir` で child の cwd を選びます。

Codex child の MCP は既定で `inherit`（従来互換）です。`/delegate --codex-mcp orrery-only` は認証済み ORRERY Mail と session-binding plugin を残して、他の継承 MCP/plugin を無効化します。plugin skill や外部 app tool が必要な task では使いません。

model の世代名は `spawn_child.sh` の model catalog が正本です。Claude は無指定 / `opus` が `claude-opus-5`、`sonnet` が `claude-sonnet-5`、Codex は無指定 / `sol` が `gpt-5.6-sol` です。`terra` / `luna` は対応する `gpt-5.6-*` alias です。旧世代の正式 ID は互換性のため有効なままですが、warm pool を claim するのは catalog が示す current 200K Opus / Sonnet と完全一致するときだけです。

1. 対象 resource、排他性、失敗点、可逆性から risk と監視頻度を決める
2. `agentstack-preregister-child` で child-owned token と canonical name を作る
3. file reservation、contact、mode `0600` の正本 task file を準備する
4. `spawn_child.sh --embed-task --task-file` で Claude / Codex、model、worktree を起動する（task mail は送らない）
5. ORRERY Mail の完了報告と `monitor_child_agent.sh` を読み、自分で成果物を検証する
6. reservation を release してから親の結果として報告する

worktree child の cwd は `/tmp/cc-worktrees/<name>` に変わりますが、ORRERY Mail project は変わりません。task には必ず `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY` を正本として明記します。`--worktree-base <rev>` を使うと複数 child の baseline を固定できます。

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
