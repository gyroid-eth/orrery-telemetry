# インストール

> English version: planned.

[README に戻る](../README.md) · [次: Launcher と identity](launchers.md)

## 動作環境

主対象は macOS です。launcher と hook は macOS 標準 Bash 3.2 でも動くよう実装されています。

必須:

- `python3`
- `tmux`
- `git`
- `uv`
- Claude Code または Codex CLI

任意:

- `fswatch`: mail watcher。なければ 2 秒間隔の polling に fallback します（通知は届きます）
- `fzf`: 引数なし launcher の directory picker。なければカレントディレクトリを使います
- Ghostty: click-to-jump と window title。iTerm2、Terminal.app、`none` へ fallback。ただし既存ウィンドウの前面化は Ghostty のみで、iTerm2 と Terminal.app では jump のたびに新しいウィンドウが開きます
- Obsidian: `/log` の vault / Daily Note 統合と、vault 内 Output item を開く link。`/log` の Obsidian モードは `AGENTSTACK_OBSIDIAN_APP` を設定して初めて有効になります（installer は設定しません）。未設定なら `/log` はローカルの `logs/` に書き、dashboard は generic project log を非リンク項目として表示します

Linux では systemd user service、利用できなければ `nohup` で dashboard を起動します。WSL2 でも localhost dashboard は使えますが、Ghostty の click-to-jump は使えません。Windows native は対象外です。

## 基本インストール

```bash
git clone https://github.com/gyroid-eth/claude-agent-stack.git
cd claude-agent-stack
./scripts/install.sh
```

installer は次を行います。

1. dependency と port を検査
2. upstream `mcp_agent_mail` を既定の `~/mcp_agent_mail` へ clone、または既存 clone を検証
3. `~/.agentstack` に dashboard、launcher、hook、skill、managed instruction template、`VERSION` を配置
4. `~/.agentstack/env.sh` と `install-state.json` を生成
5. launchd / systemd user / `nohup` のいずれかで dashboard と watcher を登録
6. Tier 1 では Claude Code settings と managed instructions の差分を preview し、明示的な `yes` の後だけ merge

完了後:

```bash
~/.agentstack/bin/agentstack-doctor
open http://127.0.0.1:8770/
```

生成する `env.sh` は mode `0600` です。bearer token は `env.sh` へ書き込みません。

## Install tier

| 呼び出し | Tier | 内容 |
| --- | --- | --- |
| `./scripts/install.sh` | Tier 1 / default | 全 payload。hooks・permissions・`skillsDirectories` と Codex / Claude managed block は preview 後、承認時だけ merge |
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
```

`--bin-dir` は installer の公開 option ではありません。permissions template の `__AGENTSTACK_BIN_DIR__` を展開するため、installer が内部で `agentstack-merge-settings --bin-dir "$INSTALL_DIR/bin"` を呼びます。

## Settings と permissions の merge

Tier 1 の merge は `scripts/lib/merge_settings.py` による JSON parser ベースです。

- 既存の hooks、permissions、`skillsDirectories` を保持
- AgentStack が追加する値だけを重複なしで追記
- merge 前の settings backup を `~/.agentstack/backups` に保存
- 追加した entry と変更結果を manifest に記録
- managed block は marker 間だけを idempotent に更新
- `install-state.json` を uninstall の削除範囲の正本にする

単純な文字列置換ではなく構造を読んで merge するのは、再インストールと uninstall でユーザー設定を巻き込まないためです。

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
