# インストール

> English version: planned.

[README に戻る](../README.md) · [次: Launcher と identity](launchers.md)

## 動作環境

主対象は macOS です。launcher と hook は macOS 標準 Bash 3.2 でも動くよう実装されています。

必須:

- Python 3.10 以上（`python3`）。全 suite を実測済みなのは 3.10 / 3.12 / 3.13 / 3.14 です。上限は設けていません（CI が 3.10・3.12・3.14 を毎回回すので、新しい Python で壊れた場合はそこで落ちます）
- `tmux`
- `git`
- `uv`
- Claude Code または Codex CLI

任意:

- `fswatch`: mail watcher。なければ 2 秒間隔の polling に fallback します（通知は届きます）
- `fzf`: 引数なし launcher の directory picker。なければカレントディレクトリを使います
- Ghostty: click-to-jump と window title。iTerm2、Terminal.app、`none` へ fallback。ただし既存ウィンドウの前面化は Ghostty のみで、iTerm2 と Terminal.app では jump のたびに新しいウィンドウが開きます
- Obsidian: `/log` の vault / Daily Note 統合と、vault 内 Output item を開く link。`/log` の Obsidian モードは `AGENTSTACK_OBSIDIAN_APP` を設定して初めて有効になります（installer は設定しません）。未設定なら `/log` はローカルの `logs/` に書き、dashboard は generic project log を非リンク項目として表示します

macOS では launchd の `gui/$UID` domain への実際の bootstrap 成否で常駐経路を選びます。画面スリープ中や SSH 専用環境などで bootstrap できない場合は、dashboard server の終了を検知して再起動する supervised background mode に自動で切り替えます。Linux では systemd user service、利用できなければ同じ supervised background mode を使います。WSL2 でも localhost dashboard は使えますが、Ghostty の click-to-jump は使えません。Windows native は対象外です。

`AGENTSTACK_PYTHON` を指定した場合も Python 3.10 以上か検証します。未指定時は PATH 上の `python3` を検査し、不適格なら version 付き command や `/opt/homebrew/bin/python3`、`/usr/local/bin/python3` も探索します。互換 interpreter がなければ、サービス file を生成する前に検査した version と path を示して停止します。

## 基本インストール

```bash
git clone https://github.com/gyroid-eth/claude-agent-stack.git
cd claude-agent-stack
./scripts/install.sh
```

非対話実行では、既定のままなら Tier 1 settings merge と Codex / Claude managed block を警告付きでスキップします。repository と preview 内容を確認した**ユーザー本人**が、これらの承認を事前に明示する場合だけ次を使えます。

```bash
./scripts/install.sh --assume-yes
```

`--assume-yes` は approval の事前付与であり、`--force` ではありません。Python 3.10 未満、dashboard port の競合、agent-mail DB の未解決・不存在・稼働 server との不一致は従来どおり停止します。自動承認した settings merge、managed block、既存 agent-mail server の利用は `assume-yes:` 行として個別に出力されます。agent や自動化が「便利だから」とユーザーの明示選択なしにこの option を追加してはいけません。

環境変数 `AGENTSTACK_ASSUME_YES=1` も同じ明示 opt-in です。command-line の `--assume-yes`（短縮 `-y`）は環境変数より優先されます。この installer 選択は生成する `env.sh` には永続化しません。

installer は次を行います。

1. dependency、dashboard port、agent-mail endpoint を検査
2. 稼働中の agent-mail server があれば再利用を確認し、その server の実 DB path を health response、listener process、既存 DB 候補から解決。server がなければ実在する DB を探索してから upstream clone を検証
3. `~/.agentstack` に dashboard、launcher、hook、skill、managed instruction template、`VERSION` を配置し、Claude skill を `~/.claude/skills` から参照できるようにする
4. `~/.agentstack/env.sh` を生成
5. Tier 1 では Claude Code settings と managed instructions の差分を preview し、対話で明示した `yes` またはユーザーが事前に選んだ `--assume-yes` の場合だけ merge
6. launchd / systemd user / supervised background のいずれかで dashboard を起動し、実際の方式を `install-state.json` に記録

agent-mail の既定 SQLite URL は server の current working directory 相対です。installer は `AGENTSTACK_MAIL_DIR/storage.sqlite3` を実体確認なしで採用しません。既定 endpoint（`127.0.0.1:8765`）がすでに LISTEN していれば agent-mail の health response を検証し、対話実行ではその既存 server と DB を使うか確認します。非対話実行では検出結果を表示して再利用します。DB を一意に解決できない場合は、存在しない path を設定せず `AGENTSTACK_MAIL_DB` の明示を求めて停止します。

サービス登録や health check が失敗しても、payload、承認済み managed block、`install-state.json` の生成は完了します。installer は warning と supervised background の手動起動コマンドを最後に表示します。実際の常駐方式は `~/.agentstack/dashboard/agentctl.sh status` と `agentstack-doctor` で確認できます。

完了後:

