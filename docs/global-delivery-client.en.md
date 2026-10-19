# Isolated global client launch and delivery

[日本語](global-delivery-client.md)

Legacy mode remains the default and global activation remains false. Use a temporary HOME and a freshly prepared isolated server candidate. Live cutover and import of released legacy formats remain outside this change.
Prepare the fixed Claude / Codex provider HOME with `agentstack-runtime-client instruction-configure operator claude` / `codex` before launch. Normal launch only reads the block; missing or stale instructions return `INSTRUCTION_PROVIDER_CONFIG_REQUIRED`. See [instruction migration](instruction-migration.en.md#custom-provider-home).


## Authorities and layout

The existing context and credential own authentication and binding. Window identity lives only in context.identity. Old extra window fields in global credentials/profiles/session receipts are ignored without rewriting on read and omitted on the next regular write. Existing live-sessions and session_index own process and provider-session evidence. runtime/launch.json contains only launch-specific intent and resolution.

The server notification fact is the completion authority. Local delivery intent records an interrupted external effect and retry schedule, never a delivered cache. Inbox reads do not consume notifications. Explicit read, acknowledgement and delivery completion do.

The consumer root is agentstack_home/global-consumer/SHA256(mail_instance_id)/agents/agent_id. The existing server-config.json must match isolation, instance, candidate generation, epoch, runtime_root, authority and lock. Mismatch fails before output with CONSUMER_ISOLATION_MISMATCH. This corrects the authority-derived root in wire v3: client state is outside the server preparation workspace. All clients of one instance share the same root. Agent and App external routes contain only client_name, without tokens, display names or a current launch copy.

## Launch recovery

Standalone and child launch/resume use the same helper. Output slots, command, cwd and server capabilities are checked before window binding and execution. Registration and launch are distinct operations; an uncertain launch is never retried by registering another identity.

Internal `execute-launch` / `provider-exec` commands terminate options with
`--` after the fixed context, preserving even a launch nonce starting with
`-` as a positional value. `receipt_nonce` distinguishes launch generations;
it is not a registration token or API credential. Owner authentication still
uses the fixed credential, and execution retains its phase, runner parent
PID/start and trusted session-receipt checks.

| Phase | Exit |
| --- | --- |
| planned / gated | launch-retry or launch-cancel with the current status digest |
| exec-unknown | Explicit launch-resolve: abandon, confirmed-running or confirmed-ended |
| running | runtime_ready requires trusted SessionStart and remote owner/window verification |
| ended / cancelled | Prepare a new explicit launch after all confirmed-ended live writes finish |
| abandoned | Leave the unknown process alone; resolve explicitly when evidence is available |

Pass JSON on stdin to `agentstack-runtime-client operator-json OPERATION`. The source schema is [local_operator_wire.operations](../packages/agentstack_mail/fixtures/global-client-4c.json). `launch-cancel`, `launch-resolve` and `resolve-delivery` require `confirm:true`; `launch-retry` does not accept confirm.

Set client_root to the fixed client root and AGENTSTACK_CLIENT_CONFIG to its runtime-client.json. Read launch ID from the existing launch.json and message ID from doctor-local or the received notification. Read current status first:
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

Inspect phase, operator_required, next_operations and remote_owner_verified. state_sha256 covers current context, credential, launch, live sessions and intent under the same lock. On RESOLUTION_DIGEST_MISMATCH, read status again and reconsider the action.

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

Read status again after the operation. Abandon returns phase=abandoned/runtime_ready=false; cancel returns phase=cancelled/prepared=true/runtime_ready=false; retry returns phase=gated/prepared=true and is not ready until trusted SessionStart. For delivery, obtain the delivery-status digest and pass message_id, expected_state_sha256, confirm:true and resolution to resolve-delivery. Actions are retry-at-least-once, confirm-external-completed and abandon-without-consuming; operator_evidence records the basis for the decision. Abandon-without-consuming only blocks locally, without consuming unread state or the server notification fact.

If confirmed-ended is interrupted while saving live sessions, status returns operator_required=true and next_operations=[launch-resolve]. Resubmit the same confirmed-ended with a fresh digest to finish the remaining writes before a new launch. Local status and abandon also work while Mail is unreachable.

The bound proxy derives server schemas by removing owner and binding inputs. Local bootstrap verifies the trusted receipt; runtime_status reports the bound runtime. Caller tokens and display names never select another owner. New windows use the existing agent_id foreign key and NULL legacy_project_id.

## R3 operator full form (isolated integration in progress)

The Python `RuntimeOperator.spawn` API separates the existing minimal standalone
`agent_name/program/model/cwd` form from a delegated full form with one shared
caller dictionary:

```python
operator.spawn("child_01", caller=caller, parent_client_name="parent", prepare_only=True)
```

The shared canonical schema validates all required fields and preserves omitted
optional keys. Unknown keys, wrong types, or mixing full and minimal fields
(including explicit `None`) fail before effects. Existing tools grammar validates
selection; every caller field must match the saved intent at the configured
fixed owner. Caller input never selects owner/root/auth/scope or re-resolves a
model. The full form requires an explicitly configured fixed intent owner.

The explicit fixed `parent_client_name` selects the parent; its canonical name
must match `caller.parent`. A display name never discovers another credential
or root, and parent/child root overlap is refused. Omitted transport
`task_delivery` uses the caller field, defaulting to mail only when that field
is absent; an explicit transport selection must agree. This does not fill
omitted intent fields or rewrite the saved intent.

`prepare_only=True` uses existing prepare_child and task preparation without
starting a provider or tmux. Ordinary execution uses R2 and saved detached,
without automatically configuring/approving instructions, trust or native
permissions. Unverified real CLIs still fail with
`LOCAL_MODEL_ADAPTER_UNSUPPORTED`. A delivery-only TEST binding is not native
acceptance. Existing minimal standalone spawn semantics remain unchanged.

Full admission is repeated under the existing child C, which is released after
task preparation. The admitted digest also reaches R2 launch preparation and
is checked under its C before any launch row is written: an intervening explicit
revision cannot select another intent for the old caller. `detached=False`
hands the calling command to the existing R2 foreground runner.

API 20 also accepts this full form at `POST /api/global/spawn`: required
`client_name`, `parent_client_name`, `caller`, `prepare_only`, with optional
`expected_digest` and `task_delivery`. Never mix it with the existing minimal
HTTP fields. `caller.task` is the absolute path of an approved task **file**,
not the legacy NEW AGENT task body. The fixed server context and existing HTTP
operator admission lead through actual `do_spawn` to the same operator. Existing
roots still guard cwd, but the full caller literal is not canonicalized into a
matching approval or backfilled from its owner. Malformed inputs return HTTP 400,
intent/identity/parent mismatch HTTP 409, and unverified native HTTP 503, with fixed reasons.
HTTP rejects `prepare_only:false` plus `caller.detached:false` before effects with
HTTP 400 `HTTP_FOREGROUND_UNSUPPORTED`, without changing detached. `prepare_only:true`
can still prepare the task with detached false. The Python/CLI foreground runner
contract above is unchanged. Minimal HTTP schema and successful behavior, and
legacy semantics, stay unchanged. Shared refusal classification improves minimal
`LOCAL_MODEL_IDENTITY_MISMATCH` and `LOCAL_MODEL_INTENT_CHANGED` from HTTP 503 to 409
(an API 20 compatibility difference); see the [HTTP format](api.en.md).
`global_spawn_full_caller:1` advertises only this delivery entry, never native
approval. UI, catalog/status and full five-entry acceptance remain separate gates.

## Codex hook trust

Claude mcp.json and orrery-hooks.json are derived files regenerated under the launch gate operation mutex. Hooks use `--settings orrery-hooks.json`; the provider-owned settings.json model and permissions stay untouched. Gemini keeps its native `.gemini/config/mcp_config.json` as the sole configuration, CAS-updating only the `orrery-mail` entry under the same mutex and preserving other top-level keys and MCP entries. Codex uses per-call `-c` overrides without generating its default config. Concurrent edits return `GENERATED_CONFIG_CHANGED`, unreadable files return `GENERATED_CONFIG_UNREADABLE`, and invalid Gemini JSON or structure returns `GENERATED_CONFIG_JSON_INVALID` without overwriting bytes. Correct private paths/permissions or JSON, then retry explicitly. There is no separate configuration ledger.

Normal launch never approves trust. The approved plan in the fixed provider HOME must match plugin payload and native config trust. Otherwise launch fails with `CODEX_HOOK_CONFIGURE_REQUIRED`. Approval cannot be reused for changed payload.

With `AGENTSTACK_CLIENT_CONFIG` pointing to the intended fixed context, the operator prepares a plan:

```sh
agentstack-runtime-client codex-plan operator > hook-plan.review.json
```

A human reviews provider HOME, plugin placement and hook definitions. Only after approving them, the human changes `approved` to `true` in the JSON and supplies it on stdin:

```sh
agentstack-runtime-client codex-apply operator < hook-plan.review.json
```

This reuses existing plan / apply_trust code. Launch, update and resume never grant approval. Arbitrary HOME/placement and installing another binary version are unsupported. The plugin's Mail MCP is disabled for this process; the supplied common-client proxy runs while approved hooks remain active.

Gemini uses the fixed provider HOME's `.gemini/config/mcp_config.json` and existing stream renderer. Headless children accept session evidence only from provider `init/conversation_id`. Missing init fails with `GEMINI_SESSION_RECEIPT_UNAVAILABLE`; interactive roots fail with `GEMINI_INTERACTIVE_RUNTIME_UNSUPPORTED`. Neither becomes ready. Gemini is tested with a synthetic stream-provider fixture only; real agy remains unverified.

<a id="persistent-execution"></a>

## Operator configuration for persistent execution

The fixed profile.json stores one typed execution object. It adds no owner/binding copies. Regular run only reads it. Released persistent profiles remain LEGACY_FILE_REQUIRES_IMPORT; explicit import belongs to PR7.

```sh
client_root="$(dirname "$AGENTSTACK_CLIENT_CONFIG")"
# Only if this fixed profile is absent:
agentstack-runtime-client save-profile
agentstack-persistent configure --profile "$client_root/profile.json" --operator <<EOF_JSON
{"argv":["/absolute/python","/absolute/bot.py"],"interaction":"headless","cwd":"/absolute/private-workdir"}
EOF_JSON
agentstack-persistent run --profile "$client_root/profile.json"
```

argv must be an array, never a shell command string. cwd must be private, within the same isolation root, with no symlink components. Run without execution returns PERSISTENT_EXECUTION_REQUIRED. Arbitrary profile destinations and concurrent reads of legacy profiles remain unsupported.

The bot uses AGENTSTACK_CLIENT_CONFIG and the common client. After recording trusted SessionStart, it receives the existing orrery-mail-notification-reply-v1 protocol on the fixed AGENTSTACK_PERSISTENT_WAKE_SOCKET. Requests include the message and a stable reply_request_id. Complete reply_message using that UUID before returning status:replied with the original message_id. An interrupted or uncertain reply does not complete delivery.

## Delivery and health

Hints carry IDs and binding only. Owner pending-only/exact-ID reads select actual messages. Purged, read, acknowledged and imported-consumed messages are never reinjected. One formatter produces complete-body, preview and no-body notices.

| Channel | Retry bounds |
| --- | --- |
| tmux / standalone hook / persistent / daemon | Existing 30-second cooldown; no attempt cap; unknown waits for both channel timeout plus 30-second lease and backoff |
| Codex App | 0.25-second poll; 2-second coalesce; 12 wakes/hour; 900-second lease; 2..300-second backoff; 5 attempts |

The watcher waits with the existing two-second poll when the server-owned signals directory is absent, without creating it. Empty slots are removed with rmdir under the existing message lock, including previously abandoned empty slots on the next reconciliation. The client operation mutex remains held through removal, so a concurrent prepare opens a fresh slot without losing its intent. Numeric intent names are candidates only; per-message locking validates existence, type and permissions. Diagnostics report unsafe slots without stopping other messages. BELOW_MIN_IMPORTANCE and SELF_MESSAGE are policy decisions, not operator incidents; existing reconciliation removes these intents after their hint disappears through read/ack or consumption. Bare shells, capture timeouts, absent sessions, disabled Mail and unresolved routes precede handoff and consume no attempt. App rate/attempt limits block without dead-lettering. Unknown retry reports duplicate_possible. Once confirmed, retry only resends server completion. Late attempt, old credential-generation and old-launch callbacks fail closed.

Numeric compatibility covers decimal inputs without leading zeros and the five tested boundaries (0, 1, 300, 301 and nonnumeric). For leading-zero timeouts, legacy watcher arithmetic is octal while global uses decimal; the legacy watcher body remains unchanged.

Top-level server status describes request readiness; signal.status describes notification delivery health. Existing doctor warnings and local status/worker logs report unknown or blocked work. Dashboard delivery-health UI remains later work.

For an unsafe slot, `repair-local-state` is a manual instruction, not a CLI operation or automatic repair. Correct the reported message slot type/permissions, leave healthy slots untouched, never follow a symlink, and rerun diagnostics.

## Lock order and tests

Acquire agent directory A, client operation C, message directory M, mutation U, authority F in that order, using only required locks. Release all locks before provider execution, tmux/bot waits and hook stdout flush. No launch.lock or delivery database is added. Clients never open, close, copy or hash SQLite main/WAL/SHM/journal files.

Tests wait for real HTTP health and use temporary HOME and a dedicated tmux socket. Installer payloads and synthetic layouts derive from the installer's single declaration. Report related, outside-sandbox and full-suite results separately.

Only the management delivery_completed callback can record a completed handoff after recipient retirement, with the existing recipient and exact credential generation still checked. Public tools continue to reject retired recipients, and reactivation preserves the completion fact.

For task_delivery=mail, a child task is sent using the parent’s existing mutation intent and the same task-digest-derived UUID on retries. Task bytes are not embedded in launch arguments; the child fetches its inbox. Ordinary contact policy applies without automatic approval. tools_selection uses the existing child_tools policy against fixed provider HOME configuration; missing selected servers fail with TOOL_SELECTION_UNAVAILABLE.

### Repeating an explicit Codex plan

If `codex-plan` stops after building the marketplace, the operator explicitly repeats the same command. Only the fixed provider HOME's `marketplace/` is rebuilt. Every replan returns an unapproved plan, even for identical content: one rule prevents an old approval from authorizing different content. Ordinary launch neither rebuilds nor approves anything.

### App cold-wake boundary

The App history HOME uses the same shared resolver as the legacy consumer: `CODEX_HOME`, or the ordinary `~/.codex` when absent. History is never copied or moved into a child root; only the resume process writes to it. Resume receives the target child's context-bound proxy, with one concurrent resume per root. It uses the existing plugin and human-approved native trust without automatic approval. Ordinary child launch still uses that child's fixed provider HOME.

App cold wake is tested with fixtures only; actual resume is unverified. At 12 wakes per hour, admission becomes `blocked / WAKE_RATE_LIMITED` without incrementing handoff attempts. Once the time window permits, the same intent resumes. The server notification fact remains pending; polling never consumes it.

App uses the same execution settings reader as the legacy consumer for `AGENTSTACK_CODEX_APP_COLD_WAKE`, `AGENTSTACK_CODEX_BINARY`, `AGENTSTACK_CODEX_APP_PLUGIN_ID`, and `AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK`. Explicit cold-wake disablement and Mail disabled prevent new handoffs.

Global watcher reacts to signal-directory events, falling back to the legacy two-second poll; the existing scan interval rescues missed events. Legacy watcher numeric declarations remain the sole source for cooldown, headless timeout and poll/scan defaults. App timing uses the same AGENTSTACK_CODEX_APP_WAKE_* reader and WakePolicy defaults as the legacy bridge. Waiting/blocked decisions precede fetch; below-minimum importance is cached in the same intent and reconsidered when its threshold changes. A prior unknown delivery keeps duplicate_possible even if a later attempt is known not to have been submitted. A previous launch for the same owner/binding is replanned; a genuine owner/credential change blocks only that message and can be explicitly rebound by retry-at-least-once. Errors in one message or App root do not stop the others.

<a id="operator-ui-access"></a>
## Isolated dashboard operator API

[日本語](global-delivery-client.md#operator-ui-access)

Default installation and the legacy dashboard are unchanged. API generation 15 adds an isolated global backend reading a completed S2c candidate through its private management socket. The dashboard never opens Mail SQLite. No public Mail tools are added. API 16 enables `global_dashboard_ui=1` and `instruction_migration=1` for the [global dashboard](dashboard.en.md#global-mail-view); cockpit integration follows separately. Never infer screen support from the API number alone.

Use a dedicated temporary HOME and explicitly set `AGENTSTACK_RUNTIME_MODE=global`, `AGENTSTACK_GLOBAL_SERVER_CONFIG` (the completed candidate's `server-config.json`), `AGENTSTACK_MCP_URL` (that isolated Mail HTTP endpoint), `AGENTSTACK_HOME` (the same isolation root's `agentstack`) and `AGENTSTACK_PORT` (a dedicated free port). Invalid global configuration never falls back to the legacy database. Live cutover is a separate procedure.

An operator first sets `AGENTSTACK_HOME` explicitly to the isolated home and runs `agentstack-http-operator configure` locally. Without that variable, the CLI refuses with `HTTP_OPERATOR_HOME_REQUIRED`; it never defaults to the real HOME. This exclusively creates the fixed `runtime/http-operator.json` with mode 0600; existing files are never overwritten. Use the value from `agentstack-http-operator show` in `Authorization: Bearer …`. This capability is separate from agent registration tokens. Keep it out of URLs, logs and shared files. For revocation, stop both participating servers, set aside this fixed file explicitly and configure again.

Open the isolated dashboard URL and paste this operator capability in Unlock. The page stores it in sessionStorage and reuses it for reloads, never a URL. Same-origin online tabs share this same capability through BroadcastChannel and propagate Lock. A separate origin must be unlocked independently. Lock clears Mail and credentials from participating online pages; it does not revoke a copied, closed or restored tab, an opener/duplicate sessionStorage copy, an extension granted page access, or another browser profile. Use server capability rotation and restart to revoke all copies. The OS user and browser profile are the trust boundary. Wrong/stale capability is 403; an unavailable or unsafe capability file is 503. Neither causes a legacy fallback.

TCP does not prove peer UID. The capability proves access to the user's private file; one admission checks loopback, configured Host and any supplied Origin/Referer/Fetch Metadata. Every route except static GET/HEAD passes this admission, including future routes. Same-UID/root actors able to steal the capability, XSS and extensions granted page access are outside this boundary. Sharing one Mail instance across users is unsupported.

| Read | Selection |
|---|---|
| `/api/agents`, `/api/mail-watcher-health` | Operator roster / health, without bodies |
| `/api/mail/recent`, `/api/mail/message`, `/api/mail/conversation` | Required `view_as_agent_id` visibility |
| `/api/mail/legacy-label` | Visible legacy label history; `source` is rejected |
| `/api/agent-history` | Independent target `agent_id`, intersected with visibility |
| `/api/messages-since` | Visible rows after `after_id`, with keyset continuation |
| `/api/edge-messages` | Visible sender/recipient relationship between `a_agent_id` and `b_agent_id` |

MAX, keyset, limit and response revision share one read transaction. Fetch never consumes read/ack/notification facts. `/api/graph` uses that same visible metadata page. Invisible messages, hidden BCC and invisible upper-bound IDs never enter the projection.

### Registration states and interrupted operations

`agentstack-ui-client --client-name` uses the existing CLI slug: lowercase ASCII letter/digit first, then lowercase ASCII letters/digits, `_` or `-`, up to 64 characters. No name-derived alternate root is created. Use `clients --name NAME` to locate existing roots and select one explicitly; multiple roots require user selection.

| `status --client-name SLUG` | Explicit next operation |
|---|---|
| absent | `prepare-root --client-name SLUG` |
| root_only / registration_pending | Same `register --client-name SLUG --name NAME --program PROGRAM --model MODEL` |
| registration_saved | `finalize-registration --client-name SLUG` |
| registered_unconfigured | Existing provider configure / Codex plan, human approval and apply against the fixed context |
| configured | Existing explicit launch |
| launch_present | Existing launch-status / resolution with the same launch ID and digest |
| unavailable | Repair the cause; a new identity needs a new root |

One existing pending record stores name/program/model/token/binding; only that same intent is resent. An unsent request creates one row. A lost response resolves atomic NAME_CONFLICT by exact name, then proves ownership with whois using the saved token. Partial credential/context/metadata saves finalize the saved row with the same ID/token and no registration retry. Status/list never configure providers or rewrite contexts.

For an existing foreign owner, compute the pending file's SHA-256 and explicitly invoke `registration-abandon --client-name SLUG --expected-digest DIGEST`. Only verified foreign ownership permits a terminal record; it neither registers another name nor adopts that owner. Resend an unsent/unknown intent; finalize a proven owned row. Terminal roots are not automatically reset: use a new root for a new identity.

`POST /api/global/spawn` shares this core and validates cwd against existing spawn roots. Its existing minimal form explicitly configures provider instructions, without approving Codex trust. Missing configuration returns registered_unconfigured. The full form above adds no configuration/approval and compares the same caller against its configured owner. `POST /api/global/resume` reuses the root's existing launch/session and creates no next launch while operator resolution is pending. If disk saving fails after network success, return through status to the same pending/finalize entry.


Name lookup rejects an existing Mail name with no matching client root as `NAME_CONFLICT` before creating a root or pending record. `clients.name_agent_id` exposes that read result; `diagnostics` reports each unavailable root independently. Such roots remain unusable, while unrelated names and roots can proceed. Terminal pending records retain their known name and owner-conflict evidence and prevent name reuse. Retired identities are checked through the same private owner inspection.

A registration race can still leave a pending intent. Read `registration_sha256` from `GET /api/global/spawn-status?client_name=SLUG`, then use the admitted `POST /api/global/registration-abandon` with `{"client_name":"SLUG","expected_digest":"DIGEST"}`. A mismatched digest leaves the record unchanged. This uses the existing CLI implementation: only proven foreign ownership permits a terminal record, and a different identity requires a new root. A missing root or registration pending returns 404 `TARGET_UNAVAILABLE`; existing-file admission and digest checks still apply. Status never exposes the token. Malformed JSON returns 400 `QUERY_INVALID`, registration conflicts return 409, and transport failures return 503.

API 17 additionally accepts the status digest on explicit registration replay. Browser Lock invalidates queued handoffs and in-flight bodies; denied storage uses page memory and requires Unlock after reload.

For API 18 `global_runtime_observation=1`, `operator_session=1` and the non-secret `agentstack-http-operator probe`, see [activity and capability checks](dashboard.en.md#global-runtime-proof). Cockpit implementation and PR7 activation remain separate gates.

## Mode selection and services without an owner context

The shared client's `selected` function is the sole mode selector. Selection does not read owner credentials, register an identity, or contact the server. A new top-level launcher or service reads the existing `env.sh` once and pins its selection in the process environment. Hooks and workers inherit that value. Editing `env.sh` does not switch a running process; an explicit service stop/start adopts the new default.

| Input | Result |
|---|---|
| No mode, context or config | Legacy by default, preserving the existing path |
| Valid global context | Global; the context overrides the installed default |
| Explicit legacy with global context/config | `RUNTIME_MODE_CONTEXT_CONFLICT` |
| Global without context | Selection succeeds; agent operations return `CONTEXT_UNAVAILABLE` |
| Only an existing isolated server config | Global operator/service, without borrowing an owner |
| Unknown mode, broken context or invalid config | Fixed rejection; no legacy database fallback |

`agentstack-runtime-client select --json` returns an ephemeral selection. Legacy context formats return `LEGACY_CONTEXT_REQUIRES_PR7`; import belongs to a later explicit procedure. An old session without a pinned mode can continue in legacy when encountering a new global default only if its existing non-secret session_index self binding verifies. Otherwise `LEGACY_SESSION_MODE_UNRESOLVED` requires ending and explicitly restarting that old session. A project-key string alone is not proof.

The ownerless watcher entry is `agentstack-runtime-client watch-service --server-config /absolute/server-config.json`. Existing RuntimeOperator admission verifies readiness and binding, then dispatches each fixed global-consumer route to the existing owner client delivery engine. Zero recipients is healthy waiting. An invalid recipient emits a reason without stopping another valid recipient. Owner and service watchers share directory events and rescue scans. Process liveness or waiting is not delivery completion; the server notification fact remains authoritative.

Terminal, service and cron calls to `agentstack-mailctl` and `agentstack-enroll` use the same selection. Global enrollment uses `inspect --agent-id ID`, or `claim/recover --agent-id ID --request-id UUID --expected-generation N`, over the existing management socket. Supply the new credential on stdin, never argv. The legacy enrollment executable runs only after legacy selection. A newly deployed controller missing its common loader stops with `GLOBAL_ENTRY_UNAVAILABLE` and requires redeployment.

Legacy remains the default and activation remains false. Asking the installer to switch a live installation to global returns `GLOBAL_INSTALL_REQUIRES_PR7`. Running isolated server configuration and changing the user default are separate stages. Isolated launch transport and termination recovery follow the lifecycle contract below.

The installer uses the existing literal settings reader for mode defaults; it does not execute other env.sh assignments or command substitutions. Empty or invalid modes are rejected.

### System controllers and explicit watchers

Hooks, workers, and directly sourced Codex/Gemini bootstraps preserve their running mode. Installer, agentctl, mailctl, enroll, and the persistent launcher are system entries that reread the installed default. An inherited mode that disagrees fails with `RUNTIME_MODE_INHERITED_CONFLICT` before changes. Explicit `--runtime-mode` arguments go to the same selector. From an old agent shell, remove inherited mode/config/context for a system operation, or supply the supported explicit arguments.

Installed legacy mode together with a leftover global server config fails with `RUNTIME_MODE_CONTEXT_CONFLICT`. Legacy session proof applies only to an unpinned process that encounters a global installed default, matching its name/session/project self binding. An ambient agent name does not block explicitly selected or inherited global operator access.

The normal resident watcher is config-only `watch-service`. A shell explicitly bound only to an owner context uses `watch`, sharing the same route engine and wait loop. This entry remains for context-only manual checks. Temporary connection refusal, missing socket, or timeout retries at the rescue interval. Authentication, binding, and response-contract errors stop without legacy fallback. Partial App installations also require the common loader admission; missing or unsupported helpers fail with `GLOBAL_ENTRY_UNAVAILABLE` and require redeploying the common client.

The legacy Mail runner executes the original service argv directly without the global selector. Global installer refusal precedes any Codex candidate execution.

Standalone App installs and marketplace payloads also bundle the canonical shared loader. They call the same admission against the core root, preserving standalone legacy installs.

Disabled global Mail hooks skip before reading owner context or credentials. Legacy hook skip behavior stays unchanged. The dashboard demo uses its synthetic legacy DB and excludes inherited global owner settings.

Codex/Gemini launchers pass selected mode/config/context into each new tmux pane. Direct bootstraps with a live project preserve its project/MCP URL; only an unpinned mode is supplemented through the shared selector. The watcher wrapper uses the selected config for both environment and `--server-config PATH` / `--server-config=PATH` forms. Unsupported select operations in old helpers use the same partial admission and fixed refusal reasons.

An unpinned continuation stops with `RUNTIME_DEFAULT_UNAVAILABLE` if its selector cannot verify an installed env.sh. Redeploy the helper and rerun the same entry. An unverified file is refused even if it says legacy; pinned legacy and the default legacy path without an env.sh remain available. The installer marker `--literal` matches one complete argument, never text inside a description.

<a id="explicit-tmux-target"></a>
## Explicit target inspection, attach and compact

`agentstack-runtime-client target --context /absolute/runtime-client.json --agent-id 2 --print` checks existing credentials and live evidence against the actual socket, session/pane and PID/start. It returns `state_sha256` and permitted next operations. It never falls back to a name prefix, case alias or the same pane number on another server. A standalone process outside tmux supports target inspection only. `capture --agent-id 2 --lines 40` uses the same context, accepts 1–1000 lines and reports truncation. Reads never create an operation mutex; unavailable locks fail closed.

Operators use `--operator --runtime-mode global --server-config /absolute/server-config.json`. Admission uses the same UID, private configuration and current binding, without registering another owner. `attach --agent-id 2 --read-only --print` returns the complete argv. Actual attach emits a pre-exec admission on stderr and then a terminal stream; it does not fabricate a completion receipt. Owner/parent attach is refused; child instructions belong in Mail.

A verified parent uses `send-compact --context /absolute/parent/runtime-client.json --agent-id 2 --confirm-state-digest <64-hex target hash>`. Operators additionally confirm yes on a TTY. Only the fixed `/compact` is submitted, after a final empty-idle prompt check. Work, approval, unknown evidence and a caller's own tool call refuse with `TMUX_TARGET_NOT_IDLE`; idle input containing any text refuses with `TMUX_TARGET_INPUT_NOT_EMPTY`. `submitted` means tmux submission, not provider completion.

**Known limitation:** Claude suggestions and placeholders also count as text, so compact may frequently be refused. No color-based attribution is attempted. Wait for a subsequent empty-idle prompt or let the provider compact automatically. Never clear the input with `send-keys` or switch launchers to bypass refusal.

The digest includes normalized context usage. An uncertain result after submission is exit 3 with `effects=unknown`, with no automatic replay or new journal. After target/capture inspection, an explicitly reconfirmed compact is a new operation and may duplicate an earlier submission if the screen and context value are indistinguishable. This differs from pre-effect `effects=none` refusals. The cooperative mutex does not stop human/provider input; a final observation-to-input race remains.

Legacy notification, monitor, OPEN and prune consumers use the same admission. Unproven targets have no effects while healthy targets continue. New isolated global launches use the dedicated transport described below. Existing sessions are not renamed or adopted; default cutover and provider configuration migration remain later activation gates.

The identity digest binds the socket, pane, PID/start and owner/parent proof for capture, attach and terminal open. Ordinary display updates do not change target identity. The compact target digest additionally binds input-field evidence and context usage, excluding prose, clocks and spinner updates. Human confirmation consumes no observation budget; a fresh short budget rechecks the same digest afterward. Actual ttyd and terminal children use the same helper to re-admit the endpoint and target. Cleanup retains an identity when observation is unknown. A failed terminal open after successful resume returns `TERMINAL_OPEN_FAILED` without undoing resume.

For `send-compact`, confirm the `state_sha256` returned by `target`. The digest returned by `capture` or `attach` proves target identity only and is not a compact confirmation value.

Only ENOENT from a direct `lstat` proves absence; EACCES and other I/O errors stay unknown. The parent directory must also grant owner rwx. The shared legacy consumer treats an absent socket or confirmed `no server running` on a safe socket as an empty inventory. Cleanup may retire when no live lease remains. Unsafe sockets, timeouts and other read failures stay unknown and retain the identity; observation never creates a server.

## Dedicated launch and termination recovery

A new detached isolated-global launch uses the dedicated socket derived from the instance and client HOME, with the exact agent name as its session name. A foreign exact-name or case-only collision returns `TMUX_SESSION_NAME_CONFLICT`; it is never renamed, adopted or overwritten. Existing UUID sessions and standalone evidence remain readable. Legacy defaults and the PR7 cutover gate are unchanged.

The existing launch stores the runner PID/start in `execution.runner`; the existing live slot stores the native provider PID/start. The runner waits, saves the exit outcome, then reports and cleans up. SessionEnd is not proof of process exit: SDK /clear must not retire a running provider. Provider exit, runner exit and unknown liveness remain distinct; unknown evidence preserves identity and reservations.

`target --agent-id ID --print` can return lifecycle evidence and `state_sha256` after exit or a failure before SessionStart. Recovery-only targets use null for unproven current PID/pane fields and cannot authorize capture, attach, compact or signals. A verified parent context or explicit operator continues through `replay-end --agent-id ID --confirm-state-digest HASH --expected-intent-sha256 HASH`; use literal `null` for the latter only when no pending intent exists. Unknown launch execution returns to `launch-resolve` instead of automatically relaunching.

The complete terminal report lives only in the existing mutation pending slot and replays the same UUID and bytes. Its successful receipt and termination phase are durable before that slot is cleared. Unknown results preserve active ownership; only confirmed outcomes permit retire/release and local cleanup. After a retire commit and interrupted local save, a verified parent/operator replay completes only the remaining work.

A retired identity with an unresolved report cannot resume ordinary sends. Existing `resolve-mutation` still checks its digest, old/new binding, IDs and explicit confirm; retired local acknowledgement additionally requires a verified active `--parent-context`, or `--operator` with the same private server config and a human TTY yes. It records local ack/abandon in the existing launch resolution before clearing pending. Read target again, then replay-end to finish cleanup. It never enrolls, revives or resends a retired identity.

CLI `end` shares the existing dashboard exit request: literal `/exit` and Enter to a verified pane, or SIGINT only to the verified headless Gemini provider PID. Submission does not prove termination. A normal provider without a proven pane refuses rather than falling back to SIGTERM.

A standalone without a launch also recovers through `target --print` and `replay-end` with the same digest. A residual ended launch does not replace its saved standalone session. Cleanup releases only its own ACTIVE leases and does not retire the owner. A lost release response replays the existing pending UUID. Other live or uncertain sessions and other pending operations remain protected; an incorrect pending hash is rejected with `MUTATION_PENDING_CONFLICT`.

Legacy bootstrap continues to reject the dedicated socket. Shared legacy target admission also reads runner PID/start and the live nonce from the existing launch, rejecting that global runner's pane even after the provider exits. No second PID record is added.

Neither owner nor operator treats an ended live slot as current liveness evidence. Only unresolved sessions participate in PID/start checks. Reuse of a completed session's numeric PID cannot block recovery, and an unresolved session's uncertain liveness never counts as absence.
