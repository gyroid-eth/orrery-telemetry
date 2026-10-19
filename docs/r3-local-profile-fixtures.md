# R3 local-model profile: red acceptance fixtures / 実装前の受入 fixture

2026-10-15。承認済みの設計 v3 §§1–7 を対象とする #266 の契約 fixture の記録。
#266 自体は tests-only で確定した。後続の [core その1](r3-local-profile-core.md) が
入力検査・認証・設定導出を実装するが、local profile を通常の子で使う入口はまだ
有効化していない。R2-Ta #263 と R2-Tb の後に接続する。GLM は Responses/Messages の
互換性を条件とし、Chat Completions の変換 gateway は含めない。

This records the tests-only contracts finalized in #266. Subsequent
[core part 1](r3-local-profile-core.en.md) implements validation/auth/projection,
not working child-launch integration. The remaining integration cases stay red
until R3 is wired to existing common launch/receipt/tool-plan paths. Default
selection, live services, trust and user configuration are unchanged.

## ケースと green の意味 / Case map

| Fixture | 設計・既存 caller 表 | 実装後に確認できること / Green means |
| --- | --- | --- |
| `test_profile_contract.py` | §§1–2, 6–7; V3/V6 | literal schema、固定 root・UID・mode・regular file・symlink/hardlink/unsafe parent、名前、adapter/env/URL 制限。無視せず拒否。全 adapter/argv を含む canonical digest、TOML 表記の不変性。 |
| `test_intent_contract.py` | §§1, 3, 6–7; V3/V6, A12 | resolver の **source ラベル非依存性**（実 caller の検証ではない）、保存 intent と実行投影、固定 layout の owner/pending/receipt の bytes・UUID・hash、shadow owner の走査。profile と LaunchIntent は独立 canonical preimage に照合。 |
| `test_caller_contract.py` | V3/V6; L6/7/10/13/67, L33/71/73 | 実 shell/client prepare・resume/operator/dashboard と R2 の delivery-only 接続。捕捉 binary の argv/env/cwd の経路間一致、実 model/HOME/caller field の literal を照合する。保存点の中断・再構築・未確定 receipt（pending replay）は未接続で red。native の受入ではない。 |
| `test_caller_tools_contract.py` | 実5入口の tools 選択 | 空の追加選択を保存して Mail/shell 設定を保つ既存 matrix と、非空の正規 MCP 選択を保存する独立 matrix、`mail`/`shell` selector の拒否を分離。実入口の binding は未完で red。測定器の正負対照と既存 grammar の直接検査は実入口の合格と数えない。 |
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

最初の2本は測定器試験。3本目は、未実装の保存・caller/native 接続が **red (exit 1)**
として残る。core ごとの対象と QA 結果は上記のその1の表を参照する。skip/xfail/importorskip は使わず、
collection は完了させる。既存試験の代用や、merge 済みの機能の CI 成功とは数えない。
既存の `test_child_tools.py` / `test_codex_models.py` /
`test_settings_permissions.py` も同じ条件で変更前後に直列実行する。

The full run is deliberately failing and belongs in a draft PR. Do not hide its
failures with a marker, skip, or production shim. Existing regression tests must
still pass. Fixture listeners use OS-assigned loopback ports and are always shut
down; the in-process network guard permits only currently registered fixture
socket addresses (including credential sources), never arbitrary loopback ports.

## 未完の接続部 / Incomplete bindings, explicitly not evidence

以下は設計から切り出した **private seam の契約**であり、public API ではない。
その1の部分実装と未実装 binding を区別する。既存共通経路へ接続するときに名前を合わせてよい。契約の
期待値を捨てたり、test 内に resolver/permission engine を実装して green にしない。

