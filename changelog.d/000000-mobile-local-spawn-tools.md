### スマホの New agent で Local の起動設定と失敗理由を表示

スマホで dsh・Hermes を選ぶと、選択したフォルダで利用できる場合に Browser が表示されます。起動に失敗すると次の操作を英語で案内します。API 48 の `GET /api/spawn-names` に `native_browser_dirs` を追加しました。

dsh・Hermes で Chrome DevTools を明示して選ぶと、Codex と同じブラウザ設定を使えます。
