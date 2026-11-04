### A user-facing change

- ChatGPTなどの外部クライアントから、ORRERYの状態・エージェント一覧・個別エージェントを読み取れる任意のMCPアダプターを追加しました。通常のインストーラー、daemon、UI、Mail、既存サービスには影響しません。
- legacy/freshのモードを明示して検証し、freshのMail instance・安定したagent ID・capability・未観測状態を保持します。MCPの起動とMail送信には対応せず、読み取り専用で拒否します。
- 手動接続・停止と安全確認の手順は[日本語ガイド](../integrations/chatgpt_mcp/README.md)（[English](../integrations/chatgpt_mcp/README.en.md)）を参照してください。Dashboard APIの世代番号は変更していません。
