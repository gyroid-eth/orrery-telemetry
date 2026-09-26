# 設定

> English version: [configuration.en.md](configuration.en.md)

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
| `AGENTSTACK_MAIL_DB` | `~/.agentstack/mail/storage.sqlite3` | ORRERY Mail SQLite |
| `AGENTSTACK_MAIL_ENV` | `~/.agentstack/mail/.env` | standalone dashboard の互換 bearer 参照先。installer は service render を明示 |
| `AGENTSTACK_MAIL_HTTP_BEARER_MODE` | `disabled` | legacy Authorization header を付けない |
| `AGENTSTACK_PROJECT_KEY` | 未設定 | ORRERY Mail project の human key |
| `AGENTSTACK_VAULT` | 未設定 | project key 不在時の fallback と、vault 内 Output item の Obsidian link hint |
| `AGENTSTACK_DELIVERABLE_ROOTS` | 未設定 | `:` 区切りで `LOG_*.md` を再帰走査する root。未設定時は project の `logs/` |
| `AGENTSTACK_LANG` | browser language | murmur の言語を `ja` / `en` で上書き |
| `AGENTSTACK_MURMUR` | enabled | `off` で murmur の吹き出しを無効化 |
| `AGENTSTACK_LABEL_PREFIX` | `org.agentstack` | launchd label prefix |
| `AGENTSTACK_TERMINAL` | `auto` | `ghostty / iterm / terminal / wt / none`。`wt` は WSL2 の Windows Terminal（`auto` は WSL2 内で `wt.exe` が見えれば選ぶ） |
| `AGENTSTACK_HOOKS_DIR` | `~/.agentstack/hooks` | hook と既定 spawn script の root |
| `AGENTSTACK_RUNTIME_DIR` | `~/.agentstack/runtime` | token、annotation、session index、child / watcher state |
| `AGENTSTACK_MAIL_HOME` | `~/.agentstack/mail` | signal data root |
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
既定として吹き出しを止めます。利用者は dashboard の `SETTINGS` › DISPLAY の
Murmur switch でも on / off を切り替えられ、その選択は browser に保存されます
（優先順は URL、browser の保存値、service 既定）。環境変数を
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
| `AGENTSTACK_MAIL_DIR` | `$AGENTSTACK_HOME/mail-service` | candidate、immutable render、runtime log / pidfile |
| `AGENTSTACK_MAIL_HOME` | `~/.agentstack/mail` | canonical DB / archive / signals root |
| `AGENTSTACK_MAIL_DB` | `$AGENTSTACK_MAIL_HOME/storage.sqlite3` | dashboard が読む ORRERY Mail SQLite |
| `AGENTSTACK_MAIL_ENV` | render ID から導出 | ORRERY Mail service env |
| `AGENTSTACK_MAIL_STATE_ROOT` | `~/.agentstack/mail` | canonical DB / archive / signals root |
| `AGENTSTACK_MAIL_SERVICE_ROOT` | `$AGENTSTACK_HOME/mail-service` | candidate、immutable render、runtime log / pidfile |
| `AGENTSTACK_MAIL_SERVICE_VENV` | candidate ID から導出 | 検証済み candidate venv を明示的に再利用する場合の path |
| `AGENTSTACK_MAIL_ENROLL_BIN` | 採用した Mail deployment から導出 | installer が `env.sh` に保存する対応済み `agentstack-enroll`。通常は手動設定しない |
| `AGENTSTACK_MAIL_HTTP_BEARER_MODE` | `disabled` | legacy HTTP bearer を使用しない |
| `AGENTSTACK_PROJECT_KEY` | 再 install 時は既存 `env.sh`、初回は必須 | project human key。`--project-key` が最優先 |
| `AGENTSTACK_PROTECTED_ROOTS` | live project key、次に既存 `env.sh`、最後に resolved project key | reservation hook の保護 root |
| `AGENTSTACK_RELEASE_GRACE_SECONDS` | `90` | 成功した Edit / Write 後、reservation を解放するまでの debounce 秒数。旧 `FILE_RESERVATION_RELEASE_GRACE_SECONDS` も fallback として利用可 |
| `AGENTSTACK_DELIVERABLE_ROOTS` | 未設定 | Output index の `:` 区切り走査 root。env / service / manifest へ保存 |
| `AGENTSTACK_LANG` | 未設定 | murmur の `ja` / `en` override。未設定時は browser 判定 |
| `AGENTSTACK_MURMUR` | 未設定 | `off` で murmur を無効化 |
| `AGENTSTACK_PORT` | `8770` | dashboard port |
| `AGENTSTACK_LABEL_PREFIX` | `org.agentstack` | service label prefix |
| `AGENTSTACK_TERMINAL` | `auto` | terminal integration |
| `AGENTSTACK_PYTHON` | `python3` の解決結果 | installer が version 検証した service / installed persistent launcher 用 Python。persistent launcher は ambient `PATH` の `python3` へ fallback しない |
| `AGENTSTACK_PATH` | Homebrew と system path | service に渡す `PATH` |
| `AGENTSTACK_MCP_URL` | `http://127.0.0.1:18765/mcp` | launcher / hook / dashboard / Bridge の MCP endpoint |
| `AGENTSTACK_CLAUDE_SETTINGS` | `~/.claude/settings.json` | merge 対象 settings |
| `AGENTSTACK_CLAUDE_MD_SCOPE` | `project` | `agentstack-claude-setup` が managed block を書く先。`project / global / both` |

