# インストール

> English version: planned.

[README に戻る](../README.md) · [次: Launcher と identity](launchers.md)

この文書は、はじめて ORRERY Telemetry（この repository）を入れる人向けです。3 コマンドで入れて、2 コマンドで確かめ、1 コマンドで最初の agent を起動します。以前 third-party の MCP Agent Mail を使っていた人向けの移行手順は、末尾の[付録](#付録-以前-mcp-agent-mail-を使っていた場合)にまとめました。

## 動作環境

主対象は macOS です。launcher と hook は macOS 標準 Bash 3.2 でも動くよう実装されています。

必須:

- Python 3.10 以上（`python3`）。全 suite を実測済みなのは 3.10 / 3.12 / 3.13 / 3.14 です。上限は設けていません（CI が 3.10・3.12・3.14 を毎回回すので、新しい Python で壊れた場合はそこで落ちます）
- `tmux`
- `git`
- `uv`（同梱の ORRERY Mail 用の Python 環境を作るために使います）
- Claude Code または Codex CLI

任意:

- `fswatch`: mail watcher。なければ 2 秒間隔の polling に fallback します（通知は届きます）
- `fzf`: 引数なし launcher の directory picker。なければカレントディレクトリを使います
- Ghostty: click-to-jump と window title。iTerm2、Terminal.app、`none` へ fallback。ただし既存ウィンドウの前面化は Ghostty のみで、iTerm2 と Terminal.app では jump のたびに新しいウィンドウが開きます
- Obsidian: `/log` の vault / Daily Note 統合と、vault 内 Output item を開く link。`/log` の Obsidian モードは `AGENTSTACK_OBSIDIAN_APP` を設定して初めて有効になります（installer は設定しません）。未設定なら `/log` はローカルの `logs/` に書き、dashboard は generic project log を非リンク項目として表示します

macOS では launchd の `gui/$UID` domain への実際の bootstrap 成否で常駐経路を選びます。画面スリープ中や SSH 専用環境などで bootstrap できない場合は、dashboard server の終了を検知して再起動する supervised background mode に自動で切り替えます。Linux では systemd user service、利用できなければ同じ supervised background mode を使う実装ですが、実 Linux ホストでは未検証です（CI は `systemctl` をスタブにした unit 生成テストのみ）。WSL2 も未検証で、設計上は localhost dashboard が使え、Ghostty の click-to-jump は使えない想定です。Windows native は対象外です。

`AGENTSTACK_PYTHON` を指定した場合も Python 3.10 以上か検証します。未指定時は PATH 上の `python3` を検査し、不適格なら version 付き command や `/opt/homebrew/bin/python3`、`/usr/local/bin/python3` も探索します。互換 interpreter がなければ、サービス file を生成する前に検査した version と path を示して停止します。

## インストール

```bash
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/path/to/your-project
```

`--project-key` には、agent たちに作業させる project の絶対パスを渡します（この repository の checkout ではありません）。初回は必須で、未指定なら installer は何も書かずに停止します。2 回目以降は前回の値を `~/.agentstack/env.sh` から引き継ぐので省略できます。

installer は途中で 3 つの変更を preview し、それぞれ `yes` を求めます。

1. Claude Code の MCP 登録（`~/.claude.json` に `orrery-mail` を追加）
2. Claude Code の settings（hooks と permissions を `~/.claude/settings.json` に追記）
3. managed instruction block（project の `CLAUDE.md` と `~/.codex/AGENTS.md` の marker 間）

いずれも既存の内容は保持し、merge 前の backup を `~/.agentstack/backups` に置きます。`no` と答えた項目は後から helper で個別に入れられます（[Claude Code から使えるようにする](#claude-code-から-agent-mail-を使えるようにする)）。

installer が置くもの:

- `~/.agentstack/` に dashboard、launcher、hook、skill、同梱の ORRERY Mail、`env.sh`、`VERSION`、`install-state.json`
- `~/.claude/skills/delegate` と `~/.claude/skills/log`（`~/.agentstack/skills/` への symlink）
- 常駐 service 2 つ: dashboard（port 8770）と ORRERY Mail（`http://127.0.0.1:18765/mcp`、state は `~/.agentstack/mail`）。それぞれ launchd（macOS）に登録し、できなければ supervised background mode で起動します

`env.sh` は mode `0600` で、token は書き込みません。shell の dotfile は変更しません。

### 確認

```bash
~/.agentstack/bin/agentstack-doctor
~/.agentstack/bin/agentstack-selftest
open http://127.0.0.1:8770/
```

`agentstack-doctor` は、必要な file と設定が揃っているか、dashboard の `/api/version` が実際に応答しているか、launchd / systemd への登録と実行状態はどうかを別々に報告します。endpoint が応答しているのに manager が実行していない場合は `unmanaged-background` と出ます。

`agentstack-selftest` は実際に 2 つの agent を登録し、message の往復と file reservation を行い、その結果を dashboard が同じ DB から読めているところまで確かめます。install 後は必ず一度実行してください。

### 最初の agent を起動する

install 前から開いていた Claude Code session は、新しく入った skill を再走査しません。既存 session を `/exit` してから、新しい terminal で project を指定して起動します。

```bash
export PATH="$HOME/.agentstack/bin:$PATH"
agent-start /path/to/your-project
# Codex CLI なら
agent-start-codex /path/to/your-project
```

`agent-start` は agent-mail の identity と同名の tmux session を作ります。dashboard の jump、mail 通知、token 復旧はこの名前で結びつきます。起動した Claude Code では `/delegate` のように先頭の slash を付けて skill を呼びます。初回の child 起動は [Skills と file reservation](launchers.md#skills2件と-file-reservation) を参照してください。

## 非対話で入れる（`--assume-yes`）

CI や script から入れる場合、既定のままだと 3 つの承認は警告付きでスキップされます。repository と preview 内容を確認した**ユーザー本人**が、承認を事前に与える場合だけ次を使えます。

```bash
./scripts/install.sh --project-key /absolute/path/to/your-project --assume-yes
```

`--assume-yes`（短縮 `-y`、環境変数 `AGENTSTACK_ASSUME_YES=1` も同じ）は approval の事前付与であり、`--force` ではありません。Python 3.10 未満、dashboard port の競合、既存 agent-mail DB の複数候補・不存在・稼働 server との不一致、自動 setup の失敗は従来どおり停止します。自動承認した項目は `assume-yes:` 行として個別に出力されます。agent や自動化が「便利だから」とユーザーの明示選択なしにこの option を付けてはいけません。command-line の指定は環境変数より優先され、生成する `env.sh` には残しません。

## Install tier と option

| 呼び出し | Tier | 内容 |
| --- | --- | --- |
| `./scripts/install.sh` | Tier 1 / default | 全 payload と Claude skill link。hooks・permissions と Codex / Claude managed block は preview 後、承認時だけ merge |
| `./scripts/install.sh --dashboard-only` | Tier 0 | dashboard と helper のみ。hooks、skills、Codex / Claude template は導入しない |
| `./scripts/install.sh --scoped` | Tier 2 placeholder | payload は導入するが、user settings / managed docs は変更しない |
| `./scripts/install.sh --dry-run` | preview | 変更予定を表示し、file や service を変更しない |

`--dashboard-only` と `--scoped` は排他的です。不明 option や値不足は変更前に停止します。

```text
--install-dir PATH      default: ~/.agentstack
--project-key PATH      first install: required / re-install: existing env.sh
--port PORT             default: 8770
--label-prefix PREFIX   default: org.agentstack
--terminal MODE         auto | ghostty | iterm | terminal | none
--spawn-dirs PATHS      NEW AGENT の launch directory preset（`:` 区切り）
--spawn-roots PATHS     directory typeahead が閲覧できる root（`:` 区切り）
--retire-legacy-mail    付録参照（以前の MCP Agent Mail を退役させる）
-y, --assume-yes        approval prompts only; validation errors remain fatal
```

`--project-key` は、明示した値がいつでも最優先です。次に環境変数 `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY`、最後に install 先の既存 `env.sh` を見ます。`--spawn-dirs` / `--spawn-roots` も同じ順序で解決し、再インストール時は前回の値を引き継ぎます（詳細は [configuration.md](configuration.md) の「Spawn directory」）。`--bin-dir` は公開 option ではありません。permissions template の `__AGENTSTACK_BIN_DIR__` を展開するため、installer が内部で `agentstack-merge-settings --bin-dir "$INSTALL_DIR/bin"` を呼びます。

## Settings、permissions、Claude skill の merge

Tier 1 の merge は `scripts/lib/merge_settings.py` による JSON parser ベースです。

- 既存の hooks、permissions、その他の user settings を保持
- AgentStack が追加する値だけを重複なしで追記
- merge 前の settings backup を `~/.agentstack/backups` に保存
- 追加した entry と変更結果を manifest に記録
- managed block は marker 間だけを idempotent に更新
- `install-state.json` を uninstall の削除範囲の正本にする

permissions の `deny` は、**不可逆で復旧手段がない操作だけ**に限定します。破壊的でも復旧できる操作は allow にも deny にも入れず、実行時の人間による確認に委ねます。また、allow 済みの別 tool で同じ状態へ到達できる場合は、deny に追加しても安全上の意味がないため追加しません。現在 deny するのは `hard_delete_agent`、`hard_delete_project`、`purge_old_messages` の 3 つです。

単純な文字列置換ではなく構造を読んで merge するのは、再インストールと uninstall でユーザー設定を巻き込まないためです。

`skillsDirectories` は Claude Code の setting ではなく、installer は新しい値を追加しません。skill payload の正本は `~/.agentstack/skills/<name>` のままにし、Claude Code が標準で読む `~/.claude/skills/<name>` へ絶対 symlink を作ります。

```text
~/.claude/skills/delegate -> ~/.agentstack/skills/delegate
~/.claude/skills/log      -> ~/.agentstack/skills/log
```

同じ AgentStack payload を指す symlink がすでにある場合は再利用し、manifest に所有登録します。この link は payload と一緒に無効になるため、uninstall では削除対象です。同名の file、directory、または別 target の symlink がある場合は warning を出して保持し、所有登録しません。uninstall は manifest の path と実際の symlink target を照合し、所有登録された、AgentStack payload を指す symlink だけを削除します。利用者が file や directory に置き換えた path、または retarget した symlink は残します。

旧 installer が `skillsDirectories` に `~/.agentstack/skills` を追加していた環境では、Tier 1 の settings merge を承認した再インストール時にその旧 AgentStack entry だけを削除します。同じ配列の他の user value と、それ以外の settings は保持します。

installer は shell dotfile を変更しません。project 内では、Tier 1 の preview 後に承認した場合だけ `CLAUDE.md` の managed marker 間を更新し、それ以外の file は変更しません。Claude Code user settings の既定位置は `~/.claude/settings.json` で、`AGENTSTACK_CLAUDE_SETTINGS` で変更できます。

## Claude Code から agent-mail を使えるようにする

`/delegate` skill は `mcp__orrery-mail__*` tool を許可し、Claude Code の user-scope MCP server 名も **`orrery-mail` 固定**です。

Tier 1 installer は `AGENTSTACK_CLAUDE_JSON`（既定 `~/.claude.json`）の `mcpServers` を構造として読み、既存の他 server と project 設定を保持したまま次の entry だけを追加・更新します。

```json
{
  "mcpServers": {
    "orrery-mail": {
      "type": "http",
      "url": "http://127.0.0.1:18765/mcp"
    }
  }
}
```

diff preview では bearer token を `<redacted>` に置き換えます。対話で `yes` と答えた場合、またはユーザーが明示した `--assume-yes` の場合だけ mode `0600` で atomic write し、元 file を `~/.agentstack/backups` に保存します。非対話で未承認なら書き込まず、installer と `agentstack-doctor` が安全な preview / apply コマンドを表示します。`agentstack-selftest` は HTTP server の動作だけでなく、この固定名・endpoint・authorization の登録も検査します。

install 時に `no` と答えた、または登録が無い場合は、doctor の出力に従って preview してください。

```bash
~/.agentstack/bin/agentstack-doctor
```

Codex child は launcher が child-scoped MCP proxy config を自動生成します。top-level Codex CLI を `agent-start-codex` から使う場合は、`$CODEX_HOME/config.toml` に次を一度設定します。bootstrap が `MCP_AGENT_MAIL_TOKEN` を process environment に読み込むため、token 自体を TOML に保存する必要はありません。

```toml
[mcp_servers.orrery-mail]
url = "http://127.0.0.1:18765/mcp"
```

key は `[mcp_servers.orrery-mail]` に固定します。

### Managed instruction helper

Tier 1 が preview / merge に使う helper は単独でも実行できます。

```bash
~/.agentstack/bin/agentstack-codex-setup --print
~/.agentstack/bin/agentstack-claude-setup --print
```

`--print` は placeholder を解決した block と対象を表示するだけで変更しません。引数なしでは既存 file を backup し、marker 間の AgentStack block だけを install / update します。

```bash
~/.agentstack/bin/agentstack-codex-setup
~/.agentstack/bin/agentstack-claude-setup
```

block だけを外す場合はそれぞれ `--uninstall` を使います。Codex は `$CODEX_HOME/AGENTS.md`、Claude は `AGENTSTACK_CLAUDE_MD_SCOPE=project / global / both` で選んだ `CLAUDE.md` が対象です。marker 外の既存内容は保持します。

## ORRERY Mail service の扱い

同梱の ORRERY Mail は、launcher が要求した identity をそのまま登録する設定（`AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough`）で動きます。既定 endpoint は `http://127.0.0.1:18765/mcp`、state root は `~/.agentstack/mail` です。HTTP bearer は使わず、各 agent の owner token は tool argument / local proxy で扱います。

installer は endpoint に既に何かが応答している場合、それが同じ DB を返す ORRERY Mail のときだけ再利用し、別 DB を返す listener は再利用せず最初の書き込み前に停止します。新規環境では同梱 package の venv を candidate ID ごとに配置し、空 state を初期化して、health response が設定した DB を返すまで確認してから先へ進みます（内部構成は [agentstack-mail 文書](agentstack-mail.md)）。

service の操作は次の controller で行います。runner 自身がクラッシュを 5 秒後に再起動し、controller は PID file と endpoint と DB を照合して、二重起動や無関係な process の停止を拒否します。

```bash
~/.agentstack/bin/agentstack-mailctl start
~/.agentstack/bin/agentstack-mailctl status
~/.agentstack/bin/agentstack-mailctl stop
~/.agentstack/bin/agentstack-mailctl restart
```

dashboard 側のサービス登録や health check が失敗しても、payload、承認済み managed block、`install-state.json` の生成は完了し、installer は warning と supervised background の手動起動コマンドを最後に表示します。mail service の provisioning / DB health が失敗した場合は install を停止します。実際の dashboard 常駐方式は `~/.agentstack/dashboard/agentctl.sh status` と `agentstack-doctor` で確認できます。

## VERSION

version の正本は repository 直下の `VERSION` です。installer は install root にコピーします。

`GET /api/version` は次の順で version を解決します。

1. install 済み artifact に隣接する `VERSION`
2. repository の `VERSION`
3. `git describe --tags --always --dirty`
4. `unknown`

dashboard の HTML だけをコピーせず `VERSION` も installer 経由で更新してください。配布物と表示 version を一致させるためです。

## macOS の TCC / Full Disk Access

`~/Desktop`、`~/Documents`、`~/Downloads` は macOS TCC の保護対象です。Full Disk Access のない terminal から root agent を起動すると、その terminal identity が tmux と子孫へ伝播し、子 agent だけ `EPERM` になることがあります。

対処:

1. root agent を Full Disk Access 済み terminal から起動
2. または project を保護対象外へ移動
3. context を変えた後は既存 tmux server / session を作り直す

launcher はこの状態を警告します。必要なら:

```bash
export AGENTSTACK_TCC_GUARD=0
export AGENTSTACK_TCC_DIRS="$HOME/Desktop:$HOME/Documents:$HOME/Downloads"
```

`AGENTSTACK_TCC_DIRS` は `:` 区切りが正本です。colon を含まない旧 whitespace 区切りも compatibility のため引き続き受け付けます。

権限エラーを `chmod` だけで直そうとしないでください。判定主体は file mode ではなく起動元 app です。

## Upgrade

```bash
git pull
./scripts/install.sh
~/.agentstack/bin/agentstack-doctor
```

installer は payload と `VERSION` を更新し、service を再登録して、managed merge を再び preview します。同梱 ORRERY Mail の candidate と state を検証して再利用します。`--project-key` は前回の値を引き継ぎます。

**in-place upgrade 中も agent-mail server は稼働させたまま**にしてください。稼働 listener から解決した実 DB path は filesystem の候補探索より優先されます。agent-mail を先に止めると候補探索へフォールバックし、複数の DB がある環境では誤選択を避けるため installer が停止します。

dashboard port を現在の AgentStack launchd job または supervised-background pidfile 配下のプロセスが保持している場合、installer は所有者を照合してその dashboard を新しい payload で置換します。同じ port を無関係なプロセスが保持している場合は、従来どおり停止します。

service の environment は install 時に plist / unit へ書き込まれます。`~/.agentstack/env.sh` を変更しただけでは既存 service に反映されないため、installer を再実行するか service definition も更新してください。

## Uninstall

```bash
~/.agentstack/bin/agentstack-uninstall --dry-run
~/.agentstack/bin/agentstack-uninstall
```

uninstaller は `install-state.json` に記録された file、service、settings 変更だけを対象にします。

- merge した Claude settings entry を構造的に除去
- AgentStack 所有 file を削除
- 空になった所有 directory だけを削除
- ORRERY Mail state / DB と runtime directory（annotation、token、session state / log）は既定で保持

旧 `dashboard/annotations.json` は user state として payload の owned file に含めません。upgrade 時は installer が payload copy の前に `$AGENTSTACK_RUNTIME_DIR/annotations.json` へ自動移行し、通常の uninstall 後も runtime directory に保持します。

保持データも削除する場合:

```bash
~/.agentstack/bin/agentstack-uninstall --purge-data
```

`--purge-data` も manifest に記録された exact path だけを対象にし、home directory や未記録 path は削除しません。runtime directory は purge path に含まれるため、この option では annotation も削除されます。

## 付録: 以前 MCP Agent Mail を使っていた場合

はじめて入れる人には関係ありません。この節は、同梱の ORRERY Mail の元になった third-party の [MCP Agent Mail](third-party.md) を、launchd job として自前で動かしていた人向けです。

### 旧 launchd mail service の退役

旧 `mcp_agent_mail` の launchd job が endpoint を保持している場合は、明示的に `--retire-legacy-mail` を付けて installer を実行します。installer は既存 listener を ORRERY Mail として再利用できるか調べる**前**に、既知の legacy label と plist の実行内容を照合し、該当 job を bootout して plist を `~/.agentstack/parked-launchd/` へ退避します。フラグなしでは service を止めず、検出 label と同フラグを示して停止します。

`--dry-run --retire-legacy-mail` は job を実際には止めませんが、退役計画を先に表示し、その listener は退役される前提で同梱 ORRERY Mail の provision 計画を表示します。同じ installer process 内で legacy scan が再度呼ばれても二重に bootout / 退避しません。

Claude Code の旧 `mcp-agent-mail` key が同じ endpoint を指す場合、承認された設定 merge で `orrery-mail` key へ移します。別 endpoint を指す旧 key は無関係な entry として残します。

### 旧 DB の手動移行

旧 state（DB、archive、signals）を引き継ぐ場合は、先に旧 writer を止め、3 つの path を確認します。installer は自動移行しません。移行先がまだ存在しない状態で、repository checkout から migration CLI の `copy` と `verify` を手動実行し、その後に installer を走らせます。

```bash
LEGACY_DB="/absolute/path/to/the-stopped-legacy-database"
LEGACY_ARCHIVE="/absolute/path/to/the-stopped-legacy-archive"
LEGACY_SIGNALS="/absolute/path/to/the-stopped-legacy-signals"
DESTINATION="$HOME/.agentstack/mail"

uv run --project packages/agentstack_mail agentstack-mail-migrate copy \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

uv run --project packages/agentstack_mail agentstack-mail-migrate verify \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

./scripts/install.sh
```

source と destination の DB / archive を共有する構成は migration helper と service controller が拒否します。2026-08-12 の切替では、この手順で DB と archive の合計約 6 万件を実際に移送し、照合済みです。copy から verify まで旧 writer を停止したままにしてください。

### ロールバック

installer は third-party 版への自動切替を行いません。必要なら ORRERY Mail を停止し、migration backup と設定 backup を使って手動で復旧します。

## 関連文書

- [Launcher と identity](launchers.md)
- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
- [第三者コンポーネント](third-party.md)
