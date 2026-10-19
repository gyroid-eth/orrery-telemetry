# Hermes child adapter

This directory contains an offline feasibility probe, an operator-owned Mail
pilot and a thin managed-spawn adapter. Existing launchers and defaults are unchanged. The tested Hermes revision is
`d923a5eb487a17c0a68801bba9ef0f56c81040a8`.

`offline_probe.py` takes `--source`, `--site` and `--output` as explicit absolute
paths. Run it with the installed Python interpreter, `-I -B`, an empty inherited
environment, a private persistent `HOME` and `HERMES_HOME`,
`PYTHONDONTWRITEBYTECODE=1`, `HERMES_DISABLE_LAZY_INSTALLS=1`, and `UV_OFFLINE=1`.
It uses a dummy credential held only in memory, refuses socket connections,
checks actual builtin writes through Hermes's dispatcher, and starts a fixture
stdio MCP server. The fixture is not ORRERY Mail and produces no Mail receipt.
SDK client construction and the real CLI are deliberately outside this probe:
client construction can import bootstrap and prepare a borrowing home's
dependencies. The probe writes its private config without credential values.

`reservation_guard.py` maps Hermes's `path` argument and `read_file`/`write_file`
names to the shared attempt/task/receipt contract. It supports exact paths and
subtree `/**` patterns; other glob patterns deny. The tests exercise missing,
expired and wrong-owner receipts, root escapes, and startup without a guard.
The guard's two-tool policy is a fixture policy, not the final policy for
Hermes memory, skill management or curator. Those capabilities must remain
enabled and confined to the profile's private home in a production adapter.

The public common contract should own backend selection, credential references,
attempt/task state, receipt validation, task completion, process ownership,
reservation release and retirement. Hermes supplies configuration projection,
in-process hooks, provider/auxiliary transport enforcement, and turn/result
translation. Auxiliary routing defaults to the primary route; Hermes's per-task
auxiliary provider settings can also project a future optional separate route.
Use Hermes's named profiles under one persistent managed root: runtime
dependencies are shared while memories, skills, config and sessions stay per
profile. Temporary storage is used only for this spike. Initial managed policy
must pin cross-profile reads to the current profile and constrain external skill
directories, skill creation and trusted project directories to that profile.

The Mail pilot and reserved-file entry have separate operator acceptance records.
Controller integration is tested offline; a real managed-spawn round trip remains
operator acceptance work. Offline fixture success does not certify live operation.
Provider credentials must not enter shell-hook children. Fail-open observer
hooks cannot enforce the provider egress contract.

`runner.py` offers separate `init`, `preregister`, `configure`, `probe`, `start`,
`status`, `stop`, `retire` and `confirm-retire` commands, each taking `--state`. `init` also takes
`--config` and `--profile strata|deepseek`. The private operator config contains
absolute product/interpreter/worktree/managed-root paths, the canonical parent
and connection references, and readonly profile template paths. Credentials are
represented by the template's `key_cmd`; literal keys are never copied.
Keep that machine-specific config outside the repository.

The pilot calls the product preregistration helper with `program=hermes`, uses
`child_tools.py claude-config` and its unchanged `run-mcp.sh` proxy definition,
and compares remote `whois.program` with `hermes`. Local proxy `PROGRAM` remains
the generated value. Its official-SDK preflight distinguishes schema listing
from backend authentication. The owned tmux pane selects an explicit profile
and receives one fixed initial Mail task; no task text is evaluated by a shell.
Oneshot tool selection uses the configured server name `orrery_mail`; the
`mcp-orrery_mail` registry name is not valid before MCP discovery. The adapter
checks the selection before start and resolves its dependency interpreter from
the managed root's current `facts.json`, so an explicit package install can
select a new generation without retaining the previous interpreter path.
Main and every auxiliary task use the selected named provider with empty
fallback chains. Memories, skills, review and curator remain enabled.

Only the exact parent message is exposed through MCP. An in-process plugin
records its structured product receipt, rejects an automatic second send and
cross-profile selectors, and confines explicit skill paths to the profile.
Profile paths and configured shared skill directories are checked before start.
The supervisor passes a fresh environment, sets offline package preparation and
lazy-install suppression, bounds the process group and records its exit.
Dependencies in the separate managed root must already be prepared by the
operator. These flags do not certify every bootstrap recovery path as inert.

`status` accepts only a matching verified Mail receipt. Exit zero and model prose
do not count; `full_managed_task_accepted` stays unknown. `retire` first requires
observed owned-group absence, invokes the product cleanup hook, and leaves its
receipt unconfirmed until the parent's bound `whois` confirms `retired_at`.
Save that canonical result privately, then run `runner.py confirm-retire
--state STATE --input WHOIS_JSON` (also available through `mail_lifecycle.py`).
Only the same child name with a valid nonempty `retired_at` confirms the existing
cleanup receipt; the original cleanup exit status is retained. Never-started
attempts can run cleanup without inventing a runtime exit. A partial tmux start
needs observed absence of the exact session on the saved socket, and a started
supervisor still needs an observed owned-group exit. Pending cleanup is resolved
by parent `whois`, without automatically running cleanup again.

