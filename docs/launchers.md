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

`agentstack-reregister` は既存 token を使う helper であり、token を新規発行しません。launcher を一度も通らない parentless bot、`server-null` row の初回 claim、または exact `stage=local-token reason=credential-unavailable` からの明示 recovery は、モデルではなく operator が [常駐 agent の enrollment と起動](persistent-agents.md) に従って行います。通常の再起動では enrollment を繰り返しません。

## 常駐 parentless agent

同じ identity を restart 後も使う bot は `agentstack-persistent` profile で起動できます。profile は provider、parentless、persistent lifecycle、interactive / headless を別 field にし、保存済み credential と Mail authority、数値 row を照合してからだけ command を `exec` します。自動 enrollment や alias 作成は行いません。interactive Claude は通常の `--channels plugin:...` を保持し、working directory から filesystem root までの `.mcp.json`、通常 / separate gitdir の checkout root local、linked worktree の main-checkout local、実効 inventory / marketplace registry / catalog から解決した installed plugin を含む有限な実効 config を検査して、standalone Mail alias だけを同名 bound overlay へ置き換えます。plugin / managed の非対応 config や読めない実効 source は推測せず起動前に拒否します。

新しい bot、既存 bot の移行、credential recovery、Claude Channels の PTY、headless bridge、別 Mail / 別の機体の手順は [常駐 agent の enrollment と起動](persistent-agents.md) を正本とします。

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

Codex child の fresh launch は、`launch_origin: child` と選択した `codex_mcp_profile` を launch expectation に記録します。`cx` / `agent-start-codex` と persistent launcher の top-level Codex は `launch_origin: standalone` を記録し、child profile は持ちません。公式 `SessionStart` が identity と rollout を検証した後、その非秘密 provenance は数値 agent ID・project・provider とともに bound session receipt へコピーされます。receipt は private resume state・credential・専用 home の外に残るため、dashboard は cleanup 済み child、製品起動の top-level session、origin 不明の session を区別できます。provenance 導入前の receipt は名前や履歴から origin を推測せず resume 不可にします。

正常 cleanup は reservation release と remote retire を行い、child home、proxy runtime、旧 MCP config を削除します。一方、schema・`retired_at`・`resume_expires_at` 付き state と canonical credential は既定30日保持します（`AGENTSTACK_CHILD_RESUME_RETENTION_DAYS=0` は全削除）。dashboard resume は receipt、正式な project・数値 agent ID・name、private state / credential、期限、permission を照合し、保存した `codex_mcp_profile` と現在の source Codex home から新しい child home を作ります。古い home や proxy runtime の snapshot は再利用しません。

resume bootstrap は retained credential で同じ identity を再登録し、fresh binding expectation を保存してから remote identity を unretire します。unretire は `codex resume` の直前に行い、それ以前の失敗では生成物だけを捨てて credential と retired 状態を保ちます。unretire の失敗でも Codex は起動しません。再開後の cleanup は provenance を保持した新しい receipt と private material を再び期限付きで残すため、同じ child を複数回 resume できます。

`codex-cli 0.154.0` と `0.156.1` の interactive TUI では、resume の `SessionStart` hook は composer を開いて idle の間ではなく、最初の user message を submit した後に発火することを実測しています。resume bootstrap は dashboard が選んだ session ID を既存 receipt と rollout header の両方で照合し、fresh resume expectation が未 claim・非 conflict の間だけ、その receipt の exact `launch_id` / `receipt_id` を history の根拠として保持します。prompt を送らず終了しても同じ session を再び resume できます。最初の submit 後は公式 payload の session ID も固定済み ID と header に一致するときだけ fresh receipt に置き換わり、別 ID・競合・次の startup expectation は fallback を無効にします。この発火時点は上記 version の実測範囲であり、他 version / mode の一般保証ではありません。

SessionStart recorder は lock 待ち、rollout header 検査、launch transition、receipt write、最終 outcome の所要時間を `$AGENTSTACK_RUNTIME_DIR/codex-session-binding.log` に JSONL で残します。session ID、launch nonce、transcript path、credential は記録しません。`started` の後に `outcome` が無ければ hook が途中で打ち切られたことを区別できます。SessionStart の hook deadline は、cold start や短い lock 待ちで receipt 作成が途切れないよう5秒です。

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

worktree child の cwd は `${AGENTSTACK_WORKTREE_ROOT:-$AGENTSTACK_HOME/worktrees}/<name>`（通常は `~/.agentstack/worktrees/<name>`）に変わりますが、ORRERY Mail project は変わりません。task には必ず `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY` を正本として明記します。`--worktree-base <rev>` を使うと複数 child の baseline を固定できます。旧 `/tmp/cc-worktrees` は移行せず、新規 spawn だけが永続 root を使います。

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
- [常駐 agent の enrollment と起動](persistent-agents.md)
- [Codex App 統合](codex-app.md)
- [Dashboard](dashboard.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