```bash
~/.agentstack/bin/agentstack-doctor
open http://127.0.0.1:8770/
```

install 前から開いていた Claude Code session は、新しく導入した skill を再走査しません。既存 session で `/exit` した後、新しい terminal から project を指定して起動し直します。

```bash
agent-start /path/to/project
```

再起動後は Claude Code で `/delegate` のように先頭の slash を付けて呼び出します。初回の child 起動は [Skills と file reservation](launchers.md#skills2件と-file-reservation) を参照してください。

生成する `env.sh` は mode `0600` です。bearer token は `env.sh` へ書き込みません。

## Install tier

| 呼び出し | Tier | 内容 |
| --- | --- | --- |
| `./scripts/install.sh` | Tier 1 / default | 全 payloadと Claude standard skill link。hooks・permissions と Codex / Claude managed block は preview 後、承認時だけ merge |
| `./scripts/install.sh --dashboard-only` | Tier 0 | dashboard と helper のみ。hooks、skills、Codex / Claude template は導入しない |
| `./scripts/install.sh --scoped` | Tier 2 placeholder | payload は導入するが、user settings / managed docs は変更しない |
| `./scripts/install.sh --dry-run` | preview | 変更予定を表示し、file や service を変更しない |

`--dashboard-only` と `--scoped` は排他的です。不明 option や値不足は変更前に停止します。

## Installer option

```text
--install-dir PATH      default: ~/.agentstack
--project-key PATH      default: AGENTSTACK_PROJECT_KEY, PROJECT_KEY, repo root
--port PORT             default: 8770
--label-prefix PREFIX   default: org.agentstack
--terminal MODE         auto | ghostty | iterm | terminal | none
-y, --assume-yes        approval prompts only; validation errors remain fatal
```

`--bin-dir` は installer の公開 option ではありません。permissions template の `__AGENTSTACK_BIN_DIR__` を展開するため、installer が内部で `agentstack-merge-settings --bin-dir "$INSTALL_DIR/bin"` を呼びます。

## Settings、permissions、Claude skill の merge

Tier 1 の merge は `scripts/lib/merge_settings.py` による JSON parser ベースです。

- 既存の hooks、permissions、その他の user settings を保持
- AgentStack が追加する値だけを重複なしで追記
- merge 前の settings backup を `~/.agentstack/backups` に保存
- 追加した entry と変更結果を manifest に記録
- managed block は marker 間だけを idempotent に更新
- `install-state.json` を uninstall の削除範囲の正本にする

単純な文字列置換ではなく構造を読んで merge するのは、再インストールと uninstall でユーザー設定を巻き込まないためです。

`skillsDirectories` は Claude Code の setting ではなく、installer は新しい値を追加しません。skill payload の正本は `~/.agentstack/skills/<name>` のままにし、Claude Code が標準で読む `~/.claude/skills/<name>` へ絶対 symlink を作ります。

```text
~/.claude/skills/delegate -> ~/.agentstack/skills/delegate
~/.claude/skills/log      -> ~/.agentstack/skills/log
```

同じ AgentStack payload を指す symlink がすでにある場合は再利用し、manifest に所有登録します。この link は payload と一緒に無効になるため、uninstall では削除対象です。同名の file、directory、または別 target の symlink がある場合は warning を出して保持し、所有登録しません。

uninstall は manifest の path と実際の symlink target を照合し、所有登録された、AgentStack payload を指す symlink だけを削除します。利用者が file や directory に置き換えた path、または retarget した symlink は残します。

旧 installer が `skillsDirectories` に `~/.agentstack/skills` を追加していた環境では、Tier 1 の settings merge を承認した再インストール時にその旧 AgentStack entry だけを削除します。同じ配列の他の user value と、それ以外の settings は保持します。

installer は `~/.claude.json` と shell dotfile を変更しません。project 内では、Tier 1 の preview 後に承認した場合だけ `CLAUDE.md` の managed marker 間を更新し、それ以外の file は変更しません。Claude Code user settings の既定位置は `~/.claude/settings.json` で、`AGENTSTACK_CLAUDE_SETTINGS` で変更できます。

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

installer は payload と `VERSION` を更新し、service を再登録して、managed merge を再び preview します。既存 clone の `mcp_agent_mail` は remote URL を確認して再利用します。

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
- agent-mail clone、DB、`.env`、runtime directory（annotation、token、session state / log）は既定で保持

旧 `dashboard/annotations.json` は user state として payload の owned file に含めません。upgrade 時は installer が payload copy の前に `$AGENTSTACK_RUNTIME_DIR/annotations.json` へ自動移行し、通常の uninstall 後も runtime directory に保持します。

保持データも削除する場合:

```bash
~/.agentstack/bin/agentstack-uninstall --purge-data
```

`--purge-data` も manifest に記録された exact path だけを対象にし、home directory や未記録 path は削除しません。runtime directory は purge path に含まれるため、この option では annotation も削除されます。

## 関連文書

- [Launcher と identity](launchers.md)
- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
- [第三者コンポーネント](third-party.md)
