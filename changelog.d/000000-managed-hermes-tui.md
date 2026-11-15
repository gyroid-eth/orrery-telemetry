### 管理下の Hermes に bot 用の TUI を同梱

Hermes の管理 installer は、固定 source と既存 PM の Node/npm で TUI を build し、独立した新世代に収録します。TUI が欠ける・build に失敗する場合は公開せず、以前の世代を保持します。source・lock・tool・出力を receipt で照合し、Telegram bot には検証済みの TUI と Node の path を渡します。通常の子と CLI の起動計画は従来どおりです。起動時の npm 取得や手動での旧世代の修理は必要ありません。
