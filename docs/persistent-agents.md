# 常駐 agent の enrollment と起動

> English version: [persistent-agents.en.md](persistent-agents.en.md)

[Launcher と identity](launchers.md) · [トラブルシューティング](troubleshooting.md) · [README に戻る](../README.md)

常駐 agent は、再起動をまたいで同じ ORRERY Mail identity を使う parentless bot です。Claude Channels のように人が tmux の REPL へも入力する `interactive` と、専用 bridge が通知を bot へ渡す `headless` を別の軸として扱います。

通常の `agent-start` / `agent-start-codex` や child launcher を通る一時的な agent は、従来の登録経路を使います。この文書の enrollment は、launcher を一度も通らなかった既存 bot、または local credential を失った同じ数値 row を移行するための operator 手順です。

## 保証範囲

ここでいう **operator** は、この機体の local terminal で専用 CLI を明示的に実行する人です。実装が保証するのは次の範囲です。

- owner 限定の local Unix socket と同一 OS user の peer UID
- 数値 agent ID、project、期待 name、Mail instance UUID、credential generation の照合
- 同じ row の credential だけを compare-and-swap で更新
- 固定 field だけを持つ local audit record
- secret を出力、log、argv、MCP/proxy tool に載せないこと

これは人間の本人証明ではありません。同じ OS user で shell を使える process は CLI を実行できます。TTY の有無も人間を証明しません。「人が対象と操作を確認して手で実行する」は運用規約であり、新しい login 認証を追加したものではありません。モデルへ enrollment の実行を依頼しないでください。

`agentstack-enroll` は MCP / proxy の catalog と dispatch に存在しません。Mail process の local management socket だけを使います。`claim` / `recover` は alias や新しい row を作らず、retirement や profile を変更しません。

## 二つの JSON profile

installer は local Mail 用の connection profile を作ります。

```text
~/.agentstack/connections/local.json
```

これは transport（management socket、loopback MCP URL、Mail env）と、初回確認後の `expected_server_instance_id` を保持します。host 名、port、socket path だけでは Mail の identity になりません。file は owner-only とし、group / world permission を付けないでください。

bot ごとの persistent profile は operator が次の directory に mode `0600` で置きます。

```text
~/.agentstack/profiles/<name>.json
```

profile 自体に token を書きません。credential は connection profile が指す runtime directory の owner-private file に保存されます。

## 新しい bot を作る場合

`claim` は row 作成 API ではありません。新しい bot は先に、通常の正規登録経路で project 内の identity を作成してください。たとえば top-level launcher、dashboard の NEW AGENT、またはその用途に対応した preregistration 経路です。その正式な応答から canonical name と正の数値 agent ID を記録します。

存在しない ID、別 project の ID、retired row を `claim` で代用してはいけません。name 検索から別名 row を作ることもありません。

正規作成後の row が `server-null` なら下の `claim`、既に正常な owner credential があれば credential mutation は省略します。ただし新しい bot でも connection の Mail UUID 確認 / pin、`inspect`、persistent profile の検証は省略しません。

## 既存 bot を移行する手順

以下の例では値を自分の機体のものへ置き換えます。数値 ID は正規作成時の `register_agent` 応答にある `id` を記録します。それを失った既存 row は、同じ project と exact name を指定した、認証済み ORRERY Mail `whois` 応答の `id` を operator が確認します。name や dashboard の表示順から推測しないでください。

```bash
export BOT_ID=123
export BOT_NAME=ChannelsBot
export PROJECT_KEY=/absolute/project/path
export CONNECTION="$HOME/.agentstack/connections/local.json"
```

### 1. secret なしで対象を inspect する

```bash
"$HOME/.agentstack/bin/agentstack-enroll" inspect \
  --agent-id "$BOT_ID" \
  --project-key "$PROJECT_KEY" \
  --connection "$CONNECTION" \
  --name "$BOT_NAME"
```

出力で少なくとも次を確認します。

