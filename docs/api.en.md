# API reference

> 日本語版: [api.md](api.md)

[Previous: Dashboard](dashboard.en.md) · [Back to README](../README.en.md) · [Next: Configuration](configuration.en.md)

## Basics

Base URL:

```text
http://127.0.0.1:8770
```

Response bodies are JSON except for images and HTML. The dashboard itself has no login layer. When using `AGENTSTACK_BIND_HOST=0.0.0.0`, limit access to a trusted LAN / VPN.

Every POST endpoint requires `Content-Type: application/json` and a JSON object body. Browser requests receive HTTP 403 unless `Origin` / `Sec-Fetch-Site` are same-origin; CLI requests may omit both headers. This is the common guard that rejects simple-form CSRF.

Failures generally return:

```json
{"ok":false,"error":"reason"}
```

with HTTP 400. A media-type mismatch is HTTP 415, a cross-origin POST is HTTP 403, an unknown route is HTTP 404, and an unreadable spawn-catalog source is HTTP 503.

## Route list

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/` | optional `?embed=1` | dashboard HTML |
| GET | `/api/version` | none | `{name, version, api}` |
| GET | `/api/spawn-names` | none | name / dir / provider catalog |
| GET | `/api/name-status` | `name` | exact identity status |
| GET | `/api/suggest-name` | `scientist` | verified `Adjective-Scientist` (requested form; read-back from the registration response is authoritative for the actual registered name) |
| GET | `/api/fs/dirs` | optional `path` | root-scoped child directories |
| GET | `/api/agents` | none | `{ts, agents}` |
| GET | `/api/graph` | `days`, `all` | `{nodes, edges, spawn, timestamp_diagnostics, degraded, ts}` |
| GET | `/api/history` | `session`, `limit` | transcript events |
| GET | `/api/agent-history` | `name` or `names`, `hours`, `include_pane_states` | agent event timeline |
| GET | `/api/edge-messages` | `a`, `b`, `limit` | messages between two agents |
| GET | `/api/messages-since` | `since`, `limit` | live mail events |
| GET | `/api/annotations` | none | role / group map |
| GET | `/api/deliverables` | `agent` | vault deliverables |
| GET | `/api/custom-portraits` | none | custom portrait map |
| GET | `/api/term` | `session`, `lines` | tmux capture |
| GET | `/api/ptty` | `session` | browser terminal URL |
| GET | `/api/mail-watcher-health` | none | watcher / signal health |
| GET | `/portrait` | `name`, `hi` | PNG or fallback SVG |
| GET | `/assets/<file>` | `.svg` / `.png` basename | static asset |
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

See [Installation](install.en.md#version) for version resolution order.

## GET `/api/spawn-names`

There is no query.

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
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1"
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
      "models":["gpt-5.6-sol","gpt-6-astra","gpt-5.6-terra","gpt-5.6-luna"],
      "default_model":"gpt-5.6-sol",
      "efforts":["low","medium","high","xhigh","max","ultra"],
      "effort_default":"xhigh"
    }
  ]
}
```

The scientist rail's `status` indicates whether at least one pairing of that scientist with the 134 adjectives is available. It becomes `occupied` when every combination is occupied and `unknown` when the database is missing or the query fails. It is not based only on whether the bare surname is registered.

The adjectives are synchronized word-for-word with agent-mail's canonical `SIMPLE_ADJECTIVES` Round 3 list, and the launcher, catalog, and suggestion API use the same source. Custom additions are prohibited because they diverge from name validation in strict deployments.

When present, `AGENTSTACK_CODEX_MODELS` overrides the Codex model list. Otherwise the order is `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`; the default is `gpt-5.6-sol`, and the default effort is `xhigh`.

## GET `/api/name-status`

Performs an exact check of a complete identity name.

```bash
curl -s 'http://127.0.0.1:8770/api/name-status?name=WindyFermi'
```

```json
{"name":"WindyFermi","status":"available"}
```

