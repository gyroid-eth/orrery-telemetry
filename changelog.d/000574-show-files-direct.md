### ORRERY Mail に直接つないだセッションでファイルを表示

Claude Code の bot など、loopback の ORRERY Mail に直接つなぐセッションでも `show_files` を使えます。作業フォルダーの外にあるテキストや画像も、絶対パスを明示すれば cockpit とスマホで preview できます。ツールは読み取れる通常ファイルを `present` に入れ、読み取り権限がない場合は理由を返します。