Configure checks the prior profile's confirmed retirement before changing it.
A saved product `mcp.json` may be reused only if every bound reference matches
this attempt and profile writing has not begun. `profile-write-intent.json`
marks that boundary; a later partial failure requires retirement and a fresh
attempt. Do not delete an intent or generated file to force a retry.
The Mail-only pilot takes no file reservations. Reserved-file runs use the
receipt and completion flow below.

`init-task` without `--input` prepares the original reserved `trial-ok.txt` trial.
With `--input TASK_JSON`, it accepts the managed contract's flat object containing
`prompt`, `allowed_paths` and `expected`. Paths are unique relative literal file
paths; each expected file has exactly one string `contains` or lowercase SHA-256
`sha256` check. The child must create or update the file after this attempt begins.
Requested artifacts are not seeded. Symlink artifacts cannot satisfy completion.
Use `preregister`, `configure`, `probe`, `reserve`, `preflight`, then `start`.
Reserve uses the bound product proxy and projects its real receipts into
`task.json`. Preflight checks the installed plugin manager, and the inference
entry checks that same callback again in its own process before importing the
CLI. No shell-hook approval or a ready-file claim substitutes for registration.
`orrery_file` is a small runtime toolset exposing the real builtin read/write
tools. The synchronous guard rejects outside, missing and expired reservations,
while private memory/skills/curator remain enabled.

The task entry installs a synchronous HTTPX transport policy before provider
resolution. Only the configured Messages or Chat Completions endpoint is allowed;
redirect targets and unmanaged Python socket/DNS operations are checked too.
Every provider attempt is recorded without headers, bodies or credential values.
Denied HTTP requests record method, host, port and path without query, fragment
or URL credentials, plus source locations without source text or local variables.
This is not an OS sandbox for arbitrary descendant processes. Provider behavior
on the real selected endpoint still needs operator acceptance.

Hermes can reload profile, source-project and managed dotenv files. This entry
requires the profile/source candidates absent and denies subsequent `.env` and
`.op.env` opens. The managed adapter returns Hermes's empty mapping before
opening a missing dotenv file; it records that absence and the caller. Existing
files and dangling symlinks still reach the denying guard. The named provider
keeps its key-command reference. Native
oneshot completion flags are translated into common turn events without saving
raw exception strings from the original usage writer.

An optional positive integer `profiles.<name>.context_length` pins the main
model, the named route and the exact `model_overrides` entry. Route pinning also
covers auxiliary callers after Hermes resolves the provider name to `custom`.
This prevents context detection through model-list or local-server probe paths;
it does not allow those paths through the transport policy. No output limit is
added. Profiles without an operator-supplied context value keep Hermes's normal
detection behavior and may be denied by the managed policy until configured.

The managed entry also enforces `model_catalog.enabled=false` for the pinned
runtime metadata fetchers. Hermes's pricing path otherwise fetches endpoint
model lists even with that setting. Endpoint and OpenRouter metadata fetchers
return empty metadata before transport; existing pricing and prewarm aliases
are updated too. Unpriced custom usage remains `unknown`, rather than zero.
Static known pricing, token counts, memory, skills and auxiliary inference stay
enabled. Suppression is recorded separately; any actual blocked request still
fails the completion check.

There is no default request cap. A trial run-config may explicitly set
`provider_request_limit` to a positive integer, with no fixed upper bound.
When set, it caps shared main/auxiliary HTTPX requests; the next request is denied
before transport and records `limit-reached`. Retries count toward this trial cap.
An operator can select a native Anthropic-compatible endpoint through the route
fields without pinning context. Verify context resolution offline for that
provider/model first. The pinned billing resolver may call a named endpoint
`custom` and leave its price unknown even when a native-provider snapshot exists;
metadata suppression does not manufacture a price or a zero-cost result.

The five-condition evaluator reads only saved state. After the parent's bound
inbox result is saved, `record-mail --state STATE --input INBOX_JSON` requires
the actual same-child message ID, `kind=to`, and the inbox shape without a `to`
field. `complete` accepts the server's verified send receipt, including a positive
delivery count, bound to this attempt, task, child and parent. If a parent receipt
exists it must also match; malformed or conflicting evidence fails. Unknown
evidence stays pending; exit zero is never task proof.
Stop the owned group, run `scan-diagnostics`, `release`, `retire`, save the parent's
canonical whois, run `confirm-retire`, then `complete-runtime`. Release confirmation
requires the exact task IDs and the full actual release count.