健康な ORRERY Mail listener が既にある場合、installer は server を更新・再起動せず、稼働中の immutable render に記録された deployment metadata を採用します。metadata 導入前の managed render は、render ID と candidate directory の決定論的対応が一意な場合だけ採用します。生成する `env.sh` の `AGENTSTACK_MAIL_ENV` と `AGENTSTACK_MAIL_ENROLL_BIN` は同じ deployment を指します。既知の legacy render に enrollment CLI が無い場合は Mail と既設 autostart を維持し、enrollment だけ unavailable として保存します。

DB が一致する健康な listener でも service env を特定できない場合、明示 pin が無い通常の再 install は listener をそのまま再利用します。この degraded 状態では deployment / enrollment の path を空にし、enrollment profile と Mail autostart を更新しません。既設 trigger は削除・停止せず残しますが、installer は次回 login での再起動を保証しません。明示した `AGENTSTACK_MAIL_SERVICE_VENV` / `AGENTSTACK_MAIL_SERVICE_ENV` を稼働 deployment と照合できない場合、既知 metadata が不正な場合、または metadata が約束した enrollment CLI が無い場合は、listener を切り替えず明示エラーで停止します。

installer の project key 解決順は `--project-key` / process の
`AGENTSTACK_PROJECT_KEY` → `PROJECT_KEY` → install 先の既存 `env.sh` です。
初回 install でどれも無い場合は repo checkout を project と推測せず、変更前に
exit 2 で停止します。永続設定には `AGENTSTACK_PROJECT_KEY` を推奨します。

hook と helper の実行時は `AGENTSTACK_PROJECT_KEY` → `PROJECT_KEY` →
`${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh` → 現在の cwd の順です。installed
`env.sh` は source せず、`AGENTSTACK_PROJECT_KEY`（protected root の fallback では
`AGENTSTACK_PROTECTED_ROOTS` も）だけを literal として読み取ります。このため install
済みの editor を別 directory から起動しても reservation と registration は同じ project
key を使い、同時に `env.sh` 内の任意 shell code は実行されません。

installer は `AGENTSTACK_MAIL_DB`、`AGENTSTACK_MAIL_ENV`、`AGENTSTACK_SIGNALS_DIR`
を state / render から導出し、`env.sh` へ state root と
`AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled` を一緒に保存します。

