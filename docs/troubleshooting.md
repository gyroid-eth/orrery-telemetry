# トラブルシューティング

> English version: planned.

[前: 設定](configuration.md) · [README に戻る](../README.md) · [次: 第三者コンポーネント](third-party.md)

最初に:

```bash
~/.agentstack/bin/agentstack-doctor
git -C /path/to/orrery-telemetry status --short
```

core doctor は install footprint、必須 command、managed block、managed agent 名、tmux mouse、tmux global identity env、dashboard endpoint と service manager の状態を検査します。repository 側は `git status` で変更を確認します。

dashboard は `/api/version` の正しい JSON response を「実際に配信中」の正本として判定し、launchd / systemd の登録・実行状態とは別に報告します。endpoint が応答していて manager が実行していなければ `unmanaged-background` です。mail-watcher health は `/api/mail-watcher-health` で別に確認してください。

## バグを報告するとき

```bash
~/.agentstack/bin/agentstack-doctor --report
```

`--- copy from here ---` から `--- copy to here ---` までをそのまま貼ってください。

これまでに見つかった不具合は**すべて「報告者の環境と開発機の違い」**から出ており、その差がどこにあるかを突き止めるまでに毎回何往復もかかりました。この出力は、その往復で実際に聞いた項目だけを並べたものです。

| 項目 | これが分かれば判ること |
|---|---|
| agent-mail の commit・origin より何コミット先か | 動いているコードが本当はどれか |
| `AGENT_NAME_ENFORCEMENT_MODE` | 要求した名前がそのまま通るかどうか |
| passthrough patch の有無 | 上のモードがそもそも受け付けられる版かどうか |
| requested-name handling | #140 / passthrough / 旧処理を合わせた最終判定。`unknown` は未判定 |
| `agents.retired_at` カラムの有無 | dashboard のクエリが成立するかどうか |
| open file limit | descriptor を使い切って落ちる側かどうか |
| tmux / python3 / uv / claude / codex の有無と版 | 前提コマンドが揃っているか |

**token や Authorization ヘッダは含みません**（そのままチャットに貼れるように、意図的に値を出さない作りにしてあり、テストで固定しています）。「何をして」「何を期待して」「何が起きたか」を末尾の欄に書き足してください。エラー文はそのまま貼ってもらうのが最も速いです。

## `NOT CONFIGURED`

原因は dashboard service に `AGENTSTACK_PROJECT_KEY` または `AGENTSTACK_VAULT` がないことです。

確認:

1. `~/.agentstack/env.sh`
2. launchd plist / systemd unit の environment
3. service の再起動後に `/api/graph`

修復:

```bash
export AGENTSTACK_PROJECT_KEY=/absolute/project/path
./scripts/install.sh
```

DECK の tmux state は設定なしでも見えます。mail edge、history / replay、spawn だけが使えない状態は意図された縮退動作です。Output は cwd / git root の `logs/` fallback を引き続き探索します。

## Output が空、または link にならない

Output の file は `LOG_*.md` で、frontmatter の `agent:` が dashboard の canonical agent 名と一致する必要があります。

1. `AGENTSTACK_DELIVERABLE_ROOTS` を設定した場合は `:` 区切りの各 directory が service process から読めるか確認
2. 未設定なら `AGENTSTACK_PROJECT_KEY/logs/`、次に vault、cwd / git root の fallback を確認
3. `env.sh` だけを変えた場合は installer を再実行し、launchd / systemd environment に反映
4. item が `AGENTSTACK_VAULT` の外なら非リンク表示が正常です。vault 内 item だけが `obsidian://` link になります

## launchd が起動しない

```bash
label=org.agentstack.agentdashboard
plist="$HOME/Library/LaunchAgents/$label.plist"
target="gui/$(id -u)/$label"

launchctl enable "$target"
launchctl bootout "$target" 2>/dev/null || true
while launchctl print "$target" >/dev/null 2>&1; do sleep 0.1; done
launchctl bootstrap "gui/$(id -u)" "$plist"
tail -f ~/.agentstack/dashboard/dashboard.log
```

