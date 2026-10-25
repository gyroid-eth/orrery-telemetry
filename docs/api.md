# API reference

> English version: [api.en.md](api.en.md)

[前: Dashboard](dashboard.md) · [README に戻る](../README.md) · [次: 設定](configuration.md)

## 基本

DeepSeek / Local は、opt-in と検証済みの native runtime 設定が揃うと標準 spawn の候補に加わります。
`/assets/deepseek.svg` は無効時404・有効時200。残高型はAPI 21の既存 `/api/quotas` に載せる。

base URL:

```text
http://127.0.0.1:8770
```

response body は、画像と HTML を除き JSON です。dashboard 自体に login layer はありません。`AGENTSTACK_BIND_HOST=0.0.0.0` を使うときは trusted LAN / VPN に限定してください。

legacy mode は全メソッド・全パスで固定の `Host` を検査し、不信・欠落・重複・不正な Host や重複 Origin、絶対形式 request-target の authority 不一致を dispatch 前に HTTP 403 で拒否します。既定の許可先は実待受ポートの `127.0.0.1` / `localhost` / `[::1]` です。LAN / VPN 向けの固定 origin は [`AGENTSTACK_DASHBOARD_ALLOWED_ORIGINS`](configuration.md#lan--vpn-から使う場合の-host-設定) に明示します。Host は認証の代わりにはなりません。

すべての POST endpoint は `Content-Type: application/json` と JSON object body が必須です。browser request は `Origin` / `Sec-Fetch-Site` が same-origin でなければ HTTP 403、CLI は両 header を送らない場合に利用できます。simple-form CSRF を受け付けないための共通 guard です。

POST の Origin は、検証済み Host に対応する固定 origin とだけ照合します。scheme・host・port を照合し、HTTP `80` / HTTPS `443` の省略と大文字小文字を正規化します。別の許可済み Host の origin を送っても拒否します。Host 検査の拒否応答は、通常の API JSON ではなく HTTP error body の場合があります。

失敗は原則:

```json
{"ok":false,"error":"reason"}
```

で HTTP 400 です。media type 不一致は HTTP 415、cross-origin POST は HTTP 403、存在しない route は HTTP 404、spawn catalog source が読めない場合は HTTP 503 です。

## Route 一覧

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/` | optional `?embed=1` | dashboard HTML |
| GET | `/api/version` | なし | `{name, version, api}` |
| GET | `/api/spawn-names` | なし | name / dir / provider catalog |
| GET | `/api/name-status` | `name` | exact identity status |
| GET | `/api/suggest-name` | `scientist` | verified `Adjective-Scientist`（要求形。実登録名は register 応答の read-back が正） |
| GET | `/api/fs/dirs` | optional `path` | root-scoped child directories |
| GET | `/api/agents` | なし | `{ts, agents}` |
| GET | `/api/graph` | `days`, `all` | `{nodes, edges, spawn, timestamp_diagnostics, degraded, ts}` |
| GET | `/api/history` | `session`, `limit` | transcript events |
| GET | `/api/agent-history` | `name` または `names`, `hours`, `include_pane_states` | agent event timeline |
| GET | `/api/edge-messages` | `a`, `b`, `limit` | 二者間 messages |
| GET | `/api/messages-since` | `since`, `limit` | live mail events |
| GET | `/api/annotations` | なし | role / group map |
| GET | `/api/deliverables` | `agent` | vault deliverables |
| GET | `/api/custom-portraits` | なし | custom portrait map |
| GET | `/api/term` | `session`, `lines` | tmux capture |
| GET | `/api/ptty` | `session` | browser terminal URL |
| GET | `/api/mail-watcher-health` | なし | watcher / signal health |
| GET | `/portrait` | `name`, `hi` | PNG または fallback SVG |
| GET | `/assets/<file>` | `.svg` / `.png` の basename | static asset |
| POST | `/api/jump` | `{session, open?, base?, tools?}` | open / focus / resume action |
| POST | `/api/exit` | `{session}` | graceful exit action |
| POST | `/api/kill` | `{session, mode}` | kill / retire action |
| POST | `/api/annotate` | `{name, role, emoji, group}` | saved annotation |
| POST | `/api/spawn` | spawn payload | child launch result |

## GET `/api/version`

```bash
curl -s http://127.0.0.1:8770/api/version
```

```json
{"name":"orrery-telemetry","version":"2026.09.16.1","api":30}
```

version の解決順は [インストール](install.md#version)を参照してください。

- `version`: いつの配布物か（`YYYY.MM.DD`、同日の追加リリースは `YYYY.MM.DD.N`。[CHANGELOG](../CHANGELOG.md)）。互換性の約束ではありません
- `api`: この API を使う側（[ORRERY cockpit](https://github.com/gyroid-eth/orrery) など）との**互換の世代**です。利用者が頼る endpoint や field を**足す・意味を変える**ときだけ 1 つ上げ、[changelog の断片](../changelog.d/)に書きます（release 時に CHANGELOG へ集約。[貢献・release 手順](../CONTRIBUTING.md#changelog-fragments-and-releases)）。release のたびには上げません。利用者は、必要な機能があるかを `version` の日付ではなく `api` で判定してください（cockpit は、`api` が必要な世代に足りないと起動時に警告します）。この運用は `api` 2 からで、それより前の版はどれも、中身にかかわらず 1 を返します

### 隔離 global backend（API 17）

明示 global mode では static 以外の全 route に operator capability の admission を適用します。`/api/version` も認証が必要です。`capabilities` の `operator_http_admission`・`ui_read`・`global_spawn`・`instruction_migration`・`global_dashboard_ui` は1です。画面を API 番号だけで有効にせず、対応する capability を確認してください。[設定・権限・中断復旧](global-delivery-client.md#operator-ui-access)を参照してください。旧 mode の payload は維持し、Host の検査は上記のとおりです。

API 17 は global dashboard の返信表示と次の認可済み読取 route を追加します。既定は旧 mode、activation は false のままです。

| Route | 投影 |
| --- | --- |
| `GET /api/instruction-migration?agent_id=ID&session_id=SESSION` | session は任意。その identity の固定 provider HOME から共通 finding を返します。所有を証明できない・曖昧な場合は `disk_state:unknown` とし、dashboard の利用者 HOME で代用しません。 |
| `GET /api/fs/dirs?path=PATH` | 既存の spawn-root の directory guard。Mail identity や作業 folder の変更から独立しています。 |
| `GET /api/deliverables` | `configured` と agent 名を key にした `items`。明示した `AGENTSTACK_DELIVERABLE_ROOTS` だけを使い、未設定なら空です。 |

指示の `data` は共通の [status schema](../bin/lib/instruction_contract.json) の provider・target_path・findings・checked_at・disk_state・session_state・evidence_kind・reason です。finding は既存の1定義を参照します。起動時の候補 hash は実際に読み込まれた指示の証明ではなく、cwd の変更も再読込ではありません。3 route とも Mail 読取と同じ operator admission を通ります。


### 同梱 Mail の識別子（API 12）

API 12の配布物では、同梱 ORRERY Mail の MCP `health_check` が `server_instance_id` を返します。これは管理 socket の inspect と同じ永続 UUID で、秘密や認証の代わりにはなりません。常駐 daemon は両方の transport の UUID と確認済みの connection pin を照合し、field の無い旧 build や不一致では停止します。既存の `status` / `environment` / `http_host` / `http_port` は維持します。これは dashboard の新しい HTTP route ではありません。[専用 binding と通知](daemon-agents.md)を参照してください。

## GET `/api/quotas` — 任意のDeepSeek残高（API 21）

dsh opt-inが有効な時だけ `providers` に `provider: "dsh"` を加える。
無効時は取得せず、既存のquota応答の形も変えない。
残高型は `measurement_kind: "balance"`、`buckets: []` とし、
`balance` に `currency: "USD"`、`total_balance`（金額文字列又はUSDなしのnull）、
`is_available` を持つ。CNYは返さない。金額の原精度を保持する。
`is_available: false` は取得成功で利用不可という意味であり、通信失敗とは区別する。
失敗・未取得では `status: "unavailable"`、`balance: null`、固定の理由codeを返す。
0円や百分率bucketへ置き換えない。`observed_at` / `last_observed_at` / `checked_at`
で取得時刻を示し、4分cacheを使う。鍵・Keychainの応答・HTTPの生エラーは返さない。

取得workerだけがKeychainの `deepseek-api` を読み、dashboardのenvへ鍵を入れない。
既存の同一origin検査とglobal operator admissionはそのまま使う。
[公式の残高API](https://api-docs.deepseek.com/api/get-user-balance/) と
[取得・実確認の手順](dsh-stage-a.md)も参照。

## GET `/api/spawn-names`

API 27ではnative接続の`id`と`kind`を分けます。`id`は設定した接続先そのもの（例：`strata`と`strata-epyc`）、`kind`は`deepseek`または`strata`です。catalogが返した`id`を`POST /api/spawn`の`provider`へそのまま送り、選んだ`model`と`harness`も送ります。同じmodel IDでも接続IDが異なれば別の接続先です。`label`と`server`は表示用で、経路の判定には使いません。旧`deepseek`/`strata`はそのまま使えます。[複数接続の設定](configuration.md#native-connections)を参照してください。

同梱 NEW AGENT も Model → Harness → Helper model → Effort の順で選択します。未操作・Use default では catalog が報告した有効な具体 effort を送り、既定が null・不明・候補外なら effort を省略します。候補先頭を既定と推測せず、effort 必須の provider は明示選択を要求します。Helper model は Same as main なら省略します。Terminal は未操作なら headless を省略して現行の起動方針を継承し、明示操作時だけ送ります。

API 23 では、明示設定された DeepSeek (`provider=deepseek`) と Local (`provider=strata`, `locality=local`) も標準 spawn の候補です。モデルの `harnesses` にある `available` と `efforts` / `default_effort` / `effort_source` を使います。能力はローカルの検証済み cache 由来で、試運転の effort 設定を既定値へ転用しません。通常の catalog は managed task mode や attempt ID を要求しません。runtime 未準備や opt-in 無効の候補を起動可能とは表示しません。

Claude provider の `model_source` は `override` / `local_cache` / `bundled`、`model_error` は明示設定の診断です。不正な明示指定ではClaudeの `models` は空になり、他providerは残ります。top-levelの `models` / `default_model` は同じClaude候補を返します。探索規則と権限の境界は [Claude model catalog](configuration.md#claude-model-catalog) を参照してください。

query はありません。

```bash
curl -s http://127.0.0.1:8770/api/spawn-names
```

```json
{
  "names": [
    {"name":"Curie","portrait":true,"status":"available"}
  ],
  "adjectives":["Windy","Curious"],
  "naming":"adjective+scientist",
  "dirs":["~","/path/to/project"],
  "models":[
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1",
    "claude-sonnet-5",
    "claude-opus-5"
  ],
  "default_model":"claude-opus-5-5",
  "providers":[
    {
      "id":"claude",
      "label":"Claude",
      "program":"claude-code",
      "models":["claude-opus-5-5","claude-sonnet-5-5","claude-haiku-4-5-20251001","claude-fable-5-1","claude-sonnet-5","claude-opus-5"],
      "default_model":"claude-opus-5-5",
      "efforts":null
    },
    {
      "id":"codex",
      "label":"Codex",
      "program":"codex-cli",
      "models":["gpt-6.1-sol","gpt-6-sol","gpt-6-astra","gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna","gpt-6-luna"],
      "default_model":"gpt-6.1-sol",
      "model_source":"bundled",
      "model_error":"",
      "model_note":"",
      "efforts":["low","medium","high","xhigh","max","ultra"],
      "effort_default":"xhigh"
    }
  ]
}
```

scientist rail の `status` は、その scientist と134語の adjective の組み合わせに少なくとも1件の空きがあるかを表します。全組み合わせが埋まると `occupied`、DB がない、または query に失敗すると `unknown` です。bare surname の登録有無だけでは決めません。

adjective は ORRERY Mail の正典 `SIMPLE_ADJECTIVES` Round 3 と逐語同期し、launcher・catalog・suggestion API が同じ source を使います。独自追加は strict deployment の name validation と乖離するため禁止です。

Codex は同梱候補に取得から300秒以内のCLIローカルcacheの候補を追加します。`AGENTSTACK_CODEX_MODELS` 明示時はその許可リストだけを使い、不正設定は `model_error` に返します。`model_source` は `bundled` / `local_cache` / `override` です。モデル省略と `sol` の既定候補は、起動対象 CLI が 0.159.0 以上なら `gpt-6.1-sol`、古い版なら `gpt-6-sol`、版不明なら新鮮な catalog で初期判定します。その ID が選択された `CODEX_HOME` の cache に無ければ、`gpt-6.1-sol` → `gpt-6-sol` → `gpt-6-luna` → `gpt-5.6-terra` → `gpt-5.6-luna` の順で次の掲載候補へ下げます。既定を下げる判断には期限切れの cache も使いますが、古い CLI を GPT-6.1 Sol へ上げることはありません。使える cache や次の候補が無ければ従来の既定を維持します。正式 ID の明示要求は置き換えません。provider の `model_note` は優先する既定から変更が無ければ空文字列で、変更があれば変更前後と理由を伝え、期限切れの場合は取得日時と `codex` 起動による更新案内も含みます。cache の `identity` は現在のログインと照合しておらず、fallback は利用権限の証明ではありません。provider の `model_efforts` と `model_effort_defaults` はモデルIDをkeyとする辞書で、UIはこちらを優先します。空のeffort一覧はCLI既定を使うため、effortを省略してください。詳細は [設定](configuration.md#codex-model-catalog) を参照してください。

## GET `/api/name-status`

完全な identity 名を exact check します。

```bash
curl -s 'http://127.0.0.1:8770/api/name-status?name=WindyFermi'
```

```json
{"name":"WindyFermi","status":"available"}
```

`status` は `available / occupied / unknown` です。DB がない、query error、空の名前は fail-closed の `unknown` になります。この endpoint 自体は name syntax を検証せず、`POST /api/spawn` が別途検証します。HTTP status は結果にかかわらず 200 です。

## GET `/api/suggest-name`

scientist rail で選んだ suffix に、空きが確認できた adjective を server が付与します。

```bash
curl -s 'http://127.0.0.1:8770/api/suggest-name?scientist=Fermi'
```

```json
{"name":"WindyFermi"}
```

server は正典 adjective から最大20候補をランダム順で検査し、`available` を確認した最初の名前だけを返します。scientist が roster 外、または検査した候補がすべて occupied / unknown の場合:

```json
{"error":"no available name found"}
```

を HTTP 409 で返します。UI の SHUFFLE は local で名前を組み立てず、この endpoint で毎回再検証します。

## GET `/api/fs/dirs`

NEW AGENT の directory typeahead 用です。

```bash
curl -s 'http://127.0.0.1:8770/api/fs/dirs?path=/Users/me/code'
```

```json
{
  "path":"/Users/me/code",
  "dirs":[
    {"name":"project-a","path":"/Users/me/code/project-a"}
  ],
  "truncated":false
}
```

`path` を省略すると `AGENTSTACK_SPAWN_ROOTS` の先頭を使います。許可 root 外、`..` を含む path、存在しない directory には:

```json
{"path":null,"dirs":[]}
```

を返します。hidden directory と root 外へ出る symlink は除外し、名前順に最大 500 件を返します（page 側が入力中の prefix で絞るので、一覧全体が要ります）。501 件以上なら `truncated: true` です。

## GET `/api/agents`

API 世代24では dsh/Hermes の稼働中 row と `/api/graph` node の `act_state` が、確認できた現在の操作表示から `work`・`question`・`ask` を返します。完全な入力欄の形があり、作業・質問・承認の印が無い場合は `wait` です。非表示・不完全・未対応の形は `null`（不明）とし、単なる表示の欠如を入力待ちにしません。生存・終了・停止可能性の判定には使いません。既存 Claude/Codex の分類は不変です。[対応する native 表示と制限](native-capabilities.md#activity-classification-boundary)を参照してください。

API 世代19では legacy の稼働中 Codex CLI row と `/api/graph` node に nullable な `provider_stall` を追加します。停止中は `{kind: "model_capacity", since: <UTC epoch秒>, error: <エラー1行>, notification_status: <状態>}`。通知状態は `sent`、`no_parent`、`parent_unverified`、`delivery_unknown`、`failed` です。停止しても `act_state` の既存の意味は変えません。provider での停止は独立した field です。

dashboard の常駐 worker が現在の capacity エラーを別の capture で2回確認し、検証済みの子 identity から `PARENT_AGENT` へ1回だけ通知します。GET に送信の副作用はありません。作業再開、または同じ pane で「前の停止→提出済みの新入力→新しい容量エラー」の追記を特定して2回確認した場合に、次の停止を通知します。履歴の境界が曖昧なら元の停止を維持します。送達不明なら再送せず `delivery_unknown` を保持します。global は未対応・後続です。詳しくは [provider 停止](dashboard.md#provider-停止の自動通知legacy) を参照してください。

query はありません。DECK が数秒ごとに取得します。

response:

```json
{
  "ts":1785480000,
  "agents":[
    {
      "name":"WindyFermi",
      "session":"WindyFermi",
      "running":true,
      "category":"agent",
      "model":"gpt-5.6-sol",
      "provider":"openai",
      "task":"README を更新",
      "resume_capability":"not_required",
      "last_active":1785480000
    }
  ]
}
```

実際の row には pane title、state、elapsed、context、attach、latest message などの表示用 field も含まれます。frontend は未知 field を無視します。

各 row の `resume_capability` は backend が判定した固定 reason code です。事前検証済みなら `ready` です。終了済み Claude row に exact index も transcript directory mtime と一致する確定済み cache もない場合、表示 API は transcript を全読みせず `verification_required` を返します。この code は NETWORK の一括 resume 対象にはなりませんが、DECK の action から `/api/jump` を1回呼ぶと full 検証し、`ready` なら同じ request で resume まで続けます。確定結果は directory mtime が変わるまで再利用します。稼働中など resume が不要な row は `not_required`、確認できない provider は `unsupported_provider`、履歴・cwd・CLI・正式登録・Codex launch provenance・credential・設定の検査に失敗した row はそれぞれ `no_history`、`cwd_missing`、`cli_missing`、`registration_missing`、`provenance_missing`、`credential_missing` / `credential_permission`、`identity_mismatch`、`config_unrestorable` になります。Codex の provenance は製品起動の `child` または `standalone` だけを受け入れ、origin 不明の旧 row は fail-closed です。期限切れの retained material は `retention_expired`、明示 purge 後は `purged` です。DECK / NETWORK 間の表示値は短期 cache されますが、`/api/jump` はそれを使わず操作直前に再検査します。

会話だけの再開は `resume_mode: conversation_only`、`mail_status: unavailable`、固定 `mail_reason`（`credential_absent` / `retention_expired` / `mail_schema_unsupported`）と「この agent は ORRERY Mail を送受信できない」旨の `mail_message` を返します。DECK の chip と NETWORK のラベル・詳細にも表示し、起動端末にも1行出します。`resume_capability: ready` は会話再開の検査結果であり、Mail の owner 認証成功を意味しません。

API 世代4からの追加 field です。GET `/api/agents` / `/api/graph` の Claude row にも `mail_status` / `mail_reason` / `mail_message` を表示します。field が無い旧 client/row の Mail 到達性は未報告であり、認証成功を保証しません。明示 purge は拒否のまま、保持期限切れの Claude は安全性検査後に会話だけ再開可能です。Codex の期限切れ拒否は変わりません。

API 世代5からの追加 field です。resume できる（`ready` または `verification_required`）終了済み Claude row には、resume の前に `resume_mode` を出します。`mail` は、resume が owner を認証して Mail の row を unretire すること（できなければ起動せずに拒否し、会話だけの再開に切り替えないこと）を、`conversation_only` は Mail に触れずに会話だけを再開することを示し、このときは `mail_status: unavailable` と `mail_reason` / `mail_message` も付きます。`/api/jump` の応答の `resume_mode` はこの見込みと同じ値で、Mail まで戻した Claude の resume も `resume_mode: mail` を返します。`resume_capability` の code と意味は変えていないので、`ready` で resume を許す利用側はそのまま動きます。Codex row と、resume できない row・稼働中の row には `resume_mode` を出しません。

`mail_reason: mail_schema_unsupported` の row と応答には `mail_remedy` も付きます。この installer が入れた ORRERY Mail を `./scripts/install.sh --update-mail` で更新し、会話だけのセッションを終えてから再開し直す手順で、dry-run が拒否したら [Mail の更新手順](agentstack-mail-update.md) に従うよう案内します。同じ文は `mail_message` の末尾にも入るので、`mail_message` を表示する利用側には新しい field を読まなくても届きます（API 世代は変えていません）。`credential_absent` と `retention_expired` は dashboard から直せないので付きません。稼働中の会話だけのセッションは、Mail の row が retired で `program` が空でも `mail_status` / `mail_reason` / `mail_message` を出します。

## GET `/api/graph`

API 世代26では `/api/graph` の node に正規化前の `model_raw` を追加します。`provider=strata` / `locality=local` を既存の Local 六角形で表示し、`model_raw` を優先して Qwen などの maker を選びます。未知の maker は空の六角形です。provider / locality の意味は変わりません。

query:

| Field | Default | 意味 |
| --- | --- | --- |
| `days` | `4` | mail / spawn history の期間 |
| `all` | `0` | `1` / `true` で全 agent を含める |

```bash
curl -s 'http://127.0.0.1:8770/api/graph?days=4&all=0'
```

response:

```json
{
  "nodes":[{"id":"WindyFermi","name":"WindyFermi","resume_capability":"not_required"}],
  "edges":[{"source":"Parent","target":"WindyFermi","count":3}],
  "spawn":[{"parent":"Parent","child":"WindyFermi"}],
  "timestamp_diagnostics":{"invalid_count":0,"fields":{}},
  "degraded":false,
  "ts":1785480000
}
```

ORRERY Mail の timestamp は、legacy の ISO 8601 text と Rust 実装の integer microseconds のどちらも epoch seconds に正規化してから比較します。NULL と空文字以外の解釈不能値がある場合は `timestamp_diagnostics.invalid_count` と該当する `fields` を返し、`degraded` を `true` にします。解釈不能値を epoch 0 として扱うことはありません。

`/api/agents` と `/api/graph` は、同じ query の計算を同時に 1 本だけ走らせます。計算中に来た request は、その計算が終わった直後に始まる次の計算の結果を受け取るので、表示が古くなることはありません。ほかの request の計算を 30 秒待っても結果が無いときは、`503`、`Retry-After: 1`、`{"error":"busy","retry":true}` を返します。前の表示を残して取り直してください。この 30 秒は待ちの上限で、自分で計算を始めた request の計算時間は含みません（待った後に自分が計算する場合、request 全体は 30 秒を超えることがあります）。API 世代6からの応答です。

data source が読めない場合も HTTP 200 で空の `nodes / edges / spawn` と `error`、`degraded: true` を返し、DECK 全体を巻き込まないようにします。

## GET `/api/history`

query:

- `session`: tmux / agent name
- `limit`: 最大 event 数。既定 `220`

```bash
curl -s 'http://127.0.0.1:8770/api/history?session=WindyFermi&limit=220'
```

response は `ok`、source file、`shown` / `total`、正規化した `events` を含みます。event は `role`、`kind`、`text`、`ts` を持ち、Claude / Codex の transcript 差を frontend から隠します。

## GET `/api/agent-history`

単一 agent:

```text
/api/agent-history?name=WindyFermi&hours=24
```

REPLAY 用 union:

```text
/api/agent-history?names=Parent,WindyFermi&include_pane_states=1
```

| Field | 意味 |
| --- | --- |
| `name` | 単一 agent |
| `names` | comma-separated 複数 agent。指定時は `name` より優先 |
| `hours` | lookback。省略時は event range に auto-fit |
| `include_pane_states` | `1 / true / yes / on` で ask / pane state event を含める |

response は `{ok, events, ...}` です。各 event は replay が使う timestamp、kind、agent、相手、状態、message metadata を必要に応じて持ちます。

## GET `/api/edge-messages`

```bash
curl -s 'http://127.0.0.1:8770/api/edge-messages?a=Parent&b=WindyFermi&limit=60'
```

- `a`, `b`: agent 名
- `limit`: 既定 `60`

response は `{ok, messages}` です。message item は direction、subject、body、importance、timestamp を含み、NETWORK の edge drawer が使います。

## GET `/api/messages-since`

```bash
curl -s 'http://127.0.0.1:8770/api/messages-since?since=1785480000&limit=80'
```

- `since`: Unix epoch、既定 `0`
- `limit`: 既定 `80`

response は live comet 用の message list と watermark を含みます。設定不足は空 list と診断情報で表し、HTTP polling 自体は継続できます。

## GET `/api/annotations`

```json
{
  "ok":true,
  "annotations":{
    "WindyFermi":{"role":"docs","emoji":"","group":"release"}
  }
}
```

annotation は `GET /api/annotations` と `POST /api/annotate` を通じて読み書きします。

## GET `/api/deliverables`

```bash
curl -s 'http://127.0.0.1:8770/api/deliverables?agent=WindyFermi'
```

```json
{
  "ok":true,
  "agent":"WindyFermi",
  "vault":"",
  "items":[
    {
      "title":"LOG_2026-08-01T0900 Release audit",
      "rel":"LOG_2026-08-01T0900 Release audit.md",
      "vault":"",
      "mtime":1785542400
    }
  ]
}
```

最大25件です。`AGENTSTACK_DELIVERABLE_ROOTS` が設定されていれば、その `:` 区切り root 群を再帰走査します。未設定時は project、vault、cwd / git root の順で base を決め、その `logs/` を使います。`LOG_*.md` の frontmatter `agent:` が query の agent と一致する item だけを返します。

各 item の `vault` は、その file が `AGENTSTACK_VAULT` 内にある場合だけ vault 名になります。その場合の `rel` は vault-relative path で、UI は Obsidian link を作れます。vault 外では `vault` は空、`rel` は走査 root からの relative path となり、UI は非リンク項目として表示します。top-level `vault` は設定された vault 名を返す compatibility field です。

## GET `/api/custom-portraits`

query はありません。

```json
{"mybot":"mybot","windyfermi":"Fermi"}
```

設定 file が未指定または読めない場合は空 mapping を返します。

## GET `/api/term`

```bash
curl -s 'http://127.0.0.1:8770/api/term?session=WindyFermi&lines=500'
```

- `session`: 必須
- `lines`: capture 行数、既定 `500`

response は `{ok, session, text, ...}` です。session 名を検証してから `tmux capture-pane` を実行します。

## GET `/api/ptty`

```bash
curl -s 'http://127.0.0.1:8770/api/ptty?session=WindyFermi'
```

response:

```json
{"ok":true,"session":"WindyFermi","url":"http://127.0.0.1:PORT/"}
```

既存 `ttyd` があれば再利用し、なければ空き port で起動します。無効 session や missing dependency は HTTP 400 です。

## GET `/api/mail-watcher-health`

query はありません。

response は少なくとも次の診断を返します。

```json
{
  "status":"green",
  "watcher_running":true,
  "signal_count":0,
  "actionable_signal_count":0,
  "orphan_signal_count":0,
  "signal_sessions_known":true,
  "last_success_age_s":4,
  "recent_results":{"success":3}
}
```

watcher process、signal backlog、直近成功時刻、直近10分の配送結果を header indicator が使います。

API 11 から、`signal_count` は残っている全 signal（legacy を含む）の数、`actionable_signal_count` は health 判定に使う数、`orphan_signal_count` は24時間以上古く、tmux session の不在を確認できた数です。headless runtime manifest がある宛先と新しい signal は常に判定に残します。tmux の測定失敗時は `signal_sessions_known: false` とし、除外しません。watcher 不在か actionable が50件を超えると `red`、actionable が残り直近成功が120秒以上前（または成功なし）なら `yellow`、それ以外は `green` です。

## GET `/portrait`

```text
/portrait?name=Curie&hi=1
```

- `name`: portrait key
- `hi`: `1 / true` で高解像度を優先

overlay、高解像度 bundle、64px bundle の順で PNG を探します。安全な未登録名には fallback SVG、無効 path には 404 を返します。

## GET `/assets/<file>`

`dashboard/assets` 直下の basename だけを配信します。許可 extension は `.svg` と `.png` です。`/`、`..`、その他 extension は 404 です。

## POST `/api/jump`

API13: `/api/jump` は明示 `runtime_mode=global`・`client_config`・`client_name`・`prepare_only=true`・`open=false` で固定 root の再開準備を受け取ります。返り値の runtime_ready は false、旧 session 入力は不変です。

request:

```json
{"session":"WindyFermi"}
```

`open` は任意の boolean です。`false` は OS の窓を開かず detached tmux で resume、`true` は窓を開きます。省略時、Claude / Codex / dsh / Hermes resume は `AGENTSTACK_AUTO_OPEN_CHILD` に従います（`0` は detached、未設定 / `1` は窓を開く）。明示した `open` が設定より優先します。稼働中の session に `open: false` を渡すと attach せず、そのままにします。環境設定は resume 時の自動表示だけに適用され、手動の Open tmux は使えます。detached resume には terminal adapter は不要です。この field は API 世代3から使えます。

response は `{ok, session, actions}` です。既存 tmux session は configured terminal で open / focus します。session が無い場合と finished husk を transcript から復元する場合は、GET row と同じ判定を server が再検査します。表示が `verification_required` だった Claude row も、この1回の request 内で full 検証して `ready` になれば、そのまま resume します。Codex child は fresh home を生成し、credential 付き再登録と fresh binding expectation を作成した後、Codex exec の直前に unretire します。拒否時は HTTP 400 で `{"ok":false,"error":"...","resume_capability":"provenance_missing"}` のように確定した固定 reason code を返し、husk の kill や terminal 起動は行いません。bootstrap / unretire が失敗した場合も Codex は起動せず、生成した home / config だけを片付けます。

Claude は保存済み owner credential で同じ project・数値 ID・name・program の登録を検証し、credential 付き再登録と unretire が成功してから端末を起動します。Claude child の state / credential も既定30日保持し、resume 時は子専用 Mail proxy config を再生成します。top-level Claude は既存 owner token を使います。旧 cleanup で credential が消えた場合は `credential_missing`、期限切れは `retention_expired`、明示 purge 後は `purged` で起動を拒否します。認証・unretire の失敗でも端末を開きません。credential を自動発行したり別名で登録したりはしません。Codex top-level resume も unretire の対象です。

dsh / Hermes の子は、本人の session 作成イベントで保存した native ID だけで再開します。最新の履歴を探すことはせず、ID が無ければ `no_history` です。同じ登録・保持期限・owner credential を検査し、元の `harness_state_dir` と native profile を維持します。元の task は再送しません。Mail の再開と tmux の起動ゲートの解放より先に config / MCP を生成し、profile の変更や生成失敗では元の husk・state・資格を残します。内部の `--resume-native` は既存 pane の foreground で実行し、新しい tmux や登録は作りません。

Claude の起動準備が失敗した場合、元の husk を残し、元が retired だった Mail を再 retire します。元から active の identity は retire しません。復元にも失敗した場合は `rollback_errors` を返し、成功したようには報告しません。CLI は tmux と窓の準備が成功するまで待機します。

旧3項目形式の Claude child は、private token と既存の正式 owner 登録が検証できれば `ready` になります。表示 GET は読み取りだけです。実際の認証・同じ identity の新形式への移行は明示 resume 時に行い、認証失敗では起動しません（[移行条件・保持期限・失敗時の復元](launchers.md)）。

API 9 は任意の `base` / `tools` を受け付けます。`tools` は全置換、`{}` は追加の道具の解除です。base 省略は既存 base、base だけの指定は既存 tools を維持し、両方省略した呼び出しは従来の動作です。動作中・状態不明・retained state が無い子・standalone・conversation-only・Codex App は道具変更を拒否します。まず `/api/exit` を終え、`gone` / `finished` を確認します。変更は子ごとに直列化し、既存の認証と native resume を使い、起動失敗時に当該世代の旧記録・生成設定へ戻します。成功は tmux／terminal の起動受理であり、shell 内の bootstrap／認証の完了は待ちません。後発失敗でも新しい選択は保持し、次の resume に使います。子からの受信確認が必要です。成功応答に `tools_changed: true`, `base`, `tools` を追加します。installer の既定は追加しません。[手順と制限](delegation.md#停止した子の道具を変更して-resume-する)。

## POST `/api/exit`

request:

```json
{"session":"WindyFermi"}
```

response:

```json
{"ok":true,"session":"WindyFermi","actions":["exit-sent"]}
```

`running / finished` category かつ実在 tmux session だけが対象です。`warm-*` / `pending-*` は拒否します。attached session は処理を止めず `warn-attached` を actions に加えます。

## POST `/api/kill`

request:

```json
{"session":"WindyFermi","mode":"both"}
```

`mode` は `tmux`、`retire`、`both` を想定します。response は `{ok, session, mode, actions}` です。server が category と session 実在を再確認してから tmux kill / soft retire を行います。

## POST `/api/annotate`

request:

```json
{"name":"WindyFermi","role":"docs","emoji":"","group":"release"}
```

`name` の代わりに `session` も使えます。

response:

```json
{
  "ok":true,
  "annot":{"name":"WindyFermi","role":"docs","emoji":"","group":"release"}
}
```

`role` と `emoji` を空にし、group も残さない場合は annotation を削除します。spawn は `emoji` を保存せず role / group だけを使います。

## POST `/api/spawn`

parent ありの Claude child:

```bash
curl -s -X POST http://127.0.0.1:8770/api/spawn \
  -H 'Content-Type: application/json' \
  -d '{
    "parent":"CuriousCopernicus",
    "name":"WindyFermi",
    "dir":"/path/to/project",
    "provider":"claude",
    "model":"claude-sonnet-5-5",
    "role":"docs",
    "group":"release",
    "task":"README を検証する",
    "worktree":false
  }'
