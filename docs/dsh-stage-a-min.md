# dsh-TUI 段A-min（開発中）

同じ実装で DeepSeek / Strata を選ぶ、子1体・私有worktree・小さいread/write taskの縦切りです。正式なprovider導入、任意shell、resume、global、installer、dashboard変更は含みません。固定版は dsh 0.2.1-rc.2 / dsh-TUI 0.16.1、Nodeは絶対実体を固定して24.2以上を検査します。

offline spikeでは、固定TUIの実画面起動と、実際のagent.ctxのtoolによる許可write・root外/予約なし/期限切れの3拒否が成立しました。phase=runの登録・製品proxy・予約・鍵なしfull preflight・pane起動・後始末を接続し、Strataでは小さい編集→親宛てMail→停止・解放・retireの実一周でtask/runtimeの完了を確認しました。DeepSeekも実SDK/固定TUIと明示の無通信stdio fixtureによるfull preflightが成立していますが、DeepSeek公式APIでの実一周は未受入です。fixture成功を実task成功やretire成功には読み替えません。

## 呼出しと設定

managed launcherが、固定Nodeで `--import integrations/dsh/egress.mjs integrations/dsh/managed_entry.mjs` を起動します。直接の起動方法を増やさず、launch.py/launch_plan.pyが決めた実体とmanaged profileを使います。

| 入力 | 内容 |
| --- | --- |
| ORRERY_DSH_BACKEND_FILE | 利用者のrepo外backend設定の絶対path |
| ORRERY_DSH_BACKEND_ID | deepseek / strata |
| ORRERY_DSH_STATE_ROOT | attemptごとの私有state root（0700） |
| ORRERY_DSH_AUDIT_FILE | STATE_ROOT/audit.jsonl |
| ORRERY_DSH_ATTEMPT_ID | attempt.jsonと一致するID |
| ORRERY_DSH_TUI_PATCH_RECEIPT | 必須のmanaged TUI patch receiptの絶対path（launch.pyの--patch-receiptで指定） |
| ORRERY_DSH_OFFLINE | 1は転送0。未指定もoffline。attempt.phase=spikeは常にoffline |
| DEEPSEEK_API_KEY | 同じadapterが読む変数名。offlineはdummy、実鍵は将来の受入時だけruntime1PIDへ |

launch.pyの`--patch-receipt <receipt.json>`は必須です。managed entryはTUI/profileのimport前にrecipeのSHAと変更対象全fileのafter hashをreceiptと照合し、未適用・部分適用・receipt欠落を拒否します。

repoのbackend.jsonは空のURL/accountを持つテンプレートです。コピー先の利用者設定（例：~/.config/orrery/dsh-backends.json）に二backendのURL・model・Keychainのservice/account参照・effort/output上限・モデル定義・idle要否をまとめ、ORRERY_DSH_BACKEND_FILEでそのrepo外fileを渡します。テンプレートの空URLでは起動しません。設定に鍵の値は置きません。managed.patch.ymlは共通機能制限だけを持ち、選択済みprovider/model行はlauncherが最後に投影します。runの鍵取得はMacのsecurityを使う小さい入口へ寄せ、15秒・一回だけ、失敗時は停止します。WSLの鍵取得は未対応として止めます。spikeと鍵なしpreflightはdummyだけを使います。WSLのNode/native/TUI実機動作は未確認であり、Macの実体path/SHAを共通定数へしません。

`backends.<id>.auxiliary`は将来の補助route用の任意欄です（省略/nullなら主routeを使う設計）；現版は送り分け未実装で、補助無効の既定を維持します。

## 通信と補助process

通常の更新/onboarding/補助起動でも予定外通信や鍵継承が起こり得るため、以下だけを残します。

- global fetchは選択backendの正確な`baseURL + /v1/messages`だけ。DeepSeekの`/user/balance`、npm/GitHub、任意loopbackを両backendで拒否します。最初のURLだけで許可し続けず、redirectをmanualで受けて3xxを拒否し、自動追従しません。
- Strataのstatusだけは同じruntimeが送信前に確認します。`/v1/status`、404時だけ`/health`、無鍵→401/403時だけ認証、各5秒。in_flightが数値0、queue値があれば0を必須とし、busy/不明は止めます。pollや分散lockは作りません。
- 単一in-flightをstatus待ちより前に取り、応答bodyのEOF/error/cancelまで保持します。header到着だけでは解除しません。adapterはmessage_stopで正常に読み終えてHTTP EOF前にcancelするため、message_stopと最終usageを観測し、cancelがresolveしてsignalがabortされていない場合だけ正常完了にします。それより前のcancelやcancel拒否・timeout・abort、final usage不明では次要求を止めます。要求8回、各120秒、backendのoutput上限を守り、retry/fallbackはしません。金額上限の保証ではありません。
- 製品の標準`~/.agentstack/integrations/codex_app/plugin/scripts/run-mcp.sh`だけを補助起動に許可し、明示envを作ってAPI keyを除きます。opener/editor/clipboard/別dsh等の起動は拒否します。既知startup診断は保存前に一致鍵を伏せます。
- TUIの更新check/manual update/bootstrap、残高/OAuth、recap/btw/title、IDE/inject、共有profile/旧資格importはmanaged entry/action側でも無効にします。UI非表示だけで済ませません。managed TUIの停止処理とpatch receiptの拒否系はoffline対照試験で確認します。

