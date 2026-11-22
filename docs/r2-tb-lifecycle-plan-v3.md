# R2-Tb v3: 起動・終了 lifecycle と red fixture

基準 `b6d4720a25ee1b6617912b2b144cd5fecf1f3387`。**実装前の計画・試験だけ**。
R2-Ta の対象解決を使い、専用 socket の canonical-name 起動と報告→cleanup を閉じる。
新しい server tool、mode flag、report ledger、profile 正本を追加しない。
本案の schema は `tests/fixtures/r2-tb-lifecycle-v3.json`、同じ値を本文に二重列挙しない。

## Caller → 現在 → Tb の接続（基準 commit の行）

| Caller / source | 現在 | Tb / 対象の証明 | fixture |
| --- | --- | --- | --- |
| `hooks/spawn_child.sh:9` → `runtime_client.py:2314` | global spawn→start(detached) | canonical ID/nameを既存 observe で解決、専用 `-S`。name prefix fallbackなし | F4.01–04 |
| `bin/agentstack-resume:17`、`runtime_client.py:2314` | global resume→同start、保存sessionを渡す | 同じ transport gate、同意済み provider session/argv は変更しない | F4.03,06 |
| `runtime_operator.py:237,261`、`dashboard/global_backend.py:137,142` | spawn/resume→start、OPENはTa attach | startだけTbへ。global HTTP EXIT/history はR4、cockpitはR6 | F4.01,06 |
| `runtime_delivery.py:840,1029` | start/retryが裸tmux・UUID名 | 共通global_socket、canonical名、原子的new-session、同nonce再開 | F4.01–04 |
| `runtime_delivery.py:868,905` | Claude/Codexはexec置換 | 既存execute-launchをrunnerに。provider完了をwaitして共通end_sessionへ（PIDの役割は後述F4.07） | F4.05,F5.01–07 |
| `runtime_delivery.py:906,933,1161` | Geminiのwait/end、persistent/cold wake | 同じ共通endとlaunch/outcome規則。persistentのreply/notification receiptはS2bのまま | F4.06,F5.06 |
| `bin/agentstack-gemini-child-mail:160`、`runtime_client.py:2510` daemon-serve | helperは明示retire action→end_session、daemonはfinally | 同じnonce/sessionの終了、上位runnerとの二重end防止 | F5.02,06 |
| `global-client-loader.sh:139,145` | disabled/partial install admission→end hook | selector/disabledの既定を保持、終了経路を別dispatchへ迂回しない | F5.05,07 |
| `runtime_delivery.py:943`、`runtime_client.py:2344` | SessionStart→liveとrunning | PID/start/pane/nonceは既存live。新規foregroundは専用paneかtmux外のみ。既存standalone継続 | F4.04–06,F5.05 |
| `cleanup-child-agent.sh:9`、`release-all-reservations.sh:7` → `runtime_client.py:2329` | 両hook→end_session | runner/Claude SessionEndとも同じC mutex・同sessionのend。Codexの架空hookは作らない | F5.02–05 |
| `runtime_client.py:1684,1711` | 最後のsessionで即release/retire | 必要な報告が確定/不要/確定拒否になるまでactive。retireだけがchild ACTIVE releaseを担う | F5.01–05 |
| `claude-initial-task-status.py:322,380`、`codex-initial-task-status.py:160,214` | 既存分類/起動通知 | 同じclassifierを呼ぶ。起動通知と終端通知は同種でも別event、重複watcherは作らない | F5.01,02,06 |
| `hooks/monitor_child_agent.sh:19`、Ta `runtime_target.py:132` | 証明付きcapture/compact、end CLIなし | 同じtarget/権限/digestからendへ。旧UUID・standaloneはrename/adoptしない | F5.03,05,07 |
| `dashboard/server.py:8335` do_exit | legacy EXIT。Ta consumer | 旧挙動を維持。既存provider終了処理を抽出/注入可能にしてCLI endが共用、R4が後で接続 | F5.07 |
| `spawn_child.sh:2698` 以降の報告/title/attach案内、`skills/delegate/SKILL.md` | legacy名やbare attachを含む | globalだけID CLIと完全argv/env。legacy通知/タイトル/終了は不変 | F4.06,F5.06,07 |
| `runtime_observation.py` / `runtime_target.py:63–128` / watcher | Taのlive/transport projection | launchのglobal-instance印を読む。新しいsocket台帳/実行状態コピーなし | F4.01,04,F5.05 |

