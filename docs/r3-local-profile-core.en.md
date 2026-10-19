# R3 local-model core — part 1

[日本語](r3-local-profile-core.md) · [Contract fixtures](r3-local-profile-fixtures.md)

This stage adds isolated modules for `local:<name>` validation, immutable,
non-secret intent/configuration projections, and a shared authentication runner.
Part 2 is connecting explicit configuration of the fixed global-client owner
and R2's read-only consumption checks. **Native launch admission remains
unsupported.** This does not make local models operational in spawn_child.sh
or the dashboard. Full shell/dashboard callers are connected to delivery-only
fixtures; operational user settings/trust changes and native permission/skill
loading acceptance are not complete.

## Boundaries and private API

| Module | Owns | Does not own |
| --- | --- | --- |
| `bin/lib/local_model_profiles.py` | Explicit operator-root loading; schema, UID, mode, link, endpoint and environment validation; whole-profile digest | Ambient HOME/cwd lookup, cloud/catalog fallback, approval/deployment |
| `bin/lib/runtime_launch_intent.py` | Normalized caller plus approved context; immutable intent, full digest, read-only drift checks and common argv/env/config projections | Native launch envelopes, permission engine or R2 receipt decisions |
| `bin/lib/runtime_caller_schema.py` | Shared structural validation; preserve omitted versus explicit input | A second model/tool policy or operator configuration input |
| `bin/lib/runtime_intent_store.py` | Fixed owner and existing lock, identical replay, explicit revisions protecting pending operations | Registration, a second ledger or provider launch |
| `bin/lib/runtime_local_launch.py` | R2 owner/receipt rechecks, additive projection into existing envelopes/env, separate native admission gate | Automatic configure, native validation/approval, another launcher |
| `bin/lib/runtime_model_catalog.py` | Delegate existing Codex/Claude policy; recheck saved formal ID/effort | Another model/alias table, default/alias reselection on resume |
| `bin/lib/local_model_auth.py` | Bounded subprocess in a fixed context; runner/dependency validation; helper reading an existing owner | Credential env/storage, another auth ledger, registration/provider launch or automatic deployment |

`resolve_launch_intent(caller, context)` requires operator-owned configured
context, never HTTP input or the child's ambient environment. effective_scope
is supplied by the existing approved tool/configuration plan. This core does
not parse native permission rules or grant new permissions; source labels do
not choose different resolvers.

`materialize_launch_intent(...)` rechecks profile, settings sources, approved
scope and runner resources without writing. execution is a **partial common
projection**, not a runnable startup/resume command. Local-profile inherit_env=false and
requires_native_validation=true are explicit. Consumers must preserve the
existing fixed provider HOME/safe base env, tools/MCP/skills and native resume
envelope, validate final credential/provider/settings conflicts, and prove
native scope enforcement. dontAsk alone is not a permission ceiling.

## Part 2: input and the sole owner

This private contract is being connected; it does not declare local children
operational. All callers use the same structural check. Existing catalog policy
and local-profile validation remain distinct.

| Caller field | Storage and interpretation |
| --- | --- |
| provider / model | Normalize provider aliases only; resolve model to a tagged catalog / local-profile value. Registration metadata uses the real local model ID. |
| cwd / task / name / parent | Preserve the explicit workspace, task reference, target and parent. Never use them to discover roots or identities. |
| tools / base / clear_tools | Preserve empty selection versus explicit clear. Existing tools policy interprets the result. |
| resources / worktree / chrome / detached | Preserve explicit values; profiles cannot select reservation, placement or transport owners. |
| effort / task_delivery / strict_output_cap | Keep omitted keys absent, distinct from empty strings or explicit false. Reject unsupported requests rather than ignoring them. |

An explicit `parsed_tools` selection in the global prepare/resume Python APIs
also passes the existing tools grammar before registration, native admission or
locks. Unknown `mail` / `shell` names fail with `CHILD_TOOLS_INVALID`; valid
nonempty MCP selections are not replaced with empty ones. The legacy preview's
nonempty-tools refusal remains a separate restriction.

The complete intent has one owner: global `profile.json.launch_intent` or legacy
`child-agents/NAME.json.launch_intent`. Re-saving an identical intent leaves
bytes unchanged. The existing profile writer preserves intent and execution.
New revisions require explicit confirmation and refuse registration/enrollment/
mutation pending, nonterminal launches or incomplete ending/report/cleanup.
Receipts never duplicate the complete intent.

Named purge/reregister wrappers compare the requested name exactly against the
canonical name of the normally selected fixed context using non-creating
admission. They never discover another credential or substitute implicit self.
Existing fresh-owner checks still apply after a match.

