# 設定

> English version: planned.

[前: API reference](api.md) · [README に戻る](../README.md) · [次: トラブルシューティング](troubleshooting.md)

通常の設定箇所は installer が生成する:

```text
~/.agentstack/env.sh
```

です。file mode は `0600` です。service の environment は install 時に launchd plist / systemd unit へ書き込まれるため、変更後は `./scripts/install.sh` を再実行するか service definition も更新してください。

## Dashboard / `server.py`

`server.py` が直接参照する `AGENTSTACK_*` は次のとおりです。

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_PORT` | `8770` | HTTP port |
| `AGENTSTACK_BIND_HOST` | `127.0.0.1` | bind address |
| `AGENTSTACK_MAIL_DB` | `~/mcp_agent_mail/storage.sqlite3` | agent-mail SQLite |
| `AGENTSTACK_MAIL_ENV` | `~/mcp_agent_mail/.env` | dashboard spawn が bearer token を読む file |
| `AGENTSTACK_PROJECT_KEY` | 未設定 | agent-mail project の human key |
| `AGENTSTACK_VAULT` | 未設定 | project key 不在時の fallback と、vault 内 Output item の Obsidian link hint |
| `AGENTSTACK_DELIVERABLE_ROOTS` | 未設定 | `:` 区切りで `LOG_*.md` を再帰走査する root。未設定時は project の `logs/` |
| `AGENTSTACK_LANG` | browser language | murmur の言語を `ja` / `en` で上書き |
| `AGENTSTACK_MURMUR` | enabled | `off` で murmur の吹き出しを無効化 |
| `AGENTSTACK_LABEL_PREFIX` | `org.agentstack` | launchd label prefix |
| `AGENTSTACK_TERMINAL` | `auto` | `ghostty / iterm / terminal / none` |
| `AGENTSTACK_HOOKS_DIR` | `~/.agentstack/hooks` | hook と既定 spawn script の root |
| `AGENTSTACK_RUNTIME_DIR` | `~/.agentstack/runtime` | token、annotation、session index、child / watcher state |
| `AGENTSTACK_MAIL_HOME` | `~/.mcp_agent_mail` | signal data root |
| `AGENTSTACK_SIGNALS_DIR` | `$AGENTSTACK_MAIL_HOME/signals` | mail signal directory |
| `AGENTSTACK_PORTRAITS_DIR` | 未設定 | private PNG overlay directory |
| `AGENTSTACK_CUSTOM_PORTRAITS` | 未設定 | agent name → portrait key JSON |
| `AGENTSTACK_SPAWN_SCRIPT` | `$AGENTSTACK_HOOKS_DIR/spawn_child.sh` | NEW AGENT launcher |
| `AGENTSTACK_SPAWN_DIRS` | `~` | `:` 区切りの spawn directory preset |
| `AGENTSTACK_SPAWN_ROOTS` | `$HOME` | `:` 区切りの directory typeahead 許可 root |
| `AGENTSTACK_CODEX_MODELS` | `gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna` | `,` 区切りの dashboard Codex model allow-list |

path 系は `~` を展開します。空文字は未設定として扱います。integer の `AGENTSTACK_PORT` が不正なら `8770` に戻ります。

murmur の言語は `?lang=ja` / `?lang=en`、`AGENTSTACK_LANG`、browser の
`navigator.language` / `navigator.languages` の順で決まります。browser の言語に
`ja` 系があれば日本語、それ以外は英語です。`?murmur=on` / `?murmur=off` は
その URL だけ service の既定を上書きし、`AGENTSTACK_MURMUR=off` は service の
既定として吹き出しを止めます。環境変数を
常駐 service に反映するには、設定後に installer を再実行してください。

## Project key がない場合

`AGENTSTACK_PROJECT_KEY` と `AGENTSTACK_VAULT` の両方が未設定でも次は動きます。

- DECK の tmux state
- terminal open / local capture
- local annotation
- bundled portrait
- Output / deliverables（cwd または git root の `logs/` へ fallback）

次は動きません。

- launcher の shell-side agent registration
- NETWORK の mail edge / drawer
- mail history / DIGEST REPLAY
- dashboard spawn
- project-scoped retire

mail 系だけを `NOT CONFIGURED` にし、local telemetry を診断に残す設計です。

## Output / deliverables

Output index は `LOG_*.md` の先頭付近にある `agent: <name>` と dashboard の agent 名が一致する file を最大25件表示します。

- `AGENTSTACK_DELIVERABLE_ROOTS` を設定した場合、その `:` 区切り root 群を再帰走査します。明示 root は既定の `logs/` を置き換えます
- 未設定時は、絶対 path の `AGENTSTACK_PROJECT_KEY`、絶対 path の `AGENTSTACK_VAULT`、dashboard の cwd / git root の順で base を決め、その直下の `logs/` を走査します
- `AGENTSTACK_VAULT` は private directory layout の走査指定ではありません。検出 item がその vault 内にあるときだけ `obsidian://` link を作る hint です
- vault 外の item も一覧に出ますが、無効な Obsidian link を作らず非リンク項目として表示します

