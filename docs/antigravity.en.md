# Google Antigravity / Gemini provider

ORRERY can use Google Antigravity CLI (`agy`) as an optional provider alongside
Claude Code and Codex. The integration uses the user's existing Antigravity
authentication; ORRERY does not set a Gemini API key and does not auto-approve
Antigravity permissions.

## Design boundary

The provider is additive. Installing the optional Gemini payload does not
replace the core Dashboard or change Claude/Codex launch behavior. The core
owns provider-independent process-tree liveness; Gemini contributes only the
`program=antigravity` / `agy` process identity to that shared path.

Interactive and delegated lifecycles intentionally match the existing ORRERY
rules:

- a top-level interactive `agy` exits back to its human shell, so a surviving
  tmux shell is observed as `finished`;
- a delegated child runs cleanup after the provider process, then the runner
  exits as the sole tmux command, so the child becomes `gone` / `retired`;
- delegated cleanup reports the result, releases reservations, soft-retires the
  child identity, removes transient credential/config state, and only then
  exits.

The Dashboard does not manufacture those states. It reports the process/tmux
state it can measure.

## Prerequisites

Install and authenticate Antigravity CLI separately. Verify it with:

```sh
agy --version
agy models
```

ORRERY itself must already be installed and ORRERY Mail must be healthy.

## Install the optional provider payload

From an ORRERY checkout:

```sh
scripts/install-gemini-provider.sh --dry-run
scripts/install-gemini-provider.sh
```

The dry run intentionally does **not** require `agy`. This lets CI and reviewers
validate the optional provider on machines that do not have Antigravity.

To also configure the session-bound ORRERY Mail MCP wrapper in Antigravity:

```sh
scripts/install-gemini-provider.sh --configure-mcp
```

The optional installer fails closed if the installed core does not contain the
required preregistration, cleanup, MCP-proxy, or shared process-liveness
infrastructure. It copies only Gemini-owned payload files; it never replaces
`dashboard/server.py`.

The Dashboard extension (`dashboard/provider_server.py` and
`dashboard/gemini_provider_runtime.py`) ships only with this optional
installer. The core `scripts/install.sh` does not copy those files, so a core
install keeps running `dashboard/server.py`; a core reinstall also leaves an
already-installed provider extension in place.

## Top-level launcher

```sh
~/.agentstack/bin/agent-start-gemini /path/to/project
```

Defaults:

- binary: `agy`
- model: `gemini-3.8-flash-high`
- effort: `high` (from `AGENTSTACK_GEMINI_EFFORT`)

