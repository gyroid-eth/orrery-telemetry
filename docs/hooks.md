# Hooks と運用 helper

> English version: planned.

[前: 委任と child agent](delegation.md) · [README に戻る](../README.md) · [次: Codex App 統合](codex-app.md)

`hooks/` には、Claude Code の lifecycle event から自動実行される hook と、launcher・dashboard・skill が明示的に呼ぶ運用 helper、event hook が source / 起動する内部 library・worker が同居しています。Claude Code event hook は8件です。

[`hooks/settings.template.json`](../hooks/settings.template.json) は event と command の対応を定義し、[`hooks/README.md`](../hooks/README.md) は settings merge の安全方針を定義します。この文書は、実際にいつ起動し、何を保証するかを説明する利用者向け reference です。

## Claude Code event hook（8件）

installer が `settings.template.json` を `~/.claude/settings.json` へ merge すると、次の event で自動実行されます。

| Event / matcher | 実行ファイル | 発火タイミング | 主な動作 |
| --- | --- | --- | --- |
| `SessionStart` | [`set-ghostty-title.sh`](../hooks/set-ghostty-title.sh) | startup / resume / `/clear` / compact の直後 | 既知の identity を pane metadata、tmux session、terminal title 用 clipboard、managed agent list へ反映 |
| `SessionStart` | [`session-start-reminder.sh`](../hooks/session-start-reminder.sh) | 同上。title helper の後 | agent-mail health と既存 identity を確認し、同名再登録または登録手順と `fetch_inbox` を session context へ出力 |
| `PreToolUse` / `Edit|Write` | [`check-file-reservation.sh`](../hooks/check-file-reservation.sh) | Claude Code が file edit を実行する直前 | protected root 内の既存 exact path reservation を renew-only で確認。0件は1回だけ再確認し、なお0件なら exit 2 で block |
| `PreToolUse` / `Edit|Write|Bash` | [`check-agent-registered.sh`](../hooks/check-agent-registered.sh) | edit、write、shell command の直前 | 現在の Claude session が `register_agent` 済みか session flag で検査。未登録なら exit 2 で block |
| `PreToolUse` / reservation tools | [`invalidate-release-debounce.sh`](../hooks/invalidate-release-debounce.sh) | file reservation の取得・renew 直前 | 同じ agent/path に対する古い release worker の token を無効化し、新しい reservation が直後に消される競合を防止 |
| `PostToolUse` / `Edit|Write` | [`release-file-reservation.sh`](../hooks/release-file-reservation.sh) | 成功した file edit の直後 | 既定90秒の grace 後に、guard と同じ project・identity・相対/絶対 path で reservation を release |
| `PostToolUse` / `register_agent` | [`mark-agent-registered.sh`](../hooks/mark-agent-registered.sh) | `mcp__mcp-agent-mail__register_agent` または互換 tool の応答直後 | 応答を検証し、明示した要求名との完全一致後だけ session flag、session index、pane / tmux metadata を更新 |
| `SessionEnd` | [`release-all-reservations.sh`](../hooks/release-all-reservations.sh) | Claude session 終了時 | 現在 identity の全 file reservation を release。identity 自体は retire しない |

`Edit` / `Write` では2つの PreToolUse hook がともに走ります。登録済みでも reservation がなければ書けず、reservation があっても未登録 session なら書けません。成功後だけ PostToolUse release が arm され、失敗・block・protected root 外では何もしません。