custom root は service environment へ配線するため、設定後に installer を再実行します。

```bash
export AGENTSTACK_DELIVERABLE_ROOTS="$HOME/project-a/logs:$HOME/shared logs"
./scripts/install.sh
```

## Installer

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_HOME` | `~/.agentstack` | install root。`--install-dir` でも指定可 |
| `AGENTSTACK_MAIL_DIR` | `~/mcp_agent_mail` | upstream clone |
| `AGENTSTACK_MAIL_HOME` | `~/.mcp_agent_mail` | signal / runtime data |
| `AGENTSTACK_AGENT_MAIL_REPO` | upstream GitHub URL | agent-mail clone source |
| `AGENTSTACK_PROJECT_KEY` | repo root | project human key |
| `AGENTSTACK_PROTECTED_ROOTS` | project key | reservation hook の保護 root |
| `AGENTSTACK_DELIVERABLE_ROOTS` | 未設定 | Output index の `:` 区切り走査 root。env / service / manifest へ保存 |
| `AGENTSTACK_LANG` | 未設定 | murmur の `ja` / `en` override。未設定時は browser 判定 |
| `AGENTSTACK_MURMUR` | 未設定 | `off` で murmur を無効化 |
| `AGENTSTACK_PORT` | `8770` | dashboard port |
| `AGENTSTACK_LABEL_PREFIX` | `org.agentstack` | service label prefix |
| `AGENTSTACK_TERMINAL` | `auto` | terminal integration |
| `AGENTSTACK_PYTHON` | `python3` の解決結果 | service 用 Python |
| `AGENTSTACK_PATH` | Homebrew と system path | service に渡す `PATH` |
| `AGENTSTACK_MCP_URL` | `http://127.0.0.1:8765/mcp` | launcher / hook の MCP endpoint |
| `AGENTSTACK_CLAUDE_SETTINGS` | `~/.claude/settings.json` | merge 対象 settings |
| `AGENTSTACK_CLAUDE_MD_SCOPE` | `project` | `agentstack-claude-setup` が managed block を書く先。`project / global / both` |

`PROJECT_KEY` も fallback として読まれますが、永続設定には `AGENTSTACK_PROJECT_KEY` を推奨します。

## Launcher

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_BASE_DIR` | `$HOME` | `fzf` picker root |
| `AGENTSTACK_CLAUDE_BIN` | `claude` | Claude CLI |
| `AGENTSTACK_CLAUDE_MODEL` | `claude-code` | Claude 登録 model label |
| `AGENTSTACK_CODEX_BIN` | `codex` | Codex CLI |
| `AGENTSTACK_CODEX_MODEL` | launcher / bootstrap の既定 | Codex 登録 model |
| `AGENTSTACK_CODEX_SANDBOX` | `workspace-write` | Codex `--sandbox` |
| `AGENTSTACK_CODEX_APPROVAL` | `on-request` | Codex `--ask-for-approval` |
| `AGENTSTACK_VAULT` | 未設定 | Codex へ追加する writable `--add-dir` |
| `AGENTSTACK_MCP_URL` | `http://127.0.0.1:8765/mcp` | registration / hook endpoint |
| `AGENTSTACK_CONTACT_POLICY` | `open` | 登録後の contact policy。`skip` で server default |
| `AGENTSTACK_AGENT_NAME_ATTEMPTS` | implementation default | name 候補の最大試行数 |
| `AGENTSTACK_NAME_UNKNOWN_LIMIT` | `3` | 連続 `unknown` の停止閾値 |
| `AGENTSTACK_TCC_GUARD` | enabled | macOS TCC warning。`0` で無効 |
| `AGENTSTACK_TCC_DIRS` | `$HOME/Desktop:$HOME/Downloads:$HOME/Documents` | `:` 区切りの TCC probe 対象 |
| `AGENTSTACK_SCIENTISTS_JSON` | bundled JSON | scientist vocabulary override |

`AGENTSTACK_TCC_DIRS` は空白を含む path も保持できる `:` 区切りが正本です。colon を含まない旧 whitespace 区切りも legacy compatibility として解釈します。

`AGENTSTACK_RESERVED_IDENTITY`、proxy token path、child token などは spawner が session ごとに設定する内部値です。top-level launcher へ手動で設定しないでください。

