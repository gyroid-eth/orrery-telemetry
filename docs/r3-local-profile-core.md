# R3 ローカルモデル core — その1

[English](r3-local-profile-core.en.md) · [契約 fixture](r3-local-profile-fixtures.md)

`local:<name>` の入力検査、非秘密の intent と provider 設定への導出、共通 auth runner
を独立したモジュールにする段階。その2では明示configureの固定保存先と、R2が保存済み
intentを読む検査を接続中。**nativeの起動承認は未確定でunsupportedのまま**であり、まだ
`spawn_child.sh --model local:...` を運用できる機能ではない。shell/dashboard の full caller は
配送用 fixture へ接続済みだが、本人設定・trust の運用変更、実 CLI の権限・skill 読込の
受入は未完了。

## 境界と private API

| モジュール | 仕事 | 担当しないこと |
| --- | --- | --- |
| `bin/lib/local_model_profiles.py` | 明示 operator root の profile 読取、schema/UID/mode/link/URL/env 検査、全 profile の canonical digest | HOME/cwd からの検索、catalog/cloud への fallback、承認・配備 |
| `bin/lib/runtime_launch_intent.py` | 正規化済み caller＋承認済み context からの不変 intent、全 intent digest、再照合と共通 argv/env/config 投影 | 起動 envelope、native permission engine、既存 launch receipt の状態判断 |
| `bin/lib/runtime_caller_schema.py` | caller の共通構造検査。省略と明示入力の保持 | model/tool policy の別実装、operator 設定の受入 |
| `bin/lib/runtime_intent_store.py` | 固定 owner・既存 lock での保存、同一 intent の読取、pending を保護する明示 revision | 登録、別台帳、provider 起動 |
| `bin/lib/runtime_local_launch.py` | R2の同じowner/receiptからの読取・再照合、部分投影と既存envelope/envの合成、別native gateへの照会 | 自動configure、nativeの検証・承認、別launcher |
| `bin/lib/runtime_model_catalog.py` | 既存Codex/Claude policyへの委譲と、保存したformal ID/effortの再検査 | 第二のmodel/alias表、resume時のdefault/alias再選択 |
| `bin/lib/local_model_auth.py` | 固定文脈での bounded subprocess、runner/依存物の照合、既存 owner 参照からの helper 実行 | token の env 化・保存、別 auth 台帳、登録、provider の起動、通常起動での自動配備 |

`resolve_launch_intent(caller, context)` の context は、operator の明示 configure が
所有する入力である。HTTP 入力や子の環境をそのまま渡さない。`effective_scope` は
既存側が合成・承認した実効 plan であり、この core が native 規則を解析して新たな
許可を作るものではない。source ラベルは resolver を分岐させない。

`materialize_launch_intent(...)` は profile と設定 source、scope、runner の差分を
読取だけで検査する。`execution` は起動／resume で共通の **部分投影**であり、
実行可能な完全 command ではない。local-profileは`inherit_env=false`、
`requires_native_validation=true` を持ち、caller の env は採用しない。
consumer は既存の固定 provider HOME/安全な基本 env、tools/MCP・skill、native
resume envelope を維持したうえで、最終 config の credential/provider/settings
競合と scope の native enforcement を検証する必要がある。検証できないものを
起動可能として扱わない。Claude の `dontAsk` だけで権限の上限が完成したとはしない。

## その2: 入力と唯一の保存先

以下は接続作業中の private 契約であり、local 子を運用可能とする宣言ではない。
全 caller は同じ構造検査を使い、catalog の既存検査と local の検査を混ぜない。

| caller field | 保存・解釈の規則 |
| --- | --- |
| provider / model | provider alias だけ正規化。model は選択子から catalog / local-profile の tagged 値へ解決する。local の実 ID は登録 metadata に使う。 |
| cwd / task / name / parent | 明示した作業場所・task 参照・対象・親を保持。root や identity を検索する入力にしない。 |
| tools / base / clear_tools | 空の選択と clear を区別して保持。解釈は既存 tools policy に任せる。 |
| resources / worktree / chrome / detached | 明示値を保持。profile が予約・配置・transport を指定し直さない。 |
| effort / task_delivery / strict_output_cap | 省略した key は作らず、空文字・明示 false と区別する。未対応な要求は拒否し、黙って無視しない。 |