entry/end/exec/retryの限定grepは9file・31行、v1 source inventoryは別集合の17file。v3では下記補足を含む全sourceをJSONのsource_sha256に固定する（件数はそのkey数）。

| 補足の公開/間接caller | 接続・fixture |
| --- | --- |
| `RuntimeClient.launch` (`runtime_client.py:2063`)、main launch (`:2534`) | foreground起動。新default pane拒否・tmux外foreground・専用paneをF4.06/07、既存standalone継続はF5.05へ。 |
| `bin/agentstack-persistent:1499`→`profile_operation:2230` | typed profile run→daemon startをF4.06、daemon finallyはF5.02/06。profile解決はR3。 |
| `spawn_gemini_child.sh:4–9`、`spawn_gemini_preregistered.sh:4–9` | 実shell argvでspawn-child dispatchをF4.06へ。Gemini runner/helperの受入とは別に採る。 |
| `watch_agent_mail_signals.sh:19`→`legacy-tmux-consumer.sh` | 専用TMUX継承下でもlegacy bootstrapは固定、Ta F2/A18とR2-Cの同socket/case/旧standalone保護をF5.07の継承受入へ。 |

本表はTbの実装担当表で、全callerが接続済みという主張ではない。専用起動はTbと
配布launcherの対応・R2-C互換保護の確認がそろってから。Ta単体の有効化はしない。

## CLI/schema（提案。既存 API と誤認しないこと）

- start/retry/status/cancel/launch-resolve は既存 `operator-json` の request を維持。
  launch-retryのowned/running応答は既存launch-statusのschemaを参照し、gated専用のphase制限を外す。
  `execute-launch ID NONCE` は内部runner入口。新しい任意shell文字列は受けない。
- `end --agent-id ID --confirm-state-digest D`、`replay-end` は同じTa dispatcherへ追加。
  replayはpendingのdigest（既に無ければ明示null）を必須にし、状態と一致を確認する。
  cleanup中断でpending消去済みでも同じ入口へ戻れる。名前・root pathをtargetとして受けない。
- context無しoperatorは既存の明示global/server-config admission＋TTYの `yes`。
  owner/親は既存credential/child-state照合。兄弟/不明parent/誤digestは副作用0。
  人の入力待ちはdeadline/mutexの外。確認後にtargetとPID/start/nonceを再照合する。
- `target --agent-id ID --print` に lifecycle/next_operations のdeltaを加える。
  lifecycleのshapeはJSONのtarget_contract_delta.schema_atom_additions.lifecycle、next_operationsは既存列挙のdeltaを共有。
  endのためだけの別statusファイル/CLIは作らない。状態digestは既存target識別情報と
  live/launch終了phase/pending digestを含む。画面capture hashは終了対象の識別には使わない。
- 共通の拒否schema・reason→kind・exit codeはTa正本を継承。未知の送信結果は成功にせず
  pendingを保持。report確定拒否はS2b/S2cの既存phase別正本からのみ判定する。
- endは「停止要求」と「終了確認→報告→cleanup」を区別する。SIGTERM/exitの送信だけで
  endedにしない。外部wait/provider実行中にCを保持しない。複数対象のHTTPはR4担当。

## 終了後も戻れる共通 admission（F5.08/09）

Taの `Targets.resolve` はabsent PIDを除外するため、そのままでは使えない。**同じ
Targets snapshot/principal/digest**を拡張し、稼働runtimeとlifecycle回収を区別する。
別statusファイル/別resolverは足さない。Cの同snapshotでcontext/credential/child/launch/live/
mutationを読む。共通validate_owner_receiptでbinding・agent・generation・credential fingerprint
を管理inspectと照合し、一意root・旧nonce・新しい別launch・別live/unknownの保護を維持する。

