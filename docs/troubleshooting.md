# トラブルシューティング

> English version: planned.

[前: 設定](configuration.md) · [README に戻る](../README.md) · [次: 第三者コンポーネント](third-party.md)

最初に:

```bash
~/.agentstack/bin/agentstack-doctor
git -C /path/to/claude-agent-stack status --short
```

core doctor は install footprint、必須 command、managed block、managed agent 名、tmux mouse、tmux global identity env を検査します。repository 側は `git status` で変更を確認します。

doctor は dashboard service state と mail-watcher health を検査しません。service は後述の launchd / systemd command、watcher は `/api/mail-watcher-health` で別に確認してください。

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

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl enable "gui/$(id -u)/$label"
tail -f ~/.agentstack/dashboard/dashboard.log
```

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

優先する復旧:

```bash
AGENTSTACK_PROJECT_KEY=/absolute/project/path \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

成功後に自分の `fetch_inbox` を実行します。`pending-*` tmux session のままなら registration read-back と rename が完了していません。server が返した canonical name を使い、既存同名 tmux session を自動で kill しないでください。

## Hook が `FILE RESERVATION REQUIRED` で block する

protected root 内の Edit / Write では、hook が現在の agent の既存 reservation を renew し、なければ exact relative path の auto-acquire を試します。それでも block する場合:

1. `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY` が reservation を作った project と一致するか確認
2. tmux session 名、`AGENT_NAME`、pane metadata が同じ canonical identity を指すか確認
3. exact path または最小の glob を `file_reservation_paths` で予約
4. conflict が返ったら holder へ agent-mail で連絡し、release または expiry を待つ
5. token-strict server で renew が0なら `agentstack-reregister` で owner token state を復旧

server 到達不能または protected root 未設定では hook は fail-open します。block を消すために guard を無効化せず、project / identity / reservation の不一致を直してください。

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
