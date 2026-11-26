### 最終活動時刻に会話記録の更新を反映（API 57）

稼働中の tmux agent は、tmux・Mail・紐づけ済みの会話記録のうち最も新しい時刻を最終活動として表示します。Claude・Codex・dsh・Antigravity は履歴ファイルの更新時刻、Hermes は該当する会話の最後のメッセージ時刻を使います。会話記録が無い場合は、従来どおり tmux と Mail の活動時刻を表示します。
