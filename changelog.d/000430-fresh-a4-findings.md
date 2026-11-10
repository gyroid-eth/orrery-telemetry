### freshの通知先照合とCodex再開設定を修正

- Codexの既知の空欄placeholderを入力なしと扱い、processの出生を記録・照合ともC localeで取得します。未知のplaceholderとClaudeの色なし提案は引き続き入力ありとして通知を見送ります。既存の別locale記録は自動変換しません。
- fresh Codexの再開では、proxy・plugin MCP無効化等の設定をすべてresumeの後ろに置きます。親のnetwork許可の追加も既存の子launcherと揃え、`AGENTSTACK_CODEX_NETWORK` で追加を止められます。通常のtool承認は維持します。
- fresh Mailの送信・返信schemaに宛先のID／名前・期待名・to/cc/bccと制約を公開します。
- [freshの手順](../docs/fresh-global-update.md)にClaude初回画面の人による操作と通知の既知の制限を追記しました。実providerの通知往復・再開の本人照合は確かめ直しが必要です。
