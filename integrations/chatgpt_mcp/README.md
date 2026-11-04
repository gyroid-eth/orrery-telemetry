# ORRERY ChatGPT MCP（読み取り専用・任意導入）

これは upstream Issue #395 の第1段階です。外部クライアントからローカルORRERYの状態だけを取得する、独立した小さなアダプターです。通常の installer・daemon・Dashboard・ORRERY Mail・UIには変更を加えません。自動起動も常駐化も行いません。

公開するツールは `orrery_status`、`orrery_list_agents`、`orrery_get_agent` の3個だけです。spawn・追加指示・メッセージ送信・OAuth・service installは実装していません。リクエストの経路はDashboardの `GET /api/version` と `GET /api/agents` のみに限定します。

## 前提と方式の選択

Python 3.11以上が必要です。ORRERY DashboardはMac上のloopbackで既に起動しているものを読みます。DashboardやMailの導入・切り替えは本アダプターの責務ではありません。

`ORRERY_MCP_MODE` に `legacy` または `fresh` を必ず明示してください。legacyは従来の `/api/version` に `runtime_mode` がない形式、freshは `runtime_mode=fresh` とMail instance/capabilitiesがある形式です。名前・API世代番号だけでは互換性を判定しません。選択した方式と応答が不一致、または実験用globalなど未対応の方式であれば起動前の検査で停止します。

freshの未観測エージェントは `running=null`、`observation.state=unknown` のまま一覧に残ります。`running_only` の既定は `false` です。freshでは `mail_instance_id` と `mail_agent_id` を維持し、バージョン/一覧間のinstance一致を検査します。legacyに存在しないMail IDやcapabilityは作らず `null` とします。

## 手動起動（現在のサービスを置き換えない）

以下のコマンドは新しいターミナルで、アダプターを含むcheckoutから実行します。専用ポートの既定は8791で、既存の常駐MCPとは独立です。

```sh
cd /path/to/orrery-telemetry
export PYTHONPATH="$PWD/integrations/chatgpt_mcp/src"
export ORRERY_MCP_MODE=legacy
export ORRERY_MCP_DASHBOARD_URL=http://127.0.0.1:8770
export ORRERY_MCP_PORT=8791
python3 -m orrery_chatgpt_mcp pair --show-url
python3 -m orrery_chatgpt_mcp check
python3 -m orrery_chatgpt_mcp serve
```

freshに切り替えた環境だけで `ORRERY_MCP_MODE=fresh` を指定します。`pair` が表示する秘密付きURLはローカル本人向けです。Issue、ログ、共有資料に貼らないでください。再発行するときは `pair --rotate` を使います。稼働中のアダプターも次のリクエストから古いURLを拒否します。既定の秘密保管場所は `~/.config/orrery-mcp-readonly/pairing.json`（0600）です。

## Tunnelの利用者と公開範囲

アダプター自身は127.0.0.1だけで待ち受けます。別の端末・ChatGPTから使うには、利用者が明示的に設定したSecure MCP Tunnelなどの保護された転送経路が別途必要です。Tunnelは本体機能ではなく、このPRでは設定・インストールしません。

接続する前に、Tunnel側の利用アカウント、接続先のローカルポート、URLを知るクライアント・ユーザーの範囲、共有や公開の設定を運用者が確認してください。秘密付きパスは知っている人に読み取り権を与えます。Tunnelに到達可能な相手とURLを知る相手が一致しない場合には接続しないでください。URLの秘密だけで「自分にしか見えない」とは保証できません。

ChatGPT上の表示や確認はサーバー側の本人承認ではありません。この段階に書き込み機能は一切存在しないため、確認によって書き込みを許す動作はありません。

## 安全確認と停止

起動後、秘密付きMCP URLに対して `initialize` を呼び、`tools/list` に3個の読み取りツールしかないことを確認します。`orrery_status` の `adapter.spawn_enabled_on_server=false` と `adapter.message_enabled_on_server=false` を確認し、`orrery_message_commit` を直接 `tools/call` して `isError=true`・`error.code=unknown_tool` が返ることも確認します。秘密なしの404応答だけで書き込み禁止を証明したことにはなりません。

`python3 -m orrery_chatgpt_mcp check` はDashboardの方式と一覧の互換性検査です。実プロセスの状態、Tunnelのアクセス権限、送信権限までは証明しません。常駐サービスや `service status` はこの段階ではありません。

停止するときは `serve` を実行中のターミナルでCtrl+Cを押します。変更したい場合もサービス置換・エージェント停止・ORRERY Mailの再起動は不要です。

## テスト

```sh
PYTHONPATH=integrations/chatgpt_mcp/src:. python3 -m pytest -q integrations/chatgpt_mcp/tests/test_readonly.py
```

テストは架空のlegacy/fresh応答と隔離されたlocalhost HTTPサーバーを用います。運用中のDashboard、Mail、tmux、Tunnelには接続しません。送信の受領とエージェントの処理開始は、この第1段階の検証範囲外です。