| 観測/保存証拠 | targetの投影と次の操作 | 許可しないこと |
| --- | --- | --- |
| 稼働中provider＋runnerの両birth/所属一致 | runtime target。既存capture/attach/compactとendの権限 | 終了phaseを根拠に他PIDをsignal |
| providerとrunnerが不在、同launchのwait結果＋report pending | lifecycle target、digest＋pending digest、replay-end。同UUID/bytes | 不在をpre-execと読む、自動再launch、旧PID再利用先の操作 |
| retired child、report確定/不要/確定拒否、retire commit済み/local未完 | 検証済み親/operatorだけにlifecycle read/replay-end。inspectでretiredを確かめlocal残りだけ | retired ownerへの通常送信権限復活、retire二重送信 |
| SessionStartより前にproviderが実exit | 既存launch.executionのrunnerがwaitした結果でlifecycle target。liveが無いこと自体は失敗でない | 存在しないSDK session/liveを捏造 |
| SessionStart前にrunner SIGKILL、wait結果なし | read-only lifecycle/unknown。digestを出し既存launch-resolveへ。人のconfirmed-ended等が確定した後だけreplay-end | PID不在だけで未実行/終了済みと決める |
| birth再利用/別pane/別nonce/新launch/矛盾・別live unknown | lifecycle statusで不一致を示すか安全な拒否。作用/cleanupなし | 新しいprocessやsessionを旧終了として処理 |

parentはactiveな既存親credential＋child-stateの親ID/instanceを照合、兄弟は拒否。
active ownerは自己の回収可、retired owner自身は通常principalの拒否を維持し親/operatorへ案内。
retiredでreport pendingの場合は送信を試さず `resolve-mutation` の明示ack/abandonへ。
既存resolve_mutation末尾のwhois必須も、**親/operatorによるlocal解消だけ**は管理inspectの
fingerprint/generation照合に置換する担当をTbへ含める。未報告を承認した記録を既存launchの
operator_resolutionへ確定してからpendingを消す。新しい解消台帳・再有効化・enrollはしない。
解消前後のdigest/nonce/親関係をCの下で再照合。通常ownerのresolveや送信admissionは緩めない。

終了済みtargetに現在のsocket/paneを必須にせず、保存済みUUID/nonceとwait/明示resolutionの
終了証拠で回収する。現在同じ数値PIDが別birthなら過去の完了証拠として読むだけでsignal0。
standaloneは既存liveのowner/session/PID/startを使い、launch無しの不在/終了を自己ACTIVE解放へ
結び付ける。launch・runner証拠を作らず、別live/unknownがあれば解放/retire0。

## wait runner と provider のPID（F4.07）

単一pidを二つの意味で使わない。**launch.execution.runner のpid/startがpane基点**、
**既存live.pid/process_startが実provider**で、runnerの写しをliveに足さない。
実行待ち/exit結果は同じlaunch.execution.outcome、SDK session/nonceは既存liveのまま。
Taのresolve、runtime_tmux.live_target、Observations、monitor、通知、endがこの1つの
role-aware証明関数を共用する。既存exec型launchはexecution無しの旧receiptとして維持する。

- wrapperが自己birthをlaunchへ保存→既存gated条件を再検証→exec-unknown保存→Popen/wait。
  Popenの子（shimならnative providerまでの観測済みdescendant）を実SessionStart/既存matcherと
  結び、runner→providerのUID/PPID連鎖と両birthを前後2回照合する。nonceはlaunch/liveで一致。
  未検証のhook引数だけではproviderを採用しない。外部wait中にCは保持しない。
- AGENTSTACK_PROVIDER_PIDにrunner PIDを偽って渡さない。Popen直後の子PIDはrunnerのメモリで
  保持し、SDK hookは実providerのPIDを既存payload/ancestor観測から確定する。shim/nativeとの
  関係が証明できなければreadyにせずunknown（CLIやhookがない成功を捏造しない）。
- providerだけexit/runner報告中はactivity=ended、capture/compact/再入力0。runnerの生存は
  provider稼働の証拠ではない。runnerだけ消えてproviderが残る場合はunknown、勝手なsignal/
  cleanupをせずoperatorへ。provider再利用、runner再利用、別paneも同じ拒否規則。
- endの停止要求は既存provider終了処理を共有し、証明済み**provider**へ送る。Antigravityの
  SIGINTもprovider PIDだけ、runnerを先に止めない。runnerへのTERMはowned childへ転送して
  waitし報告/cleanupを残す。SIGKILLは確定待ち結果を作れないのでunknown。
