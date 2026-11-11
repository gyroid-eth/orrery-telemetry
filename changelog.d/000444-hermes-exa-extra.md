### Hermes の Exa SDK を固定 runtime に同梱

managed Hermes の install 時に、上流で版が固定された `exa` extra を準備します。Exa の初回検索で SDK が子専用の別環境に入り、再起動しても使えない問題を直します。既存の install も新しい runtime 世代へ更新し、公開前にその環境で SDK を import できることを確認します。私有設定の runtime 参照を更新後の `current.json` に合わせ、新しい子で利用してください。