`status` is `available / occupied / unknown`. A missing database, query error, or empty name fails closed as `unknown`. This endpoint does not itself validate name syntax; `POST /api/spawn` validates it separately. HTTP status is 200 for every result.

## GET `/api/suggest-name`

The server attaches a confirmed-available adjective to the suffix selected on the scientist rail.

```bash
curl -s 'http://127.0.0.1:8770/api/suggest-name?scientist=Fermi'
```

```json
{"name":"WindyFermi"}
```

The server checks up to 20 randomly ordered candidates from the canonical adjectives and returns only the first name confirmed as `available`. When the scientist is outside the roster or all checked candidates are occupied / unknown, it returns:

```json
{"error":"no available name found"}
```

with HTTP 409. The UI's SHUFFLE does not construct a name locally; it revalidates through this endpoint every time.

## GET `/api/fs/dirs`

Used by NEW AGENT directory typeahead.

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

When `path` is omitted, the first `AGENTSTACK_SPAWN_ROOTS` entry is used. For a path outside allowed roots, containing `..`, or naming a nonexistent directory, it returns:

```json
{"path":null,"dirs":[]}
```

Hidden directories and symlinks that leave the root are excluded. Up to 500 entries are returned in name order because the page filters the complete list by the typed prefix. With 501 or more entries, `truncated` is `true`.

## GET `/api/agents`

There is no query. DECK fetches it every few seconds.

Response:

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

Actual rows also include display fields such as pane title, state, elapsed, context, attach, and latest message. The frontend ignores unknown fields.

## GET `/api/graph`

Query:

| Field | Default | Meaning |
| --- | --- | --- |
| `days` | `4` | Period for mail / spawn history |
| `all` | `0` | Include every agent with `1` / `true` |

```bash
curl -s 'http://127.0.0.1:8770/api/graph?days=4&all=0'
```

Response:

```json
{
  "nodes":[{"id":"WindyFermi","name":"WindyFermi"}],
  "edges":[{"source":"Parent","target":"WindyFermi","count":3}],
  "spawn":[{"parent":"Parent","child":"WindyFermi"}],
  "timestamp_diagnostics":{"invalid_count":0,"fields":{}},
  "degraded":false,
  "ts":1785480000
}
```

agent-mail timestamps are normalized to epoch seconds before comparison, whether they are legacy ISO 8601 text or integer microseconds from the Rust implementation. If an unparseable value other than NULL or empty text is present, the response reports `timestamp_diagnostics.invalid_count` and affected `fields` and sets `degraded` to `true`. An unparseable value is never treated as epoch 0.

When the data source cannot be read, it still returns HTTP 200 with empty `nodes / edges / spawn`, an `error`, and `degraded: true`, so that the failure does not take down the entire DECK.

## GET `/api/history`

Query:

- `session`: tmux / agent name
- `limit`: maximum number of events; default `220`

```bash
curl -s 'http://127.0.0.1:8770/api/history?session=WindyFermi&limit=220'
```

The response includes `ok`, the source file, `shown` / `total`, and normalized `events`. Each event has `role`, `kind`, `text`, and `ts`, hiding Claude / Codex transcript differences from the frontend.

## GET `/api/agent-history`

Single agent:

```text
/api/agent-history?name=WindyFermi&hours=24
```

Union for REPLAY:

```text
/api/agent-history?names=Parent,WindyFermi&include_pane_states=1
```

| Field | Meaning |
| --- | --- |
| `name` | Single agent |
| `names` | Multiple comma-separated agents. Takes precedence over `name` when specified |
| `hours` | Lookback. Auto-fits to the event range when omitted |
| `include_pane_states` | Include ask / pane-state events for `1 / true / yes / on` |

The response is `{ok, events, ...}`. Each event includes the timestamp, kind, agent, counterpart, state, and message metadata needed by replay, when applicable.

## GET `/api/edge-messages`

```bash
curl -s 'http://127.0.0.1:8770/api/edge-messages?a=Parent&b=WindyFermi&limit=60'
```