| Module | Expected private seam |
| --- | --- |
| `local_model_profiles` | その1の core: `LocalModelError`, `load_profile(root, ref, expected_uid=...) -> dict`, `profile_digest(dict) -> sha256` |
| `runtime_launch_intent` | その1は `resolve_launch_intent(caller, context, source=...)`, `materialize_launch_intent`, `intent_digest`, digest 参照の `make_launch_receipt`, `project_local_model`。`save/load_launch_intent`・explicit `revise_launch_intent` は未実装 |
| `local_model_auth` | その1の core: `LocalModelError`, `run_auth(plan, executor=...) -> bytes`, `validate_runner(...)`; executor は credential command のみを代替する |
| `tests/r3_local_profiles/native_acceptance.py` | **未実装の test adapter** `probe(scenario, workspace=..., provider=..., ...) -> observations`。実装時に real pinned CLI・既存 launch/receipt・native permissions/hooks と fake Mail を接続する。製品 API として追加しない |
| `tests/r3_local_profiles/caller_acceptance.py` | **全5入口への delivery-only 接続、R3 全体は未完**。隔離 install と専用 Python から `spawn_child.sh --model local:fixture`、`RuntimeClient.prepare_child/resume_child`、`RuntimeOperator.spawn`、実 HTTP→`dashboard.server.do_spawn` を呼ぶ。task を実準備した後に既存 R2 へ渡す。pending replay・native は未完で red。正常経路の entry/parser/resolver 置換や source ラベル付き resolver の直呼びで代用しない |

caller の土台は installer の宣言から隔離 payload を作り、実 manifest inventory と
source bytes を照合する。専用 Python の site-packages にある fixture module を
`.pth` から読み、`runtime_native_scope.require_native_binding` **一つだけ**を
配送試験用に置換する（無効時は置換0）。host Python・製品 source は変更せず、
unverified proposal を承認済みにもしない。他の owner/profile/source/R2 gate は実物のまま。
この土台の成功は、実入口での全 gate 到達・argv/env 配送・native 強制の証拠ではない。
全5入口の空/非空 MCP 設定を実 R2 execute と比較する対照は、この土台とは別に要求する。
ps が拒否された実配送・前提は失敗のまま残し、外側の確定 commit で確認する。
pending replay、native tool の利用、実入口の常時空 MCP 変異、製品 CLI source drift の
実負例は後続 gate として残る。

The delivery-only bootstrap copies the installer-declared payload into isolation
and checks the real manifest inventory and source bytes. A dedicated Python
loads a fixture module through its own site-packages `.pth`, replacing only
`runtime_native_scope.require_native_binding` (zero replacements when disabled).
It changes neither the host interpreter nor product source, and does not approve
unverified proposals. Other owner/profile/source/R2 gates remain real. Bootstrap
success is not proof of real-entry gate traversal, argv/env delivery or native
enforcement. Separate five-entry comparisons require actual R2 execution and
empty/nonempty MCP configuration. A real ps refusal stays a failed delivery or
precondition and requires outside confirmation at a fixed commit. Pending replay,
native tool use, actual always-empty-MCP mutations and production CLI source-drift
negatives remain required.

`test_real_tools_mutation.py` は実 TEST adapter の入力 tools だけを configure/実入口の
前に `{}` へ落とし、既存の実5入口 checker と非空 MCP の literal 期待値を変えずに
再実行する。保存 owner/capture/receipt の後書きはせず、入力と adapter は必ず復元する。
実 shell/R2/provider の配送と既存 capture 検査を通った後の tools 不一致だけを検知とする。
実 gate・終了 receipt・実行前 hash も検査する。最初の shell で止まる1変異であり、
5入口それぞれの独立変異ではない。setup/ps 拒否は失敗のまま、外側の確認待ちとする。

`test_real_tools_mutation.py` drops only the actual TEST adapter's incoming tools
to `{}` before configure/real-entry calls, reusing the unchanged five-caller
checker and independently literal nonempty MCP expectation. It never rewrites
an owner/capture/receipt into a mismatch and restores caller and adapter. Only
the designated tools assertion after actual shell/R2/provider execution and
prior capture checks counts, with real gate/ended receipt/pre-run hash evidence.
It stops at the first shell: one actual mutation, not five independent bad-entry
controls. Setup/ps refusals stay failed and require outside confirmation.

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

