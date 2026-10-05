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
{"name":"orrery-telemetry","version":"2026.09.16.1","api":9}
```

See [Installation](install.en.md#version) for version resolution order.

- `version`: when the release was made (a date; see the [CHANGELOG](../CHANGELOG.md)). It is not a compatibility promise
- `api`: the **compatibility generation** for the programs that use this API ([ORRERY cockpit](https://github.com/gyroid-eth/orrery) and others). It goes up by one only when an endpoint or field those programs rely on is **added or changes meaning**, and the CHANGELOG says so; it does not go up with every release. A program should decide whether a feature it needs is there from `api`, not from the date in `version` (the cockpit warns at startup when `api` is lower than it needs). This scheme starts at `api` 2; every earlier release reports 1, whatever it can do

## GET `/api/spawn-names`

The Claude provider exposes `model_source` (`override`, `local_cache`, or `bundled`) and `model_error` for explicit-setting diagnostics. Invalid overrides return an empty Claude `models` list without disabling other providers. Top-level `models` / `default_model` mirror the same Claude candidates. See [Claude model catalog](configuration.en.md#claude-model-catalog) for discovery and authorization boundaries.

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
      "efforts":["low","medium","high","xhigh","max","ultra"],
      "effort_default":"xhigh"
    }
  ]
}
```

The scientist rail's `status` indicates whether at least one pairing of that scientist with the 134 adjectives is available. It becomes `occupied` when every combination is occupied and `unknown` when the database is missing or the query fails. It is not based only on whether the bare surname is registered.

The adjectives are synchronized word-for-word with ORRERY Mail's canonical `SIMPLE_ADJECTIVES` Round 3 list, and the launcher, catalog, and suggestion API use the same source. Custom additions are prohibited because they diverge from name validation in strict deployments.

Codex augments bundled candidates with fresh local CLI cache entries. Explicit `AGENTSTACK_CODEX_MODELS` restricts candidates to that allowlist; invalid configuration appears in `model_error`. `model_source` is `bundled`, `local_cache`, or `override`. The default is `gpt-6.1-sol` when the selected CLI is 0.159.0 or later (fresh catalog evidence is used if its version is unknown); otherwise it is `gpt-6-sol`. Candidate order never chooses the default. Codex CLI 0.159.0 or later is required for GPT-6.1 Sol; doctor reports fallback and update guidance. Use a formal ID such as `gpt-6-sol` to pin the previous generation. Provider dictionaries `model_efforts` and `model_effort_defaults`, keyed by model ID, take precedence in the UI. An empty effort list means omit effort and use the CLI default. See [configuration](configuration.en.md#codex-model-catalog).

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
      "resume_capability":"not_required",
      "last_active":1785480000
    }
  ]
}
```

Actual rows also include display fields such as pane title, state, elapsed, context, attach, and latest message. The frontend ignores unknown fields.

Each row's `resume_capability` is a fixed reason code decided by the backend. A pre-verified row is `ready`. When a finished Claude row has neither an exact index nor a confirmed cache entry matching the transcript-directory mtime, display APIs return `verification_required` without reading every transcript. This code is excluded from NETWORK bulk resume, but one `/api/jump` call from the DECK action performs the full check and continues through resume in the same request if it becomes `ready`. The confirmed result is reused until the directory mtime changes. A live row that needs no resume is `not_required`; an unconfirmed provider is `unsupported_provider`; failures to verify history, cwd, CLI, formal registration, Codex launch provenance, credential, identity, or configuration are `no_history`, `cwd_missing`, `cli_missing`, `registration_missing`, `provenance_missing`, `credential_missing` / `credential_permission`, `identity_mismatch`, and `config_unrestorable`, respectively. Codex provenance accepts only product-launched `child` or `standalone`; older rows with unknown origin fail closed. Expired retained material reports `retention_expired`; an explicitly purged entry reports `purged`. DECK and NETWORK share a short display cache, but `/api/jump` bypasses it and rechecks immediately before acting.

Conversation-only resume returns `resume_mode: conversation_only`, `mail_status: unavailable`, a fixed `mail_reason` (`credential_absent` / `retention_expired` / `mail_schema_unsupported`), and `mail_message` stating that this agent cannot send or receive ORRERY Mail. The DECK chip, NETWORK label/details and an initial terminal line show the warning. `resume_capability: ready` verifies conversation-resume prerequisites; it does not prove successful Mail owner authentication.

API generation 4 adds these fields. GET `/api/agents` and `/api/graph` Claude rows also carry `mail_status`, `mail_reason` and `mail_message`. Missing fields mean Mail reachability is unreported, not authenticated. Explicit purge still refuses; safely validated expired Claude material permits conversation-only resume. Codex expiry refusal is unchanged.

API generation 5 adds `resume_mode` to finished Claude rows that can be resumed (`ready` or `verification_required`), before any resume. `mail` means the resume authenticates the owner and unretires its Mail row, or refuses without launching; it never falls back to a conversation-only resume. `conversation_only` means the conversation resumes without touching Mail, and the row also carries `mail_status: unavailable` with `mail_reason` / `mail_message`. The `/api/jump` response reports the same `resume_mode`; a Claude resume that restores Mail now returns `resume_mode: mail`. `resume_capability` keeps its codes and meaning, so a caller that resumes on `ready` is unaffected. Codex rows, rows that cannot be resumed and running rows carry no `resume_mode`.

Rows and responses with `mail_reason: mail_schema_unsupported` also carry `mail_remedy`: update the ORRERY Mail this installer deployed with `./scripts/install.sh --update-mail`, let the conversation-only session exit, then resume again; if the dry run refuses, follow [the Mail update guide](agentstack-mail-update.en.md) instead. The same text ends `mail_message`, so a caller that shows `mail_message` gets it without reading the new field (the API generation is unchanged). `credential_absent` and `retention_expired` cannot be fixed from the dashboard and carry no remedy. A running conversation-only session reports `mail_status` / `mail_reason` / `mail_message` even when its retired Mail row leaves `program` empty.

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
  "nodes":[{"id":"WindyFermi","name":"WindyFermi","resume_capability":"not_required"}],
  "edges":[{"source":"Parent","target":"WindyFermi","count":3}],
  "spawn":[{"parent":"Parent","child":"WindyFermi"}],
  "timestamp_diagnostics":{"invalid_count":0,"fields":{}},
  "degraded":false,
  "ts":1785480000
}
```