## Launcher

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_BASE_DIR` | `$HOME` | `fzf` picker root |
| `AGENTSTACK_CLAUDE_BIN` | `claude` | Claude CLI |
| `AGENTSTACK_CLAUDE_MODEL` | `claude-code` | Claude 登録 model label |
| `AGENTSTACK_CODEX_BIN` | install 時に operator の shell で解決した `codex`（`--codex-bin` で明示可） | Codex CLI。dashboard は launchd / systemd の最小 PATH で動くので、nvm / nodebrew / `~/.npm-global` の codex はこの値で届く |
| `AGENTSTACK_CODEX_MODEL` | launcher / bootstrap の既定 | Codex 登録 model |
| `AGENTSTACK_CODEX_SANDBOX` | `workspace-write` | Codex `--sandbox` |
| `AGENTSTACK_CODEX_APPROVAL` | `on-request` | `agent-start-codex`（利用者自身の対話 session）の `--ask-for-approval`。spawn される child は `AGENTSTACK_CODEX_CHILD_APPROVAL`（Child spawn 参照） |
| `AGENTSTACK_VAULT` | 未設定 | Codex へ追加する writable `--add-dir` |
| `AGENTSTACK_MCP_URL` | `http://127.0.0.1:18765/mcp` | registration / hook endpoint |
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
| `AGENTSTACK_AUTO_OPEN_CHILD` | `0` | `1` で Claude / Codex child 起動後に OS terminal を自動表示する。tmux session と Deck の Open tmux は常に独立 |
| `AGENTSTACK_FOCUS_CHILD` | 未設定 | 自動表示が有効な場合、`1` で前面に出す。単独では自動表示を有効にしない |
| `AGENTSTACK_STRICT_AGENT_NAMES` | 未設定 | `1` で off-list な child 名を警告ではなくエラーにする |
| `AGENTSTACK_MONITOR_DANGER_CHECK` | `0` | `1` で monitor の危険コマンド検知を有効にする。既定は passive |
| `AGENTSTACK_CODEX_CHILD_APPROVAL` | `never` | Codex child の `--ask-for-approval`。installer の `--codex-approval` で永続化 |
| `AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY` | 未設定 | macOS/Linux の全 Codex child の `config.toml` に重ねる TOML fragment の絶対 path。installer の `--codex-child-overlay <path>` で永続化 |
| `AGENTSTACK_CODEX_NETWORK` | `on` | Codex child の sandbox network（`-c sandbox_workspace_write.network_access=true`）。`--codex-network off` で切る |
| `AGENTSTACK_CODEX_ADD_DIRS` | 未設定 | Codex child に追加で書込を許す root（`:` 区切り）。`--codex-add-dirs` で永続化 |
| `AGENTSTACK_WORKTREE_ROOT` | `$AGENTSTACK_HOME/worktrees` | 新規 isolated worktree の永続 root。installer 実行時の環境変数で上書き・永続化 |
| `AGENTSTACK_CHILD_RESUME_RETENTION_DAYS` | `30` | 正常終了した Codex child の再開用 state / credential を保持する日数。installer の `--child-resume-retention-days DAYS` で永続化。`0` は従来どおり cleanup 時に全削除 |

Codex child の起動フラグは製品が組み立てます。`~/.codex/bin/` にある利用者側の launcher は参照しません（参照すると、その launcher の既定 `on-request` に静かに置き換わり、network flag と追加 root も落ちます）。child は無人で動くので既定は approval `never`・network on です。書込を許す root は「project、`AGENTSTACK_SPAWN_DIRS` / `AGENTSTACK_SPAWN_ROOTS`、install dir、`AGENTSTACK_WORKTREE_ROOT`、`~/.claude`、`~/.codex`、child 専用 `CODEX_HOME`、`AGENTSTACK_CODEX_ADD_DIRS`」で、存在しない directory は黙って外します。dashboard の Codex resume も同じ値を使います。これらは dashboard service の環境なので、shell で `export` しても届きません。installer に渡してください。config overlay は現在 `spawn_child.sh` を使う macOS/Linux（Windows では WSL2 を含む）だけに適用され、native Windows launcher には適用されません。

worktree root を変える場合は、たとえば `AGENTSTACK_WORKTREE_ROOT=/srv/agent-worktrees ./scripts/install.sh ...` として installer に渡します。相対 path は受け付けません。`Syncthing` / `Obsidian` を含む path は launcher が引き続き拒否します。旧既定の `/tmp/cc-worktrees` は移動も削除もせず、新規 spawn だけが永続 root を使います。`agentstack-doctor` は現在の root のうち live でも active registration でもない directory を報告しますが、削除は operator に任せます。

`spawn_child.sh` は既定では OS terminal を自動表示しません。child は独立した detached tmux session で動き、必要なときだけ Deck の Open tmux から開けます。`AGENTSTACK_TERMINAL=auto` はそのための terminal 選択として維持します。`none` は手動 Open tmux も無効にするため、自動表示だけを止める用途には使いません。

起動直後から全 child の画面を見たい場合は、`AGENTSTACK_AUTO_OPEN_CHILD=1 ./scripts/install.sh ...` として installer に渡してください。設定は `env.sh`、Dashboard service、install-state に保存され、再インストールでも保持されます。未設定の旧 install は `0` へ移行し、明示した `0` / `1` が保存値より優先されます。既存の `AGENTSTACK_FOCUS_CHILD=1` だけでは自動表示されなくなります。直接 shell から起動する場合は、その shell に同じ変数を export します。child から孫への新規起動と、Codex session の再開先へも設定を渡します。Gemini の別 launcher に OS terminal 自動表示を追加する設定ではありません。