- `server_instance_id`: 意図した local Mail の永続 UUID
- `project_key` / `agent_id` / `name`: 起動したい同じ row
- `retired: false`
- `credential_state`: `server-null` または `server-token`
- `local_credential_state`: local 保存状態
- `connection_pinned`: connection profile が UUID に固定済みか

`--name` は期待値の照合だけに使われます。name から row を検索したり、別名を作ったりしません。

### 2. connection を初回だけ Mail UUID へ pin する

`connection_pinned` が `false` のときは、最初の inspect で表示された UUID と対象 Mail を operator が確認し、その同じ値を明示して再度 inspect します。

```bash
"$HOME/.agentstack/bin/agentstack-enroll" inspect \
  --agent-id "$BOT_ID" \
  --project-key "$PROJECT_KEY" \
  --connection "$CONNECTION" \
  --name "$BOT_NAME" \
  --pin-server '<先ほど確認した server_instance_id>'
```

`claim` / `recover` は pin のない connection を拒否します。UUID が既存 pin と違う場合も上書きしません。

### 3. 状態に合う操作を一度だけ選ぶ

| inspect の状態 | 操作 |
| --- | --- |
| `server-null` + local `missing` | `claim` |
| `server-token` + local `missing` または `missing-token-same-identity` | `recover` |
| `server-token` + `present` / `present-legacy-verified` | 変更不要。profile の inspect へ進む |
| `identity-conflict`、別 UUID / ID / name、retired | 停止して対象を確認する。自動修正しない |

初回 claim:

```bash
"$HOME/.agentstack/bin/agentstack-enroll" claim \
  --agent-id "$BOT_ID" \
  --project-key "$PROJECT_KEY" \
  --connection "$CONNECTION" \
  --name "$BOT_NAME"
```

local credential を失った同じ row の recovery:

```bash
"$HOME/.agentstack/bin/agentstack-enroll" recover \
  --agent-id "$BOT_ID" \
  --project-key "$PROJECT_KEY" \
  --connection "$CONNECTION" \
  --name "$BOT_NAME"
```

`claim` は server credential が null の row にだけ、`recover` は server token があり local token がない同じ identity にだけ使えます。どちらも現在 generation を期待値にした CAS であり、競合時は credential を変更しません。

CLI は新しい secret を先に一意な mode `0600` pending file へ保存し、同じ request ID と secret で server へ要求します。通信切断時は**同じコマンドをそのまま再実行**してください。保存済み request の status を確認し、別 token を生成しません。pending file や active token を開いたり、削除したり、chat / log へ貼ったりしないでください。

成功時の stdout は secret ではなく `orrery-enrollment-receipt-v1` です。次を確認します。

- `result: accepted`
- `operation: claim` または `recover`
- 期待した `server_instance_id` / `project_key` / `agent_id`
- `new_generation` が `old_generation` より1つ進んだ
- `reason: operator-claim` または `operator-recover`

Mail 側は credential 更新と同じ transaction で、時刻、request ID、peer UID、authority、row、操作、旧新 generation / fingerprint、固定 reason / result を audit します。拒否も固定 reason / result だけを audit し、credential は変更しません。token、request 本文、自由文 error は記録しません。

### 4. persistent profile を作る

interactive Claude Channels の例です。

```json
{
  "kind": "orrery-persistent-agent-v1",
  "name": "ChannelsBot",
  "agent_id": 123,
  "project_key": "/absolute/project/path",
  "connection": "../connections/local.json",
  "provider": "claude",
  "parentless": true,
  "lifecycle": "persistent",
  "interaction": "interactive",
  "state_dir": "../persistent/ChannelsBot",
  "working_directory": "/absolute/project/path",
  "command": [
    "/absolute/path/to/claude",
    "--channels",
    "plugin:telegram@claude-plugins-official"
  ],
  "environment": {
    "TELEGRAM_STATE_DIR": "/absolute/path/to/channel-state"
  }
}
```

