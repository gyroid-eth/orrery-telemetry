# ORRERY Telemetry

[English](README.en.md)

ローカルで動く Claude Code / Codex エージェント群のための、協調基盤とライブ telemetry ダッシュボードです。同梱の ORRERY Mail server をメッセージ・identity・file reservation の正本にし、tmux 上の実行状態、親子関係、通信履歴、コンテキスト残量を一つの画面に重ねます。

![ORRERY Telemetry demo](assets/demo.gif)

**ブラウザで試す（インストール不要）**: [agentstack-demo.pages.dev](https://agentstack-demo.pages.dev/) — 本物の dashboard を台本データで動かした公開デモです。台本（bug report / research）と言語を選ぶと、agent の起動・mail・context の推移・child の終了までを 4 分でループ再生し、字幕が「いま何が起きているか」を説明します。仕組みは [Dashboard](docs/dashboard.md#デモサーバー不要) を参照してください。

設計の中心は「LLM に協調を期待するだけでなく、launcher・hook・mail・可視化で運用規約を実行可能にする」ことです。

## 対応環境（Supported environments）

| 環境 | サポート |
| --- | --- |
| macOS | **対応**。launchd を使い、`gui/$UID` domain を利用できない場合は supervised background mode へ切り替えます。launcher / hook は標準 Bash 3.2 対応です。 |
| Linux | **未検証（実装あり）**。`systemd --user` を優先し、なければ supervised background mode に落ちる経路を実装していますが、実際の Linux ホストで `systemctl --user` の登録・timer 起動を通した記録はありません。検証済みなのは ubuntu-latest の CI で `systemctl` をスタブに差し替えた unit 生成テストまでです。Linux で試した結果は issue で報告してください。 |
| WSL2 | **未検証**。設計上は localhost dashboard が使え、Ghostty の click-to-jump は使えない想定ですが、実機で確認していません。 |
| Windows native | **未対応**。native 向けの貢献は、macOS 側の挙動を変えない形で受け付けます（置き場と作法は [CONTRIBUTING「Windows contributions」](CONTRIBUTING.md#windows-contributions-community-lane)）。 |
| その他の OS | **未対応**。installer の preflight が書き込み前に停止します。 |

Python は **3.11 以上**が必須です。上限は設けておらず、全 suite を実測済みなのは 3.12 / 3.13 / 3.14（CI は 3.11 / 3.12 / 3.14）です。3.10 は import こそ通るものの mail service のテストが通らないため対応外です（2026-09-04）。

必須 command は `git` と `tmux` です。agent-mail を新規 provision する場合だけ `uv` も必須です。実行時には Claude Code または Codex CLI の少なくとも一方が必要です。`systemctl` は Linux の user service 用ですが、利用できなければ supervisor が代替します。`fswatch`（mail watcher）、`fzf`（directory picker）、Ghostty、Obsidian は任意です。

installer は冒頭で OS、Python、必須 command、ORRERY Mail endpoint（既定 `127.0.0.1:18765`・state root `~/.agentstack/mail`）、install directory の書込権限をまとめて検査します。endpoint が使用中でも、既存の `install-state.json` があれば上書き更新として扱います。新規 install で使用中の場合も socket の所有者を推測して停止せず、health response と canonical database を確認できた場合だけ既存 service を再利用します。無関係または解決不能な listener なら、最初の書き込み前に停止します。

CI や isolated test で platform boundary を意図的に偽装する場合に限り、`AGENTSTACK_PREFLIGHT_SKIP_OS=1`、`AGENTSTACK_PREFLIGHT_SKIP_PYTHON=1`、`AGENTSTACK_PREFLIGHT_SKIP_COMMANDS=1`、`AGENTSTACK_PREFLIGHT_SKIP_PORT=1`、`AGENTSTACK_PREFLIGHT_SKIP_WRITABLE=1` で各検査を個別に skip できます。skip は依存を提供せず、未対応環境を対応済みに変えるものでもありません。詳しくは[インストール](docs/install.md#動作環境)を参照してください。

## クイックスタート

初回は必ず dry-run から始めます。変更予定（service mode・使う agent-mail DB・settings diff）を読んでから本番の install に進みます。

```bash
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/path/to/coordinated-project --dry-run
./scripts/install.sh --project-key /absolute/path/to/coordinated-project
```

installer は 4 回 `yes` を求めます（Claude Code settings の merge・`~/.claude.json` の MCP entry・Codex / Claude の managed instructions）。インストール後、「入っているか」と「動くか」を別々に確認します。

```bash
export PATH="$HOME/.agentstack/bin:$PATH"
agentstack-doctor      # 配置・設定・service の状態
agentstack-selftest    # agent を2体登録し、mail 往復と file reservation を実測
```

最初の agent を起動し、その中から child を1体作って dashboard で確認します。

```bash
agent-start ~/code/my-project        # または agent-start-codex ~/code/my-project
# 起動した Claude Code で:  /delegate <child に頼む作業>
open http://127.0.0.1:8770/          # 別 terminal。DECK に親と child のカードが並べば完了
```

`agent-start` は agent-mail identity と同名の tmux session を作ります。これが dashboard の jump、mail signal 配送、token recovery を一意に結びます。設定を変える場合は[インストール](docs/install.md)と[設定](docs/configuration.md)、child の仕組みは[委任と child agent](docs/delegation.md)を参照してください。

Codex Desktop の root task / subagent も同じ agent-mail と dashboard に接続する場合は、任意の [Codex App 統合](docs/codex-app.md)を追加します。Codex CLI だけを使う場合、この追加 install は不要です。

## 機能ギャラリー

### 1. Launcher と identity

`agent-start` / `agent-start-codex` が identity 登録、科学者名、tmux session、CLI 起動を一つの経路にまとめます。token は mode `0600` の runtime file に置き、継承環境からの identity hijack を防ぎます。

<!-- TODO: screenshot: launcher and registered agent -->

### 2. Hook、mail、file reservation

Claude Code hook が未登録 session と競合書き込みを止め、成功した edit の reservation を短い grace 後に解放し、agent-mail inbox signal を Claude / Codex REPL へ再注入します。mail と reservation の正本を一つに保つため、UI を再起動しても協調状態が分裂しません。

agent-mail は監査 archive の Git commit を既定で非同期 queue に積み、DB 更新と archive file の書き込みが完了した時点で tool 応答を返します。同期 commit に戻す kill switch は `AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC=false` です。hard shutdown が応答直後に重なると飛行中の commit は失われる可能性がありますが、archive file は working tree に残り、DB は影響を受けません。次回起動時に未 commit file を同期 commit して回収します。詳細と測定条件は [agentstack-mail 文書](docs/agentstack-mail.md#archive-commit-latency-and-startup-repair)を参照してください。

<!-- TODO: screenshot: agent-mail notification and reservation -->

### 3. DECK

カードごとに running / standby / finished / gone、task、model、context 残量、最後の指示、成果物を表示します。History / Output、terminal open、二段確認付き EXIT / KILL を同じ場所から操作できます。

![DECK view](docs/img/deck.jpg)

### 4. NETWORK と DIGEST REPLAY

spawn 系譜と agent-mail 通信を force graph に重ね、node、edge、role / group、mail drawer を探索できます。複数 agent を選ぶと、通信と状態遷移を速度・HOLD・TIME-TRAVEL 付きで再生できます。

![NETWORK view](docs/img/network.jpg)

![DIGEST REPLAY](docs/img/digest-replay.jpg)

### 5. Control plane と NEW AGENT

dashboard から EXIT、RESUME、REPLAY、role annotation、Claude / Codex child spawn を実行できます。登録、task 配送、token file、tmux 起動を一つの監査可能な順序に固定します。

![NEW AGENT modal](docs/img/new-agent.jpg)

### 6. API とカスタマイズ

dashboard の全表示・操作は local HTTP API から利用できます。portrait overlay、spawn directory、model catalog、terminal bridge を環境変数で構成でき、private asset は repository と分離できます。

murmur は browser の言語から日本語 / 英語を自動選択し、`?lang=` / `AGENTSTACK_LANG` で上書き、`?murmur=on|off` / `AGENTSTACK_MURMUR=off` で表示を制御できます。

<!-- TODO: screenshot: API or customized portraits -->

## ドキュメント

日本語文書が正本です。英語版の詳細文書は準備中です。

| 文書 | 内容 |
| --- | --- |
| [インストール](docs/install.md) | install tier、settings merge、VERSION、TCC、upgrade / uninstall |
| [Launcher と identity](docs/launchers.md) | `agent-start`、命名、token、fail-closed、`CLAUDECODE` |
| [委任と child agent](docs/delegation.md) | 組み込み subagent との違い、いまどちらが動いているかの見分け方、使い分け |
| [Hooks と運用 helper](docs/hooks.md) | Claude event hook 8件、launcher / watcher helper、発火条件、block / release / cleanup |
| [Codex App 統合](docs/codex-app.md) | Codex Desktop plugin、Bridge、session-bound MCP、inbox 通知、cold wake |
| [Dashboard](docs/dashboard.md) | DECK、NETWORK、SELECT、REPLAY、NEW AGENT、embed |
| [API reference](docs/api.md) | 全 route、query / request、response schema |
| [設定](docs/configuration.md) | `AGENTSTACK_*` 環境変数とカスタマイズ |
| [トラブルシューティング](docs/troubleshooting.md) | `NOT CONFIGURED`、service、通知、spawn、認証 |
| [第三者コンポーネント](docs/third-party.md) | agent-mail、license、credits |

コードへ変更を送る場合は [CONTRIBUTING.md](CONTRIBUTING.md) も参照してください。

## 仕組み

```text
Claude Code / Codex CLI
        │ launcher + hooks
        ▼
tmux session ── telemetry ──► dashboard
        │                         ▲
        │                         │ sanitized snapshot
        │                  Codex App Bridge ◄── plugin hooks ── Codex Desktop
        │                         │
        └──────── ORRERY Mail ◄────┘
                  identity / inbox / reservations
```

同梱の ORRERY Mail を正本にし、その上に launcher、運用 guard、可視化、control plane を重ねます。legacy service から切り替える場合も writable DB / archive は共有しません。dashboard が落ちても identity・mail・reservation の正本は失われません。

## License

本 repository は **PolyForm Perimeter License 1.0.1** です。source-available であり、OSI の意味での open source ではありません。全文は [LICENSE](LICENSE) を参照してください。

- 利用・改変・再配布は目的を問わず可能です
- ただし**本ソフトウェアと競合する製品を他者へ提供すること**はできません。無償配布・別言語への移植・service / library / plug-in としての提供も競合に含まれます

同梱 service が継承・派生した部分の attribution は [NOTICE](packages/agentstack_mail/NOTICE.md)、適用 license は [UPSTREAM_LICENSE](packages/agentstack_mail/UPSTREAM_LICENSE) に保持しています。ORRERY Telemetry 側で新たに書いた部分（file 名の AgentStack は旧名称）には [AGENTSTACK_LICENSE](packages/agentstack_mail/AGENTSTACK_LICENSE) が適用されます。境界の説明は[第三者コンポーネント](docs/third-party.md)を参照してください。

Windows向けの実験的な開発用起動手順: [MailとDashboard起動helper](docs/windows-local.md)。
