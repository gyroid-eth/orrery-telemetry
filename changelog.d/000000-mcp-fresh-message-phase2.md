### 試験用fresh Mailへ固定宛先の追加指示を送る任意MCPアダプター

[第2段階のfresh送信アダプター](../integrations/chatgpt_mcp/FRESH_MESSAGES.md)を追加しました。既定OFFで、運用者が準備した保護clientと最大16件の宛先に固定し、全文preview・digest・永続claimを通して送信します。保存した受領だけをstatusで読み、不明な結果を再送しません。送信権の事前委任、Tunnel共有範囲、無効時の確認と証拠保全を日本語・英語で説明しています。

本番fresh切替の受入前は試験用instance限定です。従来の読み取り入口、通常installer・daemon・UIは維持し、常駐化・spawn・資格移行を含みません。Dashboard API世代とMail公開APIの変更はありません。
