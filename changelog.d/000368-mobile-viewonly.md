### 指定したスマホからTelemetryの状況を見る閲覧用画面

既定オフのmobile readerを追加しました。英語・darkの要確認一覧とMail metadataの時間軸、PCと共通のNETWORK描画・力学・6設定を使えます。本文・task・private path・操作API・quota更新は含めず、取得不能/古い観測を正常0へ変えません。

Graphは直近24時間に活動が確認されたagentを既定にし、15分〜30日の細かい期間sliderと明示した全履歴を選べます。Allは選択期間内の全体です。最近活動した退役済みを含め、不明な活動時刻を現在で補いません。専用アイコンは認証後にPCの既存設定から解決し、科学者の肖像・イニシャルへfallbackします。件名/本文・ページ送り・本文cacheは含めません。

本人を照合するloopback relayと、指定スマホを絞る実効tailnet policyを分け、Tailscale Serveでの手動有効化/停止手順を[日英guide](../docs/mobile.md)へ記載しました。通常installでは待受/Serveを開始しません。APIは29へ進め、legacy loopback専用のMail metadata/肖像取得と期間契約を追加します。実Serve・本人スマホの受入は別途必要です。