caller binding は各入口の直前に、停止を確認した前の試験所有 server の隔離 subtree
だけを捨て、既存の準備 API から新しい S2c を作る。同じ literal path/port を使い、
所有 Popen の終了と、新 config に一致する実 health の server_instance_id・
candidate_generation・authority_epoch を検査する。前 server が生存中、または応答が
別の世代なら失敗する。割当 agent ID を含む env の比較除外は増やさない。
resume の旧 owner は intent を比較する明示的入力であり、新 authority に旧 wrapper
を戻さない。新しい実 configure の intent と完全一致・独立 digest を照合し、実
prepare_child→R2 spawn→SessionStart→正常終了から retained/session-index を得る。
最小 transcript は fixture 入力だが、session/receipt/終了状態を JSON で捏造しない。
前提 spawn の捕捉・nonce・witness は resume 本番と別であり、その捕捉では本番を
満たせない。binding-off の resume も前提 spawn だけを有効にし、本番の fresh trace
では置換ゼロと実 native gate の拒否を要求する。ps で前提が止まれば失敗のまま残す。

Each entry uses a fresh real S2c at the same literal path/port after its owned
predecessor has exited. Real health bindings must match the newly prepared
config; a live predecessor or stale authority is rejected. No extra environment
exclusions are introduced. Resume takes the previous intent only as an oracle
input, never reuses its authority wrapper, and verifies full intent/digest
equality after fresh configure and a real spawn/SessionStart/normal end. The
minimal transcript is fixture input, not native execution evidence. Separate
precondition captures cannot satisfy the actual resume. For binding-off resume,
only its prerequisite spawn is enabled; the actual invocation's fresh trace
must show zero replacements and refusal by the real native gate. A failed spawn
precondition remains red rather than being counted as that refusal.

入口ごとに未作成の capture path と新しい nonce を発行し、nonce は捕捉 binary の
本文に埋める（argv/env は変えない）。以前の捕捉が残っていても、今回の path と
nonce の双方が一致しなければ失敗する。prepare-child/dashboard を含む全5入口で
no-op と古い capture のコピーを負対照として実行する。

caller の固定 owner は `isolation/agentstack/clients/child_01/profile.json` とする。
R2 の起動ごとに異なる `AGENTSTACK_LAUNCH_ID`・`AGENTSTACK_LAUNCH_NONCE`・
`AGENTSTACK_PROVIDER_PID` の **literal 3 key だけ**を比較から分離し、前2つは実
`runtime/launch.json`、PID は捕捉 process 自身の `os.getpid()` と独立照合する。
欠落・不一致・古いcapture、3 key以外の差/余分/欠落を負対照で拒否する。
それ以外の env と spawn の raw argv/cwd は全部比較する。resume は **全 argv** を照合し、取り除ける
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

tools の空の追加選択は `base=mail-only, tools={}` とする。Mail と shell/files は
選択する追加 tool ではなく、この base でも残す既存の機能である。保存 intent の空
選択とは別に、捕捉した provider-home の TOML と argv から bound Mail proxy と
shell 設定の保持を確認する。native の利用成功を証明したとは数えない。
独立した非空 matrix は literal `tools={"mcp":["fixture_extra"]}` と、隔離内の
`/usr/bin/true` を command とする MCP 定義を全5入口へ与え、保存選択・起動設定・
既知の startup prompt を照合する。「常に空へ捨てる」誤実装は保存 intent の literal
検査で失敗する。`mail`/`shell` を selector として渡す10件は、各実入口から返る
拒否と client tree 不変・捕捉/witness 未作成を要求する。adapter は fixture 準備後・
実入口直前に callback を呼び、生の非zero結果/例外/API errorを返すこと。adapter内で
拒否結果を合成したり、未知の selector を捨てたりしない。

The empty and nonempty selection matrices are independent literal contracts,
not a comparison against an adapter-produced expectation. Oracle-only controls
exercise an always-empty binding and lost Mail/shell/MCP configuration; direct
grammar controls also reject the builtin names as selectors. These controls do
not establish native tool availability or real-entry acceptance. The actual
caller adapter has a delivery-only bootstrap, but its binding remains incomplete
and red; rerun the always-empty mutation through real entries after connecting
it. Native tool execution remains a separate gate.

