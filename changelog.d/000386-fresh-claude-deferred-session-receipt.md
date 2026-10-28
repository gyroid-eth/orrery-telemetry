### fresh の Claude 子: transcript がまだ無い SessionStart を保留し、同じ session の次の正規の PostToolUse で検査し直せたときに ready にする

実機で、子の SessionStart の hook が `FRESH_CLIENT_UNAVAILABLE` で失敗し、session receipt が作られませんでした。そのため、子が ready にならず、Mail の通知が届かず、API で終了できませんでした。transcript が作られる前に SessionStart が来る場合は、合成の試験で再現した対応ケースです。今回の実機の症状と整合する有力な候補ですが、その hook に渡された path と元の例外は記録が無く、初めの原因は確定していません。

transcript がまだ無い SessionStart は候補として保留します。同じ session で次の正規の PostToolUse が来たときに、本人・現在の起動・nonce・lineage・transcript の参照を全部検査し直し、通ったときだけ receipt を公開します。候補だけでは ready にも通知の完了にも数えないので、正規の PostToolUse が来なければ未 ready のままです。Codex と managed の経路、API の世代は変えていません。実機での受入は別途必要です。
