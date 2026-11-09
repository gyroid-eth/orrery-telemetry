# R3 local-model profile: red acceptance fixtures / 実装前の受入 fixture

2026-10-15。承認済みの設計 v3 §§1–7 を対象とする **tests-only の draft**。
製品の local profile 対応を追加・有効化する変更ではない。R2-Ta #263 と
R2-Tb の後に接続する契約を先にレビューする。GLM は Responses/Messages の
互換性を条件とし、Chat Completions の変換 gateway は含めない。

This draft specifies tests before implementation, not working local-model support.
It must remain red until R3 is wired to the existing common launch/receipt/tool
plan paths. No product code, default model selection, live services, trust, or
user configuration changes are part of this patch.

## ケースと green の意味 / Case map

| Fixture | 設計・既存 caller 表 | 実装後に確認できること / Green means |
| --- | --- | --- |
| `test_profile_contract.py` | §§1–2, 6–7; V3/V6 | literal schema、固定 root・UID・mode・regular file・symlink/hardlink/unsafe parent、名前、adapter/env/URL 制限。無視せず拒否。全 adapter/argv を含む canonical digest、TOML 表記の不変性。 |
| `test_intent_contract.py` | §§1, 3, 6–7; V3/V6, A12 | resolver の **source ラベル非依存性**（実 caller の検証ではない）、保存 intent と実行投影、固定 layout の owner/pending/receipt の bytes・UUID・hash、shadow owner の走査。profile と LaunchIntent は独立 canonical preimage に照合。 |
| `test_caller_contract.py` | V3/V6; L6/7/10/13/67, L33/71/73 | 実 shell/client prepare・resume/operator/dashboard 入口の接続仕様。捕捉 binary が記録した argv/env/cwd の経路間一致と実 model/HOME/caller field の literal、client の保存点ごとの中断・再構築・未確定 receipt を照合する。test adapter はまだ未接続で red。 |
| `test_auth_contract.py` | §4, §7 | 偽 credential executable を executor seam で観測。絶対 argv・固定 cwd・空からの最小 env・5秒・8KiB・shell 無し。異常 stdout/exit/timeout は秘密無しの固定 reason。token rotation は profile/intent revision ではない。 |
| `test_native_contract.py` | §§3–7; V3/V6 | 偽 Responses/Messages peer に **実 CLI** が送った model/path/header/tool schema と、次 turn の call/result replay。native helper/TTL、予約 hook、外部 task Read→予約→Edit→renew→release→Mail、複合 Bash 全体の拒否、receipt と lifecycle の観測。 |
| `test_instruments.py` | §7 の測定器の負対照 | 各測定器が、誤モデル・Chat path・code_mode・tool result 欠落・異なる auth header・漏えい・権限拡大・部分的 Bash 副作用・cloud fallback・偽 pre-exec・replay drift を実際に検出する。**製品や native CLI の合格ではない。** |
| `test_review_controls.py` | r1 P2/P3 | 両 auth 形式とも拒否・helper 未使用・旧鍵の再利用、承認済み先頭だけの書込み、dict を保つ pending 上書き/shadow、定数/部分 digest、caller 独自 argv、未登録/終了済み socket、未信頼/異名 TLS の実行した負対照。native 合格とは区別する。 |
| native `skills`（Claude / Codex） | §3, §7; V6; L33/71 | 通常（cloud model）の子と local の子で **同じ fixture skill 読込元・一覧**。Claude は Skill を一回呼び、Codex は SKILL.md を読み、nonce を含む本文を native tool output で確認。sandbox/effective_scope は承認済みの範囲を維持し、instruction_configure を置換せず既存 source/config の hash を維持する。 |

## 実行 / Run serially

