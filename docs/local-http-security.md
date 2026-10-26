# ローカル HTTP の信頼境界

Dashboard と ORRERY Mail は、ローカル HTTP の入口で Host と要求元の情報を検査します。既定の Host は実際の待受ポートの `127.0.0.1`・`localhost`・`[::1]` です。Origin がある場合は対応する origin と照合し、cross-site の要求は navigation 以外を拒否します。手元の CLI・hook と proxy は Origin を送らず利用できます。

Dashboard の明示した LAN / VPN origin 設定、global mode の operator capability、ORRERY Mail の各操作の権限検査、mobile relay の本人・固定 origin の検査は継続します。要求元の検査はユーザー認証の代わりにはなりません。

ローカルの同じユーザーが管理する環境変数、PATH、実行ファイル、設定は信頼する前提です。取得元や外部コマンドの選択を変える設定もこの境界内にあります。同じユーザーの実行環境を書き換えられる主体を隔離する機能ではありません。別の取得元や実行ファイルを指定するときは、その内容を利用者が確認してください。