global の prepare/resume Python API に明示した `parsed_tools` も、登録・native gate・lock の前に既存 tools 文法で検査します。未知の `mail` / `shell` は `CHILD_TOOLS_INVALID` で拒否し、正規の非空 MCP 選択を空へ置き換えません。legacy preview の非空 tools 拒否は、この文法検査とは別の制約のままです。

完全な intent は global の固定 `profile.json.launch_intent`、legacy の既存
`child-agents/NAME.json.launch_intent` のいずれか一つだけに保存する。同一 intent の
再保存は bytes を変更しない。既存 profile writer も intent と execution を保持する。
別 revision は明示確認を必要とし、registration/enrollment/mutation pending、未確定の
launch、未完了の終了報告・cleanup があれば拒否する。receipt に本文を複写しない。

明示名を取る purge/reregister は、通常 selector が選んだ固定 context の canonical
name と完全一致するかを非作成の admission で検査する。別名の資格検索や暗黙 self
への置換をしない。一致後も既存の fresh owner 検査を維持する。

| 拒否条件 | 固定 reason |
| --- | --- |
| global mode だが context が無い（対象引数より先に判定） | `CONTEXT_UNAVAILABLE` |
| 固定 global context があるが、対象引数が未指定・不正 | `TARGET_UNAVAILABLE` |
| context の canonical name が未証明 | `GLOBAL_TARGET_OWNERSHIP_UNKNOWN` |
| 明示名・資格・fresh owner の不一致 | `REGISTRATION_IDENTITY_MISMATCH` |

壊れた context は既存の path/identity reason で拒否する。いずれも別 root 作成や
legacy fallback をせず、既知の名の不一致は network・purge・reconnect 前に止める。

## Profile と digest

root は絶対 path・同 UID・0700、profile は同 UID・0600・regular・link 数1。
dirfd と NOFOLLOW で祖先もたどり、symlink と unsafe writable parent を拒否する。
OS 所有の sticky temporary directory は祖先として許す。読取は64 KiBまで。
名前は `[a-z][a-z0-9_-]{0,47}`、未知 field・空/制御文字 model・不正数値を拒否する。
window/output は TOML の符号付き64 bit正整数で `output < window` とする。
この宣言は CLI の実効 context を実測した証明ではない。

remote は HTTPS、query/fragment/userinfo は不可。HTTP は認証無し loopback だけ。
env は Codex が空、Claude が `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` の1–100だけで、
model/auth/接続先/HOME/PATH/identity/loader の別口を作らない。

profile digest は schema 検査後の全 profile、intent digest は `intent_digest` field
自身だけを除いた全 intent の canonical UTF-8 JSON（key sort、空白無し、array順保持）。
両 adapter、auth argv と承認済み runner/依存物・文脈、実効 scope と設定 source の
digest を含み、token・cache・mtime は保存しない。

## CLI 版の比較契約（native 受入とは別）

比較関数は、承認時と観測時の CLI・実効 scope・非 CLI 資源・model/endpoint/auth/context
の導出を JSON 値として受け取るだけで、CLI・認証・filesystem を実行／探索しない。
版が違っても、同じ canonical path・UID・導入先で安全な CLI と、正規化後に完全一致する
scope・他の導出が揃えば、旧新版と本体 digest を比較結果に残す。承認済み intent は変えない。
同版で bytes だけ違う場合、別 path／導入先、他者書込可、他の資源の drift は拒否する。

比較から外すのは明示した版の provenance だけ。native proposal 自体の digest を先に確認し、
native 設定・組込操作・source closure・未知の作用・list の順序は消さない。
JSON の boolean／整数／浮動小数も混同せず、Bash の独自解釈や rule の並べ替えはしない。
これは比較の fixture 契約であり、観測値の正しさや native 権限の強制を証明しない。
採用済み native observer が無い実 CLI は引き続き unsupported で、比較結果だけで起動しない。

共通 producer の比較結果は、既存 R2 の prepare／execute／provider-exec で再照合し、
同じ `runtime/launch.json.cli_acceptance` に metadata と digest だけを記録する。
明示 configure が固定する導入元は、安全な canonical 実行 file の親 directory と UID
という filesystem の証拠であり、package の採用元や native 対応能力の証明ではない。
旧 intent にその証拠が無い場合は、従来どおり同じ CLI bytes だけを受理する。

