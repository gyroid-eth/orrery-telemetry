# ORRERY Telemetry

[English](README.en.md)

Claude Code、Codex CLI、Gemini など、提供元の異なる coding agent を複数動かすと、agent 同士をつなぐ役目は人間に回ってきます。片方の出力をもう片方に貼り直し、アプリやタブを行き来して、それぞれがいま何をしているかを確かめる。ORRERY Telemetry は、その伝言役を agent 同士の直接の通信に置き換え、全員の働きを一枚の画面で俯瞰し、必要なときにだけ人間が介入できるようにするツールです。agent 間の通信、同じファイルを同時に書き換えないための予約、誰が誰を起動したかの系譜を同梱の基盤が記録し、dashboard がそれを人の目で追える形に描きます。

![ORRERY Telemetry demo](assets/demo.gif)

**まずデモで体験する**: [agentstack-demo.pages.dev](https://agentstack-demo.pages.dev/) は、本物の dashboard を台本データで動かした公開デモです。agent が起動し、通信を交わし、child を作って終えるまでを 4 分でループ再生し、字幕が「いま何が起きているか」を説明します。インストールせずに、実際に近い画面でどんな体験が得られるかを掴めます。ここを 1 周見てから、以下に進んでください。

## 誰のためのものか

- **対象は chat ではなく coding agent を使う人**: ChatGPT のような chat ではなく、Claude Code や Codex のように、指示を受けて自律的にコードを書き、ファイルを変え、command を実行する agent を、すでに手元で動かしている人向けです。terminal からでも、Claude Desktop や ChatGPT app のような desktop app からでも構いません
- **向いている人**: agent を複数動かしていて、その連携を人間が仲介している人。Codex の出力を Claude Code に貼り直す、アプリやタブを行き来して各 agent の様子を確かめる、といった手作業をなくし、全体を一枚の画面で俯瞰したい人
- **向いていない人**: agent は 1 体で足りている人。このツールの価値は複数 agent の協調と、その観測にあります
- **対応する agent**: Claude Code と Codex CLI が中心です。Codex Desktop の task と subagent、Google Antigravity / Gemini は、core install のあとに追加できる optional provider として同じ dashboard に載せられます

## 言葉の説明

以下の 6 語だけ覚えれば、この README と docs はすべて読めます。

| 言葉 | 意味 |
| --- | --- |
| agent | terminal で動いている Claude Code / Codex CLI の 1 セッション。それぞれに科学者の名前が付きます |
| child | ある agent が「この作業をやって」と頼んで起動した別の agent。頼んだ側が親です。親が `/delegate` を使うと child が生まれ、child は終わると親に報告して消えます |
| ORRERY Mail | agent 同士がメッセージを送り合い、名前と file の予約を管理する同梱の小さなサーバー |
| dashboard | ブラウザで開く画面。全 agent の状態、親子関係、メッセージの往来を表示します |
| project key | agent たちに作業させる project フォルダの絶対パス。「どの project の agent か」を区別する鍵です |
| skill | agent に「こういう頼まれ方をしたらこの手順で動け」と教える手順書。`/delegate` のように先頭に slash を付けて呼びます。このツールは `/delegate` と `/log` の 2 つを同梱します（[下で説明](#同梱する-2-つの-skill)） |

## クイックスタート

macOS と Windows で同じ手順です。必要なのは Python 3.11 以上、`git`、`tmux`、`uv` で、macOS なら `brew install tmux uv` で揃います。

**Windows の人へ**: WSL2 の Ubuntu の中に入れます。Ubuntu の中は Linux なので、以下の手順がそのまま使え、dashboard は Windows のブラウザで、agent の terminal は Windows Terminal のタブで開きます。WSL2 の準備から Claude Code / Codex のログインまでを順に書いた手順が[インストールの WSL2 節](docs/install.md#windowswsl2で入れる)にあるので、Windows の人はまずそこを開いてください。

### 1. 入れる

```bash
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/path/to/your-project
```

`--project-key` には、agent に作業させたい project フォルダの絶対パスを渡します。この repository 自体のパスではありません。

installer は、あなたの Claude Code / Codex の設定に触れる前に変更内容を表示し、合計 4 回 `yes` を求めます。既存の設定は保持し、変更前の backup を `~/.agentstack/backups` に置きます。先に変更内容だけ見たいときは `--dry-run` を付けます。

**成功**: 最後に `Install complete: http://127.0.0.1:8770/` と出て、`~/.agentstack/` ができています。

### 2. 動くか確かめる

```bash
export PATH="$HOME/.agentstack/bin:$PATH"
agentstack-doctor
agentstack-selftest
```

**成功**: `agentstack-doctor` の各行が `ok:` で始まり（`warn:` は直し方がその下に書かれます）、`agentstack-selftest` が `self-test passed: two agents registered, exchanged messages, ...` で終わります。どこかで止まったら[トラブルシューティング](docs/troubleshooting.md)の該当節へ進んでください。

### 3. 最初の agent を起動し、dashboard で見る

```bash
agent-start ~/code/my-project          # Codex CLI なら agent-start-codex ~/code/my-project
```

別の terminal で dashboard を開きます。

```bash
open http://127.0.0.1:8770/
```

**成功**: いつもの Claude Code または Codex が起動し、dashboard の DECK に科学者名の付いたカードが 1 枚現れます。カードには model と context 残量が表示されます。

### 4. child を 1 体作る

起動した agent の中で、次のように頼みます。Claude Code でも Codex でも同じです。

```text
/delegate child agent を作って、自分の名前と今日の日付を答えさせて
```

**成功**: dashboard に 2 枚目のカードが現れ、親から child へ線が引かれます。child が終わると、親の terminal に「完了しました」というメッセージが届きます。NETWORK タブを開くと、2 体の間のメッセージの往来が見えます。

### 5. しりとりで通しの確認をする

install が本当にできたかを一度に確かめるには、Claude Code と Codex の child にしりとりをさせるのが手軽です。名前の登録、ORRERY Mail の往復、通知の差し込み、dashboard の描画がすべて動いていないと、しりとりは一巡もしません。

```text
/delegate Codex の child を 1 体作り、その child としりとりをしてください。1 ターンごとに ORRERY Mail で単語を送り合い、10 往復したら結果を報告してください
```

Codex から始めるなら child は Claude Code にします。どちらから始めても、2 社の agent の間でしりとりが回ることを確かめるのが目的です。

**成功**: NETWORK に 2 体の間を往復する線が流れ続け、DECK の両カードの最後の指示が単語ごとに更新されます。作者の環境では 1 人あたり 1 ターン 5〜6 秒で回りました（[動画つきの投稿](https://x.com/i/status/2095650715008168255)、倍速再生）。

ここまで通れば、あとは普段どおり agent を使うだけです。詳しい設定は[インストール](docs/install.md)と[設定](docs/configuration.md)、child の仕組みは[委任と child agent](docs/delegation.md)を参照してください。

## 同梱する 2 つの skill

skill は、agent に渡す手順書です。Claude Code はこれを `~/.claude/skills/` から自動で見つけ、`/delegate` のように先頭に slash を付けて呼ぶとその手順どおりに動きます。Codex では installer が置く管理下の指示（`~/.codex/AGENTS.md`）が同じ手順書の場所を教えるので、こちらも `/delegate` と打てば同じ手順で動きます。

### `/delegate` : 仕事を child に頼む

`/delegate <頼みたいこと>` と打つと、agent は新しい child を 1 体起動し、頼んだ内容を渡し、child が終わるまで見守り、結果を受け取ります。裏では child の名前登録、触るファイルの予約、tmux session の作成、完了報告の受け取りまでを一続きで行うので、child は起動した瞬間から dashboard に載り、他の agent と同じファイルをぶつけずに動きます。

Claude Code にも Codex にも、もともと「subagent」という似た仕組みがありますが、そちらで作った子は dashboard に載りません。ORRERY Telemetry で見守りたい子は、必ず `/delegate` で作ってください。違いの詳しい説明は[委任と child agent](docs/delegation.md)にあります。

### `/log` : この session で何をしたかを残す

`/log` と打つと、agent はその session で決めたこと、変えたファイル、確かめたこと、次にやることを 1 本の Markdown に整理して `logs/` に書きます。Obsidian を使っている人は、環境変数を 1 つ設定すると vault の中に書いて Daily Note からリンクされるようになります（[設定](docs/configuration.md)）。dashboard の各カードの Output に並ぶのが、この log です。

skill の置き場所と仕組みは [Launcher と identity](docs/launchers.md#skills2件と-file-reservation) を参照してください。

## 何が見えるか

### DECK

agent 1 体が 1 枚のカードです。running / standby / finished / gone の状態、いま何をしているか、model、context 残量、最後の指示、成果物を表示します。カードから terminal を開いたり、二段確認付きで agent を終了させたりできます。

![DECK view](docs/img/deck.jpg)

### NETWORK と DIGEST REPLAY

誰が誰を起動し、誰が誰にメッセージを送ったかを、線で結んだ図として表示します。複数 agent を選ぶと、通信と状態の変化を速度を変えながら再生でき、時間を巻き戻すこともできます。

![NETWORK view](docs/img/network.jpg)

![DIGEST REPLAY](docs/img/digest-replay.jpg)

### NEW AGENT

dashboard から新しい agent を起動できます。Claude / Codex、model、作業フォルダ、頼む内容を指定して `Spawn` を押すだけです。既存 agent の終了、再開、役割ラベル付けも同じ画面から行えます。

![NEW AGENT modal](docs/img/new-agent.jpg)

### 裏で動いているもの

- **ORRERY Mail**: agent の名前、inbox、file の予約を一つに管理します。dashboard を落としてもここに正本が残ります
- **launcher**: `agent-start` が名前の登録、tmux session の作成、CLI の起動を一続きで行い、dashboard からの jump や通知の宛先が一意に決まるようにします
- **hook**: Claude event hook 8件が、登録なしの session や予約なしの書き込みを止め、届いたメッセージを agent の入力欄に差し込みます

これらの仕組みと API の詳細は [Hooks](docs/hooks.md)、[Launcher](docs/launchers.md)、[API reference](docs/api.md) にあります。dashboard の表示・操作はすべて local HTTP API から使えます。

## 対応環境

| 環境 | サポート |
| --- | --- |
| macOS | 対応 |
| Windows（WSL2） | 対応。Windows 11 + Ubuntu 26.04 / WSL 2.7 で、install から child の起動、dashboard からの terminal jump まで実機確認済み。手順は[インストールの WSL2 節](docs/install.md#windowswsl2で入れる) |
| Linux | 利用報告あり。Ubuntu 24.04 / tmux 3.4 で install、dashboard、Codex agent が動いたという実機報告を受け、そこで見つかった 2 件（[#26](https://github.com/gyroid-eth/orrery-telemetry/issues/26)、[#27](https://github.com/gyroid-eth/orrery-telemetry/issues/27)）は修正済みです。`systemd --user` での常駐登録は作者側で未検証なので、結果は issue で報告してください |
| Windows native（WSL2 なし） | 正式対応は WSL2 経由ですが、community の貢献で PowerShell から Mail と dashboard を起動する helper と、Codex child の native 起動が実験的に動きます（[起動 helper](docs/windows-local.md)、[Codex launcher](docs/windows-codex-launcher.md)）。方針は [#3](https://github.com/gyroid-eth/orrery-telemetry/issues/3) |

Python 3.11 以上、`git`、`tmux`、`uv` が必須で、実行時には Claude Code か Codex CLI の少なくとも一方が要ります。installer は書き込む前にこれらを検査して、足りなければ何も変えずに止まります。検査の詳細と任意の依存（`fswatch`、`fzf`、Ghostty、Obsidian）は[インストールの動作環境](docs/install.md#動作環境)を参照してください。

## ドキュメント

日本語文書が正本です。主要文書には英語版があります。

| 文書 | 内容 |
| --- | --- |
| [インストール](docs/install.md) | 動作環境、install の詳細、WSL2、upgrade / uninstall |
| [Launcher と identity](docs/launchers.md) | `agent-start`、命名、token、`CLAUDECODE` |
| [委任と child agent](docs/delegation.md) | 組み込み subagent との違い、いまどちらが動いているかの見分け方 |
| [Hooks と運用 helper](docs/hooks.md) | Claude event hook 8 件、発火条件、block / release / cleanup |
| [Codex App 統合](docs/codex-app.md) | Codex Desktop の root task / subagent を同じ dashboard に載せる |
| [Google Antigravity / Gemini provider](docs/antigravity.md) | optional provider の導入と制約 |
| [Dashboard](docs/dashboard.md) | DECK、NETWORK、SELECT、REPLAY、NEW AGENT、embed |
| [API reference](docs/api.md) | 全 route、query / request、response schema |
| [設定](docs/configuration.md) | `AGENTSTACK_*` 環境変数とカスタマイズ |
| [トラブルシューティング](docs/troubleshooting.md) | `NOT CONFIGURED`、service、通知、spawn、認証 |
| [デザイン言語](docs/design.md) | dashboard の見え方と動きの正本。UI を足す前に読む |
| [第三者コンポーネント](docs/third-party.md) | ORRERY Mail、license、credits |

同梱サーバーの内部構成は [ORRERY Mail の設計文書](docs/agentstack-mail.md)、コードへ変更を送る場合は [CONTRIBUTING.md](CONTRIBUTING.md)（英語）も参照してください。

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

同梱の ORRERY Mail を正本にし、その上に launcher、運用 guard、可視化、control plane を重ねます。dashboard が落ちても identity・mail・reservation の正本は失われません。

## License

本 repository は **PolyForm Perimeter License 1.0.1** です。source-available であり、OSI の意味での open source ではありません。全文は [LICENSE](LICENSE) を参照してください。

- 利用・改変・再配布は目的を問わず可能です
- ただし**本ソフトウェアと競合する製品を他者へ提供すること**はできません。無償配布・別言語への移植・service / library / plug-in としての提供も競合に含まれます

同梱 service が継承・派生した部分の attribution は [NOTICE](packages/agentstack_mail/NOTICE.md)、適用 license は [UPSTREAM_LICENSE](packages/agentstack_mail/UPSTREAM_LICENSE) に保持しています。ORRERY Telemetry 側で新たに書いた部分（file 名の AgentStack は旧名称）には [AGENTSTACK_LICENSE](packages/agentstack_mail/AGENTSTACK_LICENSE) が適用されます。境界の説明は[第三者コンポーネント](docs/third-party.md)を参照してください。