installer は endpoint と transport credential selector を同じ generated `env.sh` で
配布します。既定の native 経路（AgentStack Mail）は
`AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled` を生成し、hook と
`spawn_child.sh` / `cleanup-child-agent.sh` は Authorization header を付けず native
endpoint へ接続します。`AGENTSTACK_MAIL_PROVIDER=upstream` の opt-out だけは
`AGENTSTACK_MAIL_HTTP_BEARER_MODE=auto` 相当で、従来どおり Keychain / `.env` の
legacy HTTP bearer を使います。agent owner token はこれとは別の identity credential で、
child token file と tool argument の既存境界を変えません。

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
- **identity:** `AGENT_NAME` を優先します。無い場合は `TMUX_PANE` で対象 pane の tmux session を明示取得し、pane metadata は一致確認にだけ使います。metadata と session が違う、placeholder、または解決不能なら HTTP を送る前に exit 2 で block します。untargeted な ambient tmux session は使いません。tmux 外の client でも、`register_agent` を呼んで `<runtime>/session_index/` に記録されていれば、hook input の `session_id` から identity を解決します（優先度は env → tmux → session index）。`session_id` は `[a-zA-Z0-9_-]` 以外を含むなら使わず、symlink の index entry は読みません。identity source が1つも無い session の扱いは下記「unmanaged session」に従います。
- **動作:** 既存 reservation を相対 path / absolute path の両方で renew-only 確認します。owner `registration_token` は読み込まず tool arguments に送りません。legacy HTTP bearer は別の transport credential で、generated selector が `disabled` の native endpoint には送りません。0件なら非同期 commit を考慮して1回だけ再確認し、auto-acquire はしません。
- **判定:** 既存 reservation は exit 0、definitive zero、HTTP rejection、JSON-RPC error、MCP `isError=true`または非boolean、schema違反、malformed response、zero後のretry failureは exit 2 です。`isError` は省略または boolean `false` だけを成功として許します。exact identity と protected scope の確定後、**最初の照会**が transport unreachable の場合だけ運用上の fail-open があります。pathなしと protected root外は enforcement 対象外なので exit 0 です。
- **deploy順:** strict identity版を既存sessionへ途中適用しません。cutover C5で全clientを`agent-start`経由でrestart/rebindし、exact identityを確認してからrepo版をliveへ同期し、予約あり/なしの両方向testを通します。

### `check-agent-registered.sh`

- **発火:** `Edit` / `Write` / `Bash` の直前。
- **動作:** `mark-agent-registered.sh` が作る `/tmp/.claude-agent-registered-<session_id>` を検査します。`/clear` などで `session_id` が変わると旧 flag は一致しないため、再登録まで保護対象 tool を block します。
- **例外:** launcher から `AGENT_NAME` を受け取る bot channel は再登録に必要な shell を使えるよう許可します。hook input に session ID がなければ fail-open です。
- **復旧手段の所在:** flag を書くのは `mark-agent-registered.sh` だけで、それは mail MCP の `register_agent` の PostToolUse です。したがって **この block を解除できるのは、その MCP を持つ session だけ**です。`agentstack-reregister` は flag を書かないので、この guard の解除手段ではありません（remote 側の登録を直す道具です）。

### reservation release hook 群

- **同じ座標系:** [`reservation-common.sh`](../hooks/reservation-common.sh) を `check-file-reservation.sh` と release hook が source し、endpoint、legacy bearer selector、identity、project key、protected root、相対/絶対 path を同じ規則で解決します。HTTP request は常に `Accept: application/json, text/event-stream` を付けます。
- **grace / debounce:** `release-file-reservation.sh` は `AGENTSTACK_RELEASE_GRACE_SECONDS`（既定90秒、旧 `FILE_RESERVATION_RELEASE_GRACE_SECONDS` も可）だけ待ってから [`release-file-reservation-worker.py`](../hooks/release-file-reservation-worker.py) が解放します。state は `$AGENTSTACK_RUNTIME_DIR/file_release_debounce/` にあり、次の edit は token を更新し、再予約 hook は state file を消して古い worker を no-op にします。
- **欠損・障害:** worker が配布されていなければ同期の即時 release に fallback します。HTTP 406、接続不能、JSON-RPC / MCP error などの解放失敗は `$AGENTSTACK_RUNTIME_DIR/release-failures.log` に1行記録します。hook 自体は edit 完了や session 終了を失敗扱いにしません。
- **SessionEnd の境界:** `release-all-reservations.sh` は reservation だけを解放します。agent を retire しないため、crash / resume でも irreversible な identity 変更を起こしません。

### service outage と unmanaged session（2つの別々の問い）

guard は編集を止める前に、順に2つを問います。**混ぜると起動経路で開閉することになります**（healthy なサーバーでも raw client を通し、outage でも tmux client を止める、という実測がありました）。

**1. mail service は応答しているか（transport）**

応答していない間は、**どのクライアントでも登録は不可能**です。flag を書けるのは `register_agent` の PostToolUse だけで、その呼び出しは応答しない endpoint には通りません。同時に、誰も予約を取れず・確認もできません。

