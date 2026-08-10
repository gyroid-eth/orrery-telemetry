# Hooks と運用 helper

> English version: planned.

[前: 委任と child agent](delegation.md) · [README に戻る](../README.md) · [次: Codex App 統合](codex-app.md)

`hooks/` には、Claude Code の lifecycle event から自動実行される hook と、launcher・dashboard・skill が明示的に呼ぶ運用 helper が同居しています。repository の実装ファイルは **11 件**です。内訳は Claude Code event hook が5件、運用 helper が6件です。

[`hooks/settings.template.json`](../hooks/settings.template.json) は event と command の対応を定義し、[`hooks/README.md`](../hooks/README.md) は settings merge の安全方針を定義します。この文書は、実際にいつ起動し、何を保証するかを説明する利用者向け reference です。

## Claude Code event hook（5件）

installer が `settings.template.json` を `~/.claude/settings.json` へ merge すると、次の event で自動実行されます。

| Event / matcher | 実行ファイル | 発火タイミング | 主な動作 |
| --- | --- | --- | --- |
| `SessionStart` | [`set-ghostty-title.sh`](../hooks/set-ghostty-title.sh) | startup / resume / `/clear` / compact の直後 | 既知の identity を pane metadata、tmux session、terminal title 用 clipboard、managed agent list へ反映 |
| `SessionStart` | [`session-start-reminder.sh`](../hooks/session-start-reminder.sh) | 同上。title helper の後 | agent-mail health と既存 identity を確認し、同名再登録または登録手順と `fetch_inbox` を session context へ出力 |
| `PreToolUse` / `Edit|Write` | [`check-file-reservation.sh`](../hooks/check-file-reservation.sh) | Claude Code が file edit を実行する直前 | protected root 内の exact path reservation を renew / auto-acquire。競合または取得失敗は exit 2 で block |
| `PreToolUse` / `Edit|Write|Bash` | [`check-agent-registered.sh`](../hooks/check-agent-registered.sh) | edit、write、shell command の直前 | 現在の Claude session が `register_agent` 済みか session flag で検査。未登録なら exit 2 で block |
| `PostToolUse` / `register_agent` | [`mark-agent-registered.sh`](../hooks/mark-agent-registered.sh) | `mcp__mcp-agent-mail__register_agent` または互換 tool の応答直後 | 応答を検証し、明示した要求名との完全一致後だけ session flag、session index、pane / tmux metadata を更新 |

`Edit` / `Write` では2つの PreToolUse hook がともに走ります。登録済みでも reservation がなければ書けず、reservation があっても未登録 session なら書けません。

### `set-ghostty-title.sh`

- **発火:** `SessionStart`。さらに `mark-agent-registered.sh` が現在 session 自身の canonical name を取得した後にも background で呼びます。
- **動作:** `AGENT_NAME` または引数を使い、`TMUX_PANE` ごとの identity metadata を runtime directory へ書きます。tmux rename は `pending-*` session にだけ許し、確立済みの親 session を child 名で上書きしません。
- **衝突時:** 同名 tmux session を kill せず、rename を拒否して fail-closed にします。対応 terminal では clipboard を title handoff に使います。名前が未解決なら何もしません。

### `session-start-reminder.sh`

- **発火:** すべての `SessionStart` source。startup だけでなく resume、`/clear`、compact 後にも走ります。
- **動作:** identity を `AGENT_NAME` → pane metadata → exact tmux session の順で解決し、agent-mail の liveness を確認します。owner token と project key があれば shell 側で同じ identity を再登録し、成功後は `fetch_inbox` から始めるよう案内します。
- **再登録できない場合:** 解決済みの同名を `register_agent` に渡す手順を表示します。別名生成へ分岐しません。child 専用 MCP proxy が認証を注入している場合は、model に token file を読ませません。

### `check-file-reservation.sh`

