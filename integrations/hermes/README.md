# Hermes adapters

## Thin native launch

`thin_config.py --input INPUT --out PLAN` generates an upstream Hermes command,
configuration and environment references without invoking a credential helper.
`validate_input(document)` is the read-only admission used by the standard
launcher and catalog. It imports no Hermes code and starts no subprocess.

The standard launcher supplies the registered identity, product-generated MCP
configuration, shared rendered Codex instructions and the actual working directory.
The generator selects the installed committed executable and a custom provider
with native `key_cmd`; provider credentials remain Hermes's responsibility.
Each child uses a private provider name and minimal route in its overlay, so
old endpoint aliases, inline keys and named credential pools in the persistent
profile cannot override the selected route. Route projection does not rewrite the profile model route or copy its auth store. Legacy model credential pointers are cleared only in
the child overlay; memory and skills remain in the stable private HOME for each project and canonical child name.
Auxiliary tasks and delegation use that same private provider, with old direct
endpoint and credential pointers cleared in the overlay. Existing helper
settings cannot pair the selected credential with a previous endpoint.
Optional model and helper selections must belong to the selected backend's
configured formal model IDs. An omitted effort leaves the native default intact.
The helper model applies to auxiliary tasks and delegation on that same provider.

For Chat Completions routes, effort choices are read as data from the installed
Hermes CLI vocabulary and custom-provider wire policy. Other transports retain
unknown metadata. The catalog and generator use the same route projection,
including the official DeepSeek transport correction described below.
Choices retain native names rather than dsh's four steps; `ultra`, clamped to
`max`, is not a separate step. DeepSeek accepts `none` as thinking off,
`minimal`/`low` as low, `medium`/`high`/`xhigh` as high, and `max` as max on its
[official Chat API](https://api-docs.deepseek.com/api/create-chat-completion/).
These are harness choices, not independent capability claims about local servers.
The public default is unspecified: saved global and model-specific reasoning
settings take precedence over the custom profile's `medium` fallback. The UI
therefore keeps its neutral default display, and no explicit CLI value is added
until selected. Missing or changed native declarations yield unknown capabilities.
Metadata reads execute only the product's data projection, no Hermes code or
credential helper, and do not read or change persistent profile configuration.

The pinned upstream Anthropic converter drops DeepSeek thinking blocks carrying a
nonempty signature. For the DeepSeek backend's official `/anthropic` endpoint,
the generator selects the same provider's `/v1` chat-completions transport so
thinking survives subsequent tool requests, including when effort is omitted.
Main, auxiliary and delegation routes share this selection; Strata and custom
hosts keep their configured transport. No reasoning level or token cap is added.

Memory and skills persist in the stable private HOME for each project and canonical child name. Each child uses the
official `HERMES_MANAGED_DIR` overlay for its MCP entries and instructions;
profile configuration is not swapped or cloned. The selected MCP section is also
projected into the same private profile as described below. Product skills are links to
their canonical source, and existing unowned skills are never overwritten.
The native input queue uses `busy_input_mode=queue`. The initial prompt uses
`chat --query-file`; empty input opens the normal interactive CLI. No request,
output-token or session-lifetime limit, native-command ban, egress preload or
usage evaluator is added by this entry.

`thin_guard.py` adapts native pre-tool events to the common credential-read,
registration and acquired-reservation hooks, in that order. A failed check
blocks the tool with a fixed diagnostic, without exposing hook output.
An allowed hook's structured `systemMessage` warning remains visible; raw
success stdout/stderr are not forwarded. Blocking exit-2 hooks return their
public JSON reason or stderr to the caller, limited to the first 2000 Unicode
code points without a truncation marker. Internal traceback/error diagnostics
are excluded, terminal control sequences are removed and invalid UTF-8 is
replaced. Other failed exit codes keep the fixed hook-name diagnostic.
Successful native edits call the common reservation release hook. Both
checks and release run in the native tool's current directory.
Generation checks the plugin file and
configuration statically; it does not import or verify runtime registration.
Explicit MCP selections that cannot be represented are rejected. As with the
standard launcher's strict MCP config, inherited profile servers not in the
selected set are disabled in both the child overlay and private raw configuration.
Product Mail and explicitly selected additional MCP servers use the freshly
generated definitions in both places, even when a profile retained the same
server name. An omitted `enabled` means enabled; an explicit false stays disabled.
Native startup checks raw `config.yaml` before starting discovery, so an overlay
alone is insufficient. Generation manages only the raw MCP section under the
existing profile lock with atomic replacement, preserving all non-MCP YAML bytes
apart from the existing shared-metrics answer described below. Unselected server
definitions are retained but disabled, and a later explicit selection can enable
them again. The private HOME, memory, native session ID and generation pins are
unchanged; ordinary regeneration applies the selection to existing profiles too.
If native deep-merge would retain extra inherited fields in a selected server,
generation refuses with `inherited_mcp_conflict` and logs the server name in the
child's `config-generation.log`,
rather than silently using the old endpoint or credentials.
Profiles with enabled third-party plugins are rejected when inheritance is
explicitly disabled because portable plugins can append servers after loading.

The native packaging locale directory is supplied through the official
`HERMES_BUNDLED_LOCALES` environment setting, so translated strings are available
even when PM's editable workspace omits non-Python catalog files.

Before chat or exact resume, an unanswered shared-metrics offer defaults to
`enabled: false`, `send: false`, `offer_version: 2`. Native consent checks raw
profile configuration, so these three fields are saved under the existing
profile lock with an atomic replacement; all YAML bytes outside those fields remain unchanged by that step.
An explicit opt-in, local-only choice or current-version opt-out is preserved.
The child's `telemetry-defaults.log` records a default only when written.
This affects only the product-selected profile and never the user's separate
Hermes home. No global noninteractive setting or upstream patch is added.

The plugin saves the native session-start event in `native-session.json` under
the child's existing state directory. Delegated sessions cannot overwrite it.
An explicit `resume_session_id` becomes native `--resume`; no shared MRU is used.
An optional `native_profile_env` pin is checked using canonical paths before
creating files or links; a changed profile is rejected.
Missing resume metadata does not prevent startup. Standard launcher cleanup,
Mail delivery and process observation own the lifecycle.

## Installation and offline checks

The installer keeps native code at `d923a5eb` and applies the three CA dependency
inputs recorded in `ca-inputs.json` and `ca-inputs.patch`, sourced from upstream
`75a8d6fc`. These select certifi 2026.10.19 and PM's uv 0.13.5 without importing
the donor's SDK, PM, or TLS code changes. The existing release-age policy stays
in place. Exact preimage, patch and postimage hashes must match before PM runs.
The native stamp records the base commit and an honest dirty source build;
the receipt separately records the donor and recipe digest.

The generation name includes that digest, so old v2 installations are retained
instead of being reused or rewritten. Both PM install steps use the same modified
inputs. Before publishing or reusing the new generation, validation binds the
source inputs, resolved lock, actual certifi metadata/CA bundle and PM's selected
uv archive identity and executable hash. The plain launch contract is unchanged;
its additional CA evidence is required by the catalog and launcher, whose static
checks execute no Hermes code or credential helper. The recipe supports macOS
arm64/x64 and GNU/Linux arm64/x64 (including WSL); other targets, including Bionic,
stop rather than claiming the older uv artifact is fixed.

The stamp uses external update ownership. Runtime version import needs no git
process. Existing generations and profile data are retained; after
installation the operator points the private template at the selected generation.
The installer does not rewrite that template automatically. Existing running
processes and separate manual Hermes homes are not updated in place. This recipe
does not update the system's trust store or the caller's pip/uv installation.

With separately prepared dependencies, run all current Hermes fixtures serially:

```sh
scripts/check-hermes.sh --config PRIVATE_RUNTIME_TEMPLATE --out RESULT_JSON
```

The check reads PM facts to select the committed dependency interpreter, without
importing Hermes or running install/repair. It uses a temporary HOME and offline
cache. Missing dependencies fail setup rather than being accepted as a skip.
Results contain `success`, `tests_run`, `failures`, `errors`, `skips`,
`allowed_skip_reasons` and `unapproved_skips`. Approved skip reasons are exact
strings; the default list is empty, so every skip fails acceptance.

Fixtures cover native configuration and hook dispatch, main/helper credential
routing, generation ownership and version stamps. They use isolated profiles and
synthetic credentials; they do not certify live authentication or operator
acceptance. Standard spawn, native resume and common cleanup retain their own
integration tests. The former managed controller, private-copy runner, provider
allowlist/usage evaluator and separate lifecycle status are removed.