| Rejection | Fixed reason |
| --- | --- |
| Global mode without a context (checked before the target argument) | `CONTEXT_UNAVAILABLE` |
| Bound global context, but missing or invalid target argument | `TARGET_UNAVAILABLE` |
| Context canonical name unproven | `GLOBAL_TARGET_OWNERSHIP_UNKNOWN` |
| Requested name, credential or fresh owner mismatch | `REGISTRATION_IDENTITY_MISMATCH` |

Malformed contexts retain existing path/identity errors, without creating a new
root or falling back to legacy. Known name mismatches stop before network,
purge or reconnect.

## Profiles and digests

Root: absolute, operator UID, 0700. Profile: same UID, regular 0600, one link.
Descriptor-relative NOFOLLOW traversal rejects symlink/unsafe writable
ancestors, except OS-owned sticky temporary directories. Reads are bounded at
64 KiB. Names match `[a-z][a-z0-9_-]{0,47}`. Unknown fields, empty/control-character
models and invalid numbers fail closed. Window/output are signed-64-bit positive
TOML integers with output < window; this declaration is not measured CLI context.

Remote endpoints require HTTPS; userinfo, queries and fragments are forbidden.
HTTP is only permitted for unauthenticated loopback. Codex profile env is empty;
Claude permits only CLAUDE_AUTOCOMPACT_PCT_OVERRIDE in 1–100. No separate model,
credential, endpoint, HOME/PATH, identity or loader overrides are allowed.

Profile digest covers the entire validated profile. Intent digest excludes
only intent_digest itself. Both hash canonical UTF-8 JSON with sorted keys,
no whitespace and preserved array order. Both adapters, auth argv and approved
runner/dependencies/context, scope and settings digests are covered. Credentials,
cache contents and file timestamps are not stored.

## CLI version comparison (separate from native acceptance)

The comparison accepts JSON snapshots of the approved and observed CLI,
effective scope, non-CLI resources, and model/endpoint/auth/context derivation.
It does not execute or discover a CLI, credential, or filesystem resource.
A different version may retain the same approval when canonical path, UID,
installation origin and safe executable metadata remain valid and normalized
scope and other derivations match exactly. The result records both versions
and executable digests without rewriting the approved intent. Same-version
byte drift, another path/origin, other-writable executables, and non-CLI
resource drift are refused.

Only explicit version provenance is removed, after checking the native
proposal's own digest. Native settings, built-ins, source closure, unknown
effects and list ordering remain significant. Boolean, integer and floating
JSON values are not conflated. There is no custom Bash interpretation or rule
reordering. This is a comparison fixture contract, not evidence that supplied
observations are correct or native permissions are enforced. Real CLIs without
an adopted native observer remain unsupported; a comparison result alone
cannot admit a launch.

The common producer's proof is rechecked at existing R2 prepare, execute and
provider-exec boundaries. Only metadata/digests join runtime/launch.json's
cli_acceptance field. Explicit configure pins the safe canonical executable's
parent directory and UID as filesystem origin evidence, not proof of package
provenance or native capability adoption. Old intents without this evidence
retain strict same-byte CLI checks.

The standalone auth helper reads the same owner/proof and active UUID, nonce
and phase. Source-pinned shared process observation checks the existing runner
PID/birth and actual same-UID helper-to-provider-to-runner ancestry twice.
Chronology uses fresh explicit UTC observations for every process and permits
same-second births; the saved zone-less birth is used only for identity.
Unknown observation, PID reuse, reparenting or stale receipts refuse before
credential retrieval. No second provider PID ledger, approved-plan rewrite or
ordinary-launch configuration is added.

This stage's TEST binding replaces only one native gate with comparison
observations. It proves connection, not actual CLI continuation, helper TTL,
headers, permission/skill enforcement or native 38 acceptance. Public caller
version notices and adopted CLI observation/measurement remain later gates.

## Authentication projections

For authenticated profiles, the consumer explicitly supplies the existing sole
owner in context.intent_owner; no guessed owner or second auth file is created.
Codex command-backed auth and Claude's safely quoted apiKeyHelper reference the
same runner and owner/digest/provider. Before fetching, the helper rechecks
owner, runner/dependencies, profile and settings. Credential commands use fixed
cwd, PATH=/usr/bin:/bin, LANG=C, and approved HOME/USER/LOGNAME only. Deadline is
5 seconds, stdout is bounded to 8 KiB with one optional trailing LF, stderr is
discarded, and errors contain a fixed reason. Oversize/timeout cleanup terminates
only the process group created by the runner.

