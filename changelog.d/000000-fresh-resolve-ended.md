### fresh の未終了 run を確認して閉じる

prepared context 共通の `resolve-ended --launch-id --confirm-ended` を追加しました。現在の本人・世代・run と記録済みの process／pane／group を再照合し、終了コード不明の解決記録を実際の終了記録と区別します。観測不能・記録不足・生存は拒否し、旧 run を保持します。

Hermes の wait と子の終了を確認した非0終了は実コード付きの失敗として記録し、次の通常起動へ進めます。閉じた run への配送・hook・session の追記を止め、日英の復旧手順を更新しました。