- group escalationはPGIDの全対象が同じprovider subtreeでrunner/foreignを含まない証明が
  得られる場合だけ。runnerと共通PGIDなら拒否し、群へのsignalでrunnerを巻き込まない。
  TTY/argv/job-controlの既定を黙って変えず、必要なnative接続をF4.07の実装gateにする。
- SessionStart前のPopen失敗は実際に子が生成されなかったOS例外だけnot-startedと確定。
  生成済みならwait結果をlaunchへ保存してからendへ。SDK記録前のexitでも通常のoutcomeを保持。
  SessionEnd hookがprovider稼働中に来た場合は終了要求/観測に留め、runnerのwait確定前に
  retireしない。SDK /clearとprocess終了を同一視しない。standaloneの既存hook契約は別に維持。

## global の初期/終端報告 classifier（F5.10）

既存 `claude-initial-task-status.py::FirstTurn` の入力正規化を拡張する。watch/runnerは
検証済みchild-stateから親IDを一度解決して同じclassifierへ渡す（FirstTurnのparent_agent_id）。
legacy正例には親IDを設定しない実旧入力を使う。globalの旧to名・string/bool ID・別宛先receiptは負例。
legacyのto/name契約は維持、globalはto_agent_idsの正の整数IDだけを見る。文字列/booleanや
旧toの名前一致で親と推測しない。normalizer/分類器の写しは作らない。

親IDを含むsend_message tool_useと、それに対応した成功tool_resultがそろう時だけreported。
既存S2b responseのschemaとmessage_id/receiptを共通readerで照合する。is_error、失敗result、
result不在/応答不明は成功にしない。別ID宛の成功も親報告に数えない。
watchとrunnerが同時に終端報告する時も同じ安定UUID/launch終了phaseを通る。
Codexの既存task_statusはstarted/bound/unknownの起動診断であり、終端分類済みとは扱わない。
Codex/Geminiの実終端情報を同じreport policyへ渡すbindingはF5.06の未接続gateとして残す。

## 状態遷移と保存順（正本は既存 launch/live/mutation）

| 起動の停止点 | 保持するもの・再開 | 許可しないこと |
| --- | --- | --- |
| planned、bind前の同名/case競合 | foreign不変、bind RPC 0。同intent retry/cancel | 大小文字変更/切詰め、foreign adopt/kill |
| bind後、atomic new-session競合 | gated、bind receiptは残す。同nonce retryか既存cancel | bindを巻き戻したと表示、has-sessionをlock扱い |
| wrapper作成後・provider前 | 専用絶対socket＋canonical名、own gated wrapperを証明後だけgate開放 | 非協調case aliasがあればprovider実行。止めるのは自分のwrapperだけ |
| exec-unknown / new-session応答喪失 | UUID/nonce/liveを保持、既存launch-resolveを案内 | 自動再exec、別UUIDを作る、unknownを未実行と偽る |
| running → provider終了 | runnerは実exit/outcomeを採取、同じsessionのendへ | exec置換で終了報告を失う、別nonceを終了する |

launchには既存Taが読む `tmux_transport=global-instance` を保存し、socket値/PIDは
既存global_socket/liveから導出。旧UUID launchは従来receiptで解決、無断移行しない。

| 終了phase | 次の一手 | cleanup |
| --- | --- | --- |
| report-required | classifier→既存mutation slotへ全文/UUIDをHTTP前保存→launch phase | 不可 |
| report-pending / 結果不明 | statusの同digestからreplay-end。同じbytes/UUID、別pendingなら解消が先 | active維持。予約の自動延長なし |
| report-confirmed / report-not-required | receipt参照/不要結果をlaunchへ保存→pending消去 | 最終live/unknown再照合後だけ可 |
| report-rejected | 既存の確定拒否だけを理由付き保存、成功に見せない | 可。auth/binding/通信不明はこの行に含めない |
| cleanup-pending | 同じretire/release intent、commit後local失敗は管理inspectで再照合 | 報告は再生成/再送しない |
| ended | live endedとlaunch終端の残りだけ確定 | 同session/nonceの二重hook/runnerはno-op |

report UUIDは既存binding・launch UUID/nonce・通知種別から安定導出する（本文は含めない）。
本文はpendingだけに保存。intent後/phase前中断も同UUID pendingを見つけて回収する。
確定phase後/pending消去前は既存receipt照合で残りを完了。別nonce/live/unknownは保護。
retired ownerの一般送信を許可せず、既存明示ack/abandonで未報告の承認を得る。
standaloneはlaunch/report phaseを捏造しない。自己ACTIVE releaseの確定後にlive ended、
retireしない。tmux外はPID/startだけ。App activity unknownの既決境界を変えない。