standalone auth helper は同じ owner と比較証拠、active な UUID／nonce／phase を読み、
既存 runner PID／出生時刻と、実 helper→provider→runner の同 UID ancestry を共有の
source-pinned process 観測で前後照合する。順序比較は全 process をその場で UTC として
観測し直し、同じ秒は許可する。zone の無い保存 birth は同一性の照合にだけ使う。
観測不能・再利用・親の変化・古い receipt は鍵の取得前に固定理由で拒否する。
provider PID の別台帳、承認 plan の書換え、通常起動での configure は追加しない。

この段の TEST binding は一つの native gate だけを置換する接続用の観測値で、
実 CLI の起動継続、TTL、header、権限／skill 強制や native38 の受入ではない。
公開 caller への版通知と、採用 CLI observer の接続・実測は後段に残る。

## Auth helper の導出

認証がある場合、呼出側は既存の唯一の owner を `context.intent_owner` で明示する。
新たな auth 設定 file や推測した owner path は作らない。Codex の command-backed
auth と Claude の safely quoted `apiKeyHelper` は同じ runner＋owner/digest/provider
参照を使う。helper は owner・runner/依存物・profile・settings を再照合してから
固定 cwd、`PATH=/usr/bin:/bin`、`LANG=C` と承認済み HOME/USER/LOGNAME だけで取得する。
5秒・stdout 8 KiB（末尾 LF 1個だけ許可）、stderr は捨て、異常は固定 reason のみ。
stdout は bounded pipe として読み、timeout/超過時は自身が作った process group を終了する。

配備の所有者は明示 configure。runner 自体は stdlib のみの1 fileで、
承認済み絶対 interpreter と `-I`（isolated startup）の shebang、0500・親0700を
持つ配備物を参照する。runner の import 前から caller の PYTHONPATH/user site を信用しない。
installerはhelperを配布するだけでrunnerを自動配備しない。下記の明示configureだけが
承認済みのrunner・非秘密設定・skill linkを既存manifestへ登録する。
CLI 内の refresh は300000 msへ導出するが、実際の発火・header・既存ログインの
無継承は後続 native gate。token を取得するのは helper であり argv/env に載せない。

