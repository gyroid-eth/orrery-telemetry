# Google Antigravity / Gemini provider

ORRERY can use Google Antigravity CLI (`agy`) as an optional provider next to
Claude Code and Codex.  The integration uses the user's existing Antigravity
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

## Top-level launcher

```sh
~/.agentstack/bin/agent-start-gemini /path/to/project
```

Defaults:

- binary: `agy`
- model: `gemini-3.8-flash-high`
- effort: `high`

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
