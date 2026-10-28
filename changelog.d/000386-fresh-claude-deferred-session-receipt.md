### fresh の Claude 子が、transcript の作成前に呼ばれる SessionStart でも ready になるよう修正

実機の Claude Code は、子の SessionStart の hook を transcript の作成より先に呼ぶことがあります。この場合、session receipt が作られず、子が ready にならない、Mail の通知が届かない、API で終了できない、という問題がありました。

transcript がまだ無いときの SessionStart は候補として保留し、同じ session の次の正規の PostToolUse で、本人・現在の起動・nonce・lineage・transcript の参照を全部検査し直してから receipt を公開します。候補だけでは ready にも通知の完了にも数えません。Codex と managed の経路、API の世代は変えていません。実機での受入は別途必要です。