- 判定は endpoint への **HTTP HEAD 1回**（2秒 deadline）。結果は3値です
  - `reachable`: **何らかの HTTP 応答があった**（401 / 500 / 404 を含む）。「サーバーが no と言った」は「サーバーが無い」ではないので guard は閉じたまま
  - `unreachable`: 接続拒否、または **accept されたが期限内に応答が無い**。後者でも登録はできないので outage 扱いにします
  - `invalid`: endpoint が設定されていない、または URL として不正。**常に block**（authority の宛先の typo が authority を消してはならないため）
- endpoint の解決順は `AGENTSTACK_MCP_URL` → `MCP_URL` → インストール先の `env.sh`。launcher を経由しない client は installer の環境を持たないので、**固定ポートへ fallback しません**（別ポートで動く install を「停止中」と誤判定して guard を開いてしまうため）
- 警告に出す endpoint は scheme / host / port / path だけに切り詰めます（userinfo・query に秘密が入りうるため）
- `AGENTSTACK_MAIL_OUTAGE_POLICY=warn-open`（既定）: 通す。`systemMessage` で可視警告（10分バケットで再掲）し、毎回 JSONL に記録する。**flag も binding も作りません**。復旧後の次の呼び出しは再評価され、未登録セッションは再び block されます
- `AGENTSTACK_MAIL_OUTAGE_POLICY=block`: 拒否する。復旧手順（`agentstack-mailctl start` / `agentstack-doctor`）を示します
- **identity conflict は outage 中でも block** です。障害を identity の曖昧さの逃げ道にしません。両 guard が検査します（Bash は任意のファイルを書けるので、Edit/Write 側だけでは不十分）
- conflict の検査は **identity の優先順位解決とは別**の走査です。優先順位 resolver は `AGENT_NAME` があればそこで返すので、それ経由で聞くと名前つきセッションは何も検査されません。`AGENT_NAME` が binding と食い違う場合も conflict として扱います（どちらかが他方の名前で書くことになるため）
- **identity が解決できたセッションも同じ policy に従います**。予約の renew が transport failure で終わった場合も同じ handler を通ります（名前があるエージェントが、名前の無いセッションより無協調な書き込みを許されることはありません）

**2. このセッションに identity source があるか（identity）**

service が応答しているなら登録は可能なので、既定は**要求する**（block）です。`AGENTSTACK_UNMANAGED_SESSION_POLICY=warn-open` は「サーバーは動いているが、この client は協調に参加しない」という operator の明示的な opt-out です。

| identity / local state | transport | 挙動 |
|---|---|---|
| self binding あり・project 一致 | reachable | 通常の enforcement |
| 未 binding / flag 無し | reachable | block（`register_agent` を案内） |
| binding の有無を問わず | unreachable | `AGENTSTACK_MAIL_OUTAGE_POLICY` に従う |
| identity conflict | 全状態 | block |
| HTTP / auth / MCP / schema の拒否 | 応答あり | block（outage として扱わない） |
| 明示 opt-out | reachable | `AGENTSTACK_UNMANAGED_SESSION_POLICY` に従う（既定 block） |

**identity source が無いこと ≠ mail MCP を持たないこと。** IDE の agent panel から `register_agent` が成功した実績があります。hook の入力に client の MCP inventory を示す field は無いので（実測: PreToolUse の共通 input は9 key のみ）、判定できるのは endpoint が今この瞬間 reachable かどうかだけです。

両 guard は同じ順序で同じ判定を使います。片方だけ開けると、もう片方で同じ行き止まりに落ちるためです。

### `mark-agent-registered.sh`

- **発火:** `register_agent` MCP tool の PostToolUse。`tool_input` と error でない server response の両方が必要です。response の canonical name を取得できない場合に tool input の明示名へ fallback しません。
- **検証:** `name` が明示されていれば response の `name` と完全一致を要求します。別名、error response、入力または応答の解析失敗は `registration-failures.log` へ記録し、exit 2 で caller に返します。名前を省略した登録だけは response の生成名を採用します。
- **動作:** 検証後に `record-session-index.py` を**同期実行してから** registration flag を作ります（flag が先だと、登録済みなのに identity が未記録という窓ができ、予約 guard が誰を照合すべきか分からなくなるため）。現在が `pending-*`、既に同名、または env の `AGENT_NAME` と一致する場合だけ title helper を呼びます。
- **親子保護:** 親が child を preregister した PostToolUse でも、親 pane metadata を child identity に書き換えません。
- **保証境界:** PostToolUse は server call 後なので、拒否した別名 row を transaction rollback はしません。また `check-agent-registered.sh` は既存 `AGENT_NAME` を持つ channel を flag なしでも許可します。この hook の保証は「不一致を黙って受理せず、成功 state を新規作成しない」であり、全 session の後続操作を強制停止することではありません。