Mail proxy の script は owner の固定 layout から oracle が独立に決める
`workspace.tmp/install/bin/lib/runtime_client.py` と、生の `args[0]` を完全一致で
照合する。隔離 install の regular/non-symlink な実体と checkout source の bytes
一致も要求する。Mail command は literal `workspace.tmp/delivery-python/bin/python`
に固定し、regular copy の bytes を元の試験 Python の canonical executable と照合
する（venv は copies で作り、symlink は許さない）。Darwin の非 framework 共有 build
は、同じ試験 Python の sysconfig の
LIBDIR/LDLIBRARY が指定する libpython だけを private venv/lib に regular copy し、
配備前の digest・元/先 bytes・mode/no-link を初期 probe と各 bootstrap で検査する。
framework build はこの copy を行わない。不足や起動失敗は固定 reason で止まり、
別の interpreter へ fallback しない。CI の実 build の分岐は専用 startup 試験の
`R3_DELIVERY_PYTHON_BRANCH` log で確認し、手元の分岐から推定しない。観測値を
resolve して別名を受け入れたり、製品 resolver/adapter の返した path を期待値へ
使ったりしない。
空/非空の両 matrix で、存在しない path、suffix だけ一致する別 root、file/directory
symlink、同 path の wrong bytes を拒否する。別 command・元の試験 Python 自体の
指定も拒否する。実 adapter 接続後にも同じ誤設定を実入口で拒否する gate を残す。
偽 wire peer の tool 名も、bare の
`fixture_echo` と正規の `mcp__fixture__fixture_echo` だけを完全名で受理し、別serverや
余分なprefixが同じ末尾になるだけではschemaが同じでも拒否する。

The oracle independently derives the literal Mail script path
`workspace.tmp/install/bin/lib/runtime_client.py` from the fixed owner layout,
not from observed argv or product resolver output. Its installed regular-file
bytes must match the checkout source. The command must equal
`workspace.tmp/delivery-python/bin/python`, a regular venv copy whose bytes match
the original test Python's canonical executable. Darwin non-framework shared builds additionally copy
only that same interpreter's sysconfig-declared libpython into private venv/lib.
The pre-copy digest, source/destination bytes, mode and no-link checks run before
the initial probe and in every bootstrap. Framework builds do not copy it.
Missing resources/startup failure stop with named reasons, never another Python.
The dedicated CI startup test logs `R3_DELIVERY_PYTHON_BRANCH`; local framework
or standalone results do not establish the branch used by setup-python.
Missing, same-suffix other-root,
file/directory-symlink and wrong-bytes resources fail before cross-caller
comparison; another command or the original host Python path also fails.
The wire instrument also uses an explicit pair of exact fixture tool names,
not wildcard server prefixes. These are oracle checks; real caller/native
integration and production source-drift checks remain separate required gates.

外部command seamの固定 driverは全callerで同じpath/bytesを使い、製品の承認済み
CLI source digestの対象であり続ける。fresh nonce入りの捕捉scriptはfixture所有の
dispatchから選び、captures直下・UID・mode・regular/no-link・hashを追加照合する。
driverのfresh witnessはscript/hashと同PIDを捕捉結果へ結び付ける。期待するscript
hashはcaller実行前にoracleのローカル値へ固定し、実行後の現物とwitness双方を照合する。
scriptとdispatchの連動変更も拒否する。外部path/../symlink/mode/別UID・hash・古い
scriptの負対照を持ち、dispatch/scriptの別UIDは正規directoryのまま各fileの検査と
拒否理由を確認する。source改変のoracle負対照は
製品gate合格の代わりではなく、後段adapterでも製品gateの負対照を必要とする。
**実Claude/Codex binaryを起動した証拠ではない。** check→exec競合は同UID所有の
一時rootと0500 scriptという試験の脅威モデル内だけで許容し、製品境界とはしない。

