### fresh の Claude 子: transcript がまだ無い SessionStart を保留し、同じ session の次の正規の PostToolUse で検査し直せたときに ready にする

実機で、子の SessionStart の hook が `FRESH_CLIENT_UNAVAILABLE` で失敗し、session receipt が作られませんでした。そのため、子が ready にならず、Mail の通知が届かず、API で終了できませんでした。transcript が作られる前に SessionStart が来る場合は、合成の試験で再現した対応ケースです。今回の実機の症状と整合する有力な候補ですが、その hook に渡された path と元の例外は記録が無く、初めの原因は確定していません。

transcript がまだ無い SessionStart は候補として保留します。同じ session で次の正規の PostToolUse が来たときに、本人・現在の起動・nonce・lineage・transcript の参照を全部検査し直し、通ったときだけ receipt を公開します。候補だけでは ready にも通知の完了にも数えないので、正規の PostToolUse が来なければ未 ready のままです。Codex と managed の経路、API の世代は変えていません。実機での受入は別途必要です。

あわせて、SessionStart で通知を渡すのを `source` が文字列 `startup` のときだけにしました。resume・clear・compact・未知・欠落・文字列以外の `source` では、本人・現在の起動・所有者の確認は行いますが、通知の取得・出力・完了はせず、通常の watcher に残します。PostToolUse は従来どおりです。前の版の実機の受入では、子が ready になり API で終了できましたが、子どうしの Mail の往復は成立しませんでした。子の会話が resume された時点で別の子からの通知に相当する内容が SessionStart の文脈として渡り、その後ターンが始まらないまま子が待ち続けました。その通知が完了済みになったか、渡された内容がその Mail と同じものかを ID で照合した記録は無く、未確認です。watcher に残しても起床の成功は保証しません。この変更を入れた版の実機での受入はまだです。