`enable` は `bootstrap` より先に行います。無効化された label を先に bootstrap すると
macOS が `Input/output error` を返す場合があります。また `bootout` は非同期なので、
`launchctl print` で job が消えるまで待ってから再登録します。installer と
`agentctl.sh start` はこの順序を自動で行い、GUI domain 自体を使えない場合の
supervised-background fallback は従来どおりです。

よくある原因:

- port `8770` を別 process が使用
- plist の Python / PATH が古い
- `~/.agentstack` を移動した
- install 後に `env.sh` だけを変更
- dashboard file の copy が不完全

port を変える場合:

```bash
AGENTSTACK_PORT=8771 ./scripts/install.sh --port 8771
```

installer は使用中 port を検出すると service 登録前に停止します。

## Linux / WSL で service がない

**Linux と WSL は未検証です。** systemd user 経路と supervised background への fallback は実装されていますが、開発者の環境は macOS のみで、実 Linux ホストでの登録・timer 起動は通していません。CI（ubuntu-latest）は `systemctl` をスタブにして unit の生成と呼び出し順だけを検査しています。以下は設計上の期待であり、うまくいかなければ issue で環境と出力を報告してください。

systemd user session が使える場合:

```bash
systemctl --user status agentstack-dashboard.service
systemctl --user daemon-reload
```

systemd user が使えない環境と WSL では installer が `nohup` と pidfile に fallback します。Ghostty click-to-jump は使えませんが、localhost dashboard と browser terminal は利用できます。

## macOS で `EPERM`

Desktop / Documents / Downloads 配下なら TCC を疑います。

1. Full Disk Access 済み terminal から root agent を起動
2. 既存 tmux server / session を終了
3. 同じ terminal から再作成
4. または project を保護対象外へ移動

