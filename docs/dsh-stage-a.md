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
