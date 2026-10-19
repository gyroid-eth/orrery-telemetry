# dsh 対話子：段Aの準備

既定ではdsh / dsh-TUI / Nodeを取得せず、spawn候補やDeepSeekロゴを出さない。
installerの `--enable-dsh` で候補とロゴ配信を明示的に有効化できる。
現段階ではruntime未接続をcatalogへ示し、通常の子としてはまだ起動しない。
installer操作自体はruntime/Nodeの取得、鍵の読取、登録、API要求を行わない。
有効化後の既存usage読取では、下記の残高workerがKeychainと残高APIを使う。

`--disable-dsh` は候補・ロゴ配信・残高の取得と行を無効化し、配置済みassetを保持する。
flagなしの更新では既存選択を保ち、fresh installでは無効のまま。
選択とassetは通常installerのownership manifestへ入り、通常uninstallの対象になる。
利用者のKeychain・worktree・会話はこの選択操作の対象にしない。

## 取得前確認の境界

`integrations/dsh/opt_in.py` は明示的な bool の選択だけを受ける。
既定値又は `False` では、取得項目も前提runtimeの計画も空である。
env、HOME、ユーザー設定、鍵を参照せず、processやネットワークを起動しない。

明示選択しても phase は `planned` に留まる。固定したdshとdsh-TUIの
版・出典・integrityを含む計画を返し、そのdigestに一致する確認データが
なければ取得項目を渡さない。Nodeは既存の固定実体を使う前提だけを表し、
自動取得の許可を作らない。依存package全体のlock、管理patch、実行実体の
固定と検査は後続作業であり、この二つのtarballだけで導入完了とはしない。

確認データのschema検査は、確認者の認証や人間の同意を証明しない。
製品の確認経路への接続、実取得、active generationの切替は未実装である。
owner検査、proxy factory、process検査、local gateは共通部品を使う。
このmoduleで別の認証・登録・起動許可の仕組みを作らない。

## この区切りで検証するもの

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_dsh_opt_in.py
```

stdlibだけで直接検査する場合は `python3 tests/test_dsh_opt_in.py` も使える。
既存のinstaller試験helperから実際の `install_payload` を一時directoryへ
実行し、専用payloadが配置されないこと、coreのserver/UI/launcherのbytesと
server選択が変わらないこと、取得・鍵・登録用commandが呼ばれないことを確認する。
通信やdsh実行は行わない。liveのinstaller/mainやserviceも起動しない。

これは配置関数のnull caseであり、fresh install全体、catalog/HTTP consumer、
TUI・cockpit・Mail・権限境界が受入済みという試験ではない。共有部分への接続は
共通基盤の統合順序が決まってから行う。

## 表示の接続と確認範囲

`tests/test_dsh_presentation.py` は同じHTTP catalog/asset経路で無効・有効・更新・再無効を
対照する。既存の数値版DeepSeekの分類は無効時も保つ。`tests/test_dsh_logos.py` と
`tests/test_network_node.py` は同じnetwork・deck・usage関数とdemo buildを使い、
無効時の非表示と従来のnetwork形状を対照する。catalogの `runtime:false` はUIでも
送信不可にする。DeepSeek残高の取得やTUI起動が接続されたという意味ではない。

networkは明示 `provider=local` の入力だけを六角形＋ライムの縁にし、
解決後のmodel名からQwen等の作り手を示す。未知はロゴなしの中立座金にする。
この入力の正本化と実機確認はR3 local統合で行う。local DeepSeekのasset有効化も
dsh導入と独立した共通profileの能力へ接続する必要があり、現段階の配信はdsh opt-inに従う。
`dashboard/demo/build.sh [outdir] --enable-dsh` は明示の表示fixture用で、既定demoは無効。
`demo/dist` は生成物であり、このrepositoryでは追跡していない。

## DeepSeek API残高を既存usageへ表示する（実鍵確認前）

通常の鍵なし・Keychainロック・通信失敗 × 残高を0と誤認する害、
及び鍵漏洩時の大きな害を対象にする。鍵を使った確認やliveの変更は準備に含めない。

公式の [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance/)
は `GET /user/balance` で利用可否と通貨別の金額文字列を返す。
取得workerは固定TUIの `lib/types/deepseekBalance.js:19–21` と同じBearerを使う。
usageにはUSDの総額だけを投影し、CNY・付与額・入金額は表示しない。
USDが無ければ「USD の残高なし」、取得失敗は「不明」。0へ置き換えない。
利用可否は公式APIのアカウント全体の値であり、falseは警告色にする。
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