`chmod` で直らない場合があるのは、file mode ではなく起動元 app identity が判定されるためです。詳しくは[インストール](install.md#macos-の-tcc--full-disk-access)を参照してください。

## Codex に通知 text は入るが送信されない

text と submit を一回の `send-keys` に混ぜないでください。

```bash
tmux send-keys -t "$session" -l "$text"
sleep 0.2
tmux send-keys -t "$session" C-m
```

Codex REPL では `Enter` keysym が submit にならない場合があるため `C-m` を使います。

追加確認:

- target pane が Codex / Claude REPL か、bare shell ではないか
- `AGENTSTACK_SIGNALS_DIR` に backlog がないか
- `/api/mail-watcher-health` の `watcher_running`
- `last_success_age_s` と `recent_results`

## Mail watcher が yellow / red

```bash
curl -s http://127.0.0.1:8770/api/mail-watcher-health
```

- watcher process がない
- signal が残り、直近成功が古い
- agent-mail endpoint / bearer token が不正
- target tmux session がない

`AGENTSTACK_MAIL_HOME` と `AGENTSTACK_SIGNALS_DIR` が service と launcher で一致しているか確認します。

## Dashboard spawn がすぐ消える

1. `dashboard/logs/spawn.log` の末尾を見る
2. `tmux has-session -t '<child-name>'` を確認
3. `~/.local/bin/claude`、`codex`、指定 CLI が service の `PATH` から見えるか確認
4. `AGENTSTACK_SPAWN_SCRIPT` と working directory を確認
5. `AGENTSTACK_PROJECT_KEY` / `AGENTSTACK_VAULT` を確認
6. `AGENTSTACK_MAIL_ENV` に `HTTP_BEARER_TOKEN` があるか確認
7. `agentstack-reregister '<child-name>'` で token state を確認

dashboard は launcher 自身の readiness / early-death verdict を最大120秒待ち、その launcher が成功した後に exact tmux session を probe します。launchd の最小 PATH では `~/.local/bin` が欠けやすいため、spawn path はこれを先頭へ補います。

Codex の場合は `AGENTSTACK_CODEX_MODELS` と request model、effort allow-list も確認してください。

## Spawn 名が拒否される

指定名は hyphen を除去した後、ASCII letter で始まる2〜64文字の alphabetic 名である必要があります。

- `occupied`: 既存 identity
- `unknown`: DB / auth / transport failure
- `available`: 使用可

`unknown` は使用できません。別名で回避する前に agent-mail と project key を直してください。identity continuity を守るためです。

## 要求した名前と違う名前で登録される

agent-mail が名前を受け入れず、生成名に置き換えた状態です。エージェントは動き続けるので気づきにくく、他のエージェントから宛先として呼べないことで初めて分かります。

```bash
~/.agentstack/bin/agentstack-doctor --report \
  | grep -E 'passthrough patch|requested-name handling'
```

`requested-name handling: replaced` なら、要求名を受け付けない既知の旧処理です。installer も事前に「要求した名前が生成名に置き換わる」と警告します。`unknown` は source が読めないか命名処理が既知の形でないため、対応しているとも対応していないとも推測していません。`passthrough patch: absent` でも #140 の `validate_explicit_agent_id` があれば `honored` になるため、patch 行だけで判断しないでください。

install 時の判定と根拠は `install-state.json` の `agent_mail.requested_name_honoring` に残ります。同梱 ORRERY Mail は要求名を受け付ける設定で起動します。旧 service がまだ応答している場合は `--retire-legacy-mail` で停止してから installer を再実行し、同梱 service へ切り替えてください。

旧 service が `sqlite+aiosqlite:///./storage.sqlite3` のような相対 DB URL を返す機体で
`unsupported database URL` が出る場合も、既知の legacy launchd label がロード中なら
`./scripts/install.sh --retire-legacy-mail ...` を使います。installer は label と plist が
実際に mail service を指すことを確認してから、listener の再利用判定より先に退役します。
フラグなしのエラーには検出 label が表示されます。label が表示されない場合は別の
listener なので、DB path を推測せず `agentstack-doctor --report` で所有者を確認してください。

### 別名で登録されたときに何が起きるか

エージェント自体は正常に動きます。壊れるのは**呼び名だけ**です。

| | |
|---|---|
| 動くもの | 送信・受信・ファイル予約・tmux セッション・dashboard 上の表示 |
| 壊れるもの | **親や他のエージェントが宛先に指定する名前**。要求名で `send_message` すると届きません |
| 肖像 | **出ません**。fallback 名（`GreenLake` 等）は科学者名で終わらないため、dashboard は顔を引けません |

肖像が出ないのは不具合ではなく、**この状態を見分けるための唯一の視覚的な手がかり**です。だからハッシュ等で機械的に顔を割り当てることはしていません。顔を配ると、失敗した登録が成功したものと見分けられなくなります。

そのうえで、顔の不在から察してもらうのは弱すぎるので、**要求名そのものを画面に出します**。

- **DECK**: 名前の下に `↯ asked for <要求した名前>`
- **ネットワーク図**: そのエージェントの名前ラベルが amber になり、hover で `requested <要求名>, registered as <実際の名前>`

記録は `$AGENTSTACK_RUNTIME_DIR/name-substitutions.json` にあり、登録が置き換わった場合のみ書かれます。

復旧するには、名前を受け付けるサーバーにしたうえで**エージェントを起動し直します**。既に登録された identity の名前は後から変えられません。

## Registration / inbox の認証に失敗する

別名を作らず:

```bash
AGENTSTACK_PROJECT_KEY=/absolute/project/path \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

を実行します。

確認対象:

```text
$AGENTSTACK_RUNTIME_DIR/agent_token_<name>
$AGENTSTACK_RUNTIME_DIR/child-agents/<name>.json
```

token が missing / stale / wrong-owner なら親または operator へ報告してください。token を chat、log、process argument に貼らないでください。

## Hook が `AGENT NOT REGISTERED` で block する

Claude Code の `check-agent-registered.sh` は、現在の `session_id` で `register_agent` の成功が記録されるまで Edit / Write / Bash を block します。`/clear`、resume、compact 後は SessionStart hook の reminder を読み、既存 identity があるなら別名を作らず再登録します。

**まず自分がどちらの状況か切り分けます。**

`AGENT_NAME` があっても素通りするとは限りません。この guard は **identity conflict → endpoint 不正 → service 停止（outage policy）→ `AGENT_NAME` 免除 → flag** の順に見ます。`AGENT_NAME` が免除するのは **flag の要求だけ**です。`AGENT_NAME` が無いなら、下の復旧コマンドではなく次節を読んでください。

優先する復旧（**mail MCP を持つ session に限る**）:

```bash
AGENTSTACK_PROJECT_KEY=/absolute/project/path \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

このコマンドは remote の登録を修復しますが、**flag file は書きません**。flag を書けるのは `register_agent` MCP tool の PostToolUse (`mark-agent-registered.sh`) だけです。したがって block の解除には、その MCP tool を session の中から呼ぶ必要があります。

成功後に自分の `fetch_inbox` を実行します。`pending-*` tmux session のままなら registration read-back と rename が完了していません。server が返した canonical name を使い、既存同名 tmux session を自動で kill しないでください。

## 最初の Edit/Write/Bash が block される（launcher を経由しない client / service 停止中）

まず **mail service が応答しているか**を確認します。ここで分岐が変わります。

```bash
# endpoint はインストールごとに違います。install が使っている値を見てから叩きます。
grep AGENTSTACK_MCP_URL ~/.agentstack/env.sh
~/.agentstack/bin/agentstack-doctor --report
```

`agentstack-mailctl` は環境によっては入っていません（このリポジトリの開発機にも無い時期がありました）。**存在するものだけを使ってください。**

### service が応答していない場合

**登録は原理的に不可能です。** flag を書けるのは `register_agent` の PostToolUse だけで、その呼び出しは応答しない endpoint には通りません。tmux の中でも同じです。`agentstack-reregister` も flag を書かないので復旧になりません。

既定（`AGENTSTACK_MAIL_OUTAGE_POLICY=warn-open`）では、この状態の編集は**警告つきで通ります**。予約は取れないので、同じ project を共有する他 agent との衝突は検出されません。記録は `~/.agentstack/runtime/logs/unmanaged_sessions.jsonl` に残ります。

復旧は service 側です。

```bash
# 実在するものだけを絶対パスで叩きます（hook の案内も同じ基準です）
[ -x ~/.agentstack/bin/agentstack-mailctl ] && ~/.agentstack/bin/agentstack-mailctl start
[ -x ~/.agentstack/bin/agentstack-doctor ] && ~/.agentstack/bin/agentstack-doctor --report
```

`AGENTSTACK_MAIL_OUTAGE_POLICY=block` を設定している場合、この状態では編集自体が拒否されます（協調できないなら書かせない、という選択）。

### service が応答している場合

登録は可能なので、guard は登録を要求します。client に mail MCP があるなら `register_agent` を呼べば解除されます（IDE の agent panel からの登録実績があります）。

MCP を持たない client を協調の外で使うと決めているなら、その client の環境に明示的に設定します。

```bash
export AGENTSTACK_UNMANAGED_SESSION_POLICY=warn-open
```

**既定は block です。** 設定しない限り、identity を持たないセッションは登録を求められます。

## installer を再実行したら、外したはずの hook が戻ってきた

戻りません（2026-08-22 以降）。installer は自分が追加した entry を `<runtime>/settings-installed-entries.json` に記録し、**次回その entry が settings から消えていれば「意図的に外した」と読んで再追加しません**。結果は merge の出力の `respected_removals` に出ます。

意図的に戻したい場合だけ、明示します。

```bash
agentstack-merge-settings ... --restore-removed
```

記録が無い状態（初回インストール、記録ファイルを消した場合）では、従来どおり template の entry がすべて入ります。「消えている」ことが「外した」ことの証拠になるのは、**こちらが一度入れた記録がある場合だけ**です。

## Hook が `FILE RESERVATION REQUIRED` で block する

protected root 内の Edit / Write では、hook が exact identity を確定してから既存 reservation を相対/絶対 path の両方で renew-only 確認します。auto-acquire はしません。block する場合:

1. `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY` が reservation を作った project と一致するか確認
2. `AGENT_NAME`、または`TMUX_PANE`で明示したtmux sessionがcanonical identityを指すか確認。pane metadataとの不一致は`AGENT IDENTITY CONFLICT`として先に直す。tmux 外の client は `register_agent` 済みなら session index から identity が解決される。同一 session に複数の identity が結び付いている場合も `AGENT IDENTITY CONFLICT` で、どちらを残すかを決めてから再登録する
3. exact path または最小の glob を `file_reservation_paths` で予約
4. conflict が返ったら holder へ agent-mail で連絡し、release または expiry を待つ

owner `registration_token` はこのhookのtool argumentsへ送られず、legacy HTTP bearerとは別物です。`isError`は省略またはboolean `false`だけを成功とします。exact identityとprotected scopeの確定後、最初の照会がtransport unreachableの場合だけfail-openです。HTTP/MCP/schema rejection、malformed response、definitive zero後のtransport failureはblockします。pathなし・protected root外はenforcement対象外なのでexit 0です。

strict版はcutover C5の全client restart/rebind後にdeployします。raw non-tmux Claude は、`register_agent` を呼んで session index に self binding ができていれば identity が解決されます。登録できない client（mail MCP を持たない起動経路）と identity source のない旧 session は `AGENTSTACK_UNMANAGED_SESSION_POLICY` の扱いになり、協調が必要なら `agent-start` 経由で再起動してください。guardを無効化したり、untargeted tmux sessionやstale metadataをidentityとして採用したりしないでください。

## Spawned child が自分の inbox を読めない

core doctor を実行します。

```bash
~/.agentstack/bin/agentstack-doctor
```

`child MCP proxy missing` または source tree 不足の warning が出る場合、`./scripts/install.sh` を再実行します。proxy がある child は owner token を model context に読み込まず、child-scoped stdio connection が代理で認証します。shared endpoint へ fallback した状態と proxy 経由を混在させないでください。

## Codex App Bridge / cold wake が動かない

Codex Desktop 統合には core doctor とは別の doctor、runtime state、失敗分類があります。[Codex App 統合の「よくある失敗」](codex-app.md#よくある失敗)を参照してください。Codex CLI session が Bridge に現れないのは意図された surface filter です。

## Dashboard に agent が二重表示される

tmux session 名と agent-mail identity が一致しているか確認します。

```bash
tmux list-sessions
printf '%s\n' "$AGENT_NAME"
```

stale な top-level environment を継承した可能性がある場合は、新しい terminal から `agent-start` / `agent-start-codex` で起動し直します。launcher は `AGENT_NAME`、`PARENT_AGENT`、token、reserved marker を削除してから登録します。

## History が見つからない

`/api/history` は agent program に応じて Claude / Codex transcript を探し、見つからなければ他方へ fallback します。

- agent-mail の program が正しいか
- transcript が disk に残っているか
- session / agent 名が一致しているか
- child と parent の transcript を取り違えていないか

transcript が存在しない agent は mail timeline だけが見えることがあります。

## Terminal が開かない

- `AGENTSTACK_TERMINAL` の値を確認
- Ghostty / iTerm2 / Terminal.app の install path を確認
- `tmux has-session -t '<name>'`
- browser terminal なら `ttyd` が PATH にあるか
- `/api/ptty?session=<name>` の error

`AGENTSTACK_TERMINAL=none` では OS terminal open を行いません。

## tmux の scrollback が使えない

`~/.tmux.conf`:

```tmux
set -g mouse on
set -g history-limit 50000
```

または `Ctrl+b [` で copy mode に入ります。`agentstack-doctor` も mouse mode を確認します。

## Uninstall が止まる

`install-state.json` が必要です。

```bash
ls -l ~/.agentstack/install-state.json
~/.agentstack/bin/agentstack-uninstall --dry-run
```

manifest がない状態で推測削除は行いません。settings や mail data を巻き込まないためです。

## 関連文書

- [インストール](install.md)
- [Launcher と identity](launchers.md)
- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [Dashboard](dashboard.md)
- [設定](configuration.md)