offlineでは要求のmodel/tool集合を組み立てた後、statusもproviderも呼ばず`offline-capture {forwarded:0}`、exit73で止まります。契約違反はexit78、runtime例外79、上限80です。spikeで必須のtoolはread/writeだけ、runではbackend.jsonのwire_tools全3件です。

拒否のblocked-*はHermesと同じmethod/host/port/path/callersを記録します。pathはURL pathnameだけ、callersはadapter/runtimeからの相対source位置（file/line/function、最大16）です。query/userinfo/fragment、本文、header、鍵、process argv/env値を保存しません。ネットワーク以外の拒否はhost/port=nullで操作名をmethodに残します。

## taskとruntimeの証拠を分ける

launchはattempt.json/task.json/runtime-exit.json、pluginはevents.jsonl、egressはaudit.jsonlを書きます。相互の内部をimportせず、state rootのJSON/JSONLを境界にします。attempt.json.phaseはspike/run、agent_nameは実際の子名です。spikeに架空のMail成功を作りません。

completion.pyのtask判定は、同じattempt/task、completed turn、親が指定したexpected成果物（worktree相対pathとcontains又はsha256、開始以降のmtime）、要求auditにfatal/blocked/limit無し、親宛ての実Mail receipt、の5条件です。receiptのfromはattempt.agent_name、verified_sender/okはbool true、正のmessage_idも照合します。欠けた証跡はpending(null)、既知の不一致はfailed(false)。空exit0や画面の「完了」は成功条件にしません。

TUIのruntime終了は別評価です。group_absentに加え予約解放/retireの証跡が無ければ、reservations_released:null、retire:unknownと残します。task成功を停止・retire成功に読み替えません。

runの後始末は`runtime-exit.json`の自己申告だけで成功にしません。`release-receipt.json`は`attempt_id`・`agent_name`・`source:product-bound-proxy`を照合し、`confirmed:true`かつ`reservation_ids`/`released_ids`がtaskの予約ID集合と一致するときだけ解放済みです。`retire-receipt.json`は同attempt/子名・`source:parent-bound-whois`で、親の正規whois確認による`retired_at`と`confirmed:true`が必要です。cleanupのexit0だけならpending、別attempt/子名/sourceの証拠は採用しません。spikeの合成receiptをrunの成功にしません。

```text
python3 integrations/dsh/completion.py /absolute/state/root
python3 integrations/dsh/completion.py /absolute/state/root --runtime
```

上のコマンドは保存済みfileだけを読み、それぞれtask-completion.json/runtime-completion.jsonを書きます。exitはsuccess=0、failed=1、pending=2。runtimeやMailへ接続しません。

親の実受領とretireは、親が正規tool結果を保存してから`mail_lifecycle.py record-mail --state STATE --input FETCH_RESULT.json` / `mail_lifecycle.py confirm-retire --state STATE --input WHOIS_RESULT.json`で記録します。前者はpluginの実Mail ID・from/to・attempt/taskとの一致を確認してparent-mail-receipt.json、後者は同じ子名のwhois retired_atとcleanup記録からretire-receipt.jsonを書きます。この2コマンド自体はRPCをせず、モデルの自己申告を成功の根拠にしません。

## offline検証

NodeもPythonも検証用の明示実体を使い、直列に実行します。本番runのload制限はoffline回帰には適用しません。

全dsh回帰は`bash scripts/check-dsh.sh --node /absolute/fixed/node --runtime /absolute/pinned/runtime --out /absolute/check-output`の1コマンドで実行できます。Python 3.11以上と既存の固定runtime/patch receiptを検査し、私有HOME・一時dir・明示envを準備してNode fixture、全Python dsh試験、catalog試験、両backendの実SDK/TUI無通信preflightを直列で流します。依存をdownload/repairせず、不足時は失敗にします。`result.json`はsuccess/tests_run/failures/errors/skips/allowed_skip_reasons/unapproved_skipsを持ち、allowed_skips.json（既定[]）の理由文字列に完全一致しないskipは失敗です。合格した試験のskipが隠れる形にはしません。

```text
/absolute/fixed/node --test tests/test_dsh_egress.mjs
/absolute/python3 tests/test_dsh_completion.py
```

