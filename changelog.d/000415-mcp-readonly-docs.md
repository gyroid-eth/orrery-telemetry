### MCPの読み取り専用の範囲と一覧APIの説明を明確化

MCPアダプターの[日本語ガイド](../integrations/chatgpt_mcp/README.md)と[英語ガイド](../integrations/chatgpt_mcp/README.en.md)に、freshの一覧取得は既存のMailの書き込み用transactionを通り、「読み取り専用」はMCPに書き込み操作を公開しない意味であることを補足しました。DB層で書き込みや書き込み用ロックが一切ないという保証ではありません。

一覧APIのdocstringも、API 37で追加したproject情報を含む現在の応答に合わせました。機能・API世代・認可・transactionの実装は変更していません。
