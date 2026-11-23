### WSL を agent の作業中に自動で保持する

WSL の従来モードでは、agent が稼働・起動している間や dashboard のページを表示している間、隠れた `wsl.exe` で WSL を保持する。無活動が60秒続くと保持を手放す。状態は dashboard と `agentstack-doctor` で確認でき、`--no-wsl-anchor` で無効にできる。詳しくは [WSL2 のトラブルシューティング](../docs/troubleshooting.md#wsl2-で-ubuntu-の窓を閉じたとき) を参照。

- PowerShell の初回起動が遅い場合や進行表示が混じる場合にも `wsl.exe` の PID を取得する。PowerShell の終了待ちが時間切れになっても、出力済みの PID は利用する。
- 起動の完了時に状態の更新時刻を記録し、保持用プロセスが解放を終えるまで `releasing` と表示する。
- Windows の WMI で保持用プロセスを元の接続から独立させ、同じ起動で Windows PID と Linux 側の準備を確認してから保持成功を表示する。保持を確認できないときは NEW AGENT の通知にも理由と対処を出す。
- systemd user manager と linger が使える WSL では dashboard を user service として登録し、Ubuntu の起動時に dashboard が立ち上がる。linger を確認できない場合は supervised background を使う。