- `a`, `b`: agent names
- `limit`: default `60`

The response is `{ok, messages}`. Each message includes direction, subject, body, importance, and timestamp and is used by the NETWORK edge drawer.

## GET `/api/messages-since`

```bash
curl -s 'http://127.0.0.1:8770/api/messages-since?since=1785480000&limit=80'
```

- `since`: Unix epoch, default `0`
- `limit`: default `80`

The response contains the message list and watermark for live comet. Missing configuration is represented by an empty list and diagnostics so HTTP polling can continue.

## GET `/api/annotations`

```json
{
  "ok":true,
  "annotations":{
    "WindyFermi":{"role":"docs","emoji":"","group":"release"}
  }
}
```

Annotations are read and written through `GET /api/annotations` and `POST /api/annotate`.

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

At most 25 items are returned. If `AGENTSTACK_DELIVERABLE_ROOTS` is set, its `:`-separated roots are scanned recursively. Otherwise the base is selected in the order project, vault, cwd / Git root, and its `logs/` is used. Only items whose `LOG_*.md` frontmatter `agent:` matches the query agent are returned.

An item's `vault` is the vault name only when the file is inside `AGENTSTACK_VAULT`. In that case `rel` is a vault-relative path and the UI can create an Obsidian link. Outside the vault, `vault` is empty, `rel` is relative to the scan root, and the UI displays a non-link item. Top-level `vault` is a compatibility field returning the configured vault name.

## GET `/api/custom-portraits`

There is no query.

```json
{"mybot":"mybot","windyfermi":"Fermi"}
```

If the configuration file is unspecified or unreadable, an empty mapping is returned.

## GET `/api/term`

```bash
curl -s 'http://127.0.0.1:8770/api/term?session=WindyFermi&lines=500'
```

- `session`: required
- `lines`: capture line count; default `500`

The response is `{ok, session, text, ...}`. The session name is validated before running `tmux capture-pane`.

## GET `/api/ptty`

```bash
curl -s 'http://127.0.0.1:8770/api/ptty?session=WindyFermi'
```

Response:

```json
{"ok":true,"session":"WindyFermi","url":"http://127.0.0.1:PORT/"}
```

An existing `ttyd` is reused; otherwise one is started on a free port. An invalid session or missing dependency is HTTP 400.

## GET `/api/mail-watcher-health`

There is no query.

The response includes at least these diagnostics:

```json
{
  "status":"ok",
  "watcher_running":true,
  "signal_count":0,
  "last_success_age_s":4,
  "recent_results":{"delivered":3}
}
```

The header indicator uses the watcher process, signal backlog, most recent success time, and delivery results from the last ten minutes.

## GET `/portrait`

```text
/portrait?name=Curie&hi=1
```

- `name`: portrait key
- `hi`: prefer high resolution with `1 / true`

PNG files are searched in the order overlay, high-resolution bundle, then 64px bundle. A safe unregistered name gets a fallback SVG; an invalid path gets 404.

## GET `/assets/<file>`

Only basenames immediately under `dashboard/assets` are served. Allowed extensions are `.svg` and `.png`. `/`, `..`, and other extensions return 404.

## POST `/api/jump`

Request:

```json
{"session":"WindyFermi"}
```

The response is `{ok, session, actions}`. An existing tmux session is opened / focused in the configured terminal; when no session exists, the server attempts to resume a saved transcript.

## POST `/api/exit`

Request:

```json
{"session":"WindyFermi"}
```

Response:

```json
{"ok":true,"session":"WindyFermi","actions":["exit-sent"]}
```

Only a real tmux session in the `running / finished` category is eligible. `warm-*` / `pending-*` are rejected. An attached session is not interrupted and gets `warn-attached` added to `actions`.

## POST `/api/kill`

Request:

```json
{"session":"WindyFermi","mode":"both"}
```

Expected `mode` values are `tmux`, `retire`, and `both`. The response is `{ok, session, mode, actions}`. The server rechecks category and session existence before tmux kill / soft retirement.