- **発火:** `Edit` / `Write` の直前。対象 path が `AGENTSTACK_PROTECTED_ROOTS`、または未指定時の project root 内にある場合だけ enforcement します。
- **動作:** pane に紐づく canonical agent と owner token を解決し、既存 reservation を相対 path / absolute path の両方で renew します。見つからなければ exact relative path の exclusive reservation を auto-acquire します。
- **判定:** 競合、認証済み renew / acquire の失敗、pending identity は exit 2 で tool call を block します。protected root 外、session を識別できない場合、server に到達できない場合は hook 自体の誤検知で全作業を止めない fail-open path があります。

### `check-agent-registered.sh`

- **発火:** `Edit` / `Write` / `Bash` の直前。
- **動作:** `mark-agent-registered.sh` が作る `/tmp/.claude-agent-registered-<session_id>` を検査します。`/clear` などで `session_id` が変わると旧 flag は一致しないため、再登録まで保護対象 tool を block します。
- **例外:** launcher から `AGENT_NAME` を受け取る bot channel は再登録に必要な shell を使えるよう許可します。hook input に session ID がなければ fail-open です。

### `mark-agent-registered.sh`

- **発火:** `register_agent` MCP tool の PostToolUse。`tool_input` と error でない server response の両方が必要です。response の canonical name を取得できない場合に tool input の明示名へ fallback しません。
- **検証:** `name` が明示されていれば response の `name` と完全一致を要求します。別名、error response、入力または応答の解析失敗は `registration-failures.log` へ記録し、exit 2 で caller に返します。名前を省略した登録だけは response の生成名を採用します。
- **動作:** 検証後にだけ registration flag を作り、`record-session-index.py` を非同期実行します。現在が `pending-*`、既に同名、または env の `AGENT_NAME` と一致する場合だけ title helper を呼びます。
- **親子保護:** 親が child を preregister した PostToolUse でも、親 pane metadata を child identity に書き換えません。
- **保証境界:** PostToolUse は server call 後なので、拒否した別名 row を transaction rollback はしません。また `check-agent-registered.sh` は既存 `AGENT_NAME` を持つ channel を flag なしでも許可します。この hook の保証は「不一致を黙って受理せず、成功 state を新規作成しない」であり、全 session の後続操作を強制停止することではありません。

## 運用 helper（6件）

以下は `settings.template.json` の event へ直接登録されません。caller と起動条件を明示して運用します。

| 実行ファイル | 呼び出し元 / 起動タイミング | 主な動作 |
| --- | --- | --- |
| [`record-session-index.py`](../hooks/record-session-index.py) | `mark-agent-registered.sh` が PostToolUse payload を渡して非同期起動 | agent-mail ID と Claude `session_id`、transcript、cwd の exact mapping を atomic write |
| [`resolve-agent-name.sh`](../hooks/resolve-agent-name.sh) | identity が必要な reminder、reservation、cleanup helper が source | env → pane metadata → exact tmux session の優先順で identity を解決 |
| [`spawn_child.sh`](../hooks/spawn_child.sh) | `/delegate` または dashboard の NEW AGENT が child 起動時に明示実行 | identity、token、task mail、reservation、tmux、Claude / Codex、worktree、readiness を一つの launch transaction にまとめる |
| [`cleanup-child-agent.sh`](../hooks/cleanup-child-agent.sh) | `spawn_child.sh` が起動した child の REPL command が終了した直後 | reservation release、remote identity retire、managed list / state / credential / MCP config の削除を best-effort 実行 |
| [`monitor_child_agent.sh`](../hooks/monitor_child_agent.sh) | `/delegate` の親が監視頻度ごとに一回ずつ実行 | tmux pane を採取し、完了、session 消失、permission prompt、stasis、任意の danger pattern を判定して exit code で返す |
| [`watch_agent_mail_signals.sh`](../hooks/watch_agent_mail_signals.sh) | launcher の登録処理が dedicated `mail-watcher` tmux service として起動 | agent-mail signal を監視し、対象と完全一致する agent tmux session へ通知文と `C-m` を注入 |

### `record-session-index.py`

