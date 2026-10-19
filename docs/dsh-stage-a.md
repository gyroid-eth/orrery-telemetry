# dsh 対話子：段Aの準備

既定ではdsh / dsh-TUI / Nodeを取得せず、spawn候補やDeepSeekロゴを出さない。
installerの `--enable-dsh` で候補とロゴ配信を明示的に有効化できる。
現段階ではruntime未接続をcatalogへ示し、通常の子としてはまだ起動しない。
このflagでruntime/Nodeの取得、鍵の読取、登録、API要求は行わない。

`--disable-dsh` は候補・ロゴ配信を無効化し、配置済みassetを保持する。
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

## DeepSeek API残高をusage欄に表示する設計（未実装）

通常起こる鍵なし・Keychainロック・通信失敗 × 残高を0と誤認する害、
及び鍵の漏洩時の大きな害を対象にする。準備中はAPIへ要求しない。

公式の [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance/)
は `GET /user/balance` に `is_available` と `balance_infos` を返す。
通貨はCNY又はUSD、`total_balance`・`granted_balance`・`topped_up_balance` は
文字列の金額である。通貨ごとに表示し、異なる通貨の合算やquota百分率への変換はしない。
認証は固定TUIの `lib/types/deepseekBalance.js:19–21` と同じBearerを予定する。
この読取APIが課金されないことは未検証であり、準備後の実物確認で記録する。

dsh opt-in時だけClaude・Codex・Antigravityと並ぶusage欄に出す。
`is_available=false` は既存の警告色で示す。3〜5分のcacheを使い、欄を開いた時
又は既存refreshから期限切れ分だけ更新する。子・turnごとには問い合わせない。
鍵なし・Keychainロック・認証失敗・通信不可・不正応答は「不明」にする。
前回値を残すなら取得時刻と古い値であることを併記し、現在値として扱わない。

Keychainの `deepseek-api` は取得専用の短命processだけが読む。
そのprocess内のHTTP headerだけへ渡し、dashboard常駐processのenv、argv、
log、保存file、ブラウザへの応答に鍵を含めない。workerはtimeout付きとし、
終了statusと検査済みの残高だけを返す。TUIの自動残高取得を開放する理由にはしない。

共有fileへ接続する段で、次の既存経路を拡張する（行番号は設計時点）。

| 接続点 | 変更する内容 |
| --- | --- |
| `dashboard/quotas/__init__.py:13` | opt-in時だけ残高adapterをserviceへ登録する。無効時は取得も欄も増やさない |
| `dashboard/quotas/antigravity.py:24–28,38–56` | timeout付きの別processという既存方式を参照し、残高専用workerを呼ぶ |
| `dashboard/quotas/base.py:29,102,131` | 百分率bucketと分けたaccount balanceの応答形を加える。金額文字列と通貨を保持する |
| `dashboard/quotas/service.py:78,92,144` | bucketの有無に依存する観測判定を残高にも対応させ、cache・前回値の期限を共通管理する |
| `dashboard/server.py:8965,9043` | 既存 `/api/quotas` の経路・同一originの検査を使い、秘密を応答へ出さない |
| `dashboard/index.html:36951,37027,37145,37173` | DeepSeekロゴ・金額・利用可能状態・不明/古い値をusageに描画する |

実装時にはAPI世代とAPI文書を揃え、合成fixtureでopt-in無効、正常、
利用不可、複数通貨、鍵なし、lock、timeout、不正応答、古い値を対照する。
実Keychain・残高APIの確認は別の承認済み実行に分ける。