## POST `/api/annotate`

Request:

```json
{"name":"WindyFermi","role":"docs","emoji":"","group":"release"}
```

`session` can be used instead of `name`.

Response:

```json
{
  "ok":true,
  "annot":{"name":"WindyFermi","role":"docs","emoji":"","group":"release"}
}
```

An annotation is deleted when `role` and `emoji` are empty and no group remains. Spawn does not save `emoji`; it uses only role / group.

## POST `/api/spawn`

Claude child with a parent:

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

Request:

| Field | Required | Details |
| --- | --- | --- |
| `parent` | child only | Valid existing agent name. Omit for `standalone: true` |
| `standalone` | no | Boolean. Start without a parent when `true` |
| `task` | yes | Task body. The UI accepts up to 4,000 characters |
| `name` | no | Remove hyphens when specified; must be `available` |
| `dir` | no | Existing working directory. Defaults to the source repository |
| `provider` | no | `claude` (default) or `codex` |
| `model` | no | From the provider catalog. Each provider has a default |
| `effort` | Codex only | `low / medium / high / xhigh / max / ultra` (max / ultra: gpt-6-astra only); default `xhigh` |
| `role` | no | At most 40 characters |
| `group` | no | At most 24 characters |
| `worktree` | no | Isolated worktree |
| `worktree_base` | no | Base revision; default `HEAD` |

Success:

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

With `standalone: true`, `parent` is fixed as empty and `PARENT_AGENT` is removed from subprocess environment. No synthetic self-mail is created; the first 4,000 task characters are passed directly to the launcher. A normal child creates an inbox message with the parent as sender plus a CC audit trail; the registration summary / launcher prompt uses the first 80 characters.

Passing `effort` for the Claude provider is rejected. Codex models follow the `AGENTSTACK_CODEX_MODELS` allowlist; Claude models follow the server's `_SPAWN_MODELS`. Codex defaults are `gpt-5.6-sol` / `xhigh`.

Codex may show a trust dialog in a non-Git directory. The spawner accepts it with `C-m`; if the dialog remains after checks every three seconds, up to ten times, it fails fast. The server waits up to 120 seconds for launcher readiness and cleans up the tmux session and token / child credential files on failure.

Identity registration itself is not deleted after mail, token, or launcher failure. The server lacks authority to delete it with the owner credential, so the error response includes:

```json
{
  "ok":false,
  "child_name":"Sunny-Curie",
  "registration_retained":true,
  "error":"... child registration 'Sunny-Curie' remains ..."
}
```

Do not retry the same request unconditionally; inspect the retained identity.

Spawn uses `AGENTSTACK_MCP_URL` from generated `env.sh`, falling back to `http://127.0.0.1:18765/mcp` when unset. The default ORRERY Mail transport uses no bearer. Because it shares this value with launchers and hooks, update `env.sh` when changing the endpoint.

## Asynchronous spawn and `GET /api/spawn-status`

Adding `"async": true` to `POST /api/spawn` returns after child registration and launcher startup (`ok: true, pending: true`; fields such as `child_name` match the synchronous response). REPL readiness and task-injection confirmation continue in the background, and the result is read with:

```bash
curl -s 'http://127.0.0.1:8770/api/spawn-status?name=WindyFermi'
```

```json
{"ok":true,"name":"WindyFermi","state":"ready","age":9.4,"error":null,"detail":null,"result":{...}}
```

`state` is `launching / ready / failed`. A `failed` record includes `error` and `detail`, the end of the launcher log, matching synchronous spawn. Records remain for 30 minutes, and an unknown name returns 404. Omitting `name` returns every retained record. NEW AGENT in both the dashboard and ORRERY cockpit uses this path, closing the modal immediately and showing the result in a toast. Calls without `async` continue to wait for the decision as before.

## Related documentation

- [Hooks and operational helpers](hooks.en.md)
- [Codex App integration](codex-app.en.md)
- [Dashboard](dashboard.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)
