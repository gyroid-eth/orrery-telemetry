### 指定したスマホからTelemetryの状況を見る閲覧用画面

既定オフのmobile readerを追加しました。英語・darkの要確認一覧とMail metadataの時間軸、PCと共通のNETWORK描画・力学・6設定を使えます。本文・task・private path・操作API・quota更新は含めず、取得不能/古い観測を正常0へ変えません。

本人を照合するloopback relayと、指定スマホを絞る実効tailnet policyを分け、Tailscale Serveでの手動有効化/停止手順を[日英guide](../docs/mobile.md)へ記載しました。通常installでは待受/Serveを開始しません。APIは28へ進め、legacy loopback専用のMail metadata取得を追加します。実Serve・本人スマホの受入は別途必要です。
