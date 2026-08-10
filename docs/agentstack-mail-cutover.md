# AgentStack Mail 通常切替手順（未実行）

これは maintainer の Mac で上から順に実行する切替手順の下書きである。現状の `agentstack-mail` は loopback HTTP console までで、service lifecycle、installer、データ移送、client 一括切替、切り戻しが未実装である。このため、以下には実在しないコマンドを書かず、書けない箇所を明示する。稼働中の `~/mcp_agent_mail`、MCP 設定、サービスにはまだ触れない。

## 移送先

| 対象 | 現在 | 切替先 |
|---|---|---|
| service / port | `mcp_agent_mail` / `127.0.0.1:8765` | `agentstack_mail` / `127.0.0.1:18765` |
| SQLite | `~/mcp_agent_mail/storage.sqlite3` | `~/.agentstack/mail/storage.sqlite3` |
| Git archive | `~/.mcp_agent_mail_git_mailbox_repo` | `~/.agentstack/mail/archive` |
| signals | `~/.mcp_agent_mail/signals` | `~/.agentstack/mail/signals` |

新 service の開発用 URL は `http://127.0.0.1:18765/mcp` である。

## 1. 新 service を旧 service と別にインストールする

実行コマンド: **製品 installer が未実装なので書けない。** Package は wheel/editable install でき、console script `agentstack-mail` も生成されるが、専用 venv、service 定義、install manifest がない。`~/.agentstack/mail` は install 先ではなくデータの既定 root である。既存の `scripts/install.sh` は旧 `~/mcp_agent_mail` を導入するため、この切替には使わない。

これを実行したら何が変わるか: 旧 service を動かしたまま、新 package と service 定義だけが別 namespace に追加される。

## 2. 旧 writer を止め、DB と archive を複製する

実行コマンド: **未実装なので書けない。** 必要なのは、旧 writer を確実に停止してから上表の SQLite と Git archive を切替先へ copy し、schema、ID、timestamp、recipient、receipt、reservation、Git archive の対応を照合する一つの migration コマンドである。稼働中 SQLite への単純な `cp` や、DB と別時点になる `rsync` はこのコマンドの代わりにならない。

Signals は永続データではなく message 到着時の wakeup なので、旧 signal を新 root へ replay するか破棄するかを migration 実装で固定する必要がある。現状はその処理もなく、copy コマンドを書けない。

これを実行したら何が変わるか: 旧 root は復旧用に保持されたまま書込みを止め、新 root に同じ durable record を持つ独立 copy ができる。

## 3. 新 service を起動する

開発用の foreground 起動は `agentstack-mail` で実在する。ただし切替に使う `agentstack-mail service start|status|stop|restart` は未実装なので、ここで必要な service 起動コマンドはまだ書けない。宣言だけある service 名は macOS が `org.agentstack.mail`、systemd が `agentstack-mail.service` である。

これを実行したら何が変わるか: `127.0.0.1:18765` で新 service だけが新 DB/archive/signal root の writer になる。

## 4. 全 consumer を新 MCP key へ一括で向ける

実行コマンド: **未実装なので書けない。** 一括切替 helper が、少なくとも次を同じ transaction で変更する必要がある。

- Claude: `~/.claude.json` の `.mcpServers["mcp-agent-mail"]` を削除し、`.mcpServers["agentstack-mail"]` を新 URL で追加する。`~/.claude/settings.json` の旧 `mcp__mcp-agent-mail__*` permissions と hook matcher も exact 22-tool の新 prefix に更新する。
- Codex: `~/.codex/config.toml` の `[mcp_servers.agent-mail]` を削除し、`[mcp_servers."agentstack-mail"]` を新 URL と新 token source で追加する。
- AgentStack: bridge、launcher、hooks、watcher、skills と child proxy に埋め込まれた旧 key、URL、env、signal root を同じ切替へ含める。Bridge 自身の client key `agentstack` は変更しない。

旧 `mcp-agent-mail` key を新 service の alias として残さない。ファイルを個別に手編集すると一部 consumer が旧 writer に残るため、手作業では切り替えない。

これを実行したら何が変わるか: Claude、Codex、Bridge と子 agent の全経路が同時に `agentstack-mail` を使う。

## 5. Client を再起動し、実送受信を確認する

実行コマンド: **未実装なので書けない。** 新 service 用 selftest は、テスト用 agent を登録し、1回 `send_message`、1回 `fetch_inbox` を実行して同じ message ID と本文を確認する1コマンドであるべきだが、現在の `scripts/selftest.py` は旧 key と旧 endpoint を検査するため代用できない。Claude/Codex の再起動も一括切替 helper の出力に従う必要がある。

これを実行したら何が変わるか: 再起動した実 client が新 service 経由で message を永続化し、その message を新 service から読み戻す。

## 6. 問題があれば一箇所から切り戻す

戻す箇所: **現状は存在しないため、切り戻し手順を書けない。** Claude JSON/settings、Codex TOML、Bridge env、hooks/skills と service が別々に管理され、現在の install manifest にそれらの before-image がない。必要なのは、手順4の一括切替が作る単一の reversible manifest を一箇所として、旧 client 設定と旧 service authority を戻し、新 service で作られた record も失わずに旧側へ反映する rollback コマンドである。

これを実行したら何が変わるか: 全 consumer が同時に旧 service へ戻り、新旧どちらか一方だけが writer の状態で復旧する。

現時点の結論: この順序は確定できるが、通常切替を実行できる手順にはまだなっていない。残る製品コマンドを実装し、未実装の段を実在するコマンドへ置き換えてから実行する。