The CLI launchers (`agent-start-gemini`, `spawn_gemini_child.sh`, and a direct
`spawn_gemini_preregistered.sh` run) keep this `high` fallback. The Dashboard
does not use it; see [Dashboard NEW AGENT](#dashboard-new-agent).

For binary-free validation:

```sh
AGENTSTACK_GEMINI_BIN=agy-not-installed \
  ~/.agentstack/bin/agent-start-gemini --dry-run /path/to/project
```

Dry-run stops before binary lookup, tmux, ORRERY Mail registration, and MCP
bootstrap. A real launch still fails clearly if `agy` is absent.

## Delegated child

```sh
PARENT_AGENT=<parent-name> \
~/.agentstack/hooks/spawn_gemini_child.sh \
  --resources "src/**,tests/**" \
  "Implement the requested change and run relevant tests." \
  /path/to/project
```

The launcher pre-registers the child, creates an isolated git worktree, reserves
declared resources, sends the task to Antigravity over stream-json stdin, and
uses the session-bound MCP wrapper. Owner-token values are not placed in the
task, argv, or Antigravity MCP configuration.

A nominal Antigravity `SUCCESS` is not enough when required actions were denied
or the textual response is empty. Those cases are reported to the parent as
incomplete. A non-zero launcher/runtime status is likewise incomplete.

## Dashboard NEW AGENT

With the optional provider payload installed and the Dashboard restarted,
`NEW AGENT` shows an `Antigravity` engine tab. A launch reuses the delegated
child path above through `spawn_gemini_preregistered.sh`.

Requirements checked before ORRERY Mail registration, so a launch that cannot
start does not leave a retained child identity behind:

- a parent agent: standalone Antigravity launches are rejected, and the parent
  must have its local runtime owner token;
- the launch directory must be inside a git repository, and the worktree base
  (default `HEAD`) must resolve to a commit;
- `agy`, `tmux`, the selected Python, `git`, the adapter hook, and the Gemini
  provider helpers under `~/.agentstack/bin` must be present;
- the model must be in the Gemini allow-list (`AGENTSTACK_GEMINI_MODELS`,
  default `gemini-3.8-flash-high,gemini-3.8-flash-medium,gemini-3.8-flash-low`). An allow-list that
  reuses a Claude or Codex model id disables the Antigravity tab.

The model card displays one family, `gemini-3.8-flash`. Before registration,
Dashboard resolves the selected effort to the matching allowed CLI model:
`low` → `gemini-3.8-flash-low`, `medium` → `gemini-3.8-flash-medium`, and
`high` → `gemini-3.8-flash-high`. Registration, launch logs, and `agy --model`
use that same resolved id alongside the explicit `--effort`. A custom model
allow-list must include the matching variant; otherwise the request is rejected
before registration. Model ids without an effort suffix are passed unchanged.

Engine and isolation rules:

- **Effort is explicit.** The Dashboard selects no effort for Antigravity, keeps
  `SPAWN` disabled until `low`, `medium`, or `high` is chosen, and the server
  rejects a request without one (`effort required for provider gemini`). The
  CLI launchers' `high` fallback is not applied to Dashboard launches.
- **Isolated worktree is forced.** The isolation checkbox is locked on while
  Antigravity is selected; switching to Claude or Codex restores the previous
  choice, and hidden resource declarations are not sent to those providers.
- **Resources are required.** Enter comma-separated paths relative to the
  repository root, for example `src/**,tests/**`. Absolute paths, `..`, `~`,
  control characters, a leading `-`, backslashes, and `.git` are rejected, as
  is any existing symlink component that resolves outside the repository. The
  adapter repeats the check inside the new worktree before reserving anything.
  Declarations are normalized (whitespace, `./`, duplicate slashes, repeats)
  before they are reserved, and a leading `-` is rejected in the normalized
  form too (`./-rf`). Wildcards are matched the way a case-insensitive
  filesystem such as default APFS resolves names, so `.GI?/config` or `SR?/**`
  is judged against `.git` or `src` in any letter case.

The launcher delivers the full task to Antigravity and reports the result to
the parent automatically, so the Dashboard sends no separate task mail for an
Antigravity child. Launch status, `/api/spawn-status`, and
`dashboard/logs/spawn.log` record `provider=gemini`.

If the Dashboard page no longer matches what the extension expects, the page is
served unmodified, the Antigravity tab is omitted, and Antigravity spawn
requests fail closed; the reason is written to the Dashboard log. Claude and
Codex are unaffected, including when the extension itself cannot be loaded.

## Dashboard controls

The Dashboard uses the same provider-aware process-tree measurement for Claude,
Codex, and Antigravity. For a delegated Antigravity child, `EXIT` re-reads the
process tree immediately, sends `SIGINT` only to the measured `agy` process, and
leaves the launcher-owned shell runner alive so report/release/retire/cleanup can
finish. For a top-level interactive Antigravity session, `EXIT` keeps the normal
interactive `/exit` path.

ORRERY does not currently implement Antigravity conversation resume or native
transcript history. Gone or retired Antigravity entries therefore fail closed
instead of falling through to Claude/Codex transcript lookup. A top-level
Antigravity session whose `agy` process has exited but whose human shell remains
is kept attachable as `finished`; Dashboard jump does not kill that shell to
attempt an unsupported resume.

## Headless permission caveat

In a measured macOS run using Antigravity CLI 1.1.27, headless `view_file`
inside the delegated worktree was auto-denied until `read_file` was explicitly
allowed by the user. The stderr message stated that headless mode could not
prompt for the required permission.

Use the narrowest rule that covers the **resolved** delegated-worktree root. In
the measured default macOS environment, `/tmp` resolved through
`/private/tmp`, so the scoped rule was:

```json
{
  "permissions": {
    "allow": [
      "read_file(/private/tmp/cc-worktrees)"
    ]
  }
}
```

Merge such a rule with the user's existing settings; do not replace existing
permission entries. If `AGENTSTACK_WORKTREE_ROOT` is customized, scope the rule
to that resolved root instead.

ORRERY does not add `read_file(*)`, does not enable
`--dangerously-skip-permissions`, and does not otherwise broaden user-owned
Antigravity permissions.

## Credential boundary

The session-bound wrapper resolves the current agent's mode-0600 runtime token
file when the MCP process starts. Antigravity workspace/global MCP config stores
only the wrapper command, not the owner-token value or token-file path.

`agy` and ORRERY still run as the same operating-system user. This reduces
accidental configuration/argv exposure; it is not an OS-level isolation
boundary against another process running as that user.

## Measured macOS E2E

The pre-upstream prototype was exercised on macOS with Antigravity CLI 1.1.27
and Gemini 3.8 Flash High.

Normal delegated completion was measured through preregistration, worktree
creation, reservation, headless execution, non-empty `SUCCESS`, parent
`Gemini child complete` reporting, release/retire/cleanup, and child tmux
session disappearance.

A live Dashboard `EXIT` run produced Antigravity `ERROR / interrupted`; the
launcher-owned runner remained alive long enough to report
`Gemini child incomplete`, release the reservation, retire/clean transient
state, and then let the child tmux session disappear. The parent and
`mail-watcher` survived, and the same resource was immediately reservable by a
subsequent child.

Those real-machine observations are evidence for the provider lifecycle. Final
upstream-ready validation must be repeated after integrating onto the current
upstream base.
