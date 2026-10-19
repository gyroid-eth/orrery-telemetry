# DeepSeekの表示と残高

段A の前半: 表示と残高。DeepSeek の子の起動は次の版。

既定は無効。installerの `--enable-dsh` はDeepSeekロゴの配信とUSD残高の表示だけを
有効化する。dsh・dsh-TUI・Nodeの取得、spawn catalogの候補、子の起動は含まない。
installer自身は鍵・API・登録に触らず、有効化後の既存usage読取で残高workerを使う。

`--disable-dsh` はロゴ配信・残高の取得と行を無効化する。配置済みassetは保持し、
flagなしの更新では選択を維持する。選択とassetは通常のownership manifest/uninstallの対象。
利用者のKeychainや会話をこの操作で変更しない。

`tests/test_dsh_presentation.py` は同じHTTP catalog/asset経路で無効・有効・更新・再無効を
対照し、どちらの状態でもdshのspawn候補が無いことを確かめる。既存DeepSeek数値モデルの
分類は維持する。`tests/test_dsh_logos.py` は実demo buildも使い、明示flagはlogoだけを
有効にして、demoのspawn候補にもdshを出さないことを確認する。

networkは明示 `provider=local` の入力だけを六角形＋ライムの縁にし、モデルの作り手を示す。
未知はロゴなしの中立座金。入力の正本化と実機確認は後続R3 local統合で行う。
local DeepSeekのasset有効化も共通profileの能力へ接続が必要で、現段階の配信は上記opt-inに従う。
`dashboard/demo/build.sh [outdir] --enable-dsh` は表示fixtureだけを有効にする。

## DeepSeek API残高を既存usageへ表示する（実鍵確認前）

通常の鍵なし・Keychainロック・通信失敗 × 残高を0と誤認する害、
及び鍵漏洩時の大きな害を対象にする。鍵を使った確認やliveの変更は準備に含めない。

公式の [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance/)
は `GET /user/balance` で利用可否と通貨別の金額文字列を返す。
取得workerはBearer認証で公式endpointを読む。SDKやTUIは起動しない。
usageにはUSDの総額だけを投影し、CNY・付与額・入金額は表示しない。
USDが無ければ「USD の残高なし」、取得失敗は「不明」。0へ置き換えない。
利用可否は公式APIのアカウント全体の値であり、falseは警告色にする。
LEFTの札は `$12` のように整数ドルでfloorし、利用可否の文字は載せない。
開いたusage行は `$12.34` のように小数第2位でfloorし、取得失敗は `$?`。
表示だけを切り捨て、API/cacheの原精度は保つ。
金額と百分率quotaは別の型で、新しいpopupは作らない。

`/api/quotas` → `QuotaService` のproviderの1つとして取得する。
既存usageの毎分pollと開いた時のrefreshを使い、取得結果には同じserviceの4分cacheを使う。
opt-inが無効なら行そのものを返さず、workerも起動しない。失敗後の前回金額は表示しない。
時刻は既存の `observed_at` / `last_observed_at` / `checked_at` を使う。

Keychainの `deepseek-api` は短命workerだけが読む。そのworkerのenvとHTTP header以外に
鍵を渡さず、dashboard・cockpit・確認runnerのenv、argv、log、保存file、API応答へ返さない。
認証helperにも鍵を継承させない。標準CAでTLSを検証し、親のproxy設定は継承せず、
redirect・自動retryも行わない。workerのstdoutは検査済みの金額か固定理由codeだけにする。

| 既存経路と接続点（実装時の行番号） | 役割 |
| --- | --- |
| `dashboard/quotas/__init__.py:14` | Claude・Codex・Antigravityと同じserviceへ任意の残高providerを登録 |
| `dashboard/quotas/claude_route.py:32` / `codex.py:22` | 既存providerは各TTL30秒/120秒。DeepSeekは `deepseek.py:18` の240秒 |
| `dashboard/quotas/service.py:54,80,94` | provider選択・取得時刻・既存cache/lock/失敗の隔離。残高はwindowへ変換しない |
| `dashboard/quotas/base.py` / `balance.py` | quotaと別の任意balance型、wireはUSD総額・利用可否だけ |
| `dashboard/server.py` の `/api/quotas` | 既存の同一origin検査とglobal operator admissionを維持。新endpointなし |
| `dashboard/index.html` の `usageTick` / `usageLogo` / `renderUsagePill` / `renderUsagePopover` | 既存の毎分取得・開いた時の更新・同じlogo・provider列へ表示 |

`tests/test_deepseek_balance.py` はdummy専用のloopback `/user/balance`、実worker、
同じHTTP usage経路の無効/有効/再無効、cache、失敗不明、秘密の非継承を対照する。
`tests/test_deepseek_balance_ui.py` はmaster由来fixtureとの表示一致とUSD各状態を確かめる。
実口座へ要求したという証拠にはしない。

### 親が承認後に使う1回の確認runner

repositoryのrootから使う。これらは準備者が実行するcommandではない。

```sh
# 既定は1GETだけ。金額だけのJSONを標準出力と指定fileへ記録する。
python3 scripts/deepseek-balance-check.py --live --output /path/to/balance-once.json
# 前後2GETだけ。間に3秒あけ、他のDeepSeek要求は挟まない。
python3 scripts/deepseek-balance-check.py --live --pair --output /path/to/balance-pair.json
```

`--live` が無ければ実確認を始めない。鍵をshellでexportする手順は不要。
runner自身はKeychainを読まず、取得workerだけが鍵を読む。
前後比較は確認記録として通貨ごとに金額文字列を保ち、Decimalで差を計算する。
usageへのUSD限定の投影とは別の記録であり、通貨を合算しない。
最初の取得が失敗したら2回目を始めず、どちらか失敗なら差分は不明にする。
表示桁より小さい課金や他clientの同時使用は区別できず、差0を非課金の証明にしない。
読取APIが非課金かは実確認前には未検証である。
