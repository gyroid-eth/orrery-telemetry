# Managed spawn — 段1の要求と応答

対象は legacy の `provider=deepseek|strata` と `harness=dsh|hermes`。providerは送り先、modelは送り先のID、harnessは実行器であり別々に保持する。既存Claude/Codex/Geminiの要求・登録・応答は維持する。設定が無い既定ではmanaged providerをcatalogへ足さない。

## HTTPの契約（A1/B1/B3/B4/D1）

`POST /api/spawn` のmanaged要求。UIはPOST前にUUIDのattempt_idを作って保持し、応答喪失時も同じIDで照会する。

```json
{"attempt_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","provider":"strata","model":"configured-model-id","harness":"dsh","helper_model":"same","parent":"ParentAgent","standalone":false,"task":"Edit the scoped file and report to the parent.","dir":"/absolute/source-directory","worktree":true,"allowed_paths":["result.txt"],"expected":[{"path":"result.txt","contains":"done"}],"async":true}
```

- `attempt_id`、provider/model/harness、parent、task、dir、allowed_paths、expectedは必須。helper_modelは省略又はsameだけ。effortは省略又はそのmodelのcatalogの集合から選ぶ。role/group/emojiは任意の表示情報。
- managed判別はharnessがdsh/hermes、又はproviderがdeepseek/strata。未知harnessを旧coreへ流さない。parent必須、standalone=true・headless=true・任意name・base/tools/resources・context/context_length上書きは段1で拒否する。asyncは省略/trueだけ。URL、実行コマンド、鍵参照、runtime/stateのpathをclientから受けない。
- `dir`は読み取り元。実行先はserverがattempt専用に作る私有worktree/作業dirでありliveのdirを直接編集しない。scope内の既存のliteral fileだけを私有作業dirへコピーする。書き戻し・commit・mergeは行わず成果物を保持する。worktreeは省略/trueだけ。段1のscopeは正規化した相対**file名の配列**（glob/絶対path/../.gitなし）、expectedのpathもscope内。検査はcontains文字列又はsha256のいずれか1つ。read/write/Mailだけでできるtaskを対象にする。
- 現在のrunner設定のcontextを使い、server報告contextは未取得。stage2のfresh context取得/上書きは別実装。clientがrunner設定値をserver報告値へ昇格させない。
- serverは旧`_spawn_launch`の登録前にmanagedへ分岐する。名前選択/登録はrunnerの正規helperが**一回だけ**行い、従来のtask Mailを重ねず起動時taskを一回渡す。
- 同じattempt_idと同じ要求は同じattemptを返し、再登録しない。Hermesは種類ごとの固定profileを維持し、同profileの稼働中の2体目は登録前にprofile_busyで拒否する（queueしない）。dshにはこの制限は無い。異なる要求で同じIDなら`ok:false,error_code:attempt_conflict`。HTTP timeout/90秒後に新IDへ自動再送しない。

受理は202（まだ登録/ready/task成功ではない）:

```json
{"ok":true,"pending":true,"attempt_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","name":null,"state":"pending","phase":"queued","managed":true}
```

`GET /api/spawn-status?attempt_id=<UUID>`で同attemptを再照会する。従来の`?name=`も維持する。成功して受理された照会のokはtask成功とは別。

```json
{"ok":true,"managed":true,"attempt_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","name":"ChildAgent","state":"ready","phase":"running","pending":false,"running":true,"finished":false,"task_success":null,"runtime_success":null,"state_path":"/private/attempt/state","error":null,"error_code":null}
```

HTTPのattempt_idは要求/jobのUUIDでありrunnerの内部IDと別。runnerのIDは生成値を変更せず、初期化後runtime_attempt_idで返す。

stateはpending/ready/failed。readyはschema/backend/preflightと**実起動後のplugin/tool-ready**の証拠があるときだけ。登録完了やwrapperのexit0をreadyにしない。子名は登録後にのみ埋まる。task終了後はphase=stopping/releasing/retiring/cleaned等、finished/runningとtask_success/runtime_successを別々に出す。後始末の失敗/不明はfailed、error_code/state_pathを残す。readyをtask成功へ読み替えない。未知attemptはok=false,error_code=attempt_not_found。statusはHTTP呼出しを超えて同attemptを保持し、再起動後も保存済みstatus JSONを読めるようにする。

`cleanup_success`はcleaned/cleaned_after_failureでtrue、cleaningの失敗でfalse、それ以外はnull。`runtime_success`は従来どおりgroup消滅・release・retireの証拠だけを表し、clean完了を意味しない。

