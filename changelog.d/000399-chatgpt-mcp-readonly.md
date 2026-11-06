### 任意の読み取り専用MCPアダプターと所属確認

ChatGPTなどからORRERYの状態・一覧・個別agentを読み取る任意のアダプターを追加しました。通常のinstaller、サービスの起動設定、UI、hook、Mail操作は変えません。spawn・送信・OAuth・常駐化には対応しません。

legacyは明示した1つのprojectに限定し、agent・finished・gone・retiredと直近30日の履歴という範囲を返します。freshは同一Mail instance全体の安定ID・capability・退役/未観測状態を保持します。未知API世代は拒否し、名前を切り詰めて別名として公開しません。

所属を確認できない旧応答を推測で通さないため、Dashboard APIを36から37へ上げました。legacyの `/api/agents` に応答全体と各行の `project_key` を追加し、曖昧なtmux行の所属はnullにします。既存の行の表示・生存判定・書込み認可は変更しません。アダプターのlegacyはAPI 37、freshは検証済みの34・35・36・37だけを受け付けます。

秘密pathを保持するTunnel転送、Host/Origin検査、接続先の実証、pair時のローカル書込みと公開fieldを日英で説明しました。[日本語ガイド](../integrations/chatgpt_mcp/README.md)・[English](../integrations/chatgpt_mcp/README.en.md)を参照してください。