egress fixtureはメモリ内の偽fetchだけを使い、endpoint選択・offline転送0・残高/更新先拒否・model/beta/output/tool制限・idle/busy/auth・redirect・stream重複/中断/timeout・proxy env scrub・診断伏字を対照確認します。製品の`child_tools.py claude-config`が生成した設定から、fake HOMEの無通信stubを実際にspawnし、全proxy envキーの保持・provider keyの除去も確認します。実proxyやMailへ接続した試験ではありません。completion fixtureのMail/lifecycle receiptは明示した合成入力であり、実Mail送信・予約解放・retireの証拠ではありません。部分解放、別identity、whois欠落、spikeのtask/runtimeが成功へ繰り上がらないことも確認します。

managed TUI側の`tests/test_dsh_managed_spike.py`では、patch receiptの未適用/部分適用/欠落の拒否、補助処理停止、実ToolRuntimeのguardを別に確認しています。TUI画面とagent.ctxのprobeを含む実験記録・検証出力は各自のrepo外作業dirへ保存します。repository全体のpytest、実Mail/APIとWSL実機受入は、これらのspike/fixtureでは確認していません。TUIの保存依存が不足した場合、offlineを外してdownloadせず親へ報告します。

run接続の`tests/test_dsh_run_connection.py`では、実SDK→無通信のsynthetic stdio server→固定TUIを使います。templateの`qwen3.8-flash-next-iq3_xxs`、contextWindow 131072/maxTokens 4096、effort highを保ち、URLだけ`.invalid`へ変更したfull preflightで、schema可視/backend不達・process不達の対照、実3 tools、retry0、offline-capture/転送0、group消滅を確認しました。dummy値の保存は0件でした。preflightのaudit/events/runtime終了証拠は本番用と分離し、変更した設定は鍵取得前に拒否します。実Strataのidle/認証・実Mail・登録/解放/retireを確認した証拠ではありません。

## 初回の子で一度だけ接続先を観測する

実受入時に起動直前→終了の一窓だけ、owned runtime/supervisor/proxy/子孫PIDへMacの`lsof -nP -a -p <PIDのCSV> -i`又はWSLの`ss -H -ntup`を約1秒間隔で採ります。時刻/PID/実行名/接続IP:port/protocolだけを保存し、argv/env/payload/headerは採りません。許可は利用者が設定したStrataの接続先と既存bindingの正確な127.0.0.1:Mail portのみです。hostname/IP対応も記録し、許可外を見つけたら止めます。常設監視・firewall・新proxyは作りません。

Macの初回は`python3 integrations/dsh/observe.py --state STATE`を使います。鍵なしで採取窓を開いてから既存launch.pyのstartを呼び、owned pane・supervisor・runtimeと子孫を約1秒間隔で採取し、停止・消滅後に一行で判定します。親は別端末でMail受領と既存stopを実行します。observerはsignalを送らず、許可外を即表示します。許可はrepo外backend設定のprovider URLを解決したIP:443とrun-configの正確な127.0.0.1:Mail portだけです。backend=deepseekでは公式api.deepseek.comだけを対象にします。CDNのIP変化は通常起こり得て固定リストによる誤停止を招くため、未知の443接続を見たときは同hostnameを再解決し、事前解決結果との和で判定します。他のhostnameや任意の443は許可しません。network-observation.jsonlに一窓、network-observation.jsonに判定を保存します。exit0は「この窓で許可外なし」、1は許可外あり、2は未確認。取得失敗・中断・15分以内に停止確認できない場合は未確認とし、親がruntime状態を確認します。WSL用の観測adapterは未実装です。`tests/test_dsh_observe.py`は無通信snapshot/起動のfixtureであり、実OSの権限や実接続を確認した試験ではありません。

DeepSeekのrunも同じinit/register/preflight/observe/task/record-mail/stop/release/retire/confirm-retire/cleanを使います。利用者のrepo外設定でbackend_id=deepseek、base_url=https://api.deepseek.com/anthropic、model=deepseek-flash、明示のKeychain service/account参照を指定します。model_definition=nullではcustom modelsを投影せず、idle_check=falseなのでStrataのstatusは呼びません。残高・onboarding・補助通信の遮断、要求8回/retry0と完了判定は共通です。`ORRERY_DSH_TEST_BACKEND=deepseek`で実SDK/TUIの無通信full preflight fixtureを選べます（既定はstrata）。

snapshotは短い通信を見逃し得ます。global fetchの置換もnode:http/net/native/別processの全socketをOSで遮断する仕組みではありません。statusは別利用者を排除するサーバー予約ではなく、WSL対応は未受入です。未観測を「外部通信は完全にゼロ」とは報告しません。

既知の限界：予約の一部だけ取れて起動しなかったattemptは、解放とretireの後もruntime-completionがpendingと出ます。後始末の証拠はabort専用の記録で確認します。

終了は所有groupの停止→予約解放→canonical retire確認の順です。失敗やMail/retire応答不明ではstateを保持し、自動再登録・再送・再開・実鍵の再読取をしません。commit/pushとliveへの採用は親が行います。