## F番号 / red と誤実装対照

| F | 実caller・入力・中断点 | greenの意味 / 誤実装の負対照 |
| --- | --- | --- |
| F4.01 | 実start(detached)、Alpha、別socketに同名 | literal `tmux -S SOCKET new-session -s Alpha` / bare・UUID名・-Aをoracleで拒否 |
| F4.02 | canonical/case alias foreignが事前に占有、bind前/後 | 前はbind0、後はgated/own副作用だけ / precheckを後へ移す変異を検出 |
| F4.03 | 実launch-retry、absent/owned/foreign/PID差替え/nonce差替え | 全argvのsocket/exact target、-d、provider/gate作用。ownedだけstatus再利用、foreignは理由付き作用0 / 誤socket/誤名/no-op/無証明再利用を拒否 |
| F4.04 | 作成直前の競合/作成直後case競合/応答喪失 | provider0かunknown、foreign bytes/プロセス不変 / adopt・無断再送を検出 |
| F4.07 | pane=runner、provider child。各片側exit/生存、birth再利用/別pane/同PGID | role-aware証明でproviderだけ作用。wait後のlifecycle read / runnerをproviderとみなす変異を拒否 |
| F4.05 | execute-launchのprovider前/後・SIGKILL | gate/receipt/outcomeの正しい区別とrunner回収 / exec済みをpre-execと偽る変異を拒否 |
| F4.06 | spawn/resume/operator/headless/App wake/title/monitorの実入口 | 同じtransport、実argv/env/cwd/session保持、旧経路不変 / source名だけ変える偽adapterを拒否 |
| F5.01 | 実child end、report HTTP前/commit応答喪失 | 未確定owner active、全文pending保持 / 先retireを検出 |
| F5.02 | hook→runner/逆/同時、同sessionと別nonce | 報告1件、二重retire0 / 異なるUUIDで二重報告を検出 |
| F5.03 | end/replay CLI、自己/親/兄弟/operator、確認前後差替え | 正規入力は権限段まで、誤digestは副作用0 / syntax errorで権限負例を代用しない |
| F5.04 | report intent前後/phase前後/pending削除前/cleanup前/retire後local前 | 同bytes再開・receipt維持・inspect後残りだけ / 第二reportコピー・再分類を検出 |
| F5.05 | standalone、tmux内外、別live/unknown、最後の自己ACTIVE | launch捏造0、最後だけrelease、childのretireと区別 / 全解放・架空launchを検出 |
| F5.06 | 初期報告全種別・Gemini/Claude/Codex終了・R3 helper失敗 | classifier共用、reported/workingは追加通知なし / 全failureをpre-execにする変異を拒否 |
| F5.08 | 両PID不在report pending→target/digest→replay、retire後local未完 | 同bytes/UUID再開。親/operatorだけretired回収 / live必須・retired送信復活を拒否 |
| F5.09 | SessionStart前の実exit / SIGKILL | wait結果ならlifecycle、証明なしunknown→launch-resolve / 不在だけでpre-exec断定を拒否 |
| F5.10 | global親ID成功/別ID/失敗/応答不明、legacy to成功 | 同一FirstTurnでreportedを正しく識別し重複通知0 / 名前だけの旧classifierを検出 |
| F5.07 | 旧UUID＋canonical二重、legacy旧通知/monitor/EXIT、異mode同名 | Ta admissionの対象別拒否、正常legacy継続 / prefix/name fallbackを拒否 |

