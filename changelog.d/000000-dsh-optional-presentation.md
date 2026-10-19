### DeepSeekの表示を明示opt-inにする

installerの `--enable-dsh` でDeepSeekの候補とロゴ配信を有効化し、
`--disable-dsh` で無効化できる。既定は無効で、flagなしの更新は既存選択を保つ。
runtimeは未接続と表示し、installer操作自体はruntime取得・鍵・登録・API要求を行わない。
有効化後の既存usage読取では次節の残高取得を行う。
provider catalogに任意のdsh候補を追加するため、API generationを19から20へ上げる。

### DeepSeekのAPI残高をusage欄に表示する

明示opt-in時だけ残高を取得・表示する。鍵は短命の取得processだけがKeychainから読み、
4分cacheを使う。取得失敗は「不明」とし、USD総額・利用可否を既存usageの1行に載せ、金額を百分率quotaから分ける。
`/api/quotas` に任意の残高型を追加するため、API generationを20から21へ上げる。
