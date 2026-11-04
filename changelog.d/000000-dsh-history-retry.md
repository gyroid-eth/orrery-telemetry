### dsh の最初の作業後に会話記録を表示できない不具合を修正

legacy dsh の TUI が起動時の履歴保存を遅らせる場合も、session の flush と turn 終了で会話記録の receipt 発行を再試行します。発行後の inode と時刻を固定し、launch・root・lock・conflict の照合を維持します。API 世代の変更はありません。
