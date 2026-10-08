# Codex plugin 導入処理の再利用

この変更は、本体 install に Codex の履歴・Resume を取り込む最初の準備段階です。専用 installer の処理を再利用できる単位へ分け、設定・所有情報と hook の信頼結果を記録します。本体 install/update/uninstall と cockpit の接続は後続の変更です。この段階だけで通常 install に plugin が自動導入されるとは説明しません。

## 呼び出す単位

`scripts/lib/codex-app-integration.sh` は source 時に処理しません。各関数は subshell で動き、呼出側の変数・shell option・終了処理を変更しません。

| 関数 | 処理 |
|---|---|
| `agentstack_codex_app_plan` | 専用 installer の dry-run。設定・cache・service を書き換えない |
| `agentstack_codex_app_apply` | 専用 installer の導入。選択 cache を検証し receipt を作る |
| `agentstack_codex_app_refresh` | 有効な既存 plugin の cache 更新。Bridge の env/service/runtime を再初期化しない |
| `agentstack_codex_app_remove_registration` | plugin の解除を registry で確認し、所有 service の停止処理へ進む。共有 payload と runtime を残す |
| `agentstack_codex_app_remove` | 従来の専用アンインストール。登録解除に失敗したら payload/runtime を残して非0 |

既存の install/uninstall script はこれらを呼ぶ wrapper です。専用 installer の既存引数は維持します。通常 install の履歴導入の有無を選ぶ新しい質問・option・env は追加しません。

呼出側は `--shared-codex-home` で解決済みの共有 HOME を渡せます。install-state.json に binary、共有 HOME、plugin ID、marketplace/root、選択 cache と payload digest、所有情報、hook_trust と history を記録します。enabled だけで信頼や履歴対応を認定しません。設定には token 本体や transcript を追加しません。

解除は保存した共有 HOME で行います。削除前に現在の plugin と marketplace の registry を取得し、ID・local source・root を保存した所有情報と照合します。service の所有確認も先に行い、別の所有者への付替えや確認不能を検出したら、登録・payload・runtime を保持して非0で終了します。CLI の戻り値だけで成功にせず、解除後の registry を再取得します。途中で plugin だけ解除できた場合も、残った marketplace の所有を確認して再実行できます。他の plugin が使う marketplace とその snapshot payload は残します。

nohup service は起動時に PID・UID・command・起動時刻を記録し、停止前と待機中に同じ process かを照合します。起動時の記録は期待した runner の command が現れ、続く観測でも一致してから行います。再利用された PID や、生きている旧 pidfile に識別情報がない場合、`ps` で確認できない場合には signal を送りません。launchd は label に加えて plist の所有者・実行引数と現在の job の実行引数を確認します。

Linux/WSL の起動識別は `/proc` の boot ID と process の starttime tick を使い、`ps` の表示時刻の揺れを比較に含めません。UID/command の取得前後でも tick が同じかを照合します。kernel 情報の取得不能や途中の process の置換は拒否します。Mac の起動時刻は `ps` の秒単位の値なので、同じ秒に同じ UID・command・PID が再利用される場合には識別の限界があります。いずれの環境でも照合直後の process の置換を完全には防げません。

`remove_registration` の終了時には plugin.enabled を false にし、JSON の結果を stdout に出します。service_stop_requested は停止処理の要求であり、service_stopped は process の終了または launchd job の不在を確認した結果です。未確認を成功として記録しません。core 全体の撤去と最終 report は後続の変更です。

## hook の定義確認と信頼

`scripts/codex_plugin_trust.py` は model/thread/login を呼びません。公開 app-server protocol の `hooks/list` と `config/read` で対象を確認し、`config/batchWrite` の `hooks.state` に当該 key の trusted_hash と enabled だけを upsert します。user config の expectedVersion を渡し、信頼を再取得してから成功を記録します。

plan は approved=false で生成します。呼出側が現在の承認経路で本人に表示・確認してもらった後だけ approved=true とし、専用 installer へ `--approved-hook-plan` を渡します。これは本体接続用の引渡しで、利用者に別の操作を要求する導入経路ではありません。後続の本体接続では通常 install の既存 yes 1回に含めます。

plan は対象 binary/HOME、plugin ID/root、payload digest、宣言と runtime が返す実定義を保持します。適用前に同じ対象・定義・payload と照合し、他の hook を承認しません。JSON の変更、API 未対応、policy 拒否、競合、再取得で信頼が確認できない場合は needs_review と `/hooks` の案内に戻します。plugin の配置自体と信頼の成否を分けます。

CLI 0.161.0 の隔離確認では6 hook の trusted/enabled を再取得でき、無関係な hooks.state は維持されました。history は unobserved です。同版では metadata が平坦な handlerType/command で、実効 timeoutSec は宣言の1/5秒に対して全て600秒でした。plan は実効値も表示し、宣言値のまま実行されるとは扱いません。metadata の新しい handler 形式にも対応します。

App の共有、既存 process への反映、実 session の BOUND と Resume はこの確認に含みません。

## Mac / Linux / WSL の隔離確認

Python 3.11以上と実行できる Codex CLI が必要です。Python の追加 package は不要です。既存 CLI または一時 npm prefix の CLI の絶対 path を渡します。CLI 自体のインストール・更新はこの script は行いません。

```bash
python3 scripts/check-codex-plugin-isolated.py \
  --codex-binary /absolute/path/to/codex \
  --output /tmp/codex-plugin-check.json
```

script は一時 HOME/CODEX_HOME、local marketplace、cache を作り、配置 digest と対象6 hook の信頼を確認します。`--version` の結果、配置確認、信頼、無関係な設定の保持、network 遮断の有無を JSON と stdout に出し、成功0・未確認/失敗1を返します。全体上限は120秒で、`--timeout-seconds` で変更できます。fixture とログは結果の fixture path に残します。

network は Mac の sandbox-exec、Linux の unshare -Urn が使える場合に遮断します。使えない場合は network_blocked=false と制約を明記し、local plugin command と config/hook RPC だけを使う確認へ進みます。model/thread/login は呼ばず、実 HOME の環境や auth を引き継ぎません。script の approved はこの合成 fixture だけの試験用で、製品の人の承認を省くものではありません。

WSL では Linux 側の CLI を指定します。Windows の shim や Windows 側の registry を操作しません。

## 関連試験

repository の CONTRIBUTING.md の venv に加え、次を serial で実行します。

```bash
PYTHONPATH=.:packages/agentstack_mail/src .venv/bin/python -m pytest -q \
  tests/test_codex_plugin_lifecycle.py tests/test_codex_app_integration_codex_bin.py \
  tests/test_bash32_local_selfref.py tests/test_codex_app_packaging.py \
  tests/test_codex_app_plugin_hooks.py tests/test_docs_consistency.py
```

Linux/WSL も同じ関数・合成 API の試験を使います。実 CLI を必要とする試験は CLI が PATH にある場合に実行され、各試験の HOME/CODEX_HOME は分離されます。launchd の native 動作は Mac の別の検証です。