## Catalogの最小契約（A2）

SharpBoseの`dashboard/managed_catalog.py`は次のpure関数を公開する。

```text
catalog_providers(backends, availability, *, mode, enabled) -> list[dict]
```

`backends`はtrustedな私有設定から解決したpublic metadata（秘密・URL・account・実行pathを含めない）:

```json
{"strata":{"model":"configured-model-id","label":"Strata","locality":"local","server":"Configured local server","efforts":["off","low","high","max"],"default_effort":"high","configured_context":131072},"deepseek":{"model":"configured-model-id","label":"DeepSeek","locality":"cloud","server":"DeepSeek API","efforts":["off","low","high","max"],"default_effort":"low","configured_context":null}}
```

availabilityはbackend×harnessごとに、実体/版/patch/設定とlegacy対応を確認した結果。

```json
{"strata":{"dsh":{"available":true,"reason":null},"hermes":{"available":false,"reason":"runtime_not_prepared"}},"deepseek":{"dsh":{"available":false,"reason":"runtime_not_prepared"},"hermes":{"available":false,"reason":"runtime_not_prepared"}}}
```

enabled=falseなら空配列。mode!=legacyはavailable=false、reason=unsupported_modeでありlegacyへfallbackしない。返却は既存`providers`に追加できる形:

```json
[{"id":"strata","label":"Strata","managed":true,"locality":"local","server":"Configured local server","context":null,"context_source":"unreported","context_note":"Server-reported context not available yet","helper_models":[{"id":"same","label":"Same as main"}],"efforts":["off","low","high","max"],"default_model":"configured-model-id","models":[{"id":"configured-model-id","label":"configured-model-id","harnesses":[{"id":"dsh","available":true,"default":true,"reason":null},{"id":"hermes","available":false,"default":false,"reason":"runtime_not_prepared"}]}]}]
```

model/harnessのavailabilityはserverでもPOST直前に再確認する。backend×harnessに紐付いたtrusted run-config templateからだけrunner/runtime/接続参照を決定する。catalogのCLI発見だけでavailableにしない。

## 内部adapter（C1–C3/C7/D3–D5）

新controllerはdashboard/managed_spawn.py。私有設定の参照は`AGENTSTACK_MANAGED_SPAWN_CONFIG`、無指定は無効。私有manifestは`enabled`、`state_root`、上記backends、harnessesを持つ。harnessesは`dsh|hermes -> {config_file:絶対run-config-template参照, python:絶対runner-Python}`。値はoperator側だけが用意する。requestのmodelとtemplate内のmodelが異なれば登録前に拒否する。

共通task JSONは`{prompt,allowed_paths,expected}`。controllerがparentと私有worktreeをtemplateへ投影する。Hermesの`init-task --input <task.json>`はこれを使い、元の固定試行CLIは維持する。

両adapterの公開関数（既存CLIを包む。内部の共通化を強制しない）:

```text
prepare(*, state, config_file, backend_id, task, worktree, parent, run) -> dict
start(state, *, run) -> process/handle
status(state) -> {ready:bool, task_success:bool|null, runtime_success:bool|null, ...}
finalize(state, *, parent_call, run) -> dict
clean(state, *, run) -> dict
```

run(argv, *, timeout=60, check=True, background=False)は鍵なしenvのCLI実行の注入口（CompletedProcess/Popenを返す）。startの短命wrapperのexit0をtask終了やreadyに使わず、statusのterminal/runtime証拠を使う。parent_call(tool,args)はdashboardの既存の正規Mail読取contextのwhoisを使う注入口（親owner tokenは読まない）。子proxyで親inboxを読まず、DB直読/新Mail polling/新proxyを作らない。whois結果→confirm-retireの保存と実receiptを照合する。段1のMail条件は両harnessともserverのverified send receipt（ok/verified_sender/from/to/message_idと非空deliveries）で判定する。親受領の自動fetchはせずD2は段2。既存contextから保存したparent-mail-receiptが有る場合はその一致も必須。whois確認ができなければretire/cleanを完了と偽らずstateを残す。子のtask成功はruntime停止と独立に記録する。

防止策の理由: 通常のHTTP timeout/重複操作×二重登録、通常の親/接続/設定取り違え×live/鍵漏れ、通常の中断/部分失敗×偽の成功。この範囲だけを守り、細工した競合のためのbroker/常駐監視/追加認証方式は作らない。
