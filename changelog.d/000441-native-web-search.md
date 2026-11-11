### dsh・Hermes の web_search を共通設定で選択

managed spawn manifest の `web_search` に Exa の資格参照と SearXNG の URL を設定し、両 harness の検索先を共通に選べるようにしました。未設定の製品の既定は従来どおりです。起動ごとの `POST /api/spawn` の任意 field `web_search=exa|searxng` と内部 launcher の `--web-search` も保存して引き継ぎます。API 世代は変えません。

鍵の値は既存の Keychain / Secret Service と native の資格 helper で取得し、起動計画・設定ファイル・引数に保存しません。鍵無し・検索先の停止は検索時にエラーを返します。SearXNG の応答が200でも、結果0件かつ応答しなかった engine がある場合はその理由を表示します。[設定方法](../docs/managed-catalog.md#native-の-web_search)を参照してください。
