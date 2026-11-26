### fresh Mail の送信結果を再取得するときに保存内容を照合

同じ request ID の送信を明示的に再実行したとき、以前の受領結果を返す前に、保存された件名・本文・重要度・ACK 要求・topic と元の送信内容を照合します。内容が異なる場合は `RESULT_CONTENT_MISMATCH`、保存された thread と受領結果が異なる場合は `RESULT_REFERENCE_MISSING` で拒否し、既存のメッセージや受領結果を保持します。正しい保存内容では同じ受領結果を返し、重複配送しません。

既存の request ID・intent hash・受領形式と、受信者の退役後にも過去の受領結果を取得できる動作を維持します。既定値を含む送信を対象とし、返信の継承規則、legacy 運用、本番切替、Dashboard API 世代は変更しません。
