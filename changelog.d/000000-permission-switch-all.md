### Agent の権限の状態と変更できる条件を分けて表示

旧運用の agent 一覧に Computer use と Browser の状態・根拠・変更可否・理由を追加しました。生成された Claude Code／Codex CLI では、Browser の Off・Temporary Chrome・My Chrome を選び、確認のうえで同じ会話を再開できます。Telemetry API 世代を 54 に上げました。

生成された dsh／Hermes でも、親の有無にかかわらず左ペインから切り替え、同じ会話を再開できます。

macOS の管理起動による Antigravity CLI にも同じ切り替えと再開を追加しました。変更できない項目には理由を表示します。

道具の選択を省略した生成済みの Codex CLI は、保存済みの既定プロファイルを引き継いで変更できます。親なしの Claude Code／Codex CLI／Antigravity CLI は、本人の会話を確認できれば、承認後に管理起動へ移行して権限を変えられます。Telemetry API 世代を 55 に上げました。
