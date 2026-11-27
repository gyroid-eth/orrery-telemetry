### ORRERY から起動する Cua Driver の利用状況送信と自動更新確認を無効にしました

ORRERY の画面操作 relay は、Cua Driver の利用状況送信と自動更新確認を無効にして起動します。Windows で常駐する daemon にも適用するには、その Windows ユーザーで `cua-driver.exe telemetry disable` と `cua-driver.exe config set update_check_enabled false` を実行し、Cua Driver の設定に保存します。
