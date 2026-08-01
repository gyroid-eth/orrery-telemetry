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
| `AGENTSTACK_VAULT` | 未設定 | project key fallback と Output scan root |
| `AGENTSTACK_LABEL_PREFIX` | `org.agentstack` | launchd label prefix |
| `AGENTSTACK_TERMINAL` | `auto` | `ghostty / iterm / terminal / none` |
| `AGENTSTACK_HOOKS_DIR` | `~/.agentstack/hooks` | hook と既定 spawn script の root |
| `AGENTSTACK_RUNTIME_DIR` | `~/.claude/runtime` | token、annotation、session state |
| `AGENTSTACK_MAIL_HOME` | `~/.mcp_agent_mail` | signal data root |
| `AGENTSTACK_SIGNALS_DIR` | `$AGENTSTACK_MAIL_HOME/signals` | mail signal directory |
| `AGENTSTACK_PORTRAITS_DIR` | 未設定 | private PNG overlay directory |
| `AGENTSTACK_CUSTOM_PORTRAITS` | 未設定 | agent name → portrait key JSON |
| `AGENTSTACK_SPAWN_SCRIPT` | `$AGENTSTACK_HOOKS_DIR/spawn_child.sh` | NEW AGENT launcher |
| `AGENTSTACK_SPAWN_DIRS` | `~` | `:` 区切りの spawn directory preset |
| `AGENTSTACK_SPAWN_ROOTS` | `$HOME` | `:` 区切りの directory typeahead 許可 root |
| `AGENTSTACK_CODEX_MODELS` | `gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna` | `,` 区切りの dashboard Codex model allow-list |

path 系は `~` を展開します。空文字は未設定として扱います。integer の `AGENTSTACK_PORT` が不正なら `8770` に戻ります。

## Project key がない場合

`AGENTSTACK_PROJECT_KEY` と `AGENTSTACK_VAULT` の両方が未設定でも次は動きます。

- DECK の tmux state
- terminal open / local capture
- local annotation
- bundled portrait

次は動きません。

- launcher の shell-side agent registration
- NETWORK の mail edge / drawer
- mail history / DIGEST REPLAY
- dashboard spawn
- project-scoped retire
- vault Output / deliverables

mail 系だけを `NOT CONFIGURED` にし、local telemetry を診断に残す設計です。

## Installer

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_HOME` | `~/.agentstack` | install root。`--install-dir` でも指定可 |
| `AGENTSTACK_MAIL_DIR` | `~/mcp_agent_mail` | upstream clone |
| `AGENTSTACK_MAIL_HOME` | `~/.mcp_agent_mail` | signal / runtime data |
| `AGENTSTACK_AGENT_MAIL_REPO` | upstream GitHub URL | agent-mail clone source |
| `AGENTSTACK_PROJECT_KEY` | repo root | project human key |
| `AGENTSTACK_PROTECTED_ROOTS` | project key | reservation hook の保護 root |
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
| `AGENTSTACK_TCC_DIRS` | Desktop / Documents / Downloads | TCC probe 対象 |
| `AGENTSTACK_SCIENTISTS_JSON` | bundled JSON | scientist vocabulary override |

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

role / emoji / group は `AGENTSTACK_RUNTIME_DIR` 下の local JSON に保存されます。

- role: 最大40文字
- emoji: 最大8文字
- group: 最大24文字

現行の保存判定は role または emoji がある entry です。group だけの annotation は保持されません。dashboard spawn は role / group を渡しますが emoji は空にします。

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
- [API reference](api.md)
- [トラブルシューティング](troubleshooting.md)
