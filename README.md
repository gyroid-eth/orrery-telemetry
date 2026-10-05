# ORRERY Telemetry

[English](README.en.md)

複数の Claude Code / Codex が直接話し、仕事を分担し、結果を確かめ合う。その働きと Mail の往復を一枚の画面で見渡せるのが ORRERY Telemetry です。端末も同じ画面で使う [ORRERY cockpit](https://github.com/gyroid-eth/orrery) と組み合わせて使えます。

![NETWORK を開いたまま子が加わり、ready の後にしりとりの Mail が3往復する](docs/img/network-mail.gif)

## Telemetry だけを入れる

この repository だけで入る、裏で動く側です。Python 3.11 以上、`git`、`tmux`、`uv` が要ります（Mac なら `brew install tmux uv`）。Windows は WSL2 の Ubuntu 内で実行します。

```bash
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/path/to/your-project
```

`--project-key` は、agent に作業させる folder の絶対パスです（この repository ではありません）。変更内容は表示され、承認してから入ります。終わったら `~/.agentstack/bin/agentstack-doctor` と `~/.agentstack/bin/agentstack-selftest` で確かめ、`~/.agentstack/bin/agent-start <project folder>` で agent を起動して `http://127.0.0.1:8770/` を開きます。手順は[単独の導入手順](docs/getting-started.md#telemetry-単独の導入と確認)にあります。

**単独で使えるもの**: dashboard の Telemetry 画面（DECK・NETWORK・REPLAY・退出した agent の RESUME・NEW AGENT）、`agent-start` での agent 起動、`/delegate` での子の作成、agent 同士の ORRERY Mail。agent の端末は OS の terminal（Windows は Windows Terminal のタブ）で開きます。

**入らないもの**: ブラウザの中で端末を並べて操作する cockpit の画面（composer・Split・Planetarium・`ORRERY.app`）。

## cockpit との関係

- **Telemetry** は、agent・Mail・dashboard を動かす裏方と、その状態を見る Telemetry 画面です。
- **[cockpit](https://github.com/gyroid-eth/orrery/blob/master/README.md)** は、端末を並べて操作する画面です。agent の起動・Mail・Telemetry 画面は Telemetry が担うので、cockpit は Telemetry と組み合わせて使います（端末だけなら Telemetry が止まっていても開けます）。
- cockpit 側から入れると、**両方まとめて入ります。**

## 両方入れるなら（orrery の 1 行）

Mac のターミナル、または WSL2 Ubuntu 内で、次の 1 行です。cockpit と Telemetry を一緒に入れます。

```bash
curl -fsSL https://raw.githubusercontent.com/gyroid-eth/orrery/master/scripts/get.sh | bash
```

入る場所（`~/orrery`・`~/orrery-telemetry`・`~/.agentstack`・`~/orrery-work`）と port、作業 folder の変え方、完全に消す手順は、[orrery の README](https://github.com/gyroid-eth/orrery/blob/master/README.md#クイックスタート)にあります。単独で入れた Telemetry の取り除き方は[Uninstall](docs/install.md#uninstall)です。

## 画面のガイドで始める

cockpit の **Your first flight → help map → Full tour** を順に試します。初回に右側へ出る7項目のガイドで基本を押さえ、`Settings → Getting started → Show help map` で主要な部品を見回し、同じ場所の `Full tour` で16段を順に触ります。

Telemetry の header の `HELP MAP`（NETWORK では `SETTINGS` の隣）を押すと、いま表示されている DECK / NETWORK の操作に注記が付きます。agent の詳細パネルでは、操作列の `HELP MAP` を押すと、そのパネルの操作に注記が付きます。cockpit 内に埋め込まれた Telemetry でも同じ操作で使えます。注記が収まらない場合は凡例で表示し、`Esc` または `Close` で閉じられます。

Full tour は `/delegate` で子を1体作り、ORRERY Mail でしりとりを3往復し、端末の配置、Telemetry の EXIT / RESUME、NETWORK / REPLAY を試す案内です。画面の説明に従って操作すると進みます。[現在のクイックスタート](https://github.com/gyroid-eth/orrery/blob/master/README.md#クイックスタート)と [Full tour](https://github.com/gyroid-eth/orrery/blob/master/docs/FULL_TOUR.md)を参照してください。

端末で仕事をするのは cockpit、チームの状態や履歴を見るのが Telemetry です。画面内のガイドを入口にし、詳しい参照は[目的別索引](docs/README.md)から開けます。

## 退出した agent を再開する

Telemetry の DECK の history を `7D` / `30D` / `ALL`、または NETWORK の範囲を `ALL` にすると、退出した agent の履歴も探せます。再開できる agent の詳細パネルで **RESUME** を押すと、同じ仕事に戻ります。履歴・認証・元の作業フォルダなどを検証できない場合は、理由を表示して再開を止めます。

Telemetry 単体での例: 名前で絞り込み、`7D` の `RETIRED` / `RESUME READY` の agent を開いて `RESUME`。会話を tmux で再開したという通知の後、`LIVE` に戻して `ONLINE` を確認します。

![Telemetry 単体で7Dの退出した agent を再開し、LIVEでONLINEへの復帰を確認する](docs/img/resume-retired-agent.gif)

[Dashboard の再開手順](docs/dashboard.md#検索)と[Launcher の Claude resume](docs/launchers.md#dashboard-からの-claude-resume)に、検証が必要な場合と復帰できない場合の扱いがあります。

## 困ったとき・更新する

- 動かない: `~/.agentstack/bin/agentstack-doctor` で不足箇所、`~/.agentstack/bin/agentstack-selftest` で Mail の往復を確認し、[トラブルシューティング](docs/troubleshooting.md)へ。
- 更新: cockpit と一緒なら orrery の 1 行、Telemetry だけなら[Upgrade](docs/install.md#upgrade)。取り除く手順は[Uninstall](docs/install.md#uninstall)。
- Windows: [WSL2 の準備とログイン](docs/install.md#windowswsl2で入れる)。native Windows の helper は[実験的な別経路](docs/windows-local.md)です。
- 対応環境・用語・単独での起動: [getting-started](docs/getting-started.md)。

## ドキュメント

日本語文書が正本です。英語の入口は [English index](docs/README.en.md)。

| 読みたいこと | 入口 |
| --- | --- |
| 目的から資料を探す | [全 docs の索引](docs/README.md) |
| Telemetry 単独で始める・言葉を調べる | [Getting started](docs/getting-started.md) |
| install・update・要件を確認する | [インストール](docs/install.md) |
| 困ったところを調べる | [トラブルシューティング](docs/troubleshooting.md) |

[Obsidian と一緒に使う](docs/obsidian.md)と公開の [demo vault](https://github.com/gyroid-eth/orrery-demo-vault)で、作業ログ・論文ノート・タスクをノートにできます。Obsidian は必須ではありません。CLI 以外の [Codex App](docs/codex-app.md)、[Antigravity / Gemini](docs/antigravity.md)は optional provider です。

install 前に雰囲気を見るなら[公開デモ](https://agentstack-demo.pages.dev/)、考え方の図解は[紹介動画](https://youtu.be/JXoa93TQolU)。開発への変更は [CONTRIBUTING.md](CONTRIBUTING.md)を参照してください。

## License

本 repository は **PolyForm Perimeter License 1.0.1** です。source-available であり、OSI の意味での open source ではありません。全文は [LICENSE](LICENSE) を参照してください。

- 利用・改変・再配布は目的を問わず可能です
- ただし**本ソフトウェアと競合する製品を他者へ提供すること**はできません。無償配布・別言語への移植・service / library / plug-in としての提供も競合に含まれます

同梱 service が継承・派生した部分の attribution は [NOTICE](packages/agentstack_mail/NOTICE.md)、適用 license は [UPSTREAM_LICENSE](packages/agentstack_mail/UPSTREAM_LICENSE) に保持しています。ORRERY Telemetry 側で新たに書いた部分（file 名の AgentStack は旧名称）には [AGENTSTACK_LICENSE](packages/agentstack_mail/AGENTSTACK_LICENSE) が適用されます。境界の説明は[第三者コンポーネント](docs/third-party.md)を参照してください。

dashboard の科学者の肖像（`dashboard/portraits_pixel/` と、その 64 px 縮小の `dashboard/portraits_64/`）は、作者（gyroid）が ChatGPT の画像生成で文章だけから作ったドット絵です。写真は入力していません。この repository と同じ条件で配布します。来歴は[第三者コンポーネント](docs/third-party.md#肖像画像)に記録しています。