## Child spawn

`spawn_child.sh` と `agentstack-preregister-child` の挙動を変える変数です。

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_FOCUS_CHILD` | 未設定 | `1` で child の terminal window を前面に出す。既定は背面で開き、手元の作業を奪いません |
| `AGENTSTACK_STRICT_AGENT_NAMES` | 未設定 | `1` で off-list な child 名を警告ではなくエラーにする |
| `AGENTSTACK_MONITOR_DANGER_CHECK` | `0` | `1` で monitor の危険コマンド検知を有効にする。既定は passive |

child の Codex model と reasoning effort は spawner が `--model` / `--effort` から決め、`AGENTSTACK_CODEX_MODEL` と `AGENTSTACK_CODEX_EFFORT` として child session へ渡します。effort の既定は `xhigh` です。これらは spawner が設定する値なので、手動で export しても top-level launcher の挙動は変わりません。

## Skill

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_OBSIDIAN_APP` | 未設定 | `/log` の Obsidian モードを有効にする。Obsidian の launcher / CLI への path |

`/log` は `AGENTSTACK_OBSIDIAN_APP` と `AGENTSTACK_PROJECT_KEY` の両方が揃ったときだけ vault へ書き、daily note へリンクします。**installer はこれを設定しません**。Obsidian が入っていても未設定なら fallback モード（`<git root>/logs/`）のままです。

```bash
export AGENTSTACK_OBSIDIAN_APP="/Applications/Obsidian.app/Contents/MacOS/Obsidian"
```

## Advanced helper override

