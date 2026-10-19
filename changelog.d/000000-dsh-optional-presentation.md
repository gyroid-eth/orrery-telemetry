### DeepSeekのロゴとUSD残高を明示opt-inにする

段A の前半: 表示と残高。DeepSeek の子の起動は次の版。

installerの `--enable-dsh` でDeepSeekロゴと残高表示だけを有効化し、`--disable-dsh` で無効化できる。
既定は無効、flagなしの更新は選択を保つ。dsh・dsh-TUI・Nodeの取得やspawn候補は含まない。
鍵は短命の取得workerだけがKeychainから読み、既存usageのproviderと4分cacheを使う。
USD総額・利用可否は既存usageへ1行で載せ、金額を百分率quotaから分ける。
LEFTは整数ドル、開いた行は小数第2位でfloorして `$12` / `$12.34` と表示する。
取得失敗は `$?` にし、過去の金額や利用可能を残さない。API/cacheは原精度を保つ。
`/api/quotas` の任意残高型を含むAPI generationは21。
