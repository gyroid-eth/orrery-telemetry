### ORRERY Mail に直接つないだセッションでファイルを表示

Claude Code の bot など、loopback の ORRERY Mail に直接つなぐセッションでも `show_files` を使えます。絶対パスでファイルの有無・種類・サイズを確認します。テキストは会話の作業フォルダーか設定済みの workspace root 内で表示でき、直接接続でその範囲を照合できないファイルは理由付きの `unverified` として返します。存在を確認できた画像は `present` に入ります。cockpit とスマホは呼び出しの記録から、表示の条件を満たすファイルに chip を出します。
