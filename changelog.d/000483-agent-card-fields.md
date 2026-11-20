### エージェント一覧で作業場所と実行情報を取得

legacy dashboard の `GET /api/agents` から、作業ディレクトリ、正式モデル ID、effort、harness、context 上限、画面とブラウザの設定上の権限、親子関係を取得できます。確認できない値は `null` です。互換 API 世代は 41 です。[API の仕様](../docs/api.md#get-apiagents)