The default pilot's `scan-diagnostics` is an explicit operator step. Its
key-command output stays in the scan process. Managed `owned-once` mode instead
reuses the credential already held by the owned process, as described below.
Only prefix match/error/file counts are saved for state and private profile logs.
Owner-token files are excluded; keys, prefixes, matching lines and filenames are
not printed. Real CLI, provider, Mail and cleanup remain operator acceptance work.

## Managed controller entry

`managed_lifecycle.py` exposes `prepare`, `start`, `status`, `finalize` and `clean`
with the managed-spawn contract's injected `run` and existing `parent_call`.
`prepare` performs init, preregistration, projection, authenticated proxy probe,
reservation and guard preflight. `start` launches the existing owned tmux wrapper.
Its exit status is not readiness: `status` requires the actual runtime PID's
plugin-ready event, current preflight/proxy evidence and owned credential receipt.
Terminal status waits for the supervisor's runtime-exit receipt: turn-end precedes
the final diagnostic scan and must not trigger an early stop.
HTTP attempt IDs remain controller-owned; `runtime_attempt_id` is the runner ID.

Profiles stay fixed by child kind to preserve private memory, skills and curator.
An installer-owned generation may use its `profiles` symlink only when its
complete Hermes receipt binds the selected root/source/executable and the link
targets that harness's persistent sibling `profiles` directory. Manual/live homes,
other targets and a symlink for an individual profile are rejected before reading
profile content. Normal private homes without a profiles symlink still work.
The controller must reject a concurrent request for the same Hermes profile
before registration with `profile_busy`; it does not queue or create an attempt
profile. Cross-profile reads and external skill directories remain confined by
the current-profile guard. Optional `profiles.<name>.reasoning_effort` projects
into Hermes's `agent.reasoning_effort` (`none` disables reasoning).

Managed preparation selects `credential_mode: owned-once`. After synchronous
gates pass, the owned process executes the validated Keychain reference once.
It supplies an in-memory command-token provider to main and auxiliary clients;
the key is not exported to tool, MCP or shell-hook environments. An exclusive
read-intent receipt prevents automatic rereads after failure. The same in-memory
key scans logs before Hermes's oneshot hard exit. A killed process without scan
evidence remains incomplete; a later diagnostic command never rereads its key.

`finalize` stops the owned group, checks diagnostics, releases actual grants
(including partial reservation responses), retires the child and confirms with
the existing parent's canonical `whois`. A saved canonical `parent-inbox.json`
can supply stronger D2 Mail evidence; the adapter does not fetch or poll a parent's
inbox. Stage 1 accepts the verified server-send receipt without a parent capture.
Never-started attempts can
be retired without fabricating a runtime exit. `clean` requires successful
runtime cleanup and a current zero-match, zero-error credential scan, then removes
attempt handoff files. It preserves state evidence, requested artifacts and the
persistent profile's memory and skills. Unknown cleanup retains state.

A pre-launch failure with a same-attempt parent-confirmed `not-started` retirement,
an empty reservation list and no launch, credential-read or reservation-intent
records closes as `runtime_conclusion: not-started-retired`. Group absence and
release fields remain null because neither resource existed. Clean can then
remove its handoffs without a diagnostic/key read; task failure stays failure.
An ambiguous launch or reservation attempt remains pending.

After deploying this version, an operator can close an existing such failed job
with `python3 -B dashboard/managed_spawn.py --job JOB --finish-unstarted`. It rechecks saved
proof, invokes only complete-runtime/clean and marks successful cleanup
`cleaned_after_failure`; it never registers, starts or sends Mail. Normal worker
failures use the same proof automatically. Failed CLI steps provide fixed reason
codes; `worker.log` and `worker-failure.json` retain stage/code without exception
contents, paths, command arguments or request bodies.

## One-command offline check

Prepare dependencies separately, then run all Hermes tests serially with:

```sh
scripts/check-hermes.sh --config PRIVATE_RUN_CONFIG --out RESULT_JSON
```

The trusted private config supplies the installed source and managed root.
The command resolves the committed dependency interpreter, creates an isolated
temporary HOME and offline cache, and includes the real stdio/tool-dispatch SDK
fixtures without real provider, Mail, registration or Keychain calls. It does
not install or repair dependencies. Missing setup fails the command.

The JSON result includes `success`, `tests_run`, `failures`, `errors`,
`skips`, `allowed_skip_reasons` and `unapproved_skips`. Skip records contain
`test` and `reason`; approved reasons are strings matched exactly.
`check-skip-reasons.json` defaults to `[]`, so every skip fails. An explicitly
reviewed list may be passed with `--allowed-skips FILE`.

Run the focused dependency-free checks with:

```sh
python3 -m unittest discover -s tests -p 'test_hermes*.py' -v
```

Set `ORRERY_HERMES_TEST_PYTHON` to the installed dependency interpreter to include
the offline SDK stdio fixture; also set `ORRERY_HERMES_TEST_SOURCE` to the tested
checkout to exercise real Hermes discovery and tool dispatch through that fixture.
Neither fixture makes a provider request or connects to a real Mail backend.
