### Hermes の Mail 起動と Telegram の資格診断

fresh の管理下 agent が使う Mail MCP に、選択済み Python を明示的に渡し、PATH の別の Python で起動する失敗を修正しました。Hermes Telegram の `credential-unavailable` は維持し、設定・参照・資格取得の失敗理由だけを owner 専用の診断に残します。資格の値や helper の出力は記録せず、HOME や Keychain の設定も変更しません。
