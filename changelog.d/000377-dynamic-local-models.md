### Local接続のmodelを両NEW AGENTで動的に選択

Local接続へ任意の表示名と1本以上のAPI URLを設定すると、両NEW AGENTが接続別にmodelを列挙します。model追加ごとのORRERY設定・再installは不要です。status欠損・未知の有効IDも選択でき、明示unloadedは選択不可。選択URL・IDを起動直前に1回照合し、不一致時に別modelへ移りません。contextの報告値／既定／任意fallbackを区別し、元URL・modelでのexact resume、従来の固定接続互換を保持します。

API世代は33。動的Localのcatalogに `url_index` / `url_key` / `display_name` / context由来を追加し、spawnへ選択URLの識別値を渡します。[一度のagent支援移行手順](../docs/native-setup.md)を追加しました。新しいtransport adapter、serverのmodel起動・停止、実機接続の自動移行は含みません。