```

Codex:

```json
{
  "standalone":true,
  "name":"Sunny-Curie",
  "dir":"/path/to/project",
  "provider":"codex",
  "model":"gpt-5.6-sol",
  "effort":"high",
  "task":"API を検証する"
}
```

request:

| Field | 必須 | 内容 |
| --- | --- | --- |
| `parent` | child のみ | 有効な既存 agent 名。`standalone: true` では省略 |
| `standalone` | no | boolean。`true` なら parentless 起動 |
| `dry_run` | no | boolean。`true` なら登録・起動せず起動プレビューを返す |
| `headless` | no | boolean。`true` は端末ウィンドウの自動表示を無効、`false` は有効にする。省略時は launcher 設定を継承。agent はいずれも tmux で動く |
| `emoji` | no | string。cockpit 互換として受け付ける。spawn annotation は `role` / `group` だけを使用 |
| `async` | no | boolean。readiness 確認をバックグラウンドで行う。`dry_run: true` では無視 |
| `task` | yes | task 本文。UI は最大4000文字 |
| `name` | no | 指定時は hyphen を除去し、`available` 必須 |
| `dir` | no | 存在する working directory。既定は source repo |
| `provider` | no | `claude`（既定）、`codex`、またはcatalogが返したnative接続ID（旧`deepseek`/`strata`、追加例`strata-epyc`）。表示名や`kind`へ置き換えず、そのIDを送る |
| `harness` | DeepSeek / Local 必須 | `dsh` または `hermes`。Claude は `claude-code`（従来 alias `claudecode`）、Codex は `codex-cli`（従来 alias `codex`）を省略可 |
| `helper_model` | no | 省略または `same` は主モデルを継承。DeepSeek / Local は選択した model × harness の `helper_models` にある同 backend の正式 ID も指定可。未提供・別 backend の ID は拒否 |
| `model` | DeepSeek / Local 必須 | 正式 ID。DeepSeek / Local は選択した provider と harness の有効な catalog entry と一致必須。Claude / Codex は既存の省略時の既定を維持 |
| `effort` | no | 明示時だけ送る。Claude / Codex は `model_efforts` / `model_effort_defaults`、DeepSeek / Local は選択した model × harness の能力で検証。未指定は CLI 設定を使用し、能力不明の値は推測しない |
| `role` | no | 最大40文字 |
| `group` | no | 最大24文字 |
| `worktree` | no | isolated worktree |
| `worktree_base` | no | base revision。既定 `HEAD` |
| `claude_chrome` | Claude only | boolean。`true` で child に `--chrome` を付ける。省略・`false` は inherit（利用者の Claude 設定に従う）。launcher の env 既定は使わない。[Claude in Chrome](delegation.md#claude-child-とブラウザ操作claude-in-chrome) |
| `claude_chrome_device` | Claude only | 使うブラウザの deviceId（`[A-Za-z0-9._:-]{1,128}`）。指定すると `claude_chrome: true` と同じ。`claude_chrome: false` との併用は拒否 |
| `base` | Claude / Codex / dsh / Hermes | `"default"`（明示すると installer の道具の既定を適用しない）または `"mail-only"`（ORRERY Mail と `tools` で選んだものだけ）。api 7 から |
| `tools` | Claude / Codex / dsh / Hermes | object。`browser`（`true` は Claude/Codex、`{"device": "<deviceId>"}` の deviceId は Claude only）、`screen`（`"read"` / `"operate"` または `{"access": ..., "server": "<name>"}`。server を省くと Mac の computer use で Claude only）、`mcp`（server 名の配列。tool は承認しない）、`approve_all`（`mcp` のうち全 tool を承認する server。表に無い版は拒否）。`claude_chrome_device` と deviceId が食い違えば拒否。api 7 から。[子に渡す道具を選ぶ](delegation.md#子に渡す道具を選ぶ--base----tools) |

DeepSeek / Local の通常 catalog は、plain 世代の識別と generator の静的検査を launcher と共用します。旧 managed 世代を自動で利用可能にはしません。effort は選択した model × harness の検証済み cache を使用し、不明は不明のまま返します。生存判定は pane の子プロセスに native CLI があるかを確認し、確認不能を終了済みにはしません。provider は子の明示的な harness_backend から読み、モデル名から推測しません。

dsh / Hermes では、選択を native config で表せる場合だけ `base` / `tools` を受け付けます。stdio / HTTP の MCP server を写し、内蔵 computer use・Chrome device・表せない tool 単位の承認やフィルターは理由つきで拒否します。起動できたことを、それらの承認が有効になった意味にはしません。

API 8 では、`tools: {}` または `base: "default"` を明示すると `--base default` を渡します。dashboard/API はフォームの選択を正とし、installer の `AGENTSTACK_CHILD_DEFAULT_TOOLS` を launcher の env から外して、選択から外した道具を追加しません。明示した道具が使えなければ起動を止めます。CLI で base/tools/MCP profile を省略したときだけ、使えない既定の項目を通知して外す経路を使います。

`base` か `tools` で選択した child は、選択どおりにできなければ（Mail の proxy が無い、server を写せない、computer use が有効な project での `mail-only` など）起動しません。形の誤りはここで 400、child の起動ディレクトリで決まる誤りは launcher の失敗として返ります。Gemini など他の provider では、`mail-only` と `tools` を拒否します。

未知の request field はモデル解決や起動の前に HTTP 400 で拒否します。任意の Gemini provider を導入した環境では `resources` も受け付けます（[Gemini の委任](delegation.md)を参照）。他の provider では拒否します。

起動失敗後は launcher と同じ共通 cleanup を呼びます。cleanup が確認に失敗した場合、応答は `ok: false`・`cleanup_state: "unknown"`・`cleanup_incomplete: true` となり、API は子の state・owner credential・handoff を消しません。cleanup が正常に完了した場合も、保持期間内の再開に必要な state と資格を API が追加で削除することはありません。

`"dry_run": true` を付けると request を検証し、provider・model・effort・directory を解決した起動プレビューを HTTP 200 で返します。

```json
{
  "ok":true,
  "dry_run":true,
  "provider":"claude",
  "model":"claude-opus-5-5",
  "effort":"",
  "dir":"/path/to/project",
  "standalone":true,
  "worktree":false,
  "argv":["/path/to/hooks/spawn_child.sh","--pre-registered","<child-name>","--child-token-file","<child-token-file>","--standalone","--model","claude-opus-5-5","dry","/path/to/project"],
  "launcher_env":{}
}
```

`argv` は実起動と同じ builder で組み立てます。`name` を省略した場合の `<child-name>` は実起動で選ぶ名前の仮置きです。指定名は検証してそのまま表示しますが、登録時にサーバーが正規化する場合があります。`<child-token-file>` と Gemini の `launcher_env` にある `<gemini-task-file>` は、実起動でだけ作るファイルの仮置きです。`launcher_env` は provider 固有の上書き値で、継承する環境全体は含みません。プレビューでは identity 登録・予約・Mail・tmux・agent process 起動を行わず、token・task・annotation・log・worktree のファイルも作りません。parent の認証情報も不要です。検証では read-only の Git 確認と、実際の CLI/model の組を解決するため既存の時間制限付き Codex `--version` probe を実行する場合があります。`async: true` を併用してもプレビューだけを返します。名前や resources は予約しないため、実起動までに使用状況が変わる場合があります。

Codex のモデル省略または `sol` で既定を変更した場合、preview・同期結果・非同期の pending と最終結果に `model_note` を含みます。変更前後の ID と理由を伝え、期限切れの cache の場合は取得日時と更新方法も示します。正式 ID を指定した要求にはこの置換通知を付けません。

成功:

```json
{
  "ok":true,
  "child_name":"Sunny-Curie",
  "tmux_session":"Sunny-Curie",
  "annot":"ok",
  "worktree":false,
  "standalone":true,
  "provider":"codex",
  "model":"gpt-5.6-sol",
  "effort":"high"
}
```

`standalone: true` では `parent` を空に固定し、`PARENT_AGENT` を subprocess environment から削除します。synthetic self-mail は作らず、task の先頭4000文字を launcher へ直接渡します。通常 child は parent を sender とする inbox message と CC audit trail を作り、登録 summary / launcher prompt は先頭80文字です。

Claude provider に `effort` を渡すと拒否します。Codex は正式な `gpt-*` IDをcacheにないという理由では拒否せず、明示 `AGENTSTACK_CODEX_MODELS` がある場合だけmembershipを制限します。UI はモデル別 effort 情報を候補表示と無指定時の既定に使い、明示 effort はそのまま Codex CLI へ渡して最終判定を任せます。無指定モデルと短縮名 `sol` は起動対象 CLI が 0.159.0 以上なら `gpt-6.1-sol`、古い版なら `gpt-6-sol`（版不明なら新鮮な catalog で判定）、`luna` は GPT-6 Luna です。Claude は local catalog の有無や期限とは独立して、正しい形式の正式IDを受け付けます。明示的な `AGENTSTACK_CLAUDE_MODELS` は厳格な許可リストとして扱い、不正な設定ではClaudeの起動を拒否します。Claude の固定既定は `claude-opus-5-5` で、許可リストから除外した状態でモデルを省略すると拒否します。別モデルへは置き換えません。詳細は [Claude model catalog](configuration.md#claude-model-catalog) を参照してください。

non-git directory では Codex が trust dialog を出すことがあります。spawner は `C-m` で受理し、3秒ごと・最大10回を超えて dialog が残る場合は fail-fast します。server は launcher readiness を最大120秒待ち、失敗時は tmux session と token / child credential file を cleanup します。

登録後の mail、token、launcher failure では identity registration 自体は削除されません。server は owner credential で削除する権限を持たないため、error response に:

```json
{
  "ok":false,
  "child_name":"Sunny-Curie",
  "registration_retained":true,
  "error":"... child registration 'Sunny-Curie' remains ..."
}
```

を含めます。同じ request を無条件 retry せず、残った identity を確認してください。

spawn は generated `env.sh` の `AGENTSTACK_MCP_URL`（未設定時は `http://127.0.0.1:18765/mcp` に fallback）を使います。既定の ORRERY Mail transport は bearer を使いません。launcher / hook と同じ値を共有するので、endpoint を変更する場合は `env.sh` 側を更新してください。

