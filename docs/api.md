# API reference

> English version: planned.

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
| GET | `/api/graph` | `days`, `all` | `{nodes, edges, spawn, ts}` |
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
{"name":"claude-agent-stack","version":"0.9.0","api":1}
```

version の解決順は [インストール](install.md#version)を参照してください。

## GET `/api/spawn-names`

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
    "claude-opus-5",
    "claude-haiku-4-5-20251001"
  ],
  "default_model":"claude-sonnet-5",
  "providers":[
    {
      "id":"claude",
      "label":"Claude",
      "program":"claude-code",
      "models":["claude-sonnet-5"],
      "default_model":"claude-sonnet-5",
      "efforts":null
    },
    {
      "id":"codex",
      "label":"Codex",
      "program":"codex-cli",
      "models":["gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna"],
      "default_model":"gpt-5.6-sol",
      "efforts":["low","medium","high","xhigh"],
      "effort_default":"xhigh"
    }
  ]
}
```

scientist rail の `status` は、その scientist と134語の adjective の組み合わせに少なくとも1件の空きがあるかを表します。全組み合わせが埋まると `occupied`、DB がない、または query に失敗すると `unknown` です。bare surname の登録有無だけでは決めません。

adjective は agent-mail の正典 `SIMPLE_ADJECTIVES` Round 3 と逐語同期し、launcher・catalog・suggestion API が同じ source を使います。独自追加は strict deployment の name validation と乖離するため禁止です。

`AGENTSTACK_CODEX_MODELS` があれば Codex model list を上書きします。未設定時は `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna` の順で、default は `gpt-5.6-sol`、effort default は `xhigh` です。

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

を返します。hidden directory と root 外へ出る symlink は除外し、名前順の先頭20件だけを返します。21件以上なら `truncated: true` です。

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
      "last_active":1785480000
    }
  ]
}
```

実際の row には pane title、state、elapsed、context、attach、latest message などの表示用 field も含まれます。frontend は未知 field を無視します。

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
  "nodes":[{"id":"WindyFermi","name":"WindyFermi"}],
  "edges":[{"source":"Parent","target":"WindyFermi","count":3}],
  "spawn":[{"parent":"Parent","child":"WindyFermi"}],
  "ts":1785480000
}
```

data source が読めない場合も HTTP 200 で空の `nodes / edges / spawn` と `error` を返し、DECK 全体を巻き込まないようにします。

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

response は `{ok, session, actions}` です。既存 tmux session は configured terminal で open / focus し、session がなければ保存 transcript の resume を試みます。

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
| `task` | yes | task 本文。UI は最大4000文字 |
| `name` | no | 指定時は hyphen を除去し、`available` 必須 |
| `dir` | no | 存在する working directory。既定は source repo |
| `provider` | no | `claude`（既定）または `codex` |
| `model` | no | provider catalog 内。provider の default あり |
| `effort` | Codex のみ | `low / medium / high / xhigh`。既定 `xhigh` |
| `role` | no | 最大40文字 |
| `group` | no | 最大24文字 |
| `worktree` | no | isolated worktree |
| `worktree_base` | no | base revision。既定 `HEAD` |

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

Claude provider に `effort` を渡すと拒否します。Codex model は `AGENTSTACK_CODEX_MODELS` allow-list、Claude model は server の `_SPAWN_MODELS` に従います。Codex の既定は `gpt-5.6-sol` / `xhigh` です。

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

spawn は fixed `http://127.0.0.1:8765/mcp` を使い、`AGENTSTACK_MAIL_ENV` の `HTTP_BEARER_TOKEN` を読みます。launcher / hook の `AGENTSTACK_MCP_URL` とは別なので、agent-mail endpoint を変更する場合は注意してください。

## 関連文書

- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [Dashboard](dashboard.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
