### スマホTerminalの通常起動設定とagent向け手順を整理

ORRERY側の共有設定`enable_mobile_terminal`で通常の起動時にTerminalを有効にする手順を、[日本語](../docs/mobile.md)・[英語](../docs/mobile.en.md)のガイドへ追加しました。既定はオフで、稼働中のbackendには明示的な再起動が必要です。agentが行う設定・送信なしの確認と、本人が行うpolicy・実端末確認を分けています。

操作に使う自分の端末だけへのアクセス制限を推奨し、スマホ・タブレットを複数許可する例と、既存の広いgrant/ACLも整理する必要を説明しました。端末限定は必須条件ではなく、既存の`--phone-policy-reviewed`は運用者の申告のままです。readerの新しい常駐機構・policy自動変更は追加していません。API世代31・既存の本人/Host/Origin照合・入力guardは維持します。