```bash
chmod 700 "$HOME/.agentstack/profiles"
chmod 600 "$HOME/.agentstack/profiles/ChannelsBot.json"
```

軸の意味は独立しています。

| field | 値 | 意味 |
| --- | --- | --- |
| `provider` | `claude` / `codex` | 実行 provider と proxy 設定 |
| `parentless` | `true` | parent を持たない top-level identity |
| `lifecycle` | `persistent` | restart 後も同じ row を使う |
| `interaction` | `interactive` / `headless` | 人が REPL に入力できるか、bridge が介在するか |

`standalone` は dashboard / launcher で「親なし」を表す既存概念のままです。常駐、親なし、対話可否を一つの enum にまとめません。

`environment` に token、password、credential、API key を置けません。Mail identity、parent、proxy credential の内部変数も profile から上書きできません。service manager の PATH は短いことがあるため、常駐起動では command の絶対 path を推奨します。

### 5. 起動前確認と起動

```bash
"$HOME/.agentstack/bin/agentstack-persistent" inspect \
  --profile "$HOME/.agentstack/profiles/ChannelsBot.json"

exec "$HOME/.agentstack/bin/agentstack-persistent" run \
  --profile "$HOME/.agentstack/profiles/ChannelsBot.json"
```

`inspect` は profile、connection、Mail UUID、数値 row、name、credential generation / fingerprint を照合します。`run` も同じ検証を繰り返し、profile ごとの instance lock を取ってから起動します。同じ profile の二重起動は拒否されます。

launcher は enrollment を自動実行しません。未登録、local credential 不在、authority / row 不一致なら docs path と固定 reason を表示して停止します。

interactive REPL が起動したら、operator は次を確認します。

1. `/mcp` で、wrapper が生成した各 Mail alias（既存 alias がなければ通常 `orrery-mail`）と期待する channel plugin が connected であること。既存 alias が複数なら、複数の bound 接続が正常です。
2. 実際に使う bound namespace の `runtime_status` を呼び、返る name と project が profile と一致すること。これは local proxy binding の確認であり、Mail server の認証 / 到達性や Telegram の受信 / 返信を証明しません。
3. `/mcp` の表示名だけでは raw / proxy の区別や二重起動抑止を証明できません。詳細に command が表示される場合は、secret 値を開かず、生成された bound proxy runner を指すことを確認します。

## interactive と headless

### interactive

Claude の `interactive` profile は command が本物の `claude --channels ...` でなければなりません。準備後に wrapper 自身を `exec` で置き換えるため、tmux の PTY、stdin、signal、process surface がそのまま REPL に渡ります。Claude を wrapper の子に隠す headless 化はしません。

通常の channel selector（例: `plugin:telegram@claude-plugins-official`）は profile に書いたまま保持されます。wrapper は development channel bypass を追加しません。Channels plugin を消してしまうため `--strict-mcp-config` も追加せず、owner-private な `--mcp-config` overlay だけを追加します。

strict を外しても raw Mail を再露出しないため、wrapper は Claude が読む次の有限な設定源を、profile の `environment` を適用した後に検査します。

| source | 検査と動作 |
| --- | --- |
| user | 実効 `CLAUDE_CONFIG_DIR/.claude.json`、未指定時は実効 `HOME/.claude.json` の top-level `mcpServers` |
| local project | 同じ user JSON の `projects[project root].mcpServers`。Git 内では `git rev-parse --show-toplevel`、Git 外では `working_directory` の exact key を使う |
| project | profile の `working_directory` から filesystem root までの各 `.mcp.json`。Mail alias は同名 bound overlay へ入れ、無関係な定義は変更しない |
| enabled plugin | 実効 user `settings.json`、同 working directory の `.claude/settings.json` / `settings.local.json`、Git repository の実効 root local `settings.local.json` の `enabledPlugins` を順に適用する。通常 checkout と separate gitdir は現在の checkout root、linked worktree は main checkout の root local を最後に適用する。型が正しい root、`.git`、`.claude`、解決した git metadata の owner が異なる場合だけ、Claude と同じく working directory の legacy local へ戻る。symlink / 型不明 / 検査不能は source を見落とさないよう拒否する。user config の `settings.local.json`、shared / local の中間祖先は Claude が読まないため source に含めない。installed plugin は実効 plugins root の inventory、marketplace registry、catalog と exact ID を照合して検査する |
| selected plugin | `--channels plugin:...` と、caller が明示した `--dangerously-load-development-channels plugin:...` で選んだ installed plugin を enabled 状態とは別に検査。wrapper 自身は development selector を追加しない |
| explicit plugin | profile の各 `--plugin-dir` を検査 |