`test_lifecycle_v3_red.py` はF4.01/02/03、F5.01（HTTP前/後）/02とF5.03のCLI発見を
**既存製品入口に接続**。外部tmux/providerだけstub、serverは実S2c HTTP、liveは合成PID。
F4.03の各既存wrapper状態とF4.07/F5.08–10にv3の製品入口fixtureを追加した。
誤実装対照はoracleへ合成traceを渡す測定器確認であり、製品コードを変異させた実行ではない。
新record schema/entryがbaseに無い部分は明示的な未実装redであり、実lifecycle/nativeが通った証拠ではない。
F4.04–06/F5.03の権限・F5.04–07の全中断点はJSON inventoryに定義した次のbinding gate。
これらの実caller/native/並行中断が通ったという主張はしない。missing adapterのtraceを
実実行と数えず、実装PRで全行をcaller別に接続してからR2-Tb完了を宣言する。
実tmuxの次binding gateでは専用socket/sessionをfixture内で作り、finallyのteardownで
その正確なsessionだけkillし、server/session消失を確認する。default/liveを使わず、
assert失敗・timeoutでも子processをwait/reapする。今回のv3接続はtmuxをstub化し、
Unix socketはfinally close、HTTP serverは既存fixtureのteardownを使う。
測定器の正負対照はgreen、未実装の製品受入はred（skip/xfail/collection失敗ではない）。

## R3 / 後続との担当境界

#266 `docs/r3-local-profile-fixtures.md`（13b2bdb）のLaunchIntent/profile/auth/skillsはR3。
Tbは**保存済みtyped executionの argv/env/cwd を不透明な入力として保持**し、local:解決、
credential helper、model/default/tools/base/worktree正規化、provider HOME/trust変更をしない。
R3の全5caller adapterは同じstart/execute gate/receiptへ接続し、独自tmux launcherを作らない。
`LOCAL_MODEL_REQUEST_FAILED/PROTOCOL_FAILED` はR3 fixture仮名で、Tbの共通outcomeに
bindingする。既にexec済みの認証/helper/provider失敗をpre-execに戻さない、unknown自動retry0。
Tbはfixture stubだけ。native pinned CLI/peer/transcript/skills/auth受入を代行したとは言わない。

global dashboard exit/retire HTTPと一括操作はR4、旧画面統合R5、cockpit global bridge/WSはR6。
#263の共有os.kill mock改善は別PR。本計画はそれを含まず、全suite・live操作・push/PRなし。

## v3 の検証状態と実行

v2の途中停止ログはv3の完走結果へ流用しない。v3は4本を直列・分割で完走し、
重複なし**45件（22 passed / 23 意図したred）**。1回の全体実行ではない。

| 完走scope | passed / red | pytest/runner exit | 到達点 |
| --- | --- | --- | --- |
| schema/oracle | 6 / 0 | 0 / 0 | lifecycle全shape、偽PID/pane/操作/欠落fieldの拒否、旧runtime shape、retry変異 |
| classifier/role reader | 11 / 6 | 1 / 1 | legacy正例/型負例などはpass、global分類とrunner/provider分離が未実装red |
| lifecycle入口 | 5 / 12 | 1 / 1 | 改変caller対照4件とoracleはpass。裸argv/衝突/報告/CLIがred |
| recovery CLI | 0 / 5 | 1 / 1 | actual target CLIの不在PID拒否1件、新record読取拒否4件 |

retryのowned/foreign/PID/nonce 4件とrecoveryの新record4件はOUTPUT_SCHEMA_INVALIDで
後段に到達していない。実装後にemit/fullschema、実wrapperの証明、replay/解消まで通す必要がある。
終了後のtarget CLI 5件は実dispatchで失敗応答を出しており、private DTOを成功扱いしていない。
報告pendingの回収ケースは、実commit receipt/message IDと親inboxの1件保存を確認後にCLIへ進んだ。
fixture前提のCONTACT_BLOCKEDがあった初回runは除外し、修正後の完走分だけ数えた。
元ログとnode別結果はhandoffのvalidation JSONを参照。collection/setup error、skip/xfailは0。
全体・実tmux/provider・liveは未実行、外側へ回したnodeは今回0。未接続gateは前節どおり。

load<15で直列、実行中のload guardも保持する。TEST_PYTHONは任意の依存済み検証用Python。

```sh
PYTHONPATH=. "$TEST_PYTHON" -m pytest -q tests/r2_tb_v3/test_instruments_v3.py
PYTHONPATH=. "$TEST_PYTHON" -m pytest -q tests/r2_tb_v3/test_classifier_v3_red.py tests/r2_tb_v3/test_roles_v3_red.py
PYTHONPATH=. "$TEST_PYTHON" -m pytest -q tests/r2_tb_v3/test_lifecycle_v3_red.py tests/r2_tb_v3/test_recovery_v3_red.py
```

## v1から継承する訂正記録（講評の役割別対応）

