### WSL を agent の作業中に自動で保持する

WSL の従来モードでは、agent が稼働・起動している間や dashboard のページを表示している間、隠れた `wsl.exe` で WSL を保持する。無活動が60秒続くと保持を手放す。状態は dashboard と `agentstack-doctor` で確認でき、`--no-wsl-anchor` で無効にできる。詳しくは [WSL2 のトラブルシューティング](../docs/troubleshooting.md#wsl2-で-ubuntu-の窓を閉じたとき) を参照。

- PowerShell の初回起動が遅い場合や進行表示が混じる場合にも `wsl.exe` の PID を取得する。PowerShell の終了待ちが時間切れになっても、出力済みの PID は利用する。
- systemd user manager が使える WSL では dashboard を user service として登録する。`loginctl enable-linger` が有効なら Ubuntu の起動時に dashboard と Mail が立ち上がる。