Only the three literal R2 launch UUID/nonce/provider-PID environment keys are
separated, and each requires independent receipt/process evidence. All other
environment keys, argv and cwd remain exact comparisons. The fixed fixture
driver retains its approved source digest; variable capture scripts have extra
fixture-only path/owner/mode/hash checks and a fresh same-PID witness. This seam
pins each script's expected hash before the caller runs and compares both the
post-run bytes and witness to that independent value, including coordinated
script/dispatch edits. File-UID controls must reach the named resource guard.
This seam does not prove actual Claude/Codex execution. The check-to-exec race is accepted
only inside the owned temporary root with 0500 scripts, not as a product security
boundary. Production source-drift gates must still be exercised by the eventual
real caller binding; an oracle-only tamper control is not that evidence.

directory側の検査も別の対照として残す。fileをすべて正規のままcaptures directoryの
mode/UID/symlinkだけを変え、directory固有の拒否理由とexec/capture/witness未到達を
要求する。directory guardだけを外す変異がこの対照で落ちることも実行確認する。
Independent directory-only mode/UID/symlink controls keep every file valid and
require the directory-specific refusal before exec/capture/witness creation.

operator の full form は共有 caller dict と固定 parent client-name を実 API に渡す。
prepare-only は実 prepare_child/task 準備までで、起動しないことを独立に検査する。
配送の比較では、その結果を既存 R2 の foreground 境界から同じ fixture binary に渡す。
別の通常実行の正例は製品 operator 自身から R2 を通し、adapter の execute helper に
救済させない。古い admitted digest は実 R2 の row/window 作成前の拒否も要求する。
設定済み owner は変えず、実 operator に入る caller の各欄だけを変える16対照で、
required 全13欄と optional の省略対空/false/既定値の不一致を拒否し、client tree が
不変であることも確かめる。owner から入力を埋め戻して通す実装は合格しない。
専用 launcher/実 tmux は足さない。これは detached transport/native の受入ではない。

shell は実 parent.prepare_child を前提にし、その段で task が未公開であることを確認する。
次に実 install の shell を全 literal と --prepare-only で呼び、本番 CLI process の
entry/task helper 到達、task bytes、owner 不変を独立に検査する。その後だけ既存 R2 の
foreground 配送へ進み、前提時の digest を pin する。補助処理は task 準備を再実行せず、
no-op を救済しない。保存 detached=true は変えず、通常 detached/tmux/native の証拠には
数えない。空/非空 MCP・prepare-only・16欄の incoming 差・実 R2 の stale pin 拒否を分ける。
binding-off では前提だけを有効にし、実 CLI の fresh trace は置換0と実 native 拒否を要求する。

dashboard は、試験 process の main thread で実 HTTPServer/Handler を1要求だけ処理し、
client thread は必ず join する。capability は隔離 home に fixture の明示 setup で作り、
argv/env/trace/capture には出さない。実認証・Handler・do_spawn・backend/operator・task
helper の同一 PID を要求する。full request は必ず prepare_only=true とし、実 status/body、
task bytes/hash・phase・identity/固定 parent・owner と独立 digest の不変を確認する。
その後だけ別の TEST R2 foreground helper を task_prepared=True・保存 digest の pin 付きで
呼ぶ。HTTP 内では foreground を実行せず、helper が task を再公開して no-op を救済しない。
保存 detached は変えない。空/非空・prepare-only・16欄の差・stale pin を分離する。
binding-off は実 HTTP503 と実 native gate が認証/root と同一 PID で拒否したことを要求する。
通常 HTTP foreground・detached/tmux・native の受入を証明するものではない。

実 mutation の対照は、接続済み prepare-child/resume-child/operator の実 API method、
shell の実 CLI root、dashboard.server.do_spawn を試験 subprocess 内だけで no-op に置き換え、独立 literal の method 名・実 file・
実 PID と installed/activated の両記録を照合する。準備の欠落または resume phase の
不一致という指定理由、fresh capture/witness 未作成、client tree 不変を要求する。
これは明示した負の変異だけの追加置換で、正常 binding の置換を増やさない。
stale は前回の実捕捉を current script に返させる。実入口/R2 が正常に通り、事前 hash・
不変 driver bytes・今の witness/PID を確認した上で、今の nonce 不一致で拒否する。
shell は正確な install の __main__.main で entry_operation 一つだけを置き換え、
追加置換の名前/数/PIDと実 activation を検査する。内部 task helper の stub ではない。
dashboard は module の関数 identity を前後比較し、do_spawn 一つだけの変更と実 HTTP
activation、正確な module/qualname/file/PID・置換名/数を確認する。seed なし対照も
task 欠落を R2 helper より前に要求する。無関係な例外や setup の失敗、ps で seed 配送が
止まったものは合格しない。**全5入口**の実 no-op/stale と全体比較を要求し、oracle-only
の成功で代替しない。