Deployment belongs to explicit configure. The runner is one stdlib
source file, deployed 0500 under a 0700 parent with an approved absolute
interpreter and `-I` isolated-startup shebang. Caller PYTHONPATH/user site must
not affect runner imports. The installer copies helpers, not a deployed runner.
Only the explicit configure below publishes approved resources into the existing
manifest. Refresh is projected as 300000 ms; real CLI refresh/header
behavior and absence of inherited login remain native gates. Tokens are fetched
by helpers, never inserted into argv or environment.

The [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
defines Codex command stdout, cwd, timeout and refresh. Authenticated definitions
do not combine auth with env_key, a literal bearer, or requires_openai_auth.
Context minus output reservation produces the compact limit; no invented
model_max_output_tokens key is emitted.

## Part 2: explicit configure (integration in progress)

The existing operator instruction-configure entry has local plan/apply for an
isolated global client. Ordinary launch/resume and HTTP model input never run
configure. The existing two-argument provider-instruction operation is unchanged.
This local operation does not approve Codex hook trust.

```sh
agentstack-runtime-client instruction-configure operator codex local-plan < request.json > plan.json
agentstack-runtime-client --assume-yes instruction-configure operator codex local-apply PLAN_ID < plan.json
```

The request has only caller, context and resources. Context accepts the explicit
root, effective_scope, approved settings digests and minimal auth_context. The
CLI supplies the current UID and fixed owner. Resources contain absolute Python
and CLI paths, a declared CLI version, settings_paths, native_sources path/layer
records, skill_sources and optional nonsecret auth_dependencies. Unknown keys and
owner/UID injection are rejected. Planning creates no file, lock or bytecode;
apply requires the same plan ID and explicit advance consent via --assume-yes.
Ordinary launch cannot introduce a new plan or silently approve broader scope.

The full intent stays in the same profile.json. A typed prepared stage handles
pre-registration without inventing an agent ID, credential or registration.
Task hashes bind the source or the fixed retained task copy. Changes check idle/
pending state under existing C/fence and manifest mutexes before publication.
Identical settings replay read-only. Outputs join existing install-state.json;
unknown artifacts are never adopted and external auth/skill sources are not
owned or deleted. Actual uninstaller wiring remains a later stage.

The native configuration proposal is a strict subset: unknown effects are
unsupported, not erased. Nonempty inherited Claude allow/deny/ask and unknown
hooks/plugins/environment are refused. Bash is passed as native rule strings,
not parsed or simulated by a custom permission engine. Extra allows are not a
permission ceiling. Codex workspace-write is not a per-file/per-command policy.

There is no adopted native capability contract yet. Version is declared, not
measured; a binary hash and present/absent settings hashes do not establish
complete managed/cloud source discovery. Built-ins and source closure remain
unverified in the same intent, with requires_native_validation=true and
launch_admissible=false. A self-asserted verified flag cannot promote this.
**Configured means resources were published, not native acceptance or an
operational local child.**
Real CLIs with unestablished capabilities stay unsupported; native 38 stays red
until the adopted version is measured. Later caller 16 evidence from the fixed
driver proves argv/env delivery only, not native permission or settings
enforcement. These remain separate in QA and release claims.

## Explicit legacy preview configuration (preparation only)

An operator process normally selected as legacy can use the same
`instruction-configure operator claude local-plan/local-apply` surface. This is
not a way around global-context refusal. Existing provider-instruction
configuration and global plan/apply remain unchanged.
Add explicit execution_policy and legacy_context to the existing context fields.
The former is `{kind:"local-preview-v1",native_scope:"unverified",native_isolation:"unverified",operator_decision:"nonsecret-reference"}`.
The latter is a closed schema: schema_version=1, canonical install_root,
runtime_dir and owner, candidate name, project_key/project_id, mail_url, and the
fixed parent's id/name/program/model. Obtain a candidate from the existing name
picker first; no existing-registration selection is added.

The initial preview supports tools={} and base=default|mail-only for Mail,
reservations, shell and Skill. External MCP servers are unsupported; nonempty
tools are refused before effects, never silently discarded.

Only a match between the operator's existing AGENT_NAME, caller.parent and
target.parent permits reading the canonical parent credential. Require Mail's
existing read-only register_agent(existing_agent_id=...) owner branch; whois
only supplies an ID candidate. This never creates a registration, claims an
unowned row, starts a provider or fetches a key. Tokens do not enter plans or
results. Planning is read-only; apply requires the same plan ID and --assume-yes.
The same child-agents/NAME.json stores a prepared intent; the configuration
result still reports launch_admissible=false.

The private provider HOME's CLAUDE.md is another output of the same explicit
plan/apply. It uses the fixed install template and shared instruction renderer;
template/source digests and output ownership join the existing manifest.
Ordinary user instructions are not implicitly copied, migrated or overwritten,
and existing unowned files are not adopted. The internal registration consumer
also rechecks generated instruction bytes, private mode, regular-file type and
single link. This is resource evidence, not actual CLI loading, effective skill
visibility or launch acceptance. Proxy and actual hook/launch connections are
still in progress.

The same approved outputs include local-model-mcp.json in the private HOME.
Mail names come only from the approved scope and canonical orrery-mail; no
token enters command/args. Fixed existing proxy sources and the explicit
Python path/UID/mode/bytes join the same intent's evidence, even for profiles
without auth. This is not evidence of Python execution or a working proxy.

The internal preview proxy factory reuses the existing stdio MCP server,
direct binding and HTTP client, without a second identity/snapshot ledger.
Every call rechecks the same registered owner and active launch, with same-UID
helper-to-provider-to-runner ancestry and birth observations before and after
resource checks. Owner, source, settings or generated-output drift refuses
before Mail effects. Profiles without auth use the same checks; credentials
never enter tool/status results. This is delivery/checking fixture evidence
with fake OS/peer only; it does not by itself prove actual provider startup,
hooks or native acceptance.

Internal `entry runner|provider|proxy NAME` consumes only the same registered
owner. Runner takes the fixed intent digest/registration generation; provider
takes only this launch ID/nonce, never arbitrary argv/env/owner/observer input.
P persists unknown before creating Q and stays alive through wait. Even if
owner/token/parent observation validation fails after spawn, P waits/reaps its
actual child before returning failure; unknown is not rescued into ended.
Q rechecks
its actual same-UID parent, birth and resources before same-PID exec. The
existing pure provider argv projection is reused; ambient credentials are not
inherited. Isolated Python (`-I`) resolves only the fixed installed sibling
library. Tests cover fake P/Q/exec plus actual Python status/refusal paths,
not real provider startup, hooks, remote cleanup or public spawn acceptance.

Internal `entry session-start|end-session NAME` records only a session receipt
with the same active owner/process proof before/after and the owner CAS.
Same-session repeats are idempotent; another session, End before Start and
Start after End are rejected. End alone does not complete wait, lease release
or retirement. Failed post-save proof does not roll back facts into success.
Legacy hook Python matches the explicit configure runtime Python pin; the
ordinary global renderer is unchanged. Notification hooks, public launch
and termination joins remain pending, not actual native hook acceptance.

Internal pre-edit verifies the same active session and uses the ordinary
900-second renew and shared conflict reader for protected-project paths.
Relative paths use the saved cwd. Missing leases, foreign exclusive leases,
uncertain observations and service failures refuse the edit. The initial
preview's post-edit only rechecks the active session: it does not use the
ordinary child's 90-second automatic release. Leases remain until explicit
Mail release_reservations, ordinary expiry or the later termination join;
other writers may remain blocked for that TTL. TTL/renew rules are not
extended, and ordinary child hooks are unchanged. Fake-peer/OS checks do not
prove native hook enforcement or completion of the termination join.

Registration and auth use the same reader to recheck generated instructions
and MCP/native settings plus selected skill links. Auth repeats it before and after process observation,
including TTL refresh. Shared renderer/reader code
comes from validated sources in the fixed install, not an unchecked module
cache, another rendering implementation or automatic resource repair. Changed
or missing output and type/mode/link mismatches refuse with a fixed reason
before running the credential command.
Skill directories require the exact entry set and each link's own UID,
single-link count and target; enumeration failure refuses. Native settings use
the existing shared pure projection, not another settings renderer. This is
generated-resource drift checking, not native permissions or effective skill
acceptance.

Preview does not approve the normal native gate or verify permissions,
isolation or CLI capability. Registration has the ordinary-child guarantee,
not atomic create-only. Execution records retain
`registration_guarantee={level:"ordinary-child",atomic_create_only:false}`.
Public preregister/spawn/dashboard success paths and actual hooks are still
being connected: successful configuration alone does not make a local child
operational.

The internal fixed helper exposes only `status NAME` and
`resolve NAME --expected-state-digest DIGEST --action finalize-registration|abandon`.
It is `bin/lib/runtime_legacy_preview_cli.py` in the selected install, not a new
launcher. Status is read-only and omits credentials. Explicit finalization
reauthenticates the fixed parent and the saved child through the existing
`existing_agent_id` owner-auth branch; a lost response does not trigger another
new registration. A different ID/model, refused authentication or stale digest
leaves the unknown state in place. Abandon never makes an unknown execution
successful or frees its name for automatic reuse.

Ordinary resume/undo/cleanup/register consumers refuse these reserved fields
and tagged pending records. Canonical ordinary credential publication and final
local cleanup use the same per-name resume lock, rechecking after remote effects;
failed credential persistence is not reported as successful registration.
These protections and successful finalization do not approve provider execution.

## Part 2: R2 consumer integration in progress

Initial planning, retries and runner/provider-exec boundaries recheck the same
fixed profile intent. Launch receipts add an intent_digest reference and, only
when the comparison binding returns an observation, cli_acceptance containing
old/new CLI metadata and comparison digests in the same launch.json. They do
not copy the full intent, scope, authentication configuration or provider PID.
Task hashes and
profile/CLI/active-and-fixed renderer/settings/runner/scope drift are rejected;
an old receipt cannot silently adopt today's intent. Registration, refresh and
finalization also check admission before network effects.

Shared functions compose the minimal local environment and existing argv
envelope. Ordinary launch neither regenerates resources nor runs auth helpers;
Claude references the same explicitly deployed settings. These are configuration
checks before the separate native gate, not proof of unestablished capabilities.
Pure catalog resolution delegates existing policy. Old global profiles without
an intent retain R2's native default; a read never creates a catalog intent.

Catalog tags follow the designed {kind:"catalog", requested, id} shape. A
**design addition** stores resolved effort in model.effort and never resolves
its default again on resume (Claude remains None). intent.effort retains the
caller's omission/None/empty/explicit input. Shared readers only read the tag;
they are not another resolver. An unknown Codex model's omitted effort is saved
as an empty string, distinct
from a missing saved field; resume adds no effort argument. This preserves argv
and delegation to the CLI, not the effort tier chosen internally by the CLI.
Read-only admission of interrupted registration
validates existing pending evidence without creating locks or credentials.
Actual continuation/credential activation retains the existing C/fence and
owner checks.

Beyond full-caller delivery wiring, legacy execution entries, UI and real uninstall acceptance are
later work. Fixed-driver delivery evidence is not native 38 enforcement evidence.

## Part 2, internal stage 1 QA (before launch integration)

The six existing persistence/revision/pending reds are closed. Existing related
regressions remain 502 passed before and after; R3 moves from 282 passed plus six
failures to 288 passed. Including 58 new storage/schema/real-wrapper cases, the
final run is **848 passed / 43 missing-adapter errors / two known sandbox cases
deselected**. The remaining errors are caller 5 and native 38, not a green suite
or proof of operational local children. A context rename between offline target
admission and mutex acquisition is rechecked before fresh network access.

## Part 1 completion: historical red-to-green map

Serial QA: **340 passed / 6 out-of-scope cases deselected**: 122 newly-green
#266 core cases, 40 additional core/runner cases, 120 unchanged measurement/mutant
controls and 58 publication checks. Start load1 was 12.98. Table counts are #266 cases.
Full R3 run: 282 passed, six expected persistence/revision failures and 43
missing-adapter errors. No unexpected core failures; the complete suite is NOT green.
The three existing related regression files passed all 286 cases before and
after, with the same two pre-existing Authlib warnings.

| Existing cases | Part 1 target | Remaining boundary |
| --- | --- | --- |
| Profile 67 | Green validation/digest | Configure and CLI capabilities remain separate gates |
| Auth 15 | Green shared core | Not native helper scheduling/TTL |
| Intent 40 | Green pure hashing/projection/input-drift | Not real caller equivalence |
| Intent 6 | Remain red | save/load/revision/pending and existing owner integration absent |
| Native 38, caller 5 | Remain red, missing adapters | Real CLIs/entries/hooks/Mail/skills/lifecycle |
| Instruments/review controls 120 | Run unchanged negative controls | Measurement success is not native success |

test_core_projection.py consumes actual generated TOML/settings, executes the
real runner deployed explicitly inside the fixture, and sends model/path/header,
tool turns and rotation to fake TLS peers via a **test HTTP consumer**. It is
neither a native CLI substitute nor a product protocol gateway. Claude headers
are explicitly chosen by the harness, not inferred CLI behavior. Existing
contract expectations and mutant controls remain intact: no skips or xfails.

Run serially in repo-local venv with isolated HOME/TMPDIR/runtime, only starting
when load <15. Run the existing 286 relevant regressions before and after. No
live service, actual Keychain credential, real API key or native CLI is used.