実効 plugins root は、profile environment に絶対 `CLAUDE_CODE_PLUGIN_CACHE_DIR` があればその directory、なければ実効 config root の `plugins/` です。plugins root と解決した `installPath` / `installLocation` は local user 所有かつ group / world 書込不可でなければ停止します。wrapper は `installed_plugins.json` の exact `name@marketplace` と `installPath`、`known_marketplaces.json` の exact marketplace と絶対 `installLocation`、その location の catalog にある exact plugin entry を順に照合し、cache directory 名から identity を推定しません。marketplace の `mcpServers` 参照も catalog directory ではなく inventory の `installPath` を基準に解決します。

marketplace entry が `strict: false` なら manifest が無い plugin を許容し、entry の MCP 定義を検査します。現行 CLI の source precedence が変わっても Mail を見落とさない安全側の superset として、存在する root `.mcp.json` も検査します。manifest が component field を宣言していれば conflict として停止します。`strict: true` または省略なら manifest を必須とし、manifest、存在する root `.mcp.json`、marketplace entry をすべて検査します（root `.mcp.json` 自体は任意です）。source 間で同じ server 名があっても各定義を独立検査するため、benign 同士は許容し、一方でも Mail なら停止します。

standalone source で Mail alias または同じ local Mail endpoint / 既知 runner を見つけると、その**同じ名前**だけを bound proxy へ向けた overlay に入れます。対応する名前は ASCII の大小を無視して `-` / `_` を除いた `orrerymail`、`agentmail`、`mcpagentmail`、`agentstackmail`、`agentstack` です。検出が0件のときだけ `orrery-mail` を1本生成します。たとえば既存が `agent-mail` だけなら `orrery-mail` を追加せず、生成名と案内は `agent-mail` です。実際の生成名は `AGENTSTACK_PERSISTENT_MAIL_MCP_NAMES` に comma-separated で渡されます。

plugin 由来の同名置換は Claude の standalone precedence では抑止できません。plugin が Mail alias / raw local endpoint を持てば `claude-plugin-mail-conflict` で停止します。operator は該当 plugin を明示的に disable するか profile から `--plugin-dir` を除けますが、その plugin の channel / 機能も失われます。wrapper は plugin を自動変更しません。

次は有限契約の外なので、見落としたまま起動せず pre-exec で停止します。

- `working_directory` が symlink、Git discovery を変更する environment がある、または Git project root / separate gitdir / linked worktree の main checkout root を安全に確定できない: `claude-project-root-unsupported`。`.git` marker が無い通常の non-Git directory は対応する
- macOS の `/Library/Application Support/ClaudeCode/managed-mcp.json` または `managed-settings.json` が存在する: `claude-managed-configuration-unsupported`。v1 は内容にかかわらず非対応で、1つの Mail 定義だけを書き換えても解除されない
- profile command が `--settings`、`--setting-sources`、`--safe-mode`、caller-owned `--mcp-config` / `--strict-mcp-config` を持つ
- 設定、plugin inventory / marketplace registry / catalog、選択済み / enabled plugin、manifest / marketplace 参照を読めない、または解釈できない

