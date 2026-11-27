### macOS で ORRERY が起動する Cua Driver の利用状況送信と自動更新確認を無効にしました

macOS の画面操作 relay は、Cua Driver の利用状況送信と自動更新確認を無効にして起動します。Windows では Cua Driver の設定に保存します。daemon を実行する Windows ユーザーで `cua-driver.exe telemetry disable` と `cua-driver.exe config set update_check_enabled false` を実行してください。
