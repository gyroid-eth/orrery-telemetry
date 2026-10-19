# Managed catalog（段1）

`dashboard.managed_catalog.catalog_providers(backends, availability, *, mode, enabled)`はA1のpublic metadataとbackend×harness準備状態から、既存providersへ追加する配列を返します。serverへの差し込みと実体/版/patchの準備判定はmanaged spawn controller側です。この関数はfile/API/鍵/登録へ触れません。

`load_catalog_inputs(config_file, *, mode)`はoperatorの私有manifestから(backends, availability, enabled)を返します。無指定/欠落/無効opt-inはoff、Mac以外はunsupported_platformです。runtime/template/model・製品helper/CLIを照合し、dshのNode版/固定package版/TUI patch hash、Hermesのcommitted interpreterを読み取ります。Nodeの版確認は鍵なしenvで`--version`を呼び、Hermesのtemplate継承は同じcommitted Pythonで既存の安全なprofile projectionを読取実行します（profile書込・key_cmd実行・provider importなし）。provider/Mail/Keychainへは接続しません。失敗理由は公開codeにし、私有参照やraw stderrをcatalogへ出しません。POST時のfull preflightを省略する証拠にはしません。

provider IDはdeepseek/strata、harness IDはdsh/hermes。既定無効は空配列、legacy以外は全harnessがunsupported_modeです。available=trueの最初のharnessをdefaultにし、準備できたものだけを選べます。起動時にもcontrollerが再検査します。

段1はcontext=null/context_source=unreported/context_note="server 報告値は未取得"。runnerのconfigured_contextをserver報告へ昇格させません。補助はsameだけです。私有URL/account/実行pathなどはpublic応答にコピーしません。

無通信の確認：`python3 tests/test_managed_catalog.py`。4組、準備欠落、global拒否、既定切替、context未報告、私有参照非公開を確認します。