ORRERY Mail timestamps are normalized to epoch seconds before comparison, whether they are legacy ISO 8601 text or integer microseconds from the Rust implementation. If an unparseable value other than NULL or empty text is present, the response reports `timestamp_diagnostics.invalid_count` and affected `fields` and sets `degraded` to `true`. An unparseable value is never treated as epoch 0.

`/api/agents` and `/api/graph` run at most one computation per query at a time. A request that arrives during one receives the next computation, which starts right after it, so nothing shown gets older. A request that has waited 30 seconds for another request's computation without a result is answered `503` with `Retry-After: 1` and `{"error":"busy","retry":true}`; keep the previous view and poll again. The 30 seconds bound the wait only: a request that starts the computation itself is not limited, and one that waited and then computes can take longer than 30 seconds in total. This response exists from API generation 6.

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

Optional `open` is a boolean. An explicit `false` creates a detached tmux resume with no OS terminal window; `true` opens a window. When omitted, Claude / Codex resume follows `AGENTSTACK_AUTO_OPEN_CHILD` (`0` means detached, unset / `1` opens). Explicit `open` takes precedence. A running session with `open: false` is left running without attach; the environment setting only controls automatic resume opening, so manual Open tmux remains available. Detached resume works without a terminal adapter. This request field is available from API generation 3.

The response is `{ok, session, actions}`. An existing tmux session is opened / focused in the configured terminal. When no session exists, or when a finished husk must be restored from its transcript, the server rechecks the same predicate used for GET rows. A Claude row displayed as `verification_required` receives its full check inside this one request and continues through resume if it becomes `ready`. A Codex child gets a fresh home; credential-backed registration and a fresh binding expectation complete before the identity is un-retired immediately before Codex exec. A refusal returns HTTP 400 with a confirmed fixed reason code such as `{"ok":false,"error":"...","resume_capability":"provenance_missing"}` and does not kill the husk or open a terminal. Bootstrap or unretire failure also prevents Codex startup and removes only generated home / configuration artifacts.