Codex の設定根拠は [公式 config reference](https://learn.chatgpt.com/docs/config-file/config-reference)。
`auth.command` の stdout と cwd/timeout/refresh を使い、`env_key`・直書き bearer・
`requires_openai_auth` を認証付き定義に混ぜない。output 予約は context-output から
compact limit に導出し、架空の `model_max_output_tokens` は書かない。

## その2: 明示 configure（接続作業中）

既存のoperator用instruction-configureに、隔離global clientのlocal plan/applyを追加する。
通常のlaunch/resume、HTTP model入力からはconfigureしない。既存のprovider instruction
configureは従来の2引数のまま使える。このlocal操作はCodex hook trustを承認しない。

```sh
agentstack-runtime-client instruction-configure operator codex local-plan < request.json > plan.json
agentstack-runtime-client --assume-yes instruction-configure operator codex local-apply PLAN_ID < plan.json
```

request.jsonのtop-levelはcaller、context、resourcesだけ。contextは明示root、
effective_scope、承認settings digest、最小auth_contextだけで、CLIが現在UIDと固定ownerを
与える。resourcesは絶対python/cli、宣言CLI版、settings_paths、native_sourcesのpath/layer、
skill_sources、任意の非秘密auth_dependencies。未知keyやowner/UIDの注入は拒否する。
planはfile/lock/bytecodeを作らず、applyは同じplan IDと明示`--assume-yes`が必要。
新しいplanやscopeの承認を通常起動へ紛れ込ませない。

完全intentの保存先は同じprofile.jsonの1箇所。登録前は同fileのtyped prepared stageを
使い、agent ID・資格・登録を捏造しない。taskは承認hashと固定retained copyを照合する。
変更revisionは既存C/fenceとmanifest mutexの下でpending/実行中状態を拒否してから適用し、
同一設定の再適用はreadonly no-op。出力は既存install-state.jsonの所有記録へ結合し、
未所有物のadopt、外部auth/skill sourceの所有・削除はしない。実uninstallへの接続は後段。

native設定の提案は未知key/設定作用をunsupportedにする厳格subset。Claudeの非空の
継承allow/deny/ask、未知のhook/plugin/env等を黙って消さない。Bashを自作parserで
分解・判定せずnative ruleのまま扱い、追加allowを権限上限とは呼ばない。
Codex workspace-writeもper-file/per-commandの制限と同一視しない。

採用CLIのcapability契約は未確定。版は宣言とbinary hashであり実測ではなく、settings
sourceの存在/不在hashだけではmanaged/cloud等の網羅性を証明しない。提案はbuiltin操作と
source closureをunverified、`requires_native_validation=true`、`launch_admissible=false`
として同じintentへ保存する。自己申告のverified flagでは起動可へ昇格できない。
**configuredは資源の配備完了であり、native38の合格やlocal子が運用可能という意味ではない。**
capability未確定の実CLIはunsupportedのまま、native38は採用版を実測で確定する段まで
redで残す。後段のcaller16が固定driverで示すargv/env配送と、nativeの権限・設定の強制は
別の証拠であり、前者の成功を後者の合格とは扱わない。

## legacy の明示 preview 設定（準備段階）

通常の selector が legacy と確定した operator process では、同じ `instruction-configure operator claude local-plan/local-apply` を使える。
global context の拒否を外して迂回する操作ではない。通常の provider instruction configure と global の plan/apply は変更しない。
context の既存欄に `execution_policy` と `legacy_context` を明示する。前者は
`{kind:"local-preview-v1",native_scope:"unverified",native_isolation:"unverified",operator_decision:"非秘密の参照"}`。
後者は schema_version=1、canonical install_root/runtime_dir/owner、候補 name、project_key/project_id、mail_url と、固定 parent の id/name/program/model だけを持つ閉 schema。
候補名は既存 picker から先に取得する。既存の登録名を選択する機能は追加しない。

最初の preview は `tools={}`、`base=default|mail-only` で Mail・予約・shell・Skill を接続する範囲です。外部 MCP は未対応で、空でない tools は作用の前に拒否し、黙って空へ捨てません。

親は operator の既存 `AGENT_NAME` と caller.parent/target.parent が一致する場合だけ canonical credential を読む。
同じ Mail の既存 `register_agent(existing_agent_id=...)` 所有認証を必須にし、whois は ID 候補の照会だけに使う。
新規登録・NULL owner の claim・provider 起動・鍵取得は行わない。token を plan/結果へ保存しない。
plan は読取専用、apply は同じ plan ID と `--assume-yes` が必要。保存先は同じ `child-agents/NAME.json` の prepared intent で、設定結果も `launch_admissible=false` のまま。

private provider HOME の `CLAUDE.md` も同じ明示 plan/apply の生成物です。固定 install の template と共通 instruction renderer を使い、template/source の digest と生成物の所有権を既存 manifest に結びます。通常の利用者の指示を暗黙にコピー・移行・上書きせず、未所有の既存 file は採用しません。内部の登録 consumer も生成済み指示の bytes・private mode・regular/単一 link を再検査します。これは設定資源の証拠であり、実 CLI の読込み・skill の実効・起動の受入ではありません。proxy と実 hook/起動の接続は引き続き作業中です。

private HOME の `local-model-mcp.json` も同じ承認済み生成物に含めます。Mail の名前は承認済み scope と通常の orrery-mail に限り、command/args に token を載せません。既存 proxy の固定 source と、認証なし profile でも使う明示 Python の path/UID/mode/bytes を同じ intent の evidence に固定します。Python 自体の実行や実 proxy の成功を確認した意味ではありません。

内部の preview proxy factory は既存の stdio MCP server・direct binding・HTTP client を再利用し、別の identity/snapshot 台帳を作りません。各 call で同じ登録済み owner・active launch と実 helper→provider→runner の同 UID ancestry/出生情報を前後照合し、設定・source・生成物の差分を Mail の作用前に拒否します。認証なし profile も同じ検査を通り、credential は tool/status に返しません。factory の配送と検査の fixture は、実 provider 起動・hook・native 受入の完了を意味しません。

内部の `entry runner|provider|proxy NAME` は同じ登録済み owner だけを消費します。runner は固定 intent digest/登録 generation、provider は今回の launch ID/nonce だけを受け、任意の argv/env/owner/observer は選べません。P は unknown の保存後に Q を作成して待ち、Q は同 UID の実親・出生情報と全資源を照合して同 PID で exec します。子の作成後に owner・token・親の観測の検査が失敗しても、P が保持する実の child を wait・回収してから失敗を返し、unknown を終了成功へ救済しません。既存の純粋な provider argv projection を再利用し、ambient credential を子へ継承しません。`-I` の Python 起動も固定 install の sibling library だけを読みます。ここまでの検査は偽 P/Q/exec と実 Python の status/拒否経路であり、実 provider 起動・hook・remote cleanup・public spawn の受入ではありません。

preview の認証 helper と登録 consumer は、同じ共通 reader で生成済み指示・MCP/native 設定・選択済み skill link を再検査します。認証は process 観測の前後と TTL 後の取り直しも同じ基準です。共通 renderer と reader は固定 install の検証済み source から読み、未検証の module cache・別の描画実装・不足資源の自動修復は使いません。変更・欠落・型/mode/link の不一致は、鍵 command を呼ぶ前に固定理由で停止します。skill directory は entry 集合と link 自体の UID・単一 link・target を照合し、列挙失敗は拒否します。native 設定は既存の純粋 projection を共有し、別の設定 renderer を作りません。これは生成物の drift 検査で、native 権限や skill の実効の受入ではありません。

内部の `entry session-start|end-session NAME` は、同じ active owner/process 検査の前後照合と owner CAS で session receipt だけを保存します。同じ session の再通知は冪等、別 session・End 先行・End 後の Start は拒否します。SessionEnd だけで wait・予約解放・retire を完了させず、保存後の再検査が失敗しても receipt を巻き戻して成功化しません。legacy hook の Python は明示 configure の runtime Python pin と一致させ、通常 global の生成経路は変えません。notifications と公開起動・終了 join はまだ接続作業中で、実 native hook の受入ではありません。

内部の pre-edit は同じ active session を照合し、保護 project 内の path で通常の900秒 renewと既存の競合 reader を使います。相対 path は保存済み cwd が基準です。予約不足・他者の排他予約・観測不能・service失敗は編集拒否になります。初回 preview の post-edit は active/session の再検査だけで、通常の子の90秒自動 release は行いません。予約は明示の Mail `release_reservations`・通常の期限切れ・後続の終了 joinまで保持され、他の writer をTTL中止めることがあります。TTL/renew規則を延長せず、通常の子の hook は変更しません。ここでの偽peer/OS対照はnative hookの実効や終了join完了の受入ではありません。

preview は通常の native gate を承認せず、権限上限・隔離・CLI の対応を検証済みにしない。
登録の保証は「通常の子と同等。atomic create-only は未保証」。実行 record は
`registration_guarantee={level:"ordinary-child",atomic_create_only:false}` を保持する。
現在は public な preregister/spawn/dashboard の成功経路と実 hook の接続作業中であり、設定の成功だけで local 子が運用可能とはしない。

内部の固定 helper は `status NAME` と
`resolve NAME --expected-state-digest DIGEST --action finalize-registration|abandon` だけを受け付ける。
選択した install の `bin/lib/runtime_legacy_preview_cli.py` であり、新しい launcher ではない。
status は読取専用・credential 非表示。明示の登録確定は、固定の親と保存済みの子を
既存の `existing_agent_id` 所有認証で取り直し、応答不明から新規登録を再送しない。
ID/model 不一致・認証拒否・古い digest は unknown のまま残す。abandon も未確定の実行を
成功に変えたり、その名前を自動で再利用できる状態にしたりしない。

旧 resume/undo/cleanup/register は予約済みの欄と新tagの pending を拒否する。
通常の canonical credential 保存と最終 local cleanup は同じ名前 resume lock を使い、
遠隔操作後にも再検査する。credential 保存の失敗を登録成功として返さない。
これらの保護や登録確定を provider の起動承認には数えない。

## その2: R2 consumerの接続中

R2の初期計画・retry・runner/exec直前は、同じ固定profileのintentを再照合する。
起動receiptには`intent_digest`の参照を足し、比較 binding が返る場合だけ
`cli_acceptance`（旧新版のCLI metadataと比較digest）も同じ`launch.json`へ記録する。
完全intent・scope・auth設定やprovider PIDの別コピーは作らない。
保存task hash、profile/CLI/activeとfixed両renderer/settings/runner/scopeの差分を拒否し、
古いreceiptに現在のintentを後付けしない。登録・再登録・登録確定もnetwork前に検査する。

localの最小envと既存argv envelopeを共通関数で合成し、通常起動は資源を再生成せず
auth helperも実行しない。Claudeは明示配備済みの同じsettingsを参照する。これらは
native gateを通る前の設定検査であり、未確定のcapabilityを起動可能とは扱わない。
catalogの純粋な導出は既存policyへ委譲する。intent無しの旧global profileはR2の
native-default互換を維持し、readだけでcatalog intentを作って保存しない。

catalog tagは設計の`{kind:"catalog", requested, id}`に揃える。
**設計への追加**として、解決済みeffortも`model.effort`に保存し、resumeで既定を
再解決しない（Claudeは`None`）。callerの入力は`intent.effort`で省略・`None`・空・明示を
そのまま保持する。共通readerはtagのfieldを読むだけで、第二resolverにはしない。
未知Codex modelの省略effortは空文字として保存し、resumeでもeffort引数を加えない。
保存済みの空とfield未保存は区別する。これはCLIへの委譲とargvを維持するもので、
CLI内部で選ばれるeffortのtierまで固定するものではない。
登録途中のread-only admissionも既存pendingの証拠を検査し、lockや資格を新規作成しない。
実際の登録継続・資格の確定は従来のC/fenceとowner照合を通す。

full caller の配送接続とは別に、legacy の実起動入口・画面・実uninstallは後段。
固定driverによる配送の証拠とnative38の権限強制の証拠を混同しない。

## その2・内部段階1の試験（起動接続前）

保存・revision・pending の既存 red 6件を閉じた。関連既存試験は前後とも502 passed、
R3 は282 passed＋6 failed から288 passedへ。追加の保存/schema/実wrapper試験58件を
含む最終実行は **848 passed / 未接続adapterの43 errors / sandbox既知2件 deselected**。
未接続の内訳は caller 5件・native 38件であり、全体 green や local 子の運用成功ではない。
明示対象検査後に context の名が更新される競合も、fresh network 前の再検査で拒否する。

## その1終了時の red 分類と試験記録

fixture #266 の対象122件、追加の core/runner 試験40件、既存の計測器/負対照120件と
公開用チェック58件を直列実行し、**340 passed / 範囲外6件 deselected** を確認した。
開始時 load1 は12.98。表の件数は #266 の収録ケース数である。
全 R3 の実行は282 passed／保存・revision の6 failed／未接続 adapter の43 errors。
予期しない core failure はなく、全体が green になったとは扱わない。
既存関連3 file は変更前後とも286 passed（既存の Authlib 警告2件）。

| #266 のケース | その1の対象 | 残す境界 |
| --- | --- | --- |
| profile 67 | green | configure/CLI の対応能力は別 gate |
| auth 15 | green | native CLI の helper 起動/TTL は含まない |
| intent 40 | 純粋な digest/設定導出/入力 drift が green | 実 caller の一致とは数えない |
| intent 6 | red のまま | `save/load_launch_intent`・revision・pending と既存 owner の接続は未実装 |
| native 38、caller 5 | missing adapter の red のまま | 実 CLI・実入口・hook/Mail・skill・lifecycle の受入 |
| instruments/review controls 120 | 既存の負対照を変更せず green | 計測器の成功と native 成功を区別 |

`test_core_projection.py` は生成 TOML/settings を読み、試験用に明示配備した実 runner
を実行し、偽 TLS peer に model/path/header/tool turn と rotation を送る **試験用 HTTP
consumer**。native CLI や製品の protocol gateway の代わりではない。Claude header は
試験で選んだ形であり、CLI がその形を使うとの主張はしない。実装前の各負対照も
継続し、確認のために contract の期待値・skip/xfail を緩めない。

全試験は repo-local venv、隔離 HOME/TMPDIR/runtime、直列、開始時 load15未満。
既存関連286件を前後で実行する。live・Keychain・本物の鍵・実 CLI は使わない。