managed 設定は operator が管理者へ相談し、製品に reviewed managed support が入るまで起動しません。任意名の shell wrapper の内部接続までは判定できないため、alias 境界外で literal endpoint / 既知 runner にも一致しない定義は保証外です。設定本文、URL query、token を診断へ出さず、固定 reason と、該当する場合は原因 file の absolute `path` だけを返します。

Codex の `interactive` profile も本物の `codex` を `exec` します。wrapper は起動ごとに fresh session-binding expectation を作ります。SessionStart が公式 session ID / receipt を claim するまで idle が unbound でも正常です。`launch_kind=startup` は wrapper process の起動を表し、bridge 内の会話 resume とは別です。旧 Codex UI session の移行機能ではありません。

`--channels`、channel state directory、検索設定、bot の順次起動は profile の command / environment または外側の service 定義に残します。この wrapper は Mail の起動待ち順序を解決しません。

### headless

`headless` は生の `claude` / `codex` command ではなく、bot bridge executable を指定します。profile に次を追加します。

```json
"interaction": "headless",
"bridge_contract": "orrery-mail-notification-reply-v1",
"command": ["/absolute/path/to/bot-bridge", "--config", "/absolute/path/to/bot.json"]
```

bridge は wrapper が渡す owner-private Unix socket を listen し、次の全経路を完了してから matching message ID の `status: replied` を返す必要があります。

1. Mail notification を受信
2. bot へ引き渡す
3. bot の応答を Mail へ返信
4. bridge acknowledgement を返す

最小 wire 契約は次のとおりです。

| 項目 | 契約 |
| --- | --- |
| socket | `AGENTSTACK_PERSISTENT_WAKE_SOCKET` の path で bridge が listen し、bind 後の socket を同じ OS user 所有・mode `0600` にする |
| transport | `AF_UNIX` stream、UTF-8 JSON 1行（末尾 `\n`）ずつ |
| timeout | watcher の `AGENTSTACK_HEADLESS_REPLY_TIMEOUT`。整数 `1..300` 秒、未指定・不正値は `300` 秒 |

要求の最小例:

```json
{"version":1,"contract":"orrery-mail-notification-reply-v1","type":"mail-notification","agent_name":"HeadlessBot","message":{"id":42}}
```

bot が matching message への Mail reply を完了した後の応答:

```json
{"version":1,"contract":"orrery-mail-notification-reply-v1","status":"replied","message_id":42}
```

ACK の消失、timeout、bridge 再起動では同じ notification が再配送され得ます。bridge は message ID 等で bot handoff と Mail reply を冪等にし、製品が exactly-once reply を保証すると仮定してはいけません。

headless Claude bridge は `AGENTSTACK_PERSISTENT_MCP_CONFIG` の path を読み、内部で実 Claude CLI を起動するなら `--mcp-config "$AGENTSTACK_PERSISTENT_MCP_CONFIG"` として明示的に適用します。SDK を使う bridge は同等の explicit MCP configuration を指定します。この独自 env を単に子 process へ継承しても Claude は読みません。`AGENTSTACK_PERSISTENT_MAIL_MCP_NAMES` は実 namespace を bot へ案内する値であり、loader 設定ではありません。wrapper が overlay を command に自動追加するのは interactive Claude だけで、headless には interactive の source 検査 / alias overlay 保証がありません。bridge 自身が provider config を検査し、raw Mail 定義との競合を抑止しなければなりません。wrapper は `--strict-mcp-config` を追加しません。headless Codex bridge は `CODEX_HOME` / `CODEX_SHARED_CODEX_DIR` と、起動ごとに新しい `AGENTSTACK_CODEX_LAUNCH_BINDING` / `AGENTSTACK_CODEX_LAUNCH_ID` を実 Codex 子 process に保持し、別 home や古い pair で上書きしてはいけません。