The operator adapter passes the complete shared caller dictionary and explicit
fixed parent client to the real API. Prepare-only is independently checked not
to start a provider. Delivery comparison then uses existing foreground R2; a
separate ordinary operator execution must reach R2 without the fixture execute
helper rescuing it. A stale admitted digest must fail before launch/window
creation. These are not detached transport or native acceptance claims.
Sixteen additional controls change only the actual incoming operator caller,
not its configured owner: all thirteen required fields and the three optional
presence distinctions must reject without changing the client tree. Backfilling
the request from the owner cannot satisfy these controls.

The shell adapter first uses real parent.prepare_child without publishing a
task. The actual installed CLI then receives the full literals and --prepare-only;
its process must reach real entry/task preparation, publish matching bytes and
leave the owner unchanged before existing foreground R2 may deliver anything.
R2 pins the prerequisite intent digest and never repeats task publication to
rescue a no-op. Saved detached=true is unchanged; ordinary detached/tmux/native
behavior is outside this proof. Empty/nonempty MCP, prepare-only, sixteen literal
differences and stale-pin refusal are separate controls. Native-off enables only
the prerequisite; the actual CLI trace requires zero replacements and real refusal.

The dashboard adapter handles one actual HTTPServer/Handler request on the
invocation main thread; its client runs on a joined thread. Explicit fixture
setup creates the isolated capability, never exposing it in argv/env/trace/
captures. Real admission, Handler, do_spawn, backend/operator and task calls must
share one PID. HTTP always uses full prepare-only input; actual status/body,
task bytes/hash, identity/parent/phase and unchanged owner/independent digest are
checked before a separate TEST R2 foreground helper with task_prepared=True and
the saved digest pin. HTTP never owns foreground execution, and the helper may
not republish a missing task to rescue a no-op. Saved detached is unchanged.
Empty/nonempty, prepare-only, sixteen literal differences and stale-pin controls
are separate. Native-off requires actual HTTP503 and the real native refusal in
the admission/root PID. This proves neither ordinary HTTP foreground nor
detached/tmux/native acceptance.

Actual no-op controls mutate only the selected prepare-child/resume-child/
operator API method, actual shell CLI root or dashboard.server.do_spawn inside the TEST invocation subprocess. Independent literal
method/file/PID and installation/activation records, exact preparation/phase
refusal, no current capture/witness and unchanged client tree are mandatory.
Only explicit negative mutants add this replacement, never normal binding.
Stale controls emit a prior actual capture from the current checked script after
real entry/R2 execution; unchanged driver bytes, pre-run script hash and fresh
same-PID witness remain required, followed by rejection at the current nonce.
The shell mutant changes only entry_operation at the exact installed __main__.main
and checks that additional replacement's name/count/PID and actual activation,
not an internal task-helper stub. The dashboard mutant snapshots module function
identities and changes exactly do_spawn, checking the exact module/qualname/file/
PID, replacement name/count and actual HTTP activation. A seedless control must
reject missing task before any R2 helper. Unrelated/setup errors and ps-blocked
seed deliveries do not pass. Actual no-op/stale controls and full comparison for
**all five entries** are required; oracle-only success cannot substitute.

pending replay の fake transport は receipt を未確定に保ち、exec-unknown の勝手な再送・
取消はせず operator 解消を要求する。

The native adapter remains absent; the caller delivery-only bootstrap exists,
but its binding is incomplete and red. A trace with
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
未完の実 caller binding の代用ではない。dashboard の直前の成功は operator とする。
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