PostToolUse payload から agent-mail の数値 ID、canonical name、Claude `session_id`、transcript path、cwd を取り出し、`$AGENTSTACK_RUNTIME_DIR/session_index/<agent_id>.json` へ一時 file + `os.replace` で書きます。dashboard はこの exact mapping を session resume に優先し、古い session だけ heuristic へ fallback します。入力不備や I/O failure は registration を妨げない quiet no-op です。

### `resolve-agent-name.sh`

source 専用 helper で、`RESOLVED_AGENT` と解決 source を caller へ返します。優先順位は `AGENT_NAME`、`TMUX_PANE` metadata、対象 pane の exact tmux session です。`pending-*`、`warm-*`、`claimed-*`、`mail-watcher` は identity と見なしません。解決不能時は空文字を返し、block するか fail-open にするかは caller が決めます。

### `spawn_child.sh`

`--resources` による対象宣言を既定で必須にし、競合を確認してから child を起動します。Claude / Codex、model、pre-registered identity、child-owned token file、per-child MCP proxy、任意 worktree に対応します。tmux REPL が ready または早期終了と判定されるまで待ち、正本 task を注入します。引数 / server / worktree failure は exit 1、resource 未宣言は2、reservation conflict は21です。通常は直接叩かず、[/delegate](launchers.md#delegate) または dashboard から利用します。

### `cleanup-child-agent.sh`

child の Claude / Codex command の後段へ連結され、REPL が戻った時だけ実行されます。全 reservation を release し、child owner token で identity を retire して、child の state、token、MCP config、分離した Codex home を削除します。remote release / retire と managed-list 更新は best-effort で試し、その後に local child state を片付けます。

これは Claude Code `SessionEnd` hook ではありません。`SessionEnd` は crash や resume でも発生しうるため、remote identity の retire をその event へ結びつけていません。

### `monitor_child_agent.sh`

常駐 daemon ではなく one-shot monitor です。親は risk に応じた cadence で繰り返し呼びます。exit code は `0` 継続、`10` shell return、`11` session 消失、`20` warning、`30` soft stop、`40` process group を `SIGSTOP`、`50` session kill です。

dangerous command pattern の検査は `AGENTSTACK_MONITOR_DANGER_CHECK=1` のときだけ有効です。一方、pane output が反復して変わらない stasis は常に数え、Escape / `C-c` → freeze → kill と段階的に escalation します。

### `watch_agent_mail_signals.sh`

`fswatch` があれば event watch、なければ2秒 polling を使います。signal file は server-owned dirty bit として削除せず、runtime の delivery state と短期 lease で同じ `(agent, message)` の重複注入を抑えます。30秒の periodic scan が取りこぼしを救済します。

配送先は agent 名と完全一致する tmux session だけです。bare shell や無関係 session を避け、通知 text を literal send した後、submit を別 call の `C-m` で送ります。tmux call は timeout 付き worker に分離し、server stall が watcher 全体を止めないようにします。

## Codex との違い

Codex CLI には Claude Code の `SessionStart` / `PreToolUse` / `PostToolUse` hook system がなく、`mark-agent-registered.sh` も走りません。`agent-start-codex` は bootstrap で identity 登録と tmux rename を済ませ、予約済み child/resume と reregister は応答名不一致で停止します。一方、direct spawn は警告後に応答名を採用し、raw MCP 登録は自動検出されません。これらは別 follow-up であり、mail service の `passthrough` 設定を省略できる根拠にはなりません。managed `~/.codex/AGENTS.md` は reservation の reserve / renew / release を指示します。mail watcher と agent-mail registry は Claude / Codex 共通なので、通知と reservation conflict は相互に見えます。

Codex Desktop はさらに別の plugin hook / Bridge lifecycle を使います。詳しくは [Codex App 統合](codex-app.md)を参照してください。

## 関連文書

- [インストール](install.md)
- [Launcher と identity / Skills](launchers.md)
- [Codex App 統合](codex-app.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
