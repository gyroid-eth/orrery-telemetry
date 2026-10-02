# API reference

> English version: [api.en.md](api.en.md)

[前: Dashboard](dashboard.md) · [README に戻る](../README.md) · [次: 設定](configuration.md)

## 基本

base URL:

```text
http://127.0.0.1:8770
```

response body は、画像と HTML を除き JSON です。dashboard 自体に login layer はありません。`AGENTSTACK_BIND_HOST=0.0.0.0` を使うときは trusted LAN / VPN に限定してください。

すべての POST endpoint は `Content-Type: application/json` と JSON object body が必須です。browser request は `Origin` / `Sec-Fetch-Site` が same-origin でなければ HTTP 403、CLI は両 header を送らない場合に利用できます。simple-form CSRF を受け付けないための共通 guard です。

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
| POST | `/api/jump` | `{session}` | open / focus / resume action |
| POST | `/api/exit` | `{session}` | graceful exit action |
| POST | `/api/kill` | `{session, mode}` | kill / retire action |
| POST | `/api/annotate` | `{name, role, emoji, group}` | saved annotation |
| POST | `/api/spawn` | spawn payload | child launch result |

## GET `/api/version`

```bash
curl -s http://127.0.0.1:8770/api/version
```

```json
{"name":"orrery-telemetry","version":"2026.09.16.1","api":7}
```

version の解決順は [インストール](install.md#version)を参照してください。

- `version`: いつの配布物か（日付。[CHANGELOG](../CHANGELOG.md)）。互換性の約束ではありません
- `api`: この API を使う側（[ORRERY cockpit](https://github.com/gyroid-eth/orrery) など）との**互換の世代**です。利用者が頼る endpoint や field を**足す・意味を変える**ときだけ 1 つ上げ、CHANGELOG に書きます。release のたびには上げません。利用者は、必要な機能があるかを `version` の日付ではなく `api` で判定してください（cockpit は、`api` が必要な世代に足りないと起動時に警告します）。この運用は `api` 2 からで、それより前の版はどれも、中身にかかわらず 1 を返します

## GET `/api/spawn-names`

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
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1"
  ],
  "default_model":"claude-opus-5-5",
  "providers":[
    {
      "id":"claude",
      "label":"Claude",
      "program":"claude-code",
      "models":["claude-sonnet-5","claude-opus-5-5","claude-opus-5","claude-haiku-4-5-20251001","claude-fable-5-1"],
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
      "efforts":["low","medium","high","xhigh","max","ultra"],
      "effort_default":"xhigh"
    }
  ]
}
```

scientist rail の `status` は、その scientist と134語の adjective の組み合わせに少なくとも1件の空きがあるかを表します。全組み合わせが埋まると `occupied`、DB がない、または query に失敗すると `unknown` です。bare surname の登録有無だけでは決めません。

adjective は ORRERY Mail の正典 `SIMPLE_ADJECTIVES` Round 3 と逐語同期し、launcher・catalog・suggestion API が同じ source を使います。独自追加は strict deployment の name validation と乖離するため禁止です。

Codex は同梱候補に期限内のCLIローカルcacheの候補を追加します。`AGENTSTACK_CODEX_MODELS` 明示時はその許可リストだけを使い、不正設定は `model_error` に返します。`model_source` は `bundled` / `local_cache` / `override` です。既定モデルは、起動対象 CLI が 0.159.0 以上なら `gpt-6.1-sol`、古い版なら `gpt-6-sol` です。版不明なら新鮮な catalog で判定します。候補順では決まりません。GPT-6.1 Sol には Codex CLI 0.159.0 以上が必要で、doctor は fallback と更新方法を note に出します。以前の世代を使う場合は `gpt-6-sol` のように正式 ID を指定してください。providerの `model_efforts` と `model_effort_defaults` はモデルIDをkeyとする辞書で、UIはこちらを優先します。空のeffort一覧はCLI既定を使うため、effortを省略してください。詳細は [設定](configuration.md#codex-model-catalog) を参照してください。

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
  "status":"ok",
  "watcher_running":true,
  "signal_count":0,
  "last_success_age_s":4,
  "recent_results":{"delivered":3}
}
```

watcher process、signal backlog、直近成功時刻、直近10分の配送結果を header indicator が使います。

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

request:

```json
{"session":"WindyFermi"}
```

