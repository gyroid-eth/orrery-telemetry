### fresh の Claude 子: transcript がまだ無い SessionStart を保留し、次の正規の PostToolUse で検査し直して ready にする

実機で、子の SessionStart の hook が `FRESH_CLIENT_UNAVAILABLE` で失敗し、session receipt が作られませんでした。そのため、子が ready にならず、Mail の通知が届かず、API で終了できませんでした。hook の時刻が transcript の作成より少し前だったことと整合する有力な候補として、transcript がまだ無いことを想定しています。初めの原因は確定していません。

transcript がまだ無い SessionStart は候補として保留します。同じ session で次の正規の PostToolUse が来たときに、本人・現在の起動・nonce・lineage・transcript の参照を全部検査し直してから receipt を公開します。候補だけでは ready にも通知の完了にも数えないので、正規の PostToolUse が来なければ未 ready のままです。Codex と managed の経路、API の世代は変えていません。実機での受入は別途必要です。