通常は installer が生成した path を使います。custom layout、複数 install、wrapper を運用するときだけ次を変更してください。

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_ENV_FILE` | 未設定 | `agentstack-preregister-child` / `agentstack-reregister` が標準 `env.sh` より先に読む追加 env file |
| `AGENTSTACK_CLAUDE_JSON` | `~/.claude.json` | Claude child 用 MCP config を作るとき、既存 agent-mail server 名を読む source |
| `AGENTSTACK_MANAGED_AGENTS_FILE` | `$AGENTSTACK_RUNTIME_DIR/managed_agents.txt` | title / spawn / cleanup helper が管理する agent 名一覧 |
| `AGENTSTACK_MCP_HEALTH_URL` | `AGENTSTACK_MCP_URL` から導出 | `session-start-reminder.sh` の liveness endpoint |
| `AGENTSTACK_MCP_PROXY` | `$AGENTSTACK_HOME/integrations/codex_app/plugin/scripts/run-mcp.sh` | spawned child ごとの認証済み stdio proxy runner |
| `AGENTSTACK_PREREGISTER_CHILD` | `$AGENTSTACK_HOME/bin/agentstack-preregister-child` | `/delegate` が child-owned token を生成する helper |
| `AGENTSTACK_MAIL_WATCHER_SESSION` | `mail-watcher` | launcher が起動・再利用する watcher の tmux session 名 |
| `AGENTSTACK_REREGISTER_PROGRAM` | `codex` | `agentstack-reregister` の第2引数を省略した場合の program |
| `AGENTSTACK_REREGISTER_MODEL` | program ごとの既定 | `agentstack-reregister` の第3引数を省略した場合の model label |

`AGENTSTACK_MCP_PROXY` が欠けても spawn 自体は継続しますが、child は shared endpoint へ fallback し、自分の owner token を明示して認証する必要があります。通常は path を差し替えるより `./scripts/install.sh` を再実行して proxy payload を復旧してください。

## 内部値

次は installer、spawner、proxy、test が生成・注入する値です。公開設定として手動 export しないでください。

- `AGENTSTACK_SKILLS_DIR`、`AGENTSTACK_TEMPLATE_HOME`、`AGENTSTACK_REGISTER_LIB`、`AGENTSTACK_SCIENTISTS_LIB`: install layout と library injection
- `AGENTSTACK_PROXY_AGENT_NAME`、`AGENTSTACK_PROXY_TOKEN_FILE`、`AGENTSTACK_PROXY_PROGRAM`、`AGENTSTACK_RESERVED_IDENTITY`: child session と owner credential の binding
- `AGENTSTACK_HOME_DIR`: `spawn_child.sh` が `AGENTSTACK_HOME` から導出する shell 内部値
- `AGENTSTACK_PYTEST`、`AGENTSTACK_RUN_AGENT_MAIL_INTEGRATION`、`AGENTSTACK_RUN_CODEX_INTEGRATION`、`AGENTSTACK_RUN_CODEX_WAKE_INTEGRATION`、`AGENTSTACK_CODEX_WAKE_SESSION_ID`: test / export の opt-in と executable injection

`__AGENTSTACK_HOME__`、`__AGENTSTACK_HOOKS_DIR__`、`__AGENTSTACK_PROJECT_KEY__` のように前後が `__` の文字列は managed document の置換 token であり、環境変数ではありません。Codex Desktop Bridge 固有の生成値と tuning 値は [Codex App 統合](codex-app.md#設定)を参照してください。

## MCP endpoint の注意

`AGENTSTACK_MCP_URL` は launcher / hook の接続先です。

現在の dashboard `POST /api/spawn` は:

```text
http://127.0.0.1:8765/mcp
```

を固定で使います。別 endpoint を使う場合は、launcher / hook だけでなく dashboard spawn との整合も確認してください。

## Spawn directory

quick-select chip:

```bash
export AGENTSTACK_SPAWN_DIRS="$HOME/code:$HOME/Obsidian/MyVault:/tmp"
```

`GET /api/spawn-names` は `:` で分割した値を順番に返します。未設定時は `["~"]` です。`~` は API では symbolic のまま保持し、実際の spawn 時に展開します。

typeahead の探索境界:

```bash
export AGENTSTACK_SPAWN_ROOTS="$HOME/code:$HOME/Obsidian"
```

`GET /api/fs/dirs` はこの root 内の child directory だけを返します。未設定時は `$HOME` が唯一の root です。server は `realpath` で境界を検証し、`..`、root 外、hidden directory、root 外への symlink を拒否します。

`SPAWN_DIRS` は「最初に見せる chip」、`SPAWN_ROOTS` は「typeahead で閲覧できる範囲」です。chip が root 外を指す構成では exact path として入力できますが、その配下の suggestion は表示されません。

## Codex model catalog

```bash
export AGENTSTACK_CODEX_MODELS="gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna"
```

空要素と前後空白は除去されます。指定がない場合は上記3モデルで、先頭の `gpt-5.6-sol` が default です。reasoning effort は `low / medium / high / xhigh`、default は `xhigh` です。dashboard spawn は allow-list 外の Codex model / effort を拒否します。

## Portrait overlay

```bash
export AGENTSTACK_PORTRAITS_DIR="$HOME/.agentstack/portraits_64"
export AGENTSTACK_CUSTOM_PORTRAITS="$HOME/.agentstack/custom_portraits.json"
```

overlay directory に `mybot.png` を置き、登録名を小文字 key で portrait stem へ対応させます。

```json
{
  "mybot":"mybot",
  "windyfermi":"Fermi"
}
```

resolution 順:

1. private overlay
2. bundled high-resolution portrait
3. bundled 64px portrait
4. safe name 用 fallback SVG

sample は [`examples/custom_portraits.example.json`](../examples/custom_portraits.example.json) を参照してください。private asset を repository へ commit せず、distribution asset と分離できます。

## Annotation

annotation の正本は:

```text
$AGENTSTACK_RUNTIME_DIR/annotations.json
```

です。`AGENTSTACK_RUNTIME_DIR` 未設定時は `~/.agentstack/runtime/annotations.json` になります。

既存 install の `dashboard/annotations.json` は自動移行されます。

- 新 path があれば常にそちらを読みます
- 新 path がなく旧 path だけがあれば旧 store を読み、次の annotate 書き込みで全 agent を保持したまま新 path へ書きます。この遅延移行では旧 file を残します
- installer を再実行した場合は payload copy より前に旧 store を runtime へ移します。移行後の旧 file 削除に失敗しても warning に留め、install と annotation は維持します
- annotation は user state として通常の uninstall で保持され、`--purge-data` のときだけ runtime directory とともに削除されます

role / emoji / group の入力上限と保持条件は次のとおりです。

- role: 最大40文字
- emoji: 最大8文字
- group: 最大24文字

role / emoji / group のいずれかがあれば entry を保持します。3項目すべてが空のときだけ削除するため、group だけの annotation も保存されます。dashboard spawn は role / group を渡し、emoji は空にします。

## Security boundary

dashboard は local-first で、認証 layer を持ちません。

- 既定 bind は `127.0.0.1`
- `0.0.0.0` は control endpoint、mail body、terminal bridge も公開
- bearer token は `mcp_agent_mail/.env` から server process が読む
- token を `env.sh`、API response、spawn log に書かない
- private portrait と vault は repository 外に置ける

remote access は SSH tunnel、trusted VPN、または別の認証 proxy を使ってください。

## 関連文書

- [インストール](install.md)
- [Launcher と identity](launchers.md)
- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [API reference](api.md)
- [トラブルシューティング](troubleshooting.md)