`open` は任意の boolean です。`false` は OS の窓を開かず detached tmux で resume、`true` は窓を開きます。省略時、Claude / Codex resume は `AGENTSTACK_AUTO_OPEN_CHILD` に従います（`0` は detached、未設定 / `1` は窓を開く）。明示した `open` が設定より優先します。稼働中の session に `open: false` を渡すと attach せず、そのままにします。環境設定は resume 時の自動表示だけに適用され、手動の Open tmux は使えます。detached resume には terminal adapter は不要です。この field は API 世代3から使えます。

response は `{ok, session, actions}` です。既存 tmux session は configured terminal で open / focus します。session が無い場合と finished husk を transcript から復元する場合は、GET row と同じ判定を server が再検査します。表示が `verification_required` だった Claude row も、この1回の request 内で full 検証して `ready` になれば、そのまま resume します。Codex child は fresh home を生成し、credential 付き再登録と fresh binding expectation を作成した後、Codex exec の直前に unretire します。拒否時は HTTP 400 で `{"ok":false,"error":"...","resume_capability":"provenance_missing"}` のように確定した固定 reason code を返し、husk の kill や terminal 起動は行いません。bootstrap / unretire が失敗した場合も Codex は起動せず、生成した home / config だけを片付けます。

Claude は保存済み owner credential で同じ project・数値 ID・name・program の登録を検証し、credential 付き再登録と unretire が成功してから端末を起動します。Claude child の state / credential も既定30日保持し、resume 時は子専用 Mail proxy config を再生成します。top-level Claude は既存 owner token を使います。旧 cleanup で credential が消えた場合は `credential_missing`、期限切れは `retention_expired`、明示 purge 後は `purged` で起動を拒否します。認証・unretire の失敗でも端末を開きません。credential を自動発行したり別名で登録したりはしません。Codex top-level resume も unretire の対象です。

Claude の起動準備が失敗した場合、元の husk を残し、元が retired だった Mail を再 retire します。元から active の identity は retire しません。復元にも失敗した場合は `rollback_errors` を返し、成功したようには報告しません。CLI は tmux と窓の準備が成功するまで待機します。

旧3項目形式の Claude child は、private token と既存の正式 owner 登録が検証できれば `ready` になります。表示 GET は読み取りだけです。実際の認証・同じ identity の新形式への移行は明示 resume 時に行い、認証失敗では起動しません（[移行条件・保持期限・失敗時の復元](launchers.md)）。

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
    "model":"claude-sonnet-5",
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
| `provider` | no | `claude`（既定）または `codex` |
| `model` | no | provider catalog 内。provider の default あり |
| `effort` | Codex only | モデル別。`model_efforts` / `model_effort_defaults` を参照。対応情報がなければ省略してCLI既定を使用 |
| `role` | no | 最大40文字 |
| `group` | no | 最大24文字 |
| `worktree` | no | isolated worktree |
| `worktree_base` | no | base revision。既定 `HEAD` |
| `claude_chrome` | Claude only | boolean。`true` で child に `--chrome` を付ける。省略・`false` は inherit（利用者の Claude 設定に従う）。launcher の env 既定は使わない。[Claude in Chrome](delegation.md#claude-child-とブラウザ操作claude-in-chrome) |
| `claude_chrome_device` | Claude only | 使うブラウザの deviceId（`[A-Za-z0-9._:-]{1,128}`）。指定すると `claude_chrome: true` と同じ。`claude_chrome: false` との併用は拒否 |
| `base` | Claude / Codex | `"default"`（省略と同じ。従来の起動）または `"mail-only"`（ORRERY Mail と `tools` で選んだものだけ）。api 7 から |
| `tools` | Claude / Codex | object。`browser`（`true` または `{"device": "<deviceId>"}`。Claude only）、`screen`（`"read"` / `"operate"` または `{"access": ..., "server": "<name>"}`。server を省くと Mac の computer use で Claude only）、`mcp`（server 名の配列。tool は承認しない）、`approve_all`（`mcp` のうち全 tool を承認する server。表に無い版は拒否）。`claude_chrome_device` と deviceId が食い違えば拒否。api 7 から。[子に渡す道具を選ぶ](delegation.md#子に渡す道具を選ぶ--base----tools) |

`base` か `tools` で選択した child は、選択どおりにできなければ（Mail の proxy が無い、server を写せない、computer use が有効な project での `mail-only` など）起動しません。形の誤りはここで 400、child の起動ディレクトリで決まる誤りは launcher の失敗として返ります。Gemini など他の provider では、`mail-only` と `tools` を拒否します。

未知の request field はモデル解決や起動の前に HTTP 400 で拒否します。任意の Gemini provider を導入した環境では `resources` も受け付けます（[Gemini の委任](delegation.md)を参照）。他の provider では拒否します。

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
