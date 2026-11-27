### ORRERY から起動する Cua Driver の利用状況送信と自動更新確認を無効にしました

ORRERY の画面操作 relay は、Cua Driver の利用状況送信と自動更新確認を無効にして起動します。Windows の対話ログオンで利用者が起動する daemon にも適用するには、`setx CUA_DRIVER_RS_TELEMETRY_ENABLED false` と `setx CUA_DRIVER_RS_UPDATE_CHECK false` を実行し、サインインし直してから daemon を起動してください。