## 非同期 spawn と `GET /api/spawn-status`

`POST /api/spawn` に `"async": true` を付けると、child の登録と launcher の起動まで済ませた時点で応答を返します（`ok: true, pending: true`、`child_name` 等は同期時と同じ field）。REPL の readiness 判定と task 注入の確認（Codex の子では、起動引数で渡した最初の task がこの起動の記録に入ったかの確認。Codex の history binding が無い環境では確かめられず、最大90秒待ってから `ready` になります）は background で続き、結果は次で読みます。

```bash
curl -s 'http://127.0.0.1:8770/api/spawn-status?name=WindyFermi'
```

```json
{"ok":true,"name":"WindyFermi","state":"ready","age":9.4,"error":null,"detail":null,"result":{...}}
```

`state` は `launching / ready / failed`。`failed` のとき `error` と `detail`（launcher の末尾ログ）が入り、同期 spawn が返すものと同じ内容です。記録は 30 分保持し、未知の名前は 404 です。`name` を省略すると保持中の全件を返します。dashboard と [ORRERY cockpit](https://github.com/gyroid-eth/orrery) の NEW AGENT はこの経路を使い、modal を即閉じて結果を toast で出します。`async` を付けない呼び出しは従来どおり判定まで待ちます。

## 関連文書

- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [Dashboard](dashboard.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)

登録 pending の画面からの同 intent 再送は `POST /api/global/spawn` に status の `registration_sha256` を `expected_digest` として添えます（API 17）。同じ operation mutex の下で照合し、不一致は `REGISTRATION_PENDING_CONFLICT`、pending が既に無ければ `TARGET_UNAVAILABLE`。登録の認証・名前・program/model の既存照合は維持します。

### global runtime 読取（API 18）

`GET /api/runtime-observations?agent_ids=1,2` は既存の HTTP operator admission に通った要求だけを受け付けます。ID は正の63bit整数、重複なし、1〜200件。入力が不正なら `QUERY_INVALID` です。公開 Mail tool の追加はありません。応答 schema は `global-runtime-observation.json` の1定義です。

`items` は各 ID の `liveness`、nullable な `act_state` / `ctx_used`、`session_count`、`parent_agent_id` / `parent_state`、`activity_freshness` / `reason` を返します。token・pane capture・私有 path は返しません。未知を waiting や context 0 に置き換えません。[観測と能力の確認](dashboard.md#global-runtime-observation)を参照してください。

`global_runtime_observation=1` と `operator_session=1` を広告します。global の `/api/quotas` と `/portrait` も同じ admission を通り、既存の読取処理を使用します。画像は共有 session の `apiBlob` で取得して object URL とし、Lock で revoke します。


API 25: 旧 managed の `attempt_id` による起動・状態照会は廃止しました。`POST /api/spawn` と `GET /api/spawn-status?name=<name>` の標準の経路を使います。旧要求には `unsupported_attempt` を返します。

<a id="スマホ閲覧用api-28"></a>

<a id="スマホ閲覧用api-29"></a>

## スマホ閲覧用（API 30）

`GET /api/mobile-mail?window=<period>`はlegacy modeのloopback専用metadata endpointです。期間の既定は`day`（24h）。`all`、`day`、`15m`と、15〜43200の15の倍数を表す`m<N>`（分）だけを受け付け、先頭ゼロや小数は拒否します。既存のHost admissionを維持します。DB不在・project/schema/読取失敗は503、global modeは明示拒否（admission通過後501、または既存global admissionの拒否）です。globalから旧DBへfallbackしません。不明・重複queryは400です。

200応答は`status: available`、`window`、`observed_at`、`edges`、`events`、`events_truncated`、`edges_truncated`、`invalid_timestamps`、`degraded`を返します。`edges`は実際の期間で集計したsource/target/kind/count/last_tsで最大10,000行。件数は方向別のto/cc宛先行数で、bcc・別projectの端点を除きます。`events`は最新200宛先行までで、安定した不透明`key`とsource/target/kind/count1/event_atを持ち、時刻・message id・recipient idの降順です。SQLはmetadataだけを読み、件名・本文・read/ack更新を含みません。不正時刻は除外してdegradedとして報告します。正常な空結果と取得不能は別です。

任意の[mobile relay](mobile.md)は認証付きGET/HEADだけを公開します。経路は`/`、固定の同梱asset、`GET /view/snapshot?window=<period>`と後述の肖像です。起動には`--enable`・正確な本人login・HTTPS .ts.net originが必要で、待受は127.0.0.1だけです。全asset/snapshot/肖像で外側Host・本人header・指定されたOriginを照合します。通常GETのOrigin欠損は許容します。他method、不明route/query、認証header重複、別Origin、upgrade、overrideは拒否。任意upstream URL・header転送・proxy環境・redirectは使わず、固定GETごとにJSON4MiB/2秒を上限とします。

snapshotは`schema:1`・観測時刻・window・`period`（秒幅、活動時刻不明件数）、componentごとの`sources`（available/stale/unavailableと最終観測/age）、明示投影した`agents`、graphのMail/親子線、Mail/観測Statusのtimeline、切詰めflag、Unavailableのquotaを返します。agentは正規名・provider/model/locality・nullable running/context・retired/state/act_state・最終活動だけです。graph/agentsのunionを、両sourceの有効な最終活動とMail時刻の最大値で現在からの期間へ絞ります。未来/不明時刻を現在に置き換えず、有限期間では不明を除外し件数を示します。最近活動した退役済みは含み、runningだけで期間外を含めません。親子/Mail線は期間内端点だけです。Statusは`observed_at`とnullの`event_at`を持ち、relay開始後の最新200変化のうち期間・agent集合に該当するものを返します（超過は`states_truncated`）。文脈sampling時刻はnull。失敗時は古い値をstaleとして保持し、未取得を正常0に変えません。metadataのperiod cacheは最大4期間でpart/publishedを同時に破棄します。ブラウザは15m〜30dの対数sliderを15分単位に丸め、200ms debounceし、全履歴は明示操作だけです。PCの期間契約は変更しません。

`GET /view/portrait?name=<NAME>&window=<period>`は、同期間の公開済rosterに含まれる正規名だけを受け付けます。固定loopbackの`GET /api/mobile-portrait?name=<NAME>`で既存のcase-insensitive custom map/overlay解決を試し、無効/欠落なら最長一致する同梱科学者suffixへfallbackします。私有pathやmapは返しません。PNGは512KiB・幅高1〜1024・signature/IHDRを検査し、2秒を上限にします。任意SVG/HTMLは返さず、取得/decode失敗はイニシャルへ。画像もno-store。loopback肖像APIもlegacy限定で、不正名/queryは400、画像なし/無効は404、globalは明示拒否です。

API30ではmetadataに不透明な`scope`と`next_cursor`を追加します。`GET /view/mail?window=<period>&cursor=<token>`は200宛先行の次ページで、内容を含みません。Graphのpair一覧は`a=<NAME>&b=<NAME>&direction=both|forward|reverse`を同経路へ追加し、最初だけcursorを省略できます。2名は同期間の公開済みrosterに必要です。方向/pairはSQLのLIMIT前に絞ります。keysetは時刻/message id/recipient idの降順。cursorはscope/period/pair/direction/boundaryと10分期限へ結び、最大256、relayの発行済eventは最大2000です。不正・重複queryは400、未知/失効event・cursorは410、取得不能は503です。ブラウザは現在200行・最大20位置を保持し、定期pollで古いページを上書きしません。

`GET /view/message?window=<period>&event=<key>`だけが、認証後の1件タップに対応する件名/本文を返します。relayの発行済metadataと、現在project・to/cc宛先・期間・eventを照合します。新しいloopback限定`/api/mobile-message`は同queryに`mode=content|revision`を受け、改訂確認でもcanonical照合を先に行います。512文字のsubject、64KiB UTF-8のbodyと切詰めflag、opaque fingerprint/revisionを内部で使い、スマホへはevent/subject/body/切詰めflagだけを返します。revisionは公開する範囲の文字列と元byte長に対するdigestで、切詰め範囲外の同じ長さの変更を識別する保証はありません。本文はliteral textのみ。read/ACK・添付・返信・操作はありません。no-store、本文のログ/永続保存なし。close/別tap/期間/画面/背景/pagehideでDOMとrequestを消し、世代番号で遅延応答を捨てます。

metadata pageのloopback経路は`/api/mobile-mail-page`、固定24hの内部cache batchは`/api/mobile-cache-page?cursor=<token>`（初回cursorなし）です。後者は外部relayのroute mapには存在しません。認証済みsnapshot取得後にだけ単一workerを開始し、最大32通ずつ、実JSON応答4MiB未満、120秒のsweep予算、25ms yield、完了後60秒以上の再試行間隔で読みます。最大63非空page・2000 distinct message・32MiBのJSON論理byte量に制限し、全24hの収録が終わらない場合はpartial/unavailableです。JSON escapeで大きくなるbatchは縮小します。`mail_cache`には進捗status/messages/logical_bytes/上限/reasonだけを返し、cache辞書や本文は含めません。

本文と対応metadataは一緒に24h期限/LRU/byte予算で破棄します。scope変更で全失効、tapのcache hitも現在のcanonical revisionを再照合し、失敗時は古い本文を返しません。24hはcache範囲だけで、30d/全履歴の公開済eventも単件取得でき、古い本文はcache外です。reader停止でworkerをstop/joinして全内容を消します。新loopback APIもglobalでは501で、旧DBにfallbackしません。ターミナル、成果物、quota refreshは読みません。本人認証と指定端末の制限は別で、配備時にはguideの実端末確認が必要です。