| 指摘 | v3の変更 | 検証 |
| --- | --- | --- |
| 観点つきP2-1 / 枠なしP2-1 | 共通target内に終了後/SDK前のlifecycle admission、retired親/operator回収とlocal解消のwhois境界を明記 | F5.08/09 |
| 観点つきP2-2 | FirstTurn入力に検証済み親ID、global成功/error/unknownとlegacy互換を定義 | F5.10 |
| 観点つきP2-3 | retryの全socket/target/nonce/PID/gate作用とowned/foreign matrix、-dのoracleを固定 | F4.03＋command oracleの正負対照（製品変異の実行ではない） |
| 枠なしP2-2 | runner birthはlaunch、provider birthはlive。共有role-aware reader・signal/wait/SDK前の終了証拠を定義 | F4.07/F5.09 |
| 枠なしP2-3 | 短いt baseと長さ前提assert、衝突reason＋occupancy照会を必須、長さ異常は別対照 | F4.02 |
| 観点つきP3-1 | foreground/persistent/Gemini shell/watcherの実caller、9file grepと17file inventoryの区別、明示retireの記述訂正 | caller表/source hash |

全件採用。v1の807bce4と既存4ファイル、v2のfd7f406と追加8ファイルは変更しない。製品変更・全体試験・push/PRなし。

## v2からの訂正（最終契約）

- F4.03はretry後の保存launchを再読し、UUID/nonce/phaseを元の期待値と比較する。
  返却launch_id/phaseも保存値に結び、返却にnonceがある場合も一致を要求する。
  作成wrapperのexecute-launch argvのUUID/nonceも同じ値。拒否caseはlaunch bytes不変。
  保存だけ/返却だけを変更するcaller境界の負対照を測定器に加える。
- classifierのlegacy正例は親ID無し。global親IDあり＋旧to名、string/bool ID、receipt宛先不一致を
  別負例にして、legacyの正常動作をglobal名fallbackとして許容しない。
- lifecycle targetの全shape/CLI emitは次節へ。caller表のdo_exitはF5.07だけに訂正した。

## lifecycle target の完全 wire（F5.08/09）

JSON target_contract_deltaを既存runtime_tmux_target_contract.jsonの**同じ正本**へ反映する。
baseのstandalone-process atomから共通field/必須fieldを継承し、launch-lifecycle と
standalone-lifecycleを導出する。前者のlaunchは既存launched-tmuxのUUID/nonce object、後者はnull。
現在を証明しないpid/process_startと全transport/socket/server/session/window/paneはnull、argvは[]。
保存されたbirthを現在PIDとして偽装しない。agent/name/generation/binding/principal/state digestは
従来と同じschema。live/launch無しの形をlaunched-tmuxとして偽ることもない。

同じtarget CLIの内部分岐だけがresult名 target.lifecycle.successを選ぶ。result fields/constantsは
既存target.successを継承し、lifecycle（phase/provider_outcome/pending_sha256）を必須追加する。
next_operationsはtarget/replay-end/launch-resolve/resolve-mutationの許可済み部分集合だけ。
compact_readinessはunknown/null/null固定、capture/attach/send-compact/end(signal)は広告も受理もしない。
既存runtimeのtarget.success/3 target形は不変。schema atom/結果導出関数は共用する。
既存target_result_schemaのfield→atom導出をresult行のatom_overrides参照にも対応させ、
lifecycle_target等を同じderiverで展開する。別のvalidatorやCLI statusは作らない。

fixtureはprivate Targets.executeではなく実runtime-client mainのtarget --printを呼び、
emitのschema検証後にstdout JSONを取得し、同じproposal contract導出schemaでも全体を検証する。
そこから得たstate digestとpending hashをreplay-endへ渡す。emitを迂回したDTOを成功と数えない。
測定器だけで提案deltaを実体化することは製品のemitを差し替えるadapterではない。
phase/許可操作の関係は同じresolverで決め、単なるschema受理を作用admissionに使わない。

v3の新規4対応は全件採用。追加のfixture前提修正として親のcontact policyをopenへ明示し、
応答喪失は実commit receipt/message IDと親inboxの1件保存を確認してから扱う。
CONTACT_BLOCKEDをlifecycleのredとして数えない。v1/v2のファイルとcommitは固定し、製品を変更しない。