tool が列挙されるだけではこの保証になりません。acknowledgement がない、期限切れ、または bridge が停止した場合、watcher は signal を保持して再試行し、同名 tmux pane へ fallback 注入しません。この repository は delivery / acknowledgement 契約を実装しますが、provider 固有の Telegram bot bridge 自体は同梱しません。隔離 fixture が確認するのは bridge の matching acknowledgement までであり、実 Mail → 実 provider bot → Mail reply は deployment ごとの実機受け入れ項目です。

dashboard の `surface` は表示媒体（tmux 等）のままです。API は profile の `interaction` を別 field で返し、画面は例外である headless の行だけ `BRIDGE · HEADLESS` と表示します。interactive と既存 `unknown` row の見た目は変えません。

## 再起動

手動 enrollment は初回または明示 recovery の一度だけです。通常の machine / bot 再起動では `claim` / `recover` を呼ばず、保存済み credential を wrapper が検証してそのまま起動します。

```bash
exec "$HOME/.agentstack/bin/agentstack-persistent" run \
  --profile "$HOME/.agentstack/profiles/ChannelsBot.json"
```

起動 service / tmux script はこの command を最後に `exec` してください。interactive surface を保つため、さらに子 process として隠さないでください。

再起動後に `local-credential-unavailable` または `stage=local-token reason=credential-unavailable` が出たときだけ、bot を停止した状態で operator が `$HOME/.agentstack/bin/agentstack-enroll inspect` からやり直し、状態が `server-token` + local missing なら `recover` を選びます。モデル自身に実行させません。単なる registration failure、HTTP 401 / 403、Mail 停止を credential 紛失と推定しないでください。

## 別 Mail と別の機体へ適用する

各機体は、その機体の local Mail と connection profile を使います。別の機体の terminal で:

1. その機体の connection profileを指定して `inspect`
2. その機体の Mail の `server_instance_id` を確認して pin
3. その機体の project に属する数値 agent ID を選択
4. 状態に応じてその機体上で `claim` または `recover`
5. その機体用の persistent profile を作って起動

最初の機体の token、pending file、receipt を別の機体へコピーしません。最初の機体の数値 ID が別の機体でも同じとは限りません。hostname、port、URL、socket path が同じでも Mail instance UUID が違えば別 authority です。

## 停止条件

次の場合は別名作成や自動 recovery をせず停止します。

- `mail-control-unavailable`: Mail service / management socket を復旧する
- `server-identity-mismatch` / `connection-not-pinned`: connection と UUID を確認する
- `agent-not-found` / `name-mismatch`: project と数値 row を確認する
- `generation-conflict`: 他の operator / process が先に更新した。再度 inspect する
- `identity-conflict` / `local-identity-conflict`: local file を別 identity の証明に流用しない
- `agent-retired`: 正規の新規作成または retirement 運用へ戻る
- `profile-already-running`: 既存 instance を停止または利用する
- `claude-project-root-unsupported`: stderr の `path` を確認し、`working_directory` の symlink、Git root、または worktree main checkout の解決を直す
- `claude-managed-configuration-unsupported`: managed file が存在する v1 構成は停止し、管理者と reviewed support を検討する
- `claude-plugin-mail-conflict`: plugin を operator が明示的に無効化するか `--plugin-dir` を外し、失う機能を確認する
- `claude-config-unreadable` / `claude-project-config-unreadable` / `claude-settings-unreadable` / `claude-plugin-inventory-unreadable` / `claude-plugin-marketplace-unreadable` / `claude-plugin-definition-unreadable` / `claude-plugin-unavailable` / `claude-settings-flag-unsupported`: stderr に `path` があればその file を直す。無い場合も hidden source を推測せず、固定 reason が示す config / plugin / command category を直す

secret file を手で直すこと、token 省略互換で所有を証明すること、alias で衝突を避けることは復旧ではありません。

## 関連文書

- [Launcher と identity](launchers.md)
- [トラブルシューティング](troubleshooting.md)
- [Mail claim / enrollment design](agentstack-mail-claim-enrollment-design.md)
- [設定](configuration.md)
