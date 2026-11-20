### Mail を使わずに再開する Claude にブラウザと画面操作を付ける

- API 世代 43 から、`/api/jump` で Claude in Chrome と `orrery-screen` を起動ごとに指定できます。画面操作は新しい起動世代の初期権限を `off`、`view`、`control` から選べます。既定は追加の道具なしで、Mail と hook は無効です。[再開 API](../docs/api.md#post-apijump)