Repo-local `.venv` は CONTRIBUTING.md の手順で作る。HOME/TMPDIR/runtime を
一時 directory に置き、load average が15未満のとき、一つずつ実行する。
本番ポート、tmux、Keychain、実 credential/API、実 profile は使わない。

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q -n 0 tests/r3_local_profiles/test_instruments.py
PYTHONPATH=. .venv/bin/python -m pytest -q -n 0 tests/r3_local_profiles/test_review_controls.py
PYTHONPATH=. .venv/bin/python -m pytest -q -n 0 --tb=short tests/r3_local_profiles
```

最初の2本は green の測定器試験。3本目は、測定器以外が **missing implementation
で red (exit 1)** になるのが現在の期待である。skip/xfail/importorskip は使わず、
collection は完了させる。既存試験の代用や、merge 済みの機能の CI 成功とは数えない。
既存の `test_child_tools.py` / `test_codex_models.py` /
`test_settings_permissions.py` も同じ条件で変更前後に直列実行する。

The full run is deliberately failing and belongs in a draft PR. Do not hide its
failures with a marker, skip, or production shim. Existing regression tests must
still pass. Fixture listeners use OS-assigned loopback ports and are always shut
down; the in-process network guard permits only currently registered fixture
socket addresses (including credential sources), never arbitrary loopback ports.

## 未実装の接続部 / Missing bindings, explicitly not evidence

以下は設計から切り出した **private seam の提案**であり、既存 API の主張ではない。
R3 実装時、既存共通経路の薄い test binding として名前を合わせてよい。契約の
期待値を捨てたり、test 内に resolver/permission engine を実装して green にしない。

| Module | Expected private seam |
| --- | --- |
| `local_model_profiles` | `LocalModelError`, `load_profile(root, ref, expected_uid=...) -> dict`, `profile_digest(dict) -> sha256` |
| `runtime_launch_intent` | `resolve_launch_intent(caller, context, source=...)`, `save/load_launch_intent`, `materialize_launch_intent`, `intent_digest`, `make_launch_receipt`, explicit `revise_launch_intent`, `project_local_model` |
| `local_model_auth` | `LocalModelError`, `run_auth(plan, executor=...) -> bytes`, `validate_runner(...)`; executor は credential command のみを代替する |
| `tests/r3_local_profiles/native_acceptance.py` | **未実装の test adapter** `probe(scenario, workspace=..., provider=..., ...) -> observations`。実装時に real pinned CLI・既存 launch/receipt・native permissions/hooks と fake Mail を接続する。製品 API として追加しない |
| `tests/r3_local_profiles/caller_acceptance.py` | **未実装の test adapter**。`spawn_child.sh --model local:fixture`、`RuntimeClient.prepare_child/resume_child`、`RuntimeOperator.spawn`、`dashboard.server.do_spawn` を本当に呼ぶ。entry/parser/resolver は置換せず、外部 binary/Mail/tmux だけを fixture にする。source ラベルを渡す resolver の直呼びでは代用しない |

native test adapter も現在は missing import で red。これは「native を確かめた」
という意味ではない。green への条件は実 CLI の argv/env/cwd、native session ID、
peer の wire transcript、実ファイル副作用と hook/予約/Mail の観測を結び付けること。
trace を返すだけの偽 CLI/偽権限判定で代替してはならない。header は事前に
Bearer/X-Api-Key と決めず、実際に送った形式と peer の受理条件を照合する。
Bearer 専用・X-Api-Key 専用のうち非互換な Claude の組合せは unsupported が正解。
refresh は fixture clock の短縮と本番 TTL=300000ms の照合を分ける。

認証 matrix は同じ pinned CLI 版で必ず1形式以上の成功を要求する。rotation は
試験所有の credential HTTP source の取得 epoch と、試験所有 peer の旧/新 token
受理 epoch を照合し、自己申告の helper_calls/rotated_token_accepted は根拠にしない。
source は固定 helper の stdout にだけ token を返し、ファイル/env/argv に保存しない。
複合 Bash は cwd 内の先頭 sentinel と範囲外の末尾 sentinel の両 directory を
直接 snapshot し、先頭だけ実行する誤 admission も拒否する。

LaunchIntent の hash は `intent_digest` field 自体だけを除く canonical JSON
（sort_keys、UTF-8、空白無し、array 順を保持）とする fixture preimage の提案。
profile 全体、resolved auth の固定文脈・runner/依存物、effective_scope を含める。
これは resolver の実装ではなく、定数 hash・部分 hash を受け入れない独立 oracle
である。R3 の schema 確定時に envelope が必要なら、この明示 preimage と共に講評する。
hash の変更感度用 vector は hash 関数の単体入力であり、変更した timeout/TTL を
configure/launch で対応済みとする試験ではない（実行時の固定値は5秒/300000msのまま）。

caller binding は各入口の直前に fixture 内の client ディレクトリを再作成する。
resume だけは最初の保存 owner を明示的な入力として戻し、adapter が既存の
session-index/transcript を隔離内に準備する。これは今回の実行証拠とは区別する。
入口ごとに未作成の capture path と新しい nonce を発行し、nonce は捕捉 binary の
本文に埋める（argv/env は変えない）。以前の捕捉が残っていても、今回の path と
nonce の双方が一致しなければ失敗する。prepare-child/dashboard を含む全5入口で
no-op と古い capture のコピーを負対照として実行する。

spawn 系は raw argv/env/cwd を比較する。resume は **全 argv** を照合し、取り除ける
差は既知の末尾 startup prompt と、指定位置の正確な resume/session envelope だけ。
binding は task_delivery=mail・interactive を明示し、mail-only の既知の指示本文と
既存 Codex 起動の5組の option pairs 後（index 10）の `resume, session ID` を golden
にする。R3 が配置を変えるなら golden も明示的に講評して変える。adapter の trace
から削除対象を選ばない。headless の `exec, resume, session ID` も独立した計測器の
正負 vector で確認するが、実 headless caller の合格を主張するものではない。
追加 sandbox、欠落/並べ替え/重複 option、誤 session/位置/exec/prompt、env/cwd の
変更を負対照で拒否する（resume を新規 spawn にして一致させない）。
実 model・cwd・HOME と保存 caller fields にも別の
literal assertion を持つ。adapter が未知の flags/UUID を黙って捨てることは不可。
operator の prepare-only は **製品 API が現時点で受け取るという主張ではない**。
R3 接続時に既存 prepare/gate の境界を使い、保存済み command を同じ fixture binary
へ渡して捕捉する。専用 launcher/実 tmux を足さない。pending replay の fake transport
は receipt を未確定に保ち、exec-unknown の勝手な再送・取消はせず operator 解消を要求する。

The new native/caller adapters remain absent and intentionally red. A trace with
cli_kind/version/observations_complete is necessary metadata, **not native proof**.
Review their eventual real-entry invocation, process captures, wire, storage and
Mail/hook integration before accepting green; synthetic JSON artifacts do not
establish native conformance. Skill controls use temporary, approved ordinary-child
skill roots (Claude: `.claude/skills`; Codex: `.codex/skills` and `.agentstack/skills`)
and isolated child homes. They neither read real user skills nor call cloud APIs.

## 規則単位の対応と未収録 / Rule coverage and explicit gaps

「追加」は red の契約であって実装済みの意味ではない。下記の保留は green の保証に
含めず、該当する R3/native binding を対応済みにする前の gate とする。

| 設計規則 | r1 の case / 委ねる先と理由 |
| --- | --- |
| schema / adapter | model NUL/改行/control、共通 required field 欠落、片 adapter 正常、選択 CLI 節欠落、Claude URL/wire/env の独立負例を追加。model 長の数値境界は設計 v3 が上限数値をまだ定めていないため、恣意的な数値を固定せず schema 確定時の gate。adapter 内 required field の全列挙も schema 正式化時に追加する。 |
| 実 context / output | Codex context=131072 / compact=114688、Claude context=131072 / output=16384 / subagent force=1 の literal、Codex strict cap 拒否を追加。100K 未満/認識済み名/[1m] の clamp と実 wire/native subagent は CLI 版・観測方法が未確定のため native binding gate。偽 server の受理だけで実 context を証明しない。 |
| auth 出力 / 資源 | 8192 bytes（改行無し/8191+末尾改行）の正例、末尾改行0/1・余分な改行の負例、実 fake credential process の8193 bytes拒否を追加。runner 内容/digest/contract version・依存物の drift は収録済み。内容不変の mode/owner/symlink は配備 validator が未接続のため instruction_configure/native binding gate（profile の同様の検査で代用済みとは数えない）。 |
| auth 非継承 / TLS | key の unit と authless peer の負対照は既存。TLS 未信頼CA/異名証明書の負対照を追加。OAuth/provider 選択/settings-env/custom header の native 負例は採用 CLI の設定 source 合成が未接続のため native binding gate。普段のログイン済み HOME を複製して試さない。 |
| effective_scope / 委任 | 両端の実副作用0を追加。外部 Read→予約→Edit→renew/release→Mail は収録済みだが、Read 本文が指示として使われる証明と fake Mail の成功 receipt、必須5 tool全ての不足・実行中の root切替/権限拡大・表現不能は native adapter が未接続のため追加 gate。tool 名列/trace bool だけでは合格にしない。 |
| 通常 launch read-only / save_profile | settings 1 file から fixture HOME 全体（profile root・runner/依存物を含む）の snapshot に拡大。保存した intent の再読取も追加。実 provider HOME の config/trust と既存 RuntimeClient.save_profile 連鎖は `caller_acceptance` に接続する gate（現時点の unit だけで保持を断言しない）。 |
| catalog / status / 計測 | project_local_model の N/A・非公開 field・auth/CLI 0 は既存。壊れた/CLI節無しの候補を空正常一覧にしないこと、status/dashboard/Mail の正規投影、quota取得/加算0 は各実 read caller に local 行を接続する段階まで保留。純粋 projection の試験で実 read 経路を代用しない。 |
| caller / owner / cloud | resolver の source 非依存性に改名し、実5入口と4保存点の red 接続仕様を追加。global profile.json / runtime child-state・launch・mutation、legacy child-agents の既存 layout を明示し shadow を走査。pending保護と新revision正例を分離。identity/binding不一致・再登録0は既存 global client/child entry 試験と caller binding gate に委ねる。通常 cloud の正例は下記の既存3 fileを変更前後に直列実行し、local の成功と混同しない。 |
| skill（追加決定） | Claude/Codex それぞれ、通常の子との一覧/読み込み元の一致と Skill/Read の本文取得を red native case に追加。新しい skill root/別 config 正本を作らず、既存 instruction_configure・子の home 接続を使う。実機の skill が読めたとはまだ報告しない。 |

### r1 の各 P2 と実行した負対照

| 講評 | 契約 / 独立測定器 | 誤実装を入れて落とす control |
| --- | --- | --- |
| 観点 P2-1 auth/rotation | grouped header matrix、credential source の取得と peer の受理 epoch | `test_auth_matrix_detects_no_native_success_or_unused_helper`、`test_wire_rotation_detects_old_token_reuse_despite_success_claim`（全拒否・helper無し・旧鍵再利用） |
| 観点 P2-2 複合 Bash | 先頭と末尾両方の file tree snapshot | `test_compound_bash_first_approved_side_effect_is_detected`（承認済み先頭だけを書換） |
| 観点 P2-3 pending/owner | 既存 layout の実保存物、UUID/hash/bytes と再読取、全 JSON の shadow走査 | `test_persisted_intent_oracle_detects_dict_preserving_overwrite`（dictを保って owner/receipt/pending を書換、3種の shadow） |
| 観点 P2-4 digest | 全 LaunchIntent の明示 canonical preimage と receipt 結合 | `test_independent_intent_digest_rejects_mutant_preimage`（定数・auth除外・scope除外・別profile） |
| 枠なし P2-1 caller | 実5入口の red 接続仕様、実行した capture binary の argv/env/cwd | `test_single_caller_bypass_is_detected_by_real_capture_binary`（単一callerが独自GPT argvを生成） |

上の control は測定器に対して誤実装を実行して拒否を確認するもので、未接続の
製品/native/caller 本体で変異を動かしたという意味ではない。後続 binding は同じ
入力を実入口へ接続し、negative control の保証を捨てない。

### r2 の再講評と負対照

両講評の capture 持越し P2 は `test_each_caller_requires_its_own_capture_despite_previous_success`
で全5入口の no-op/古い nonce を拒否し、`test_caller_contract_itself_rejects_no_op_bindings`
で契約本体の検査順にも接続する。後者は意図的な偽 binding を使う計測器の正負対照であり、
未実装の実 caller adapter の代用ではない。dashboard の直前の成功は operator とする。
枠なし P3 の resume 全 argv は `test_resume_full_argv_rejects_every_non_envelope_difference`
で interactive/headless 各1正例と各12負例を実行する。

Native probes are specifications awaiting their integration adapter. Nothing in
this patch launches an installed CLI. Before enabling that adapter, use only
explicitly selected pinned executables, isolated provider homes and transports,
and deny non-fixture egress **including child processes**. The Python socket
guard alone does not contain subprocess networking. Do not auto-discover the
operator's logged-in CLI or reuse its live Mail/tmux/runtime. Authenticated native
peers use an ephemeral fixture CA and verified HTTPS; the native adapter must
explicitly trust only that fixture CA without disabling TLS verification. Plain
HTTP auth checks in instrument self-tests do not pass through the product loader.

`LOCAL_MODEL_REQUEST_FAILED` / `LOCAL_MODEL_PROTOCOL_FAILED` は受入 fixture 上の
仮ラベルであり、R2-Tb の既存 outcome/receipt reason へ binding 時に対応付ける。
exec 済み helper 失敗を pre-exec とすることや unknown の自動 retry は許さない。
canary はメモリ内照合のみで、peer transcript には header 名/形式/一致の真偽だけを残す。
鍵を fixture ファイル・環境・argv に入れず、failure message に値を出さない。

## 今回の範囲外 / Not established

R2-Ta/Tb の製品実装・全 suite・実機での GLM/量子化モデルの互換性・速度・品質、
実 provider の全 CLI 版、native settings の実効合成、実 auth helper header/refresh
はまだ未確認。cloud の既存正例は既存試験で守り、local 契約が green になった後も
全 caller 移植表と実機受入を置き換えない。挙動を変えない tests-only patch なので、
README/公開 user guide・API generation・製品 CHANGELOG は更新しない。
PR 用の tests-only fragment は `000266-r3-local-profile-fixtures.md`。

Wire instruments follow [Responses function calling](https://developers.openai.com/api/docs/guides/function-calling)
and [Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events),
plus [Anthropic Messages streaming](https://platform.claude.com/docs/en/build-with-claude/streaming).
Their self-tests verify construction and rejection, not a provider's conformance.