Claude verifies its saved owner credential against the original project, numeric ID, name, and program, then completes credential-backed registration and unretire before opening a terminal. Claude child state / credentials are also retained for 30 days by default, and resume regenerates the child-owned Mail proxy configuration. Top-level Claude uses its existing owner token. Credentials deleted by older cleanup yield `credential_missing`; expired material yields `retention_expired`, and explicit purge yields `purged`. Authentication or unretire failure also prevents terminal launch. Resume never issues credentials or registers an alias. Codex top-level resumes also call unretire.

If Claude startup preparation fails, the original husk is preserved and Mail is re-retired only if it was originally retired. An originally active identity is not retired. Failures of the restoration itself are reported in `rollback_errors`; they are not reported as successful recovery. The CLI waits until tmux and window preparation succeed.

A known three-field legacy Claude child can be `ready` when its private token and existing owned registration pass local validation. Roster GET remains read-only; owner authentication and migration of the same identity happen only on explicit resume. Authentication failure refuses startup ([migration, retention, and rollback](launchers.en.md)).

API 9 accepts optional `base` / `tools`. `tools` replaces the whole selection; `{}` removes extra tools. Omitted base keeps the recorded base; base alone keeps prior tools. Omitting both preserves existing behavior. Changes refuse running/unknown children, missing retained state, standalone, conversation-only and Codex App. Complete `/api/exit` and confirm `gone` / `finished` first. Changes are serialized per child, using existing authentication and native resume; startup failure restores that generation's previous record/generated settings. Success adds `tools_changed: true`, `base` and `tools`. Installer defaults are not appended. See [procedure and limits](delegation.en.md#resume-a-stopped-child-with-different-tools).

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

Request:

| Field | Required | Details |
| --- | --- | --- |
| `parent` | child only | Valid existing agent name. Omit for `standalone: true` |
| `standalone` | no | Boolean. Start without a parent when `true` |
| `dry_run` | no | Boolean. `true` returns a launch preview without registering or launching an agent |
| `headless` | no | Boolean. `true` disables automatic terminal-window opening; `false` enables it. Omitted: inherit the launcher setting. The agent always uses tmux |
| `emoji` | no | String. Accepted for cockpit compatibility; spawn annotations use only `role` / `group` |
| `async` | no | Boolean. Start readiness checks in the background; ignored when `dry_run: true` |
| `task` | yes | Task body. The UI accepts up to 4,000 characters |
| `name` | no | Remove hyphens when specified; must be `available` |
| `dir` | no | Existing working directory. Defaults to the source repository |
| `provider` | no | `claude` (default) or `codex` |
| `model` | no | From the provider catalog. Each provider has a default |
| `effort` | Codex only | Model-specific; use `model_efforts` / `model_effort_defaults`. Unknown metadata: omit for CLI default. |
| `role` | no | At most 40 characters |
| `group` | no | At most 24 characters |
| `worktree` | no | Isolated worktree |
| `worktree_base` | no | Base revision; default `HEAD` |
| `claude_chrome` | Claude only | Boolean. `true` adds `--chrome` to the child. Omitted or `false` means inherit (the user's Claude settings decide). The launcher's env defaults are not used. See [Claude in Chrome](delegation.en.md#claude-children-and-browser-control-claude-in-chrome) |
| `claude_chrome_device` | Claude only | deviceId of the browser to use (`[A-Za-z0-9._:-]{1,128}`). Implies `claude_chrome: true`; combining it with `claude_chrome: false` is rejected |
| `base` | Claude / Codex | `"default"` (explicitly suppresses installer tool defaults) or `"mail-only"` (ORRERY Mail and only what `tools` selects). Since api 7 |
| `tools` | Claude / Codex | Object: `browser` (`true` for Claude/Codex, `{"device": "<deviceId>"}` with a Claude-only deviceId), `screen` (`"read"` / `"operate"` or `{"access": ..., "server": "<name>"}`; without a server it is the macOS computer use, Claude only), `mcp` (array of server names; no tool is approved), `approve_all` (servers in `mcp` whose every tool is approved; rejected for a version not in the table). Rejected when its deviceId differs from `claude_chrome_device`. Since api 7. See [Choosing the tools a child gets](delegation.en.md#choosing-the-tools-a-child-gets---base----tools) |

In API 8, explicit `tools: {}` or `base: "default"` passes `--base default`. Dashboard/API launches honor the form selection and remove installer `AGENTSTACK_CHILD_DEFAULT_TOOLS` from the launcher environment, so removed choices are not appended again. Unavailable explicit tools stop the launch. Only CLI launches omitting base/tools/MCP profile use the path that omits unavailable defaults with a notice.

A child with a `base` or `tools` selection is not started when the selection cannot be applied (no Mail proxy, a server that cannot be copied, `mail-only` in a project where the computer use is enabled, ...). Malformed selections return 400 here; errors that depend on the child's directory are returned as a launcher failure. Other providers such as Gemini reject `mail-only` and `tools`.

Unknown request fields return HTTP 400 before model resolution or launch. When the optional Gemini provider is installed, it also accepts `resources` (see [Gemini delegation](delegation.en.md)); other providers reject that field.

Adding `"dry_run": true` validates the request and resolves provider, model, effort and directory, then returns HTTP 200 with a preview:

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

`argv` uses the same builder as an actual launch. `<child-name>` represents the name that a real launch would select when `name` is omitted; a supplied name is checked and shown directly. The server may normalize it during registration. `<child-token-file>` and, for Gemini, `<gemini-task-file>` in `launcher_env` stand for files created only by a real launch. `launcher_env` contains provider-specific overrides, not the inherited environment. The preview registers no identity, takes no reservation, sends no Mail, starts no tmux session or agent process, and creates no token, task, annotation, log or worktree files. Parent credentials are not required for a preview. Validation can run read-only Git checks and the existing bounded Codex `--version` probe to resolve the actual CLI/model policy. `async: true` still returns only this preview. Names and resources are not reserved, so their availability can change before a real launch.

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

Claude rejects `effort`. Codex accepts formal `gpt-*` IDs even when absent from cache; only an explicit `AGENTSTACK_CODEX_MODELS` restricts membership. The UI uses per-model effort metadata for choices and omitted defaults; explicit effort is passed through to Codex CLI for final validation. Omitted model and `sol` both resolve to `gpt-6.1-sol` when the selected CLI is 0.159.0 or later (fresh catalog evidence is used if its version is unknown), otherwise `gpt-6-sol`; `luna` maps to GPT-6 Luna. Claude accepts well-formed formal IDs independently of local catalog membership or expiry; an explicit `AGENTSTACK_CLAUDE_MODELS` remains a strict allow-list, and invalid configuration blocks Claude launch. The fixed Claude default is `claude-opus-5-5`; it is rejected if an override excludes it and the request omits a model. Orrery never substitutes another model. See [Claude model catalog](configuration.en.md#claude-model-catalog).

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

Adding `"async": true` to `POST /api/spawn` returns after child registration and launcher startup (`ok: true, pending: true`; fields such as `child_name` match the synchronous response). REPL readiness and task-injection confirmation (for a Codex child, confirming that the first task passed as its launch argument is in this launch's record; without the Codex history binding this cannot be confirmed, and the state becomes `ready` after up to 90 seconds) continue in the background, and the result is read with:

```bash
curl -s 'http://127.0.0.1:8770/api/spawn-status?name=WindyFermi'
```

```json
{"ok":true,"name":"WindyFermi","state":"ready","age":9.4,"error":null,"detail":null,"result":{...}}
```

`state` is `launching / ready / failed`. A `failed` record includes `error` and `detail`, the end of the launcher log, matching synchronous spawn. Records remain for 30 minutes, and an unknown name returns 404. Omitting `name` returns every retained record. NEW AGENT in both the dashboard and [ORRERY cockpit](https://github.com/gyroid-eth/orrery) uses this path, closing the modal immediately and showing the result in a toast. Calls without `async` continue to wait for the decision as before.

## Related documentation

- [Hooks and operational helpers](hooks.en.md)
- [Codex App integration](codex-app.en.md)
- [Dashboard](dashboard.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)