## 運用 helper（6件）

以下は `settings.template.json` の event へ直接登録されません。caller と起動条件を明示して運用します。

| 実行ファイル | 呼び出し元 / 起動タイミング | 主な動作 |
| --- | --- | --- |
| [`record-session-index.py`](../hooks/record-session-index.py) | `mark-agent-registered.sh` が PostToolUse payload を渡して**同期**起動 | agent-mail ID と Claude `session_id`、transcript、cwd、`project_key`、`registered_by` の exact mapping を atomic write。他人を登録した呼び出しは記録しない |
| [`resolve-agent-name.sh`](../hooks/resolve-agent-name.sh) | identity が必要な reminder、reservation、cleanup helper が source | env → exact tmux session → session index（caller が `AGENTSTACK_SESSION_ID` を渡した場合）の順で identity を解決 |
| [`spawn_child.sh`](../hooks/spawn_child.sh) | `/delegate` または dashboard の NEW AGENT が child 起動時に明示実行 | identity、token、task mail、reservation、tmux、Claude / Codex、worktree、readiness を一つの launch transaction にまとめる |
| [`cleanup-child-agent.sh`](../hooks/cleanup-child-agent.sh) | `spawn_child.sh` が起動した child の REPL command が終了した直後 | reservation release、remote identity retire、managed list / state / credential / MCP config の削除を best-effort 実行 |
| [`monitor_child_agent.sh`](../hooks/monitor_child_agent.sh) | `/delegate` の親が監視頻度ごとに一回ずつ実行 | tmux pane を採取し、完了、session 消失、permission prompt、stasis、任意の danger pattern を判定して exit code で返す |
| [`watch_agent_mail_signals.sh`](../hooks/watch_agent_mail_signals.sh) | launcher の登録処理が dedicated `mail-watcher` tmux service として起動 | agent-mail signal を監視し、対象と完全一致する agent tmux session へ通知文と `C-m` を注入 |

### `record-session-index.py`

PostToolUse payload から agent-mail の数値 ID、canonical name、Claude `session_id`、transcript path、cwd を取り出し、`$AGENTSTACK_RUNTIME_DIR/session_index/<agent_id>.json` へ一時 file + `os.replace` で書きます。record は `schema_version: 2` と `binding_kind: "self"` を持ちます。**呼び出し元が別の agent を登録した場合（親による child 登録）は record を書きません** — この index は dashboard の resume と guard の identity 解決の両方に読まれるので、読む側で除外するのではなく、書かない方が誤用の余地が残りません。dashboard はこの exact mapping を session resume に優先し、古い session だけ heuristic へ fallback します。入力不備や I/O failure は registration を妨げない quiet no-op です。

### `resolve-agent-name.sh`

source 専用 helper で、`RESOLVED_AGENT` と解決 source を caller へ返します。優先順位は `AGENT_NAME`、次に`TMUX_PANE`で明示した pane の exact tmux session、最後に session index です。session index を使うのは caller が `AGENTSTACK_SESSION_ID` を渡した場合だけで、`schema_version: 2` かつ `binding_kind: "self"` の record に限り、`AGENTSTACK_LOOKUP_PROJECT_KEY` が与えられていればその project と exact match した record だけを採用します。同一 session に複数の identity が結び付いていれば時刻で選ばず `identity-conflict` を返します。pane metadata は権威ではなく一致確認にだけ使い、不一致なら`identity-conflict`を返します。`pending-*`、`warm-*`、`claimed-*`、`mail-watcher`はidentityと見なしません。pane指定なしでambient tmux sessionを照会せず、解決不能時は空文字を返してcallerが境界を適用します。

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
