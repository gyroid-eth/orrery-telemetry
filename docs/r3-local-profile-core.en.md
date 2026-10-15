# R3 local-model core — part 1

[日本語](r3-local-profile-core.md) · [Contract fixtures](r3-local-profile-fixtures.md)

This stage adds isolated modules for `local:<name>` validation, immutable,
non-secret intent/configuration projections, and a shared authentication runner.
**No existing child-launch path is connected.** It does not enable local models
in spawn_child.sh, runtime delivery, dashboard, installer or user settings/trust.

## Boundaries and private API

| Module | Owns | Does not own |
| --- | --- | --- |
| `bin/lib/local_model_profiles.py` | Explicit operator-root loading; schema, UID, mode, link, endpoint and environment validation; whole-profile digest | Ambient HOME/cwd lookup, cloud/catalog fallback, approval/deployment |
| `bin/lib/runtime_launch_intent.py` | Normalized caller plus approved context; immutable intent, full digest, read-only drift checks and common argv/env/config projections | Owner persistence, revisions/pending, native launch envelopes, permission engine or R2 receipt decisions |
| `bin/lib/local_model_auth.py` | Bounded subprocess in a fixed context; runner/dependency validation; helper reading an existing owner | Credential env/storage, another auth ledger, registration/provider launch or automatic deployment |

`resolve_launch_intent(caller, context)` requires operator-owned configured
context, never HTTP input or the child's ambient environment. effective_scope
is supplied by the existing approved tool/configuration plan. This core does
not parse native permission rules or grant new permissions; source labels do
not choose different resolvers.

`materialize_launch_intent(...)` rechecks profile, settings sources, approved
scope and runner resources without writing. execution is a **partial common
projection**, not a runnable startup/resume command. inherit_env=false and
requires_native_validation=true are explicit. Consumers must preserve the
existing fixed provider HOME/safe base env, tools/MCP/skills and native resume
envelope, validate final credential/provider/settings conflicts, and prove
native scope enforcement. dontAsk alone is not a permission ceiling.

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

Deployment belongs to subsequent explicit configure. The runner is one stdlib
source file, deployed 0500 under a 0700 parent with an approved absolute
interpreter and `-I` isolated-startup shebang. Caller PYTHONPATH/user site must
not affect runner imports. No installer/instruction_configure wiring or automatic
deployment is added. Refresh is projected as 300000 ms; real CLI refresh/header
behavior and absence of inherited login remain native gates. Tokens are fetched
by helpers, never inserted into argv or environment.

The [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
defines Codex command stdout, cwd, timeout and refresh. Authenticated definitions
do not combine auth with env_key, a literal bearer, or requires_openai_auth.
Context minus output reservation produces the compact limit; no invented
model_max_output_tokens key is emitted.

## Red-to-green map

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
