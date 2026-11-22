### WSL を agent の作業中に自動で保持する

WSL の従来モードでは、agent が稼働・起動している間や dashboard のページを表示している間、隠れた `wsl.exe` で WSL を保持する。無活動が60秒続くと保持を手放す。状態は dashboard と `agentstack-doctor` で確認でき、`--no-wsl-anchor` で無効にできる。詳しくは [WSL2 のトラブルシューティング](../docs/troubleshooting.md#wsl2-で-ubuntu-の窓を閉じたとき) を参照。