child の model は spawner の単一 model catalog と正規化関数から決まります。Claude の無指定 / `opus` は `claude-opus-5-5`、`sonnet` は `claude-sonnet-5`、Codex の無指定 / `sol` は `gpt-5.6-sol` です。旧 `claude-opus-5`、`claude-opus-4-8`、`claude-sonnet-4-6`、`gpt-5.5` の明示指定は引き続き有効です。generic な `opus[1m]` / `sonnet[1m]` は既知の legacy 1M model に正規化されます。

Codex の reasoning effort は `--effort` から決まり、`AGENTSTACK_CODEX_MODEL` と `AGENTSTACK_CODEX_EFFORT` として child session へ渡します。既定は `xhigh` です。`gpt-5.6-luna` は `ultra` を、旧 `gpt-5.5` は `max` / `ultra` をサポートしないため spawner が拒否します。これらは spawner が設定する値なので、手動で export しても top-level launcher の挙動は変わりません。

正常終了した Codex child は、remote identity を retire したまま、private state と canonical owner credential を期限まで保持します。専用 home、proxy runtime、旧 MCP config は cleanup ごとに削除され、resume 時には現在の source Codex home と保存済みの `codex_mcp_profile` から作り直します。期限切れ material は dashboard 稼働中の maintenance が削除します。`agentstack-doctor` は期限切れ・purge 待ちを報告するだけで削除しません。期限前でも `agentstack-purge-child-resume <agent>`、期限切れをまとめて片付ける場合は `agentstack-purge-child-resume --expired` を使えます。どちらも履歴 transcript と bound receipt は削除しません。

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
| `AGENTSTACK_CLAUDE_JSON` | `~/.claude.json` | Claude child 用 MCP config を作るとき、既存 ORRERY Mail server 名を読む source |
| `AGENTSTACK_MANAGED_AGENTS_FILE` | `$AGENTSTACK_RUNTIME_DIR/managed_agents.txt` | title / spawn / cleanup helper が管理する agent 名一覧 |
| `AGENTSTACK_MCP_HEALTH_URL` | `AGENTSTACK_MCP_URL` から導出 | `session-start-reminder.sh` の liveness endpoint |
| `AGENTSTACK_MCP_PROXY` | `$AGENTSTACK_HOME/integrations/codex_app/plugin/scripts/run-mcp.sh` | spawned child ごとの認証済み stdio proxy runner |
| `AGENTSTACK_PREREGISTER_CHILD` | `$AGENTSTACK_HOME/bin/agentstack-preregister-child` | `/delegate` が child-owned token を生成する helper |
| `AGENTSTACK_MAIL_WATCHER_SESSION` | `mail-watcher` | launcher が起動・再利用する watcher の tmux session 名 |
| `AGENTSTACK_MAIL_WATCHER_PIDFILE` | `/tmp/orrery-mail-watcher.lock/watcher.pid` | dashboard が非 launchd watcher の実プロセスを照合する pidfile |
| `AGENTSTACK_MAIL_WATCHER_HEARTBEAT` | pidfile と同じ directory の `heartbeat` | process command を取得できない環境で使う watcher heartbeat |
| `AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE` | `low`（＝全通） | 通知として**割り込ませる**下限。`low` \| `normal` \| `high` \| `urgent` |
| `AGENTSTACK_REREGISTER_PROGRAM` | `codex` | `agentstack-reregister` の第2引数を省略した場合の program |
| `AGENTSTACK_REREGISTER_MODEL` | program ごとの既定 | `agentstack-reregister` の第3引数を省略した場合の model label |

通知は相手の入力欄に直接タイプされます。子を何体も走らせていると、人間が親と会話している最中に進捗報告が挟まって話が細切れになります。`AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE=high` にすると、`normal` 以下は割り込まなくなります。**メールが消えるわけではありません。** signal はそのまま残り、次に `fetch_inbox` を呼べば普通に読めます。奪うのは割り込む権利であって、届く権利ではありません。完了報告を確実に受け取りたい場合は、子に `importance="high"` で送らせてください（`/delegate` の既定はそうなっています）。

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

dashboard `POST /api/spawn` は generated `env.sh` の同じ値を使います。既定
は:

```text
http://127.0.0.1:18765/mcp
```

で、installer が transport selector を `disabled` へ同時に設定するため、launcher、
hook、dashboard spawn、Codex App Bridge が同じ authority を見ます。手動で endpoint
を上書きする場合も、これらを別々に設定しないでください。

## Spawn directory

NEW AGENT の launch directory は 2 つの値で決まります。dashboard は launchd / systemd
（または supervised background）で動くので、**shell で `export` しても届きません**。
installer に渡して `env.sh`・service 定義・`install-state.json` に永続化します。

```bash
# 初回でも再インストールでも同じ。`:` 区切り、各要素は絶対パスか `~` 始まり
./scripts/install.sh \
  --spawn-dirs "$HOME/code:$HOME/Obsidian/MyVault:/tmp" \
  --spawn-roots "$HOME/code:$HOME/Obsidian"
```

環境変数 `AGENTSTACK_SPAWN_DIRS` / `AGENTSTACK_SPAWN_ROOTS` を付けて installer を実行しても同じです。
優先順位は「command-line > 環境変数 > install 先の既存 `env.sh`」で、再インストール時に何も指定しなければ前回の値を引き継ぎます。
存在しない directory は warning だけ出して受け付けます（後で clone する checkout を先に登録できます）。相対パスは error で停止します。

- `SPAWN_DIRS` は「最初に見せる quick-select chip」。`GET /api/spawn-names` が `:` で分割した値を順番に返します。未設定時は `["~"]` です。`~` は API では symbolic のまま保持し、実際の spawn 時に展開します
- `SPAWN_ROOTS` は「typeahead で閲覧できる範囲」。`GET /api/fs/dirs` はこの root 内の child directory だけを返します。未設定時は `$HOME` が唯一の root です。server は `realpath` で境界を検証し、`..`、root 外、hidden directory、root 外への symlink を拒否します

`SPAWN_ROOTS` は `SPAWN_DIRS` から自動導出しません。chip が root 外を指す構成では exact path として入力できますが、その配下の suggestion は表示されません。多くの場合は既定の `$HOME` が chip を含むので、`SPAWN_DIRS` だけ指定すれば足ります。

## Codex model catalog

```bash
AGENTSTACK_CODEX_MODELS="gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna" ./scripts/install.sh
```

installer に渡して永続化します（shell の `export` は service に届きません）。

空要素と前後空白は除去されます。指定がない場合は上記4モデル（`gpt-5.6-sol` / `gpt-6-astra` / `gpt-5.6-terra` / `gpt-5.6-luna`）で、先頭の `gpt-5.6-sol` が default です。reasoning effort は `low / medium / high / xhigh / max / ultra`（max / ultra は `gpt-6-astra` のみ）、default は `xhigh` です。dashboard spawn は allow-list 外の Codex model / effort を拒否します。

## Portrait overlay

```bash
AGENTSTACK_PORTRAITS_DIR="$HOME/.agentstack/portraits" \
AGENTSTACK_CUSTOM_PORTRAITS="$HOME/.agentstack/custom_portraits.json" \
./scripts/install.sh
```

`AGENTSTACK_SPAWN_DIRS` と同じく installer に渡して永続化します（dashboard は service として動くので shell の `export` は届きません。再インストール時は前回の値を引き継ぎます）。

overlay directory に `MyBot.png` を置くだけで、登録名 `MyBot` / `mybot` のどちらにも使われます（stem の照合は大文字小文字を区別しません）。登録名と file 名が違う場合だけ、custom map で登録名（小文字 key）を portrait stem へ対応させます。

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
- owner token は agent ごとの private file から local proxy が読む
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

## `AGENTSTACK_MAIL_LAUNCHD_LABEL`

`agentstack-mailctl` が操作する launchd job のラベル。**どの job を停止・起動してよいかを決める設定**なので、install 時の値は `install-state.json` の manifest にも記録されます。

- 明示すればその値が使われ、installer も上書きしません
- 明示せず `AGENTSTACK_LABEL_PREFIX` を既定以外にした install は `<prefix>.mail-service` を使います
- どちらも無い場合は空のまま＝`agentstack-mailctl` の組み込み既定（`org.orrery.mail`）

pytest 実行下では、**解決結果が組み込み既定になる場合、`agentstack-mailctl` は動作を拒否します**（テストが本番の job を停止した事故があったため）。テストは自分のラベルを明示してください。
