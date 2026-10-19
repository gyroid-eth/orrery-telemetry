# 隔離 global client の起動と配送

[English](global-delivery-client.en.md)

既定は旧 mode のまま、global の `activation_enabled` は false です。この手順は専用の一時 HOME と、一時 server の完成済み candidate を使います。実サービスへの切替と released 形式の import は後続です。
Claude / Codex の固定 provider HOME は、起動前に `agentstack-runtime-client instruction-configure operator claude` / `codex` で指示 block を準備します。通常 launch は読むだけで、不足・古い版は `INSTRUCTION_PROVIDER_CONFIG_REQUIRED`。詳細は [指示の移行](instruction-migration.md#custom-provider-home) を参照してください。


## 正本と固定配置

owner と binding は既存の context・credential、window は context.identity だけに保存します。旧 global credential/profile/session の window 欄は読取で書き換えず無視し、次の正規書込みで省きます。現在の PID・pane・provider session は既存 live-sessions と session_index に保存します。`runtime/launch.json` は起動 ID、nonce、phase、provider、種別、cwd、task digest、異常と明示解消だけを持ちます。

通知の完了は server の notification fact が正本です。consumer の intent は外部操作の中断と再試行に使い、成功の cache にはしません。取得は純粋な読取で、明示 read・ack・配送完了だけが通知を消費します。

consumer は `agentstack_home/global-consumer/<sha256(mail_instance_id)>/agents/<agent_id>` です。既存 server-config.json の isolation・instance・candidate generation・epoch・runtime_root・authority・lock が一致しなければ `CONSUMER_ISOLATION_MISMATCH` で出力前に止めます。wire v3 の authority からの導出を訂正し、server preparation workspace に client の状態を置きません。同 instance の各 client は同じ consumer に達します。

agent の route.json と App の external route は client_name だけを保存します。token・表示名・現在 launch の写しはありません。client ごとに owner の固定 profile・session・credential を読み直します。

## 起動と中断からの戻り方

`agent-start` / `agent-start-codex` / `agent-start-gemini` と子の spawn/resume は共通 runtime helper を使います。cwd・command・出力 slot・capability を先に確認し、window の bind と一意な launch の gate を経て起動します。登録 pending と launch は別の操作です。起動を再登録で回避しません。

内部の `execute-launch` / `provider-exec` は固定 context の後で `--` により
option を終端し、先頭が `-` の launch nonce も位置引数の値として保ちます。
`receipt_nonce` は古い process を区別する起動ごとの照合値で、登録 token や
API credential ではありません。owner の認証は固定 credential、実行の承認は
既存の phase・runner の親 PID/start・正規 session receipt の照合を使います。

| phase | 戻り方 |
| --- | --- |
| planned / gated | status の digest を指定して launch-retry または launch-cancel |
| exec-unknown | 自動で再実行せず launch-resolve で abandon / confirmed-running / confirmed-ended |
| running | trusted SessionStart receipt と owner/window の確認が揃ったときだけ runtime_ready |
| ended / cancelled | confirmed-ended の live 反映が全て済んでから次の明示起動を準備可能 |
| abandoned | 不明な process はそのまま。証明が得られたら明示解消 |

operator 操作は `agentstack-runtime-client operator-json OPERATION` に JSON を stdin から渡します。正本は [local_operator_wire.operations](../packages/agentstack_mail/fixtures/global-client-4c.json) です。`launch-cancel`・`launch-resolve`・`resolve-delivery` は `confirm:true` が必須、`launch-retry` は confirm を受け取りません。

`client_root` は対象の固定 client root、`AGENTSTACK_CLIENT_CONFIG` はその runtime-client.json を指します。launch ID は既存 launch.json、message ID は doctor-local の warning または受信した通知から取得します。次は launch の現在値を読む例です。
```sh
# AGENTSTACK_CLIENT_CONFIG points to this client's runtime-client.json.
launch_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["launch_id"])' "$client_root/runtime/launch.json")
status_file=$(mktemp)
agentstack-runtime-client operator-json launch-status <<EOF_JSON > "$status_file"
{"launch_id":"$launch_id"}
EOF_JSON
state_sha256=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_sha256"])' "$status_file")
cat "$status_file"
```

status の `phase`・`operator_required`・`next_operations`・`remote_owner_verified` を確認します。`state_sha256` は同じ lock 内の現在 context・credential・launch・live session と intent の digest です。状態や世代が変わって `RESOLUTION_DIGEST_MISMATCH` になったら、status を読み直して判断し直します。


```sh
# exec-unknown / abandoned: acknowledge uncertainty; never claims completion.
agentstack-runtime-client operator-json launch-resolve <<EOF_JSON
{"launch_id":"$launch_id","expected_state_sha256":"$state_sha256","confirm":true,"resolution":{"action":"abandon","operator_evidence":"The process could not be independently verified"}}
EOF_JSON
```

```sh
# planned / gated only: select ONE operation after reading a fresh status.
agentstack-runtime-client operator-json launch-cancel <<EOF_JSON
{"launch_id":"$launch_id","expected_state_sha256":"$state_sha256","confirm":true}
EOF_JSON
# Alternatively retry (no confirm field in its schema):
agentstack-runtime-client operator-json launch-retry <<EOF_JSON
{"launch_id":"$launch_id","expected_state_sha256":"$state_sha256"}
EOF_JSON
```

```sh
# Use the message_id reported by doctor-local (42 is an example).
agentstack-runtime-client operator-json delivery-status <<EOF_JSON
{"message_id":42}
EOF_JSON
```

操作後は再び status を読みます。abandon は `phase=abandoned / runtime_ready=false`、cancel は `phase=cancelled / prepared=true / runtime_ready=false`、retry は `phase=gated / prepared=true` を返し、trusted SessionStart が確認されるまでは ready になりません。delivery-status の digest を同じように取り、resolve-delivery へ `message_id`・`expected_state_sha256`・`confirm:true`・`resolution` を渡します。action は `retry-at-least-once` / `confirm-external-completed` / `abandon-without-consuming` で、operator_evidence に判断の根拠を記します。abandon-without-consuming は blocked にするだけで、未読や server の通知 fact を消費しません。

confirmed-ended の途中で live の保存が失敗した場合、status は `operator_required=true / next_operations=[launch-resolve]` を返します。新 digest で同じ confirmed-ended を再提示して残りを完了するまで次の launch は拒否されます。Mail に届かない場合も local status と abandon は使えます。
proxy は server fixture の schema から owner・binding の入力を除いて生成します。bootstrap と runtime_status は local tool です。bootstrap は trusted session receipt を確認します。名前や呼出し側が渡した owner token で宛先を選びません。新しい window は agent_id の既存 FK に束縛し、legacy_project_id は NULL にします。

## R3 operator の full form（隔離・接続途中）

Python API の `RuntimeOperator.spawn` は、従来の `agent_name/program/model/cwd`
を渡す最小の standalone 形と、共有 caller dict を渡す子の full form を分けます。

```python
operator.spawn("child_01", caller=caller, parent_client_name="parent", prepare_only=True)
```

`caller` は共有 canonical schema の必須欄すべてを持ち、任意欄の省略も保持します。
未知 key・型違い、full form と最小形の field の混在（明示 `None` も含む）は effect
前に拒否します。tools は既存の文法で検査し、固定 profile の保存済み intent と全欄を
照合します。owner/root/auth/scope を caller から選んだり、model を解決し直したり
しません。full form は、明示 configure 済みの固定 owner を必要とします。

親は明示の固定 `parent_client_name` で選び、その canonical な名前と `caller.parent`
を照合します。表示名から別の credential/root を lookup しません。同じ子 root を親に
することも拒否します。`task_delivery` の省略は caller の指定（欄が無ければ mail）を
使い、別に明示する場合は一致を必要とします。任意欄を intent へ補完して保存しません。

`prepare_only=True` は既存の prepare_child と task 準備までで、provider/tmux を
起動しません。通常実行は既存 R2 と保存された detached を使い、通常の full form が
instructions・trust・native 権限を自動設定/承認することはありません。未検証の実 CLI
は引き続き `LOCAL_MODEL_ADAPTER_UNSUPPORTED` です。配送用 TEST binding の成功を
native の受入とは数えません。既存の最小 standalone spawn の意味は変わりません。

full form の照合は既存 child C の中でも繰り返し、task 準備後は C を解放します。
照合済み digest を既存 R2 の launch 準備へ渡し、その C 内・row 作成前にも照合
するため、間の明示 revision が別の intent を起動することはありません。
`detached=False` は既存 R2 の foreground runner に呼出し command の実行を渡します。

API 20 の `POST /api/global/spawn` は同じ full form を受けます。必須の
`client_name`・`parent_client_name`・`caller`・`prepare_only` と、任意の
`expected_digest`・`task_delivery` を渡します。既存の最小 HTTP 入力とは混在できません。
`caller.task` は承認済み task **file の絶対 path**で、旧 NEW AGENT の本文とは別です。
固定 server context と既存の HTTP operator admission を使い、実 `do_spawn` から
同じ operator へ渡します。cwd は既存 roots で検査しますが、full form の literal は
canonical path に書き換えて承認と一致させません。省略欄を owner から埋め戻しません。
構造不正は400、intent/identity/親の不一致は409、未検証 native は503の固定 reason
です。HTTP の `prepare_only:false`＋`caller.detached:false` は副作用前に400
`HTTP_FOREGROUND_UNSUPPORTED` で拒否し、detached を変更しません。
`prepare_only:true` は detached が false でも task 準備まで許可します。
上記 Python/CLI の foreground runner の契約は変更しません。
最小 HTTP 入力の schema・正常挙動と legacy は維持しますが、共通の拒否分類により
最小入力の `LOCAL_MODEL_IDENTITY_MISMATCH`・`LOCAL_MODEL_INTENT_CHANGED` は503から409へ
改善します（API 20 の互換差）。[HTTP 形式](api.md)も参照してください。
`global_spawn_full_caller:1` はこの配送入口の capability だけを表し、native の承認を
表しません。画面・catalog/status・全5入口の受入は別の未完 gate です。

## Codex hook trust

Claude の固定 provider HOME の mcp.json と orrery-hooks.json は導出物で、launch gate の operation mutex 内で再生成します。hooks は `--settings orrery-hooks.json` で渡し、provider が使う settings.json の model・permissions は変更しません。Gemini は native `.gemini/config/mcp_config.json` を唯一の設定として使い、同 mutex 下で `orrery-mail` entry だけを CAS 更新し、他の top-level key と MCP entry を保持します。Codex は既定 config を生成せず `-c` で呼出し時に指定します。同時書換えは `GENERATED_CONFIG_CHANGED`、読取不能は `GENERATED_CONFIG_UNREADABLE`、Gemini の JSON/構造不正は `GENERATED_CONFIG_JSON_INVALID` で上書きせず停止します。private path/permission または JSON を直して明示起動をやり直してください。設定の別の台帳はありません。

通常の launch は trust を承認しません。固定 provider HOME の承認済み plan、plugin payload、native config の trust が一致しなければ `CODEX_HOOK_CONFIGURE_REQUIRED` で止まります。変更前の plan に承認を使い回すこともできません。

operator は次の手順で現在の配置を準備します。`AGENTSTACK_CLIENT_CONFIG` は対象の固定 context を指します。

```sh
agentstack-runtime-client codex-plan operator > hook-plan.review.json
```

表示された provider HOME・plugin 配置・hook の内容を人が確認し、承認する場合だけ、その JSON の `approved` を `true` に編集します。承認済み JSON を stdin で渡します。

```sh
agentstack-runtime-client codex-apply operator < hook-plan.review.json
```

既存の plan / apply_trust 処理を使います。通常の起動、更新、再開が自動で承認する入口はありません。別 HOME・任意の配置・別版 binary の導入は行いません。plugin の Mail MCP は無効にし、起動時に渡す同一 client の proxy だけを使います。hook 自体の承認は保持します。

Gemini は固定 provider HOME の `.gemini/config/mcp_config.json` と既存の stream renderer を使います。headless の子は実行した provider の `init/conversation_id` だけを session receipt にします。init が無ければ `GEMINI_SESSION_RECEIPT_UNAVAILABLE`、対話型 root は `GEMINI_INTERACTIVE_RUNTIME_UNSUPPORTED` で止まり、ready になりません。Gemini は合成 stream provider による fixture 試験のみで、実 agy では未確認です。

<a id="persistent-execution"></a>

## persistent の operator configure

固定の profile.json に実行設定 `execution` を1つだけ保存します。owner/binding は追加しません。通常 run は設定を読むだけです。旧 persistent profile は `LEGACY_FILE_REQUIRES_IMPORT` のまま、PR7 の明示 import で移します。

```sh
client_root="$(dirname "$AGENTSTACK_CLIENT_CONFIG")"
# 固定 profile がまだ無い場合だけ:
agentstack-runtime-client save-profile
agentstack-persistent configure --profile "$client_root/profile.json" --operator <<EOF_JSON
{"argv":["/absolute/python","/absolute/bot.py"],"interaction":"headless","cwd":"/absolute/private-workdir"}
EOF_JSON
agentstack-persistent run --profile "$client_root/profile.json"
```

argv は配列だけを受け、shell の文字列は拒否します。cwd は同 isolation 内の private・no-symlink な directory です。実行設定が無い run は `PERSISTENT_EXECUTION_REQUIRED` です。任意の profile 保存先や旧 profile の並行読取はありません。

bot は受け取った `AGENTSTACK_CLIENT_CONFIG` で共通 client を使います。trusted SessionStart を記録した後、固定 `AGENTSTACK_PERSISTENT_WAKE_SOCKET` で既存 `orrery-mail-notification-reply-v1` を受けます。通知には message と `reply_request_id` があり、同じ通知の再試行でもこの UUID は一定です。reply_message の intent を同 UUID で完了し、それから元 message_id を付けた `status: replied` を返します。reply 前の終了や不明応答では配送完了にしません。

## 通知の再試行と健康

hint は ID と binding だけです。owner の pending-only・exact-ID fetch で確かめた message だけを外部へ渡します。purged/read/ack/imported-consumed の hint は再注入しません。全文・preview・本文無しの文言は1つの formatter から生成します。

| channel | 再試行 |
| --- | --- |
| tmux / standalone hook / persistent / daemon | 旧 watcher の同じ定義から読む30秒 cooldown、回数上限なし。unknown は timeout＋30秒の lease と backoff の両方を待つ |
| Codex App | poll 0.25秒、coalesce 2秒、1時間12 wake、lease 900秒、backoff 2..300秒、最大5 attempt（既定値。`AGENTSTACK_CODEX_APP_WAKE_*` を旧と同じ reader で適用） |

bare shell・capture timeout・session 不在・Mail disabled・未確定 route は handoff 前なので attempt を増やしません。App の rate 超過と5回上限は blocked とし、dead_letter にはしません。global watcher は server の signal directory がまだ無ければ作らず2秒 poll で待ち、存在後は directory の event で動き、event reader が無ければ旧と同じ2秒 poll、event の取りこぼしは既存 scan 間隔で救済します。待機期限・blocked は通信前に判定します。importance 未満は同 intent の BELOW_MIN_IMPORTANCE とし、下限設定が変わった時だけ再評価します。BELOW_MIN_IMPORTANCE と SELF_MESSAGE は operator 対応待ちではなく、doctor の警告には数えません。read/ack 等で hint が消えたら、既存の reconcile が同 intent を片付けます。

数値互換の対象は、先頭0のない10進と試験した5境界（0・1・300・301・非数字）です。先頭0の timeout は旧 watcher の算術が8進、global は10進で、旧 watcher 本体は変更していません。

unknown の再試行は duplicate_possible を返し、後の未送信だけでは先行 unknown の警告を消しません。同 owner/binding の再 launch は古い intent を新 attempt へ捕まえ直します。本当に owner/credential が変わった場合はその message だけ blocked とし、明示 retry で現在値へ再計画できます。完了後の空 slot は既存 message lock 内で rmdir し、以前に残った空 slot も次の走査で片付けます。client operation mutex は削除後まで保持するため、並行 prepare は新しい slot を開き、intent を失いません。intent の列挙では数字の候補名だけを取り、存在・実体・permission の確認は message lock 内へ集約します。不正な slot は diagnostics に出し、1 message / App root の異常は他の通知を止めません。confirmed 後は server completion の再送だけです。遅れて来た別 attempt・旧 credential generation・旧 launch の callback は拒否します。

server の top-level status は要求を受けられる readiness、signal.status は通知の配送の健康です。local unknown/blocked は既存 doctor の warning と status/worker log から確認します。dashboard が配送健康を読む UI は後続です。

不正 slot の `repair-local-state` は自動修復や CLI 名ではなく、表示した message の固定 consumer slot の permission/type を利用者が直す案内です。正常な slot は変更せず、symlink の先は辿らず、修復後に diagnostics を再実行してください。

## lock と隔離試験

取得順は agent directory A → client operation C → message directory M → mutation U → authority F です。必要なものだけ取得し、外部 provider・tmux・bot の応答待ち・hook stdout flush の前に全て解放します。新しい launch.lock や配送 DB を作りません。SQLite の main/WAL/SHM/journal は client が開閉・copy・hash しません。

試験は実 HTTP health まで待つ一時 server、一時 HOME、専用 tmux socket を使います。installer の client payload は既存の宣言1か所から配布し、試験用 layout もその宣言から取得します。全体と sandbox 外の試験は作者の関連試験と分けて記録します。

管理 callback の delivery_completed だけは、通知後に宛先が退役しても既存 recipient と厳密な credential generation を照合して完了を記録します。公開 tool の退役拒否は維持し、再有効化後も同じ通知を再配送しません。

子の task_delivery=mail は、親の既存 mutation intent と task digest 由来の同じ UUID で send_message を再送します。本文を起動引数に埋めず、子は inbox から取得します。連絡承認は通常の contact policy に従い、自動では承認しません。tools_selection は既存 child_tools の方針を固定 provider HOME の設定に適用し、選択した server が無ければ TOOL_SELECTION_UNAVAILABLE で止めます。

### 明示的な Codex plan のやり直し

`codex-plan` が marketplace の作成後に中断した場合は、利用者が同じコマンドを明示的に再実行します。再構築するのは固定 provider HOME の `marketplace/` だけです。再実行は常に未承認の plan を返します。同じ内容でも再承認を必要とする一つの規則に揃え、古い承認が別の内容に使われることを防ぎます。通常の launch は再構築も承認も行いません。

### App の cold wake の範囲

App の履歴 HOME は旧 consumer と同じ共通関数で決めます。`CODEX_HOME` が無ければ通常の `~/.codex` です。履歴を子 root に複製・移動せず、resume の process 以外は書き換えません。resume には対象の子 context の proxy を渡し、単一 root の同時 resume を避けます。既存の plugin と人が承認した native trust を利用し、自動承認しません。通常の子 launch は引き続き子の固定 provider HOME を使います。

App の cold wake は fixture のみ、実 resume は未確認です。1時間12回に達すると `blocked / WAKE_RATE_LIMITED` になり、handoff の試行回数を増やしません。時間枠が空けば同じ intent から再開します。通知 fact は server に残り、poll は消費しません。

App の `AGENTSTACK_CODEX_APP_COLD_WAKE`、`AGENTSTACK_CODEX_BINARY`、`AGENTSTACK_CODEX_APP_PLUGIN_ID`、`AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK` は旧 consumer と同じ設定 reader を使います。cold wake の明示停止と Mail disabled の間は新しい handoff を作りません。

<a id="operator-ui-access"></a>
## 隔離 dashboard の operator API

[English](global-delivery-client.en.md#operator-ui-access)

既定の導入・旧 dashboard は変わりません。API 世代15の global backend は、完成した S2c candidate を私有管理 socket 経由で読みます。dashboard は Mail の SQLite を開きません。公開 Mail tool の追加はありません。API 16 では `global_dashboard_ui=1`・`instruction_migration=1` として [global dashboard](dashboard.md#global-mail-view) を有効にします。cockpit の接続は後続です。API の世代だけで画面の対応を推測しないでください。

専用の一時 HOME を使い、`AGENTSTACK_RUNTIME_MODE=global`、`AGENTSTACK_GLOBAL_SERVER_CONFIG`（完成した candidate の `server-config.json`）、`AGENTSTACK_MCP_URL`（同じ隔離 Mail の HTTP endpoint）、`AGENTSTACK_HOME`（同じ isolation root の `agentstack`）、`AGENTSTACK_PORT`（専用の空き port）を明示します。設定が壊れたときに旧 DB へ戻ることはありません。実サービスの切替は別の手順です。

最初にローカルの operator が同じ隔離先の `AGENTSTACK_HOME` を明示して `agentstack-http-operator configure` を実行します。CLI も未設定なら `HTTP_OPERATOR_HOME_REQUIRED` で拒否し、実 HOME を既定にしません。固定の `runtime/http-operator.json` を0600で新規作成し、既存 file は上書きしません。`agentstack-http-operator show` で表示した値を HTTP の `Authorization: Bearer …` に渡します。これは agent の登録 token とは別です。値を URL、ログ、共有ファイルに載せないでください。失効させる場合は、利用する dashboard と cockpit を停止し、この固定 file を operator が退避してから configure し直します。

隔離 dashboard の URL を開き、Unlock にこの operator capability を貼り付けます。値は sessionStorage に保持して reload に使い、URL には入れません。同じ origin で開いている tab は BroadcastChannel で同じ capability を共有し、Lock も伝えます。別 origin は別に Unlock が必要です。Lock は参加中の online page の Mail と資格情報を消しますが、閉じた・復元した tab、opener・duplicate が持つ sessionStorage のコピー、ページへの権限を与えた拡張機能、別 browser profile のコピーは失効しません。すべてのコピーを失効させるには server の capability を再作成して再起動します。OS 利用者と browser profile が信頼境界です。誤った・古い capability は403、不在・安全でない capability file は503で、旧経路へ fallback しません。

HTTP は TCP 上の UID を直接証明できません。私有 file を読める利用者の capability を証明にし、loopback・設定済み Host・提示された Origin/Referer/Fetch Metadata を同じ入口で確認します。static の GET/HEAD 以外は、将来追加される route もこの入口を通ります。capability を盗める同 UID/root、XSS、ページへの権限を与えた拡張機能は、この境界では防げません。別の利用者と1台の Mail を共有する用途は支えません。

| 読取 | 選択 |
|---|---|
| `/api/agents`、`/api/mail-watcher-health` | operator の roster / 健康。本文なし |
| `/api/mail/recent`、`/api/mail/message`、`/api/mail/conversation` | 必須の `view_as_agent_id` の可視集合 |
| `/api/mail/legacy-label` | 同じ可視集合の旧 label。`source` は拒否 |
| `/api/agent-history` | 独立した target `agent_id`。可視集合との共通部分だけ |
| `/api/messages-since` | `after_id` より後の可視行を keyset で継続 |
| `/api/edge-messages` | `a_agent_id` と `b_agent_id` の可視な送受信だけ |

MAX・keyset・limit・返却 revision は1つの read transaction に置き、fetch で read/ack/通知を消費しません。`/api/graph` も同じ可視 metadata page を使います。見られない message、隠れた BCC、不可視な上限 ID を足して集計しません。

### 登録の状態と中断からの復旧

`agentstack-ui-client --client-name` は既存 CLI と同じ slug（小文字英数字で始まり、以降は小文字英数字・`_`・`-`、64文字以内）です。名前から別の root を自動生成しません。`clients --name NAME` で既存 root を探し、同名があればその root を選んでください。複数なら利用者が選びます。

| `status --client-name SLUG` | 次の明示操作 |
|---|---|
| absent | `prepare-root --client-name SLUG` |
| root_only / registration_pending | 同じ `register --client-name SLUG --name NAME --program PROGRAM --model MODEL` |
| registration_saved | `finalize-registration --client-name SLUG` |
| registered_unconfigured | 固定 context を指定し、既存の provider configure / Codex plan・人の承認・apply |
| configured | 既存の明示 launch |
| launch_present | 同じ launch ID と digest で既存の launch-status / resolution |
| unavailable | 原因を直す。別の identity は新しい root |

登録は既存の pending 1つに name/program/model/token/binding を保存し、同じ intent だけを再送します。未送信なら1行を作り、応答喪失なら原子的 NAME_CONFLICT→名前の解決→保存済み token の whois で同じ行を確認します。credential/context/metadata の保存途中でも、保存済み row の finalize は ID と token を保ち、再登録しません。status と一覧は provider の設定や context を書き換えません。

別 owner の同名だった場合、pending の SHA-256 を固定して `registration-abandon --client-name SLUG --expected-digest DIGEST` を明示できます。これは所有できない行を確認した場合だけ terminal にし、別の名前の登録や自動採用はしません。未送信・結果不明は同 intent の再送、所有できた行は finalize を使います。通常入口から terminal を解除せず、新 identity には新 root を使います。

`POST /api/global/spawn` はこの共通処理を使い、cwd を既存の spawn roots で確認します。既存の最小入力は provider の指示配置だけを明示 configure し、Codex trust は自動承認しません。必要な設定が無ければ registered_unconfigured を返します。上記の full form は設定/承認を追加せず、構成済み owner の同じ caller を照合します。`POST /api/global/resume` は同じ root の既存 launch/session を使い、operator resolution 待ちなら新しい launch を作りません。通信成功後に disk が失敗した場合も、status から同じ pending/finalize の入口へ戻ってください。


名前の照合で Mail に既存の名前があり、一致する client root が無ければ、root/pending の作成前に `NAME_CONFLICT` を返します。`clients` の `name_agent_id` はその読取結果、`diagnostics` は root ごとの異常です。壊れた root は利用できませんが、他の root の一覧と別名の新規登録は続けられます。terminal pending は名前と owner conflict の証拠として保持し、同名の再利用を拒否します。退役済み identity の照合も管理側の owner 検証で行います。

登録直前の競合で pending が残った場合は、`GET /api/global/spawn-status?client_name=SLUG` の `registration_sha256` を確認し、同じ HTTP admission の下で `POST /api/global/registration-abandon` に `{"client_name":"SLUG","expected_digest":"DIGEST"}` を渡せます。digest 不一致では記録を変えません。既存 CLI と同じく別 owner と確認できた intent だけを terminal にし、別の identity には新 root を使います。root または登録 pending が無い場合は404 `TARGET_UNAVAILABLE` です。不正な既存 file の拒否と digest の照合は維持します。token は status に返しません。不正 JSON は400 `QUERY_INVALID`、登録の競合は409、transport などの障害は503です。

API 17 では登録の明示再送で status の digest を照合できます。browser の Lock は queue 済みの引継ぎと進行中の body を失効させ、storage 拒否時は page memory だけで動き reload 後に再 Unlock が必要です。

API 18 の `global_runtime_observation=1`・`operator_session=1` と非秘密の `agentstack-http-operator probe` は [稼働状態と能力の確認](dashboard.md#global-runtime-proof)を参照してください。cockpit の実装と PR7 の切替は別の関門です。

## mode の選択と context のない service

mode の正本は共通 client の `selected` です。選択だけでは owner の credential を読まず、登録や通信をしません。新しい top-level launcher・service は既存 `env.sh` を一度読み、結果を環境へ固定します。hook・worker は継承値を使い、稼働中の process は `env.sh` を変更しても切り替わりません。service は明示 stop/start で既定を採り直します。

| 入力 | 結果 |
|---|---|
| mode/context/config がない | 既定 legacy。旧経路を維持 |
| 正当な global context | global。installed default より context を優先 |
| 明示 legacy と global context/config | `RUNTIME_MODE_CONTEXT_CONFLICT` |
| global、context なし | mode の選択は成功。agent 操作は `CONTEXT_UNAVAILABLE` |
| 既存の隔離 server config だけ | global の operator/service。owner を借用しない |
| 不明 mode、broken context、不正 config | 固定 reason で拒否。旧 DB へ戻らない |

`agentstack-runtime-client select --json` は一時的な選択結果を返します。旧形式の context は `LEGACY_CONTEXT_REQUIRES_PR7` で拒否し、移行は後続の明示手順で行います。mode 未固定の旧 session が新しい global 既定に出会った場合は、既存の非秘密 session_index の自己 binding が照合できる間だけ legacy を続けます。証明不能なら `LEGACY_SESSION_MODE_UNRESOLVED` なので、その旧 session を終了してから明示再起動してください。project key だけを証拠にしません。

context のない watcher は `agentstack-runtime-client watch-service --server-config /absolute/server-config.json` です。既存 RuntimeOperator の管理境界で readiness/binding を照合し、固定 global-consumer の route ごとに既存 client の配送処理へ渡します。0宛先は正常待機、不正な1宛先は理由を出して隔離し、別の正常宛先の配送は続けます。owner 付き watcher とイベント待機・rescue scan の実装を共用します。待機や process の生存は配送完了の証明ではありません。完了の正本は server notification fact のままです。

人・service・cron の `agentstack-mailctl` と `agentstack-enroll` も同じ選択を使います。global の enrollment は `inspect --agent-id ID`、`claim/recover --agent-id ID --request-id UUID --expected-generation N` で既存管理 socket へ接続します。新 credential は stdin から渡し、argv に含めません。旧 enrollment CLI は legacy 選択時だけ呼びます。新しい controller の配布に共通 loader が欠けていれば `GLOBAL_ENTRY_UNAVAILABLE` で止まり、再配備が必要です。

既定は legacy、activation は false のままです。installer に global を渡して本サービスを切り替える操作は `GLOBAL_INSTALL_REQUIRES_PR7` で拒否します。隔離 server config の実行経路と、利用者の既定切替は別の段階です。隔離起動の専用 socket・名前と終了回収は後述の lifecycle 契約に従います。

installer は既存の literal 設定 reader で mode の既定を読み、env.sh の他の代入やコマンド置換を実行しません。空・不正な mode は拒否します。

### system controller と明示 watcher

hook・worker・直接 source する Codex/Gemini bootstrap は稼働中の mode を保持します。一方、installer・agentctl・mailctl・enroll・persistent launcher は installed default を採り直す system 入口です。呼出 shell の mode と食い違えば `RUNTIME_MODE_INHERITED_CONFLICT` で変更前に拒否します。明示 `--runtime-mode` の指定は共通 selector へ渡します。古い agent shell から system 操作をするときは継承した mode/config/context を外して実行するか、対応する明示引数で意図を指定してください。

既存 env.sh に legacy と global server config が同時に残れば `RUNTIME_MODE_CONTEXT_CONFLICT` です。旧 session の継続証明は mode 未固定で installed default が global のときだけ行い、名前・session・project の自己 binding を照合します。明示/継承 global の operator 操作を ambient agent 名だけで拒否しません。

通常の常駐 watcher は config-only の `watch-service` です。所有者 context だけを明示した shell の watcher 起動は `watch` を使い、同じ route 処理と待機 loop を共有します。この入口は context-only の手動確認にも使うため残しています。Mail の一時的な接続拒否・socket 不在・timeout は rescue 間隔で再試行します。認証・binding・応答形式の不一致は停止し、旧経路へ戻りません。App の partial 配置も共通 loader の admission を必須とし、欠落/未対応は `GLOBAL_ENTRY_UNAVAILABLE` なので共通 client を再配備してください。

legacy の Mail runner は従来の service argv を直接実行し、global の selector に依存しません。global installer の固定拒否は Codex 候補の実行より前です。

App の単独配備と marketplace にも正本の共通 loader を同梱します。core の root に対する同じ admission を呼び、旧 mode の単独配備を維持します。

Mail を無効にした global hook は owner の context/credential を読む前に skip します。旧 mode の hook の skip 契約は変えません。dashboard demo は合成の legacy DB 専用で、継承した global の owner 設定を使いません。

Codex/Gemini launcher は新しい tmux pane に選択済みの mode/config/context を渡します。project がある直接 bootstrap は env.sh でその project/MCP URL を上書きせず、mode が未固定の場合だけ共通 selector で installed default を補います。watcher wrapper は環境と `--server-config PATH` / `--server-config=PATH` のどれでも同じ選択結果で config-only / owner watch を分岐します。古い helper の select 未対応は、共通の partial admission に戻し固定理由で拒否します。

mode 未固定の継続で配置済み env.sh を selector が確認できない場合は `RUNTIME_DEFAULT_UNAVAILABLE` で停止します。helper を再配備してから同じ起動を再実行してください。env.sh が legacy でも未確認の設定を成功扱いせず、固定済み legacy と設定ファイルのない既定 legacy は維持します。installer の `--literal` は1つの引数としてだけ扱い、説明文中の同じ文字列には反応しません。

<a id="explicit-tmux-target"></a>
## 明示した対象の読取・attach・compact

`agentstack-runtime-client target --context /absolute/runtime-client.json --agent-id 2 --print` は、既存の資格・live 証拠から実際の socket、session/pane、PID/start を照合し、`state_sha256` と次に使える操作を返します。名前の prefix や大文字小文字の alias、別 server の同じ pane 番号へ fallback しません。非 tmux の standalone は process の読取だけです。`capture --agent-id 2 --lines 40` は同じ context を指定して使います（1–1000行、超過は truncated）。読取は既存 mutex を作らず、取れなければ helper unavailable で止まります。

operator は `--operator --runtime-mode global --server-config /absolute/server-config.json` を指定できます。同じ UID の private config と現 binding を使い、別の owner を登録しません。`attach --agent-id 2 --read-only --print` は完全な argv を返し、実 attach は exec 前の admission を stderr に出して terminal stream へ移ります。owner/parent には attach を許可せず、子への指示は Mail を使います。

証明済みの親は `send-compact --context /absolute/parent/runtime-client.json --agent-id 2 --confirm-state-digest <targetで得た64桁hash>` を使えます。operator はさらに TTY で yes を確認します。固定 `/compact` だけを送り、送信直前の入力欄が空で待機中の場合だけ許可します。自分の tool call 中、作業中、承認待ち、判断不能なら `TMUX_TARGET_NOT_IDLE`、待機中でも文字があれば `TMUX_TARGET_INPUT_NOT_EMPTY` です。`submitted` は tmux への提出で、provider の処理完了ではありません。

**Known limitation:** Claude の自動 suggestion と placeholder も文字として扱うため、compact は拒否されやすくなります。色から帰属を推測しません。拒否された親は次に入力欄が空になる待機状態を待つか、provider の自動 compact に任せてください。入力欄を消すための `send-keys` や、拒否を避ける別の launcher を使ってはいけません。

context 使用率も digest に含めます。提出後の応答不明は exit 3・`effects=unknown`、自動再送と新しい journal はありません。target/capture で確認後に人が選ぶ再送は新しい明示操作で、表示と context 値が元と同じなら重複し得ます。`effects=none` の事前拒否とは区別してください。協調 mutex は人/provider の入力を止めず、最後の観測と送信の間の競合窓も残ります。

旧 consumer の通知・monitor・OPEN・prune は共通の legacy admission を通し、不明な対象では副作用を止め、正常な別の対象は続けます。隔離 global の新規起動は後述の専用 socket を使い、旧 session の rename/adopt はしません。利用者の既定切替と provider 設定の移行は後続の関門です。

対象の同一性の digest は socket・pane・PID/start・owner/親の証明を結び、capture・attach と terminal open の再照合に使います。通常の画面更新では対象変更としません。compact の target digest はこの証明に入力欄の状態と ctx 値を加え、本文・時計・スピナーの更新は含めません。人の yes 待ちは観測期限に含めず、確認後の短い期限で同じ digest を再照合します。ttyd と terminal の実子も同じ helper で endpoint と対象を再照合し、cleanup は観測不明なら identity を保持します。resume 後に terminal を開けなければ `TERMINAL_OPEN_FAILED` を返しますが、成功した resume を取り消しません。

`send-compact` の確認には `target` が返す `state_sha256` を使います。`capture`・`attach` が返す値は対象の同一性の確認用なので、compact の確認値として流用しません。

不在は直接の `lstat` の ENOENT だけで判断し、EACCES・その他の I/O エラーは不明として保持します。親 directory は owner の rwx も必要です。共有 legacy consumer は、socket 不在または安全な socket で `no server running` を確認した inventory を空とします。cleanup は生存 lease がなければ退役へ進みます。unsafe socket・timeout・その他の読取不能は不明のまま保持し、観測だけで server を作りません。

## 専用起動と終了の回収

隔離 global client の新規 detached 起動は、instance と client HOME から決まる専用 socket と、agent の正確な名前の session を使います。同名・大文字小文字だけ異なる foreign session は `TMUX_SESSION_NAME_CONFLICT` で拒否し、rename・adopt・上書きはしません。旧 UUID session と standalone の証拠は旧形式のまま読めます。既定の旧 mode と PR7 の切替条件は変わりません。

runner の PID/start は既存 launch の `execution.runner`、実 provider の PID/start は既存 live slot に保存します。runner は provider を待ち、終了結果を launch へ確定してから報告と後片付けを行います。SessionEnd は process の終了証明ではなく、SDK の /clear でも早期退役しません。provider の終了・runner の終了・不明は区別し、生存が不明なら予約と identity を保持します。

`target --agent-id ID --print` は、終了済み・SessionStart 前の失敗でも保存された証拠から lifecycle と現在の `state_sha256` を返せます。現在の PID/pane が証明できない回収対象は null を返し、capture・attach・compact・signal の対象には使えません。確認した親 context、または明示 operator の同じ入口から `replay-end --agent-id ID --confirm-state-digest HASH --expected-intent-sha256 HASH` へ進みます。pending が無い場合だけ後者に literal `null` を渡します。launch の実行が unknown のときは `launch-resolve` に戻り、自動で再起動しません。

親への終端報告は既存の mutation pending に全文を保存し、同じ UUID と bytes で再送します。成功 receipt と終了 phase の確定が pending の消去より先です。結果不明では active のまま保持し、確定後だけ retire/release と local cleanup を完了します。retire commit 後の local 保存失敗も、親/operator の replay が残りだけを処理します。

退役済みで報告 pending が残る場合は、通常送信を再有効化しません。既存 `resolve-mutation` の digest/old-new binding/ID/confirm の照合に加え、`--parent-context` で確認した active な親、または同じ private server config の `--operator` と TTY の yes を要求します。これは未確認報告の明示的な local ack/abandon だけで、launch の既存 resolution に記録してから pending を消します。その後もう一度 target を読み、replay-end で後片付けを完了してください。

CLI `end` は既存 dashboard と同じ終了要求を共用します。通常は検証済み pane へ literal `/exit` と Enter、headless Gemini は検証済み provider PID だけに SIGINT を送ります。送信だけで終了とは見なしません。pane のない通常 provider は送信先を証明できないため拒否し、SIGTERM へ切り替えません。

launch を持たない standalone の終了回収も `target --print` → 同 digest の `replay-end` を使います。終了済み launch が残っていても standalone の保存済み session を区別し、自分の ACTIVE 予約だけを解放して owner は退役しません。解放の応答喪失は既存 pending を同じ UUID で再送します。別 session の生存・観測不明・別種類の pending は保持し、誤った pending hash は `MUTATION_PENDING_CONFLICT` で拒否します。

専用 socket を legacy bootstrap が拒否する境界は維持します。共通 legacy target admission 自身も、既存 launch の runner PID/start と live の nonce を読み、provider が終了していてもその runner の pane を global と判定して拒否します。PID の写しは増やしません。

終了済みの live slot は owner/operator とも現在の生存証拠に使いません。未終了 session の PID/start の対だけを照合し、古い PID の再利用で今回の回収を止めたり、逆に未終了 session の観測不明を不在扱いしたりしません。
