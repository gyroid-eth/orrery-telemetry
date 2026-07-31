# トラブルシューティング

> English version: planned.

[前: 設定](configuration.md) · [README に戻る](../README.md) · [次: 第三者コンポーネント](third-party.md)

最初に:

```bash
~/.agentstack/bin/agentstack-doctor
git -C /path/to/claude-agent-stack status --short
```

を実行し、install root、service、tmux、settings merge、project key、mail watcher を確認します。

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

DECK の tmux state は設定なしでも見えます。mail edge、history / replay、spawn、vault Output だけが使えない状態は意図された縮退動作です。

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

dashboard は launcher 起動後3秒で tmux session を probe します。launchd の最小 PATH では `~/.local/bin` が欠けやすいため、spawn path はこれを先頭へ補います。

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
- [Dashboard](dashboard.md)
- [設定](configuration.md)
