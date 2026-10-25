# API reference

段A の前半: 表示と残高。DeepSeek の子の起動は次の版。
明示opt-inはDeepSeek assetとUSD残高だけを有効化し、spawn catalogへdshを追加しない。
`/assets/deepseek.svg` は無効時404・有効時200。残高型はAPI 21の既存 `/api/quotas` に載せる。

> 日本語版: [api.md](api.md)

[Previous: Dashboard](dashboard.en.md) · [Back to README](../README.en.md) · [Next: Configuration](configuration.en.md)

## Basics

Base URL:

```text
http://127.0.0.1:8770
```

Response bodies are JSON except for images and HTML. The dashboard itself has no login layer. When using `AGENTSTACK_BIND_HOST=0.0.0.0`, limit access to a trusted LAN / VPN.

Legacy mode checks a fixed Host allowlist on every method and path. Before dispatch, it returns HTTP 403 for an untrusted, missing, duplicate or malformed Host, duplicate Origin headers, or an absolute-form request target with a mismatched authority. The defaults allow only `127.0.0.1` / `localhost` / `[::1]` at the actual listening port. Explicitly configure fixed LAN / VPN origins with [`AGENTSTACK_DASHBOARD_ALLOWED_ORIGINS`](configuration.en.md#host-configuration-for-lan--vpn-access). Host checks do not authenticate callers.

Every POST endpoint requires `Content-Type: application/json` and a JSON object body. Browser requests receive HTTP 403 unless `Origin` / `Sec-Fetch-Site` are same-origin; CLI requests may omit both headers. This is the common guard that rejects simple-form CSRF.

A POST Origin must match a fixed origin corresponding to its validated Host. Scheme, host and port are compared, normalizing host case and omitted HTTP `80` / HTTPS `443` ports. An origin belonging to another allowed Host is still rejected. Host admission failures may return an HTTP error body rather than the usual API JSON.

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
{"name":"orrery-telemetry","version":"2026.09.16.1","api":30}
```

See [Installation](install.en.md#version) for version resolution order.

- `version`: when the release was made (`YYYY.MM.DD`, or `YYYY.MM.DD.N` for another release on the same date; see the [CHANGELOG](../CHANGELOG.md)). It is not a compatibility promise
- `api`: the **compatibility generation** for the programs that use this API ([ORRERY cockpit](https://github.com/gyroid-eth/orrery) and others). It goes up by one only when an endpoint or field those programs rely on is **added or changes meaning**, and a [changelog fragment](../changelog.d/) records it (collected into the CHANGELOG at release; see [Contributing](../CONTRIBUTING.md#changelog-fragments-and-releases)); it does not go up with every release. A program should decide whether a feature it needs is there from `api`, not from the date in `version` (the cockpit warns at startup when `api` is lower than it needs). This scheme starts at `api` 2; every earlier release reports 1, whatever it can do

### Isolated global backend (API 17)

Explicit global mode applies operator capability admission to every non-static route, including `/api/version`. `capabilities.operator_http_admission`, `ui_read`, `global_spawn`, `instruction_migration` and `global_dashboard_ui` are 1. Gate the screen on its capability, never the API number alone. See [configuration, authority and interrupted recovery](global-delivery-client.en.md#operator-ui-access). Legacy payloads are unchanged; legacy Host admission is described above.

API 17 adds the global dashboard reply view and these admitted, read-only routes. Legacy mode remains the default; activation is false.

| Route | Projection |
| --- | --- |
| `GET /api/instruction-migration?agent_id=ID&session_id=SESSION` | Optional session selector; canonical instruction findings from that identity’s fixed provider HOME. Unknown or ambiguous ownership returns `disk_state:unknown`, never the dashboard operator’s HOME. |
| `GET /api/fs/dirs?path=PATH` | Existing spawn-root directory guard, independent of Mail identity and work-folder changes. |
| `GET /api/deliverables` | `configured` and `items` keyed by agent name; only explicit `AGENTSTACK_DELIVERABLE_ROOTS`. Unset means an empty result. |

Instruction `data` follows the shared [status schema](../bin/lib/instruction_contract.json): provider, target_path, findings, checked_at, disk_state, session_state, evidence_kind and reason. Findings are the existing canonical finding definition. Session-start candidate hashes do not prove effective instruction loading; a cwd change is not a reload. All three routes use the same operator admission as Mail reads.


### Bundled Mail identity (API 12)

In API 12 distributions, bundled ORRERY Mail's MCP `health_check` returns `server_instance_id`, the same persistent UUID as management-socket inspect. It is neither a secret nor authentication. Daemons compare both transports with the operator-pinned connection UUID, refusing older builds without the field or mismatches. Existing `status`, `environment`, `http_host`, and `http_port` fields remain. This adds no dashboard HTTP route. See [daemon bindings and notifications](daemon-agents.en.md).

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

API 27 separates a native connection's `id` from its `kind`. The `id` identifies the configured connection (for example, `strata` and `strata-epyc`); `kind` is `deepseek` or `strata`. Send the catalog's exact `id` as `provider` in `POST /api/spawn`, together with the selected `model` and `harness`. Equal model IDs on different connections still select different servers. `label` and `server` are display metadata, never routing selectors. Existing `deepseek`/`strata` IDs remain valid. See [multiple connections](configuration.en.md#native-connections).

The bundled NEW AGENT selects Model → Harness → Helper model → Effort. Untouched Effort and Use default send a supported concrete default reported by the catalog; a null, unknown, or unsupported default omits effort. The first candidate is not guessed as a default, and providers requiring explicit effort still require a selection. Same as main omits helper_model. Untouched Terminal omits headless to inherit the existing launch policy; only an explicit change sends it.

API 23 includes explicitly configured DeepSeek (`provider=deepseek`) and Local (`provider=strata`, `locality=local`) in standard spawn. Use the model's `harnesses` availability and `efforts` / `default_effort` / `effort_source`. Capabilities come from checked local evidence, never trial effort settings. The normal catalog does not require managed task modes or attempt IDs. Missing runtimes and disabled opt-ins are unavailable.

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
      "model_note":"",
      "efforts":["low","medium","high","xhigh","max","ultra"],
      "effort_default":"xhigh"
    }
  ]
}
```

The scientist rail's `status` indicates whether at least one pairing of that scientist with the 134 adjectives is available. It becomes `occupied` when every combination is occupied and `unknown` when the database is missing or the query fails. It is not based only on whether the bare surname is registered.

The adjectives are synchronized word-for-word with ORRERY Mail's canonical `SIMPLE_ADJECTIVES` Round 3 list, and the launcher, catalog, and suggestion API use the same source. Custom additions are prohibited because they diverge from name validation in strict deployments.

Codex augments bundled candidates with local CLI cache entries fetched within 300 seconds. Explicit `AGENTSTACK_CODEX_MODELS` restricts candidates to that allowlist; invalid configuration appears in `model_error`. `model_source` is `bundled`, `local_cache`, or `override`. For omission and `sol`, the initial default is `gpt-6.1-sol` with selected CLI 0.159.0+, otherwise `gpt-6-sol`; unknown versions use fresh catalog evidence. If that ID is absent from the selected `CODEX_HOME` cache, choose the next cached candidate in order: `gpt-6.1-sol` → `gpt-6-sol` → `gpt-6-luna` → `gpt-5.6-terra` → `gpt-5.6-luna`. Expired snapshots can lower the default too, but an older CLI never moves up to GPT-6.1 Sol. Without a usable cache or next candidate, the existing default remains. Explicit formal IDs are never substituted. Provider `model_note` is empty when the preferred default is unchanged, otherwise it reports both IDs and the reason; a stale snapshot also supplies its timestamp and guidance to start `codex` to refresh it. Cache `identity` is not checked against the current login, and fallback does not prove account authorization. Provider dictionaries `model_efforts` and `model_effort_defaults`, keyed by model ID, take precedence in the UI. An empty effort list means omit effort and use the CLI default. See [configuration](configuration.en.md#codex-model-catalog).

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

API generation 24 lets running dsh/Hermes agent rows and `/api/graph` nodes return `work`, `question` or `ask` in `act_state` from supported current operation controls. A complete current input frame with no work/question/approval evidence returns `wait`. Hidden, incomplete or unsupported shapes return `null` (unknown); absence alone never implies waiting. Activity does not determine liveness, completion or stop eligibility. Existing Claude/Codex classification is unchanged. See [supported native displays and limits](native-capabilities.md#activity-classification-boundary).

API generation 19 adds nullable `provider_stall` to live legacy Codex CLI agent rows and graph nodes: `{kind: "model_capacity", since: <UTC epoch seconds>, error: <one error line>, notification_status: <status>}`. Status is `sent`, `no_parent`, `parent_unverified`, `delivery_unknown`, or `failed`. It does not change `act_state`. The dashboard observes two distinct current capacity-error captures and sends one notice from the existing child identity to its verified parent. Observed work rearms it; a proven append of "previous stop → newly submitted input → new capacity error" in the same pane also produces a new notice after two captures confirm that boundary. Ambiguous history retains the current episode. GET requests never send messages. An uncertain delivery is not retried. Global mode is not supported yet; see [provider stops](dashboard.en.md#provider-stop-notifications-legacy).

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

API generation 26 adds the unnormalized `model_raw` field to `/api/graph` nodes. The dashboard displays `provider=strata` / `locality=local` with the existing Local hexagon and prefers `model_raw` when selecting a maker such as Qwen. Unknown makers keep an empty hexagon. Provider and locality meanings are unchanged.

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

The header indicator uses the watcher process, signal backlog, most recent success time, and delivery results from the last ten minutes.

API 11 keeps `signal_count` as the total, including legacy signals, and adds `actionable_signal_count`, `orphan_signal_count` and `signal_sessions_known`. Only signals at least 24 hours old with a confirmed absent tmux recipient and no persistent headless runtime manifest are orphans. Measurement failures keep all signals actionable. A stopped watcher or more than 50 actionable signals is `red`; a pending actionable signal with no success in the last 120 seconds is `yellow`; otherwise health is `green`.

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

API13: `/api/jump` accepts explicit `runtime_mode=global`, `client_config`, `client_name`, `prepare_only=true`, `open=false` for fixed-root resume preparation. `runtime_ready` remains false; legacy session input is unchanged.

Request:

```json
{"session":"WindyFermi"}
```

Optional `open` is a boolean. An explicit `false` creates a detached tmux resume with no OS terminal window; `true` opens a window. When omitted, Claude / Codex / dsh / Hermes resume follows `AGENTSTACK_AUTO_OPEN_CHILD` (`0` means detached, unset / `1` opens). Explicit `open` takes precedence. A running session with `open: false` is left running without attach; the environment setting only controls automatic resume opening, so manual Open tmux remains available. Detached resume works without a terminal adapter. This request field is available from API generation 3.

The response is `{ok, session, actions}`. An existing tmux session is opened / focused in the configured terminal. When no session exists, or when a finished husk must be restored from its transcript, the server rechecks the same predicate used for GET rows. A Claude row displayed as `verification_required` receives its full check inside this one request and continues through resume if it becomes `ready`. A Codex child gets a fresh home; credential-backed registration and a fresh binding expectation complete before the identity is un-retired immediately before Codex exec. A refusal returns HTTP 400 with a confirmed fixed reason code such as `{"ok":false,"error":"...","resume_capability":"provenance_missing"}` and does not kill the husk or open a terminal. Bootstrap or unretire failure also prevents Codex startup and removes only generated home / configuration artifacts.

Claude verifies its saved owner credential against the original project, numeric ID, name, and program, then completes credential-backed registration and unretire before opening a terminal. Claude child state / credentials are also retained for 30 days by default, and resume regenerates the child-owned Mail proxy configuration. Top-level Claude uses its existing owner token. Credentials deleted by older cleanup yield `credential_missing`; expired material yields `retention_expired`, and explicit purge yields `purged`. Authentication or unretire failure also prevents terminal launch. Resume never issues credentials or registers an alias. Codex top-level resumes also call unretire.

dsh / Hermes children resume only from the native ID recorded by their own session creation event. Resume never selects recent history; a missing ID yields `no_history`. It checks the same registration, retention and owner credential, preserving the original `harness_state_dir` and native profile without repeating the initial task. Config / MCP generation completes before Mail unretire and release of the tmux launch gate. A changed profile or generation failure preserves the original husk, state and credentials. The internal `--resume-native` entry runs in the existing pane foreground and creates neither a tmux session nor a registration.

If Claude startup preparation fails, the original husk is preserved and Mail is re-retired only if it was originally retired. An originally active identity is not retired. Failures of the restoration itself are reported in `rollback_errors`; they are not reported as successful recovery. The CLI waits until tmux and window preparation succeed.

A known three-field legacy Claude child can be `ready` when its private token and existing owned registration pass local validation. Roster GET remains read-only; owner authentication and migration of the same identity happen only on explicit resume. Authentication failure refuses startup ([migration, retention, and rollback](launchers.en.md)).

API 9 accepts optional `base` / `tools`. `tools` replaces the whole selection; `{}` removes extra tools. Omitted base keeps the recorded base; base alone keeps prior tools. Omitting both preserves existing behavior. Changes refuse running/unknown children, missing retained state, standalone, conversation-only and Codex App. Complete `/api/exit` and confirm `gone` / `finished` first. Changes are serialized per child, using existing authentication and native resume; startup failure restores that generation's previous record/generated settings. Success means tmux/the terminal accepted the launch, without waiting for shell bootstrap/authentication. Later failures keep the new selection for the next resume. Confirm receipt of the child’s report. Success adds `tools_changed: true`, `base` and `tools`. Installer defaults are not appended. See [procedure and limits](delegation.en.md#resume-a-stopped-child-with-different-tools).

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
| `provider` | no | `claude` (default), `codex`, or an exact native connection ID from the catalog (legacy `deepseek`/`strata`, or an added ID such as `strata-epyc`). Do not replace it with its label or `kind` |
| `harness` | DeepSeek / Local required | `dsh` or `hermes`. Claude optionally accepts `claude-code` (legacy alias `claudecode`), Codex `codex-cli` (legacy alias `codex`) |
| `helper_model` | no | Omitted or `same` inherits the main model. DeepSeek / Local also accepts same-backend formal IDs in the selected model/harness `helper_models`; undeclared or foreign IDs are rejected |
| `model` | DeepSeek / Local required | Formal ID matching an available provider/model/harness catalog entry. Existing Claude/Codex omitted-model defaults are unchanged |
| `effort` | no | Send only explicit choices. Claude/Codex use model-specific metadata; DeepSeek/Local validate the selected model and harness capability. Omit to use CLI settings; unknown values are never guessed |
| `role` | no | At most 40 characters |
| `group` | no | At most 24 characters |
| `worktree` | no | Isolated worktree |
| `worktree_base` | no | Base revision; default `HEAD` |
| `claude_chrome` | Claude only | Boolean. `true` adds `--chrome` to the child. Omitted or `false` means inherit (the user's Claude settings decide). The launcher's env defaults are not used. See [Claude in Chrome](delegation.en.md#claude-children-and-browser-control-claude-in-chrome) |
| `claude_chrome_device` | Claude only | deviceId of the browser to use (`[A-Za-z0-9._:-]{1,128}`). Implies `claude_chrome: true`; combining it with `claude_chrome: false` is rejected |
| `base` | Claude / Codex / dsh / Hermes | `"default"` (explicitly suppresses installer tool defaults) or `"mail-only"` (ORRERY Mail and only what `tools` selects). Since api 7 |
| `tools` | Claude / Codex / dsh / Hermes | Object: `browser` (`true` for Claude/Codex, `{"device": "<deviceId>"}` with a Claude-only deviceId), `screen` (`"read"` / `"operate"` or `{"access": ..., "server": "<name>"}`; without a server it is the macOS computer use, Claude only), `mcp` (array of server names; no tool is approved), `approve_all` (servers in `mcp` whose every tool is approved; rejected for a version not in the table). Rejected when its deviceId differs from `claude_chrome_device`. Since api 7. See [Choosing the tools a child gets](delegation.en.md#choosing-the-tools-a-child-gets---base----tools) |

The ordinary DeepSeek / Local catalog shares plain-generation admission and generator static validation with the launcher. Older managed generations are not automatically admitted. Effort uses the checked cache for the selected model and harness; unknown capabilities remain unknown. Liveness checks native CLI descendants of the pane and does not classify an unconfirmed observation as finished. Provider identity comes from the child's explicit harness_backend, rather than the model name.

dsh / Hermes accept `base` / `tools` only when the selection can be represented in native configuration. They copy stdio / HTTP MCP servers and reject built-in computer use, Chrome devices, and unrepresentable per-tool approvals or filters with a reason. A successful launch does not imply those approvals are enabled.

In API 8, explicit `tools: {}` or `base: "default"` passes `--base default`. Dashboard/API launches honor the form selection and remove installer `AGENTSTACK_CHILD_DEFAULT_TOOLS` from the launcher environment, so removed choices are not appended again. Unavailable explicit tools stop the launch. Only CLI launches omitting base/tools/MCP profile use the path that omits unavailable defaults with a notice.

A child with a `base` or `tools` selection is not started when the selection cannot be applied (no Mail proxy, a server that cannot be copied, `mail-only` in a project where the computer use is enabled, ...). Malformed selections return 400 here; errors that depend on the child's directory are returned as a launcher failure. Other providers such as Gemini reject `mail-only` and `tools`.

Unknown request fields return HTTP 400 before model resolution or launch. When the optional Gemini provider is installed, it also accepts `resources` (see [Gemini delegation](delegation.en.md)); other providers reject that field.

After failed startup, the API calls the launcher's common cleanup. If cleanup cannot be confirmed, the response has `ok: false`, `cleanup_state: "unknown"` and `cleanup_incomplete: true`; the API preserves child state, owner credentials and handoff files. A successful cleanup may retain resumable material, which the API also leaves intact.

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

When an omitted Codex model or `sol` changes the default, preview, synchronous, asynchronous pending and final results include `model_note`. It reports both IDs and the reason, plus the timestamp and refresh guidance for a stale snapshot. Explicit formal-ID requests do not receive a substitution notice.

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

Explicit same-intent replay from registration status sends `registration_sha256` as optional `expected_digest` to `POST /api/global/spawn` (API 17). The existing operation mutex protects comparison and replay. A mismatch returns `REGISTRATION_PENDING_CONFLICT`; an absent pending returns `TARGET_UNAVAILABLE`. Existing credential/name/program/model checks remain in force.

### Global runtime reads (API 18)

`GET /api/runtime-observations?agent_ids=1,2` requires the existing HTTP operator admission. IDs are unique positive 63-bit integers, with 1–200 per request. Invalid input returns `QUERY_INVALID`. No public Mail tools are added. `global-runtime-observation.json` contains the single response schema.

Each item returns `liveness`, nullable `act_state` / `ctx_used`, `session_count`, `parent_agent_id` / `parent_state`, and `activity_freshness` / `reason`. Tokens, pane captures and private paths are excluded. Unknown is never replaced with waiting or context zero. See [observation and capability checks](dashboard.en.md#global-runtime-observation).

Capabilities include `global_runtime_observation=1` and `operator_session=1`. Global `/api/quotas` and `/portrait` use the same admission and existing read helpers. Images use shared-session `apiBlob` object URLs, revoked on Lock.


API 25: legacy managed `attempt_id` spawn/status requests are retired and return `unsupported_attempt`. Use standard `POST /api/spawn` and `GET /api/spawn-status?name=<name>`.

<a id="mobile-reader-api-28"></a>

<a id="mobile-reader-api-29"></a>

## Mobile reader (API 30)

`GET /api/mobile-mail?window=<period>` is a legacy, loopback-only metadata endpoint. The default is `day` (24h). Allowed values are `all`, `day`, `15m`, and `m<N>` for integer minutes from 15 to 43200 divisible by 15; leading zeros and fractions are rejected. It keeps the existing dashboard Host admission. Missing DB or project/schema/read failure returns 503; unsupported global mode is explicitly refused (501 after admission, or the existing global admission refusal). It never falls back to a legacy DB from global mode. Unknown or repeated query fields return 400.

A 200 response has `status: available`, `window`, `observed_at`, `edges`, `events`, `events_truncated`, `edges_truncated`, `invalid_timestamps`, and `degraded`. `edges` contains source/target/kind/count/last_ts aggregated over the actual period, at most 10,000 rows. Counts are direction-specific to/cc recipient rows, excluding bcc and cross-project endpoints. `events` contains the latest 200 recipient rows with stable opaque `key`, source/target/kind/`count:1`/event_at, ordered by event time, message id and recipient id descending. The SQL reads only metadata; subjects, bodies and read/ack writes are excluded. Invalid timestamps are excluded and reported as degraded. An empty successful query differs from an unavailable query.

The optional [mobile relay](mobile.en.md) exposes authenticated GET/HEAD only: `/`, its fixed bundled assets, `GET /view/snapshot?window=<period>` and the portrait route below. It starts only with `--enable`, requires an exact owner login and HTTPS .ts.net origin, and binds only on 127.0.0.1. It checks the outer Host, owner header and any supplied Origin for every asset, snapshot and portrait; normal GET may omit Origin. Other methods, unknown routes/query fields, duplicate authentication headers, cross-origin requests, upgrades and overrides are rejected. No arbitrary upstream URL, header forwarding, proxy environment or redirects are accepted. Upstream JSON is bounded to 4 MiB and 2 seconds per fixed request.

The snapshot has `schema:1`, observation time, window, `period` (width in seconds and unknown-activity count), component `sources` (available/stale/unavailable with last observation and age), explicitly projected `agents`, graph Mail/spawn edges, timeline Mail and observed status changes, truncation flags, and unavailable quota. Agent projection includes only canonical name, provider/model/locality, nullable running/context, retired/state/act_state and last activity. The graph/agents union is filtered against current time using the greatest valid last-active timestamp from either source or canonical Mail time. Future/unknown activity is never replaced by now; unknowns are excluded from finite periods with a count. Recently active retired agents remain; running alone does not bypass the period. Graph edges retain only endpoints in that set. Status changes have `observed_at` and null `event_at`; the latest200 retained changes are filtered by period and agent set, with `states_truncated` on overflow. Context sampling time remains null. Failed polls retain stale observations without turning unavailable into healthy zero. At most4 metadata periods are cached, evicting parts and published snapshots together. The logarithmic15min–30d slider rounds to15minutes and debounces200ms; all-history is explicit. The PC period contract is unchanged.

`GET /view/portrait?name=<NAME>&window=<period>` accepts a canonical name only from that period's published roster. The fixed loopback `GET /api/mobile-portrait?name=<NAME>` first uses the existing case-insensitive custom map/overlay resolver, then falls back to the longest matching bundled scientific suffix if missing or invalid. Private maps and paths are not returned. PNG images are limited to 512 KiB and dimensions 1–1024, checked for signature/IHDR, with a 2-second upstream deadline. Arbitrary SVG/HTML is rejected; fetch/decode failure falls back to initials. Images are no-store. The loopback portrait API is legacy-only; invalid name/query returns 400, missing/invalid image 404, and global mode is explicitly refused.

API 30 adds opaque `scope` and `next_cursor` to metadata. `GET /view/mail?window=<period>&cursor=<token>` returns the next 200 recipient rows, without content. Graph pair lists add `a=<NAME>&b=<NAME>&direction=both|forward|reverse` to the same route; only their first page may omit the cursor. Both names must belong to that period's published roster. SQL filters pair/direction before LIMIT. Keysets sort by time/message id/recipient id descending. Cursors bind scope/period/pair/direction/boundary for 10 minutes, capped at 256; the relay retains at most 2000 issued events. Invalid/duplicate queries return 400, unknown/expired events or cursors 410, and unavailable reads 503. The browser holds 200 current rows and at most 20 positions; polling does not overwrite older pages.

Only `GET /view/message?window=<period>&event=<key>` returns the tapped single subject/body after authentication. It matches issued metadata against the current project, to/cc delivery, period and canonical event. The new loopback-only `/api/mobile-message` accepts the same query plus `mode=content|revision`, performing canonical admission before either read. Internal results use a 512-character subject,64 KiB UTF-8 body, truncation flags and opaque fingerprint/revision. The phone receives only event/subject/body/truncation flags. Revision hashes cover the visible bounded content and original byte length; they do not guarantee detection of same-length edits beyond the truncated view. Content is literal text, with no read/ACK writes, attachments, replies or operations. All responses are no-store and content is neither logged nor persisted. Close/new tap/period/view/background/pagehide clear DOM and abort requests; request generations discard late results.

The loopback metadata page route is `/api/mobile-mail-page`. The internal fixed 24 h cache route is `/api/mobile-cache-page?cursor=<token>` (no cursor initially); it has no external relay route. Only an authenticated snapshot starts the single worker. It reads up to 32 distinct messages per batch, with actual serialized responses below 4 MiB, a 120-second sweep budget,25 ms yields and at least 60 seconds after completion before retry. Bounds are 63 nonempty pages,2000 distinct messages and 32 MiB of logical JSON bytes. Incomplete 24 h coverage is partial/unavailable. JSON-escaped batches shrink to fit. Snapshot `mail_cache` returns only status/messages/logical_bytes/bounds/reason, never the cache dictionary or content.

Bodies and their metadata expire together under 24 h/LRU/byte limits. Scope changes invalidate everything. Every cache hit rechecks the current canonical revision; failure never returns stale content. 24 h limits caching, not detail viewing: published 30 d/all-history events remain readable one at a time, outside the cache. Reader shutdown stops/joins the worker and clears all content. All new loopback APIs return 501 in global mode, without legacy DB fallback. The relay does not read terminals, deliverables or quota refresh endpoints. Browser polling pauses in the background. Authentication and phone restriction remain separate; deployment requires the real-device checks in the guide.
