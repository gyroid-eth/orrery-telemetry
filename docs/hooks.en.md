# Hooks and operational helpers

> 日本語版: [hooks.md](hooks.md)

[Previous: Delegation and child agents](delegation.en.md) · [Back to README](../README.en.md) · [Next: Codex App integration](codex-app.en.md)

`hooks/` contains hooks that run automatically from Claude Code lifecycle events, operational helpers called explicitly by launchers, the dashboard, and skills, and the internal libraries and workers sourced or started by event hooks. There are eight Claude Code event hooks.

[`hooks/settings.template.json`](../hooks/settings.template.json) defines event-to-command mappings, and [`hooks/README.md`](../hooks/README.md) defines the safety policy for settings merges. This document is the user reference explaining when each component actually runs and what it guarantees.

## Claude Code event hooks (8)

After the installer merges `settings.template.json` into `~/.claude/settings.json`, the following events run automatically.

| Event / matcher | Executable | Trigger timing | Primary behavior |
| --- | --- | --- | --- |
| `SessionStart` | [`set-ghostty-title.sh`](../hooks/set-ghostty-title.sh) | Immediately after startup / resume / `/clear` / compact | Apply a known identity to pane metadata, the tmux session, the terminal-title clipboard, and the managed agent list |
| `SessionStart` | [`session-start-reminder.sh`](../hooks/session-start-reminder.sh) | Same as above, after the title helper | Check ORRERY Mail health and the existing identity, then output route-aware guidance for embedded tasks, bound proxies, and raw/direct connections into session context |
| `PreToolUse` / `Edit|Write` | [`check-file-reservation.sh`](../hooks/check-file-reservation.sh) | Immediately before Claude Code edits a file | Check an existing exact-path reservation inside a protected root with renew-only semantics. Retry zero results once, then block with exit 2 if the result remains zero |
| `PreToolUse` / `Edit|Write|Bash` | [`check-agent-registered.sh`](../hooks/check-agent-registered.sh) | Immediately before an edit, write, or shell command | Check a session flag to confirm that the current Claude session has called `register_agent`. Block an unregistered session with exit 2 |
| `PreToolUse` / reservation tools | [`invalidate-release-debounce.sh`](../hooks/invalidate-release-debounce.sh) | Immediately before acquiring or renewing a file reservation | Invalidate the token of an older release worker for the same agent/path, preventing a race that would remove the new reservation immediately |
| `PostToolUse` / `Edit|Write` | [`release-file-reservation.sh`](../hooks/release-file-reservation.sh) | Immediately after a successful file edit | After the default 90-second grace period, release the reservation using the same project, identity, and relative/absolute path as the guard |
| `PostToolUse` / `register_agent` | [`mark-agent-registered.sh`](../hooks/mark-agent-registered.sh) | Immediately after a response from `mcp__orrery-mail__register_agent` or a compatible tool | Validate the response and update the session flag, session index, and pane / tmux metadata only after an exact match with an explicitly requested name |
| `SessionEnd` | [`release-all-reservations.sh`](../hooks/release-all-reservations.sh) | When the Claude session ends | Release every file reservation for the current identity. Do not retire the identity itself |

Both PreToolUse hooks run for `Edit` / `Write`. A registered session cannot write without a reservation, and a session with a reservation cannot write if it is unregistered. The PostToolUse release is armed only after success and does nothing after failure, blocking, or an edit outside a protected root.

The installer distributes the endpoint and transport credential selector in the same generated `env.sh`. ORRERY Mail generates `AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled`, and hooks plus `spawn_child.sh` / `cleanup-child-agent.sh` connect to the endpoint without an Authorization header. The agent owner token is a separate identity credential and does not change the boundary between child token files and tool arguments. The old Keychain service remains only as a fallback read path for existing environments.

### `set-ghostty-title.sh`

- **Trigger:** `SessionStart`. It is also called in the background after `mark-agent-registered.sh` obtains the current session's own canonical name.
- **Behavior:** Uses `AGENT_NAME` or an argument and writes identity metadata for each `TMUX_PANE` to the runtime directory. tmux rename is allowed only for `pending-*` sessions, so an established parent session is not overwritten with a child name.
- **On collision:** Does not kill a same-name tmux session; it refuses the rename and fails closed. Supported terminals use the clipboard for title handoff. If the name cannot be resolved, it does nothing.

### `session-start-reminder.sh`

- **Trigger:** Every `SessionStart` source, including startup, resume, `/clear`, and after compaction.
- **Behavior:** Resolves identity in the order `AGENT_NAME` → pane metadata → exact tmux session and checks ORRERY Mail liveness. When an owner token and project key are available, it reregisters the same identity from the shell. It then tells the model to use only the first matching route: an embedded task that explicitly says registration is complete and no ritual is required, the bound proxy shown by the provided tool schema, or raw/direct MCP. An embedded task is not excluded merely because there is no parent. Shell registration and authentication of a raw model-side MCP tool session are separate.
- **Route boundary:** A child proxy configuration artifact is only a hint that makes the reminder more specific; the descriptions and argument schemas of the tools actually provided to the model remain authoritative. A bound proxy is never told to run the helper, register again, or read a token file, and a proxy failure does not trigger automatic raw/helper fallback. Only an existing identity on confirmed raw/direct MCP uses the token-safe helper, separately from new raw registration. A generic registration failure is not labelled as a stale token.

### `check-file-reservation.sh`

- **Trigger:** Immediately before `Edit` / `Write`. Enforcement applies only when the target path is under `AGENTSTACK_PROTECTED_ROOTS`, or under the project root when that setting is omitted.
- **Project key:** Resolved in the order `AGENTSTACK_PROJECT_KEY` → `PROJECT_KEY` → `AGENTSTACK_PROJECT_KEY` in `${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh` → cwd from hook input. The installed `env.sh` is not sourced as shell code; only the relevant `export AGENTSTACK_PROJECT_KEY=...` is read literally. The registration guard, session reminder, child cleanup, and `agentstack-await-reply` share this resolver, so an editor session that bypasses the launcher still refers to the same project selected at installation.
- **Identity:** Prefers `AGENT_NAME`. Otherwise it explicitly obtains the target pane's tmux session through `TMUX_PANE` and uses pane metadata only to confirm a match. If metadata and session differ, either is a placeholder, or resolution fails, it blocks with exit 2 before sending HTTP. It never uses an untargeted ambient tmux session. A client outside tmux can resolve identity from hook input `session_id` if it called `register_agent` and was recorded under `<runtime>/session_index/` (precedence: env → tmux → session index). A `session_id` containing anything outside `[a-zA-Z0-9_-]` is not used, and symlinked index entries are not read. Sessions with no identity source follow the “unmanaged session” rules below.
- **Behavior:** Checks existing reservations by both relative and absolute path using renew-only semantics. It neither reads the owner's `registration_token` nor sends it in tool arguments. A legacy HTTP bearer is a separate transport credential and is not sent to a native endpoint whose generated selector is `disabled`. A zero result is retried once to account for asynchronous commits; the guard never auto-acquires a reservation.
- **Decision:** An existing reservation exits 0. Definitive zero, HTTP rejection, JSON-RPC error, MCP `isError=true` or a non-boolean value, schema violation, malformed response, or retry failure after zero exits 2. `isError` is accepted as success only when omitted or boolean `false`. After establishing exact identity and protected scope, an operational fail-open exists only when the **first query** finds the transport unreachable. A missing path or a path outside the protected roots is outside enforcement and exits 0.
- **Deployment order:** Do not partially apply the strict-identity version to existing sessions. At cutover C5, restart/rebind every client through `agent-start`, verify exact identity, synchronize the repository version into the live installation, and pass tests in both the reservation-present and reservation-absent directions.

### `check-agent-registered.sh`

- **Trigger:** Immediately before `Edit` / `Write` / `Bash`.
- **Behavior:** Checks `/tmp/.claude-agent-registered-<session_id>`, created by `mark-agent-registered.sh`. When `/clear` or another operation changes `session_id`, the old flag no longer matches, so protected tools remain blocked until reregistration.
- **Exception:** A bot channel receiving `AGENT_NAME` from the launcher may use the shell needed for reregistration. If hook input has no session ID, it fails open.
- **Location of the recovery mechanism:** Only `mark-agent-registered.sh` writes the flag, and it runs as PostToolUse for the mail MCP's `register_agent`. Therefore **only a session with that MCP can clear this block**. `agentstack-reregister` does not write the flag and is not a recovery mechanism for this guard; it repairs remote registration.

### Reservation release hooks

- **Same coordinate system:** [`reservation-common.sh`](../hooks/reservation-common.sh) is sourced by `check-file-reservation.sh` and the release hooks. It resolves the endpoint, legacy bearer selector, identity, project key, protected root, and relative/absolute paths with the same rules. HTTP requests always include `Accept: application/json, text/event-stream`.
- **Grace / debounce:** `release-file-reservation.sh` waits for `AGENTSTACK_RELEASE_GRACE_SECONDS` (90 seconds by default; legacy `FILE_RESERVATION_RELEASE_GRACE_SECONDS` is also accepted) before [`release-file-reservation-worker.py`](../hooks/release-file-reservation-worker.py) releases the reservation. State is under `$AGENTSTACK_RUNTIME_DIR/file_release_debounce/`; the next edit updates the token, and a new-reservation hook removes the state file so the older worker becomes a no-op.
- **Missing components and failures:** If the worker was not distributed, the hook falls back to an immediate synchronous release. Release failures such as HTTP 406, connection failure, or JSON-RPC / MCP error append one line to `$AGENTSTACK_RUNTIME_DIR/release-failures.log`. The hook itself does not make completion of an edit or session count as a failure.
- **SessionEnd boundary:** `release-all-reservations.sh` releases only reservations. It does not retire the agent, avoiding irreversible identity changes after a crash / resume.

### Service outages and unmanaged sessions (two separate questions)

Before stopping an edit, the guard asks two questions in order. **Combining them makes access depend on the startup path**; measurements showed raw clients passing against a healthy server while tmux clients were blocked during an outage.

**1. Is the mail service responding (transport)?**

While it is not responding, **registration is impossible for every client**. Only PostToolUse for `register_agent` can write the flag, and that call cannot pass through an unresponsive endpoint. At the same time, nobody can acquire or verify a reservation.

- The check is **one HTTP HEAD request** to the endpoint with a two-second deadline. It has three results:
  - `reachable`: **some HTTP response was received**, including 401 / 500 / 404. “The server said no” does not mean “there is no server,” so the guard remains closed
  - `unreachable`: the connection was refused, or it was **accepted but produced no response before the deadline**. Registration is impossible in the latter case as well, so it is treated as an outage
  - `invalid`: the endpoint is unset or invalid as a URL. **Always block**, because a typo in the authority address must not remove the authority
- The endpoint precedence is `AGENTSTACK_MCP_URL` → `MCP_URL` → the installed `env.sh`. A client that bypasses the launcher does not inherit the installer's environment, so it **does not fall back to a fixed port**; doing so could mistake an installation on another port for a stopped service and open the guard
- Warning output truncates the endpoint to scheme / host / port / path because userinfo or query values may contain secrets
- `AGENTSTACK_MAIL_OUTAGE_POLICY=warn-open` (default): allow the operation. Display a visible `systemMessage` warning, repeated in ten-minute buckets, and log every occurrence to JSONL. **Do not create a flag or binding.** The next call after recovery is reevaluated, and an unregistered session is blocked again
- `AGENTSTACK_MAIL_OUTAGE_POLICY=block`: reject the operation and show recovery steps (`agentstack-mailctl start` / `agentstack-doctor`)
- **Identity conflicts remain blocked during an outage.** A failure is not an escape hatch for ambiguous identity. Both guards check this because Bash can write arbitrary files, making an Edit/Write-only check insufficient
- Conflict checking is a scan **separate from identity precedence resolution**. A precedence resolver returns as soon as `AGENT_NAME` exists, so asking through it would fail to check named sessions. A mismatch between `AGENT_NAME` and the binding is also a conflict, because one would write under the other's name
- **Sessions whose identity was resolved follow the same policy.** A transport failure while renewing a reservation uses the same handler; a named agent is not allowed more uncoordinated writes than an unnamed session

**2. Does this session have an identity source (identity)?**

When the service responds, registration is possible, so the default is to **require it** (block). `AGENTSTACK_UNMANAGED_SESSION_POLICY=warn-open` is an operator's explicit opt-out stating that “the server is running, but this client will not participate in coordination.”

| Identity / local state | Transport | Behavior |
|---|---|---|
| Self binding exists and project matches | reachable | Normal enforcement |
| No binding / flag | reachable | Block (instruct `register_agent`) |
| With or without a binding | unreachable | Follow `AGENTSTACK_MAIL_OUTAGE_POLICY` |
| Identity conflict | Any state | Block |
| HTTP / auth / MCP / schema rejection | Responding | Block (do not treat as an outage) |
| Explicit opt-out | reachable | Follow `AGENTSTACK_UNMANAGED_SESSION_POLICY` (block by default) |

**Having no identity source does not mean lacking the mail MCP.** Successful `register_agent` calls have been observed from an IDE agent panel. Hook input has no field describing the client's MCP inventory (observed: the common PreToolUse input has only nine keys), so the only thing it can determine is whether the endpoint is reachable at this moment.

Both guards use the same checks in the same order. Opening only one leads to the same dead end in the other.

### `mark-agent-registered.sh`

- **Trigger:** PostToolUse for the `register_agent` MCP tool. Both `tool_input` and a non-error server response are required. If the canonical name cannot be obtained from the response, it does not fall back to the explicit input name.
- **Validation:** If `name` was explicit, it must match response `name` exactly. A different name, error response, or parse failure in either input or response is recorded in `registration-failures.log` and returned to the caller with exit 2. Only a registration that omitted the name adopts the generated response name.
- **Behavior:** After validation, runs `record-session-index.py` **synchronously before** creating the registration flag. Creating the flag first would leave a window where registration was marked successful but identity was not recorded, and the reservation guard would not know whom to check. It calls the title helper only when the current session is `pending-*`, already has the same name, or matches the environment's `AGENT_NAME`.
- **Parent-child protection:** Even when a parent preregisters a child in PostToolUse, the parent's pane metadata is not rewritten to the child identity.
- **Guarantee boundary:** PostToolUse runs after the server call, so it cannot roll back a rejected alternate-name row. `check-agent-registered.sh` also allows a channel with an existing `AGENT_NAME` even without a flag. This hook guarantees that it “does not silently accept a mismatch or create new success state”; it does not force every session's subsequent operations to stop.

## Operational helpers (8)

These are not registered directly with events in `settings.template.json`. Their callers and startup conditions are explicit.

| Executable | Caller / startup timing | Primary behavior |
| --- | --- | --- |
| [`record-session-index.py`](../hooks/record-session-index.py) | Started **synchronously** by `mark-agent-registered.sh` with the PostToolUse payload | Atomically write the exact mapping among ORRERY Mail ID, Claude `session_id`, transcript, cwd, `project_key`, and `registered_by`. Do not record a call that registered another agent |
| [`prepare-codex-session-binding.py`](../hooks/prepare-codex-session-binding.py) | Run by a Codex CLI launcher after ORRERY Mail registration and immediately before the CLI starts | Add a fresh `launch_id` to the server-returned project, numeric agent ID, name, and program. For a child, also add its non-secret launch origin / MCP profile, then atomically record this launch's receipt expectation |
| [`record-codex-session-index.py`](../integrations/codex_app/plugin/scripts/record-codex-session-index.py) | Given the official Codex `SessionStart` payload synchronously by the plugin runner | Compare the payload `session_id` with the rollout header ID and atomically index only the current launch with the same `launch_id`; carry verified `child` / `standalone` provenance into the receipt |
| [`resolve-agent-name.sh`](../hooks/resolve-agent-name.sh) | Sourced by reminder, reservation, and cleanup helpers that need identity | Resolve identity in the order env → exact tmux session → session index (when the caller passes `AGENTSTACK_SESSION_ID`) |
| [`spawn_child.sh`](../hooks/spawn_child.sh) | Explicitly run by `/delegate` or dashboard NEW AGENT when starting a child | Combine identity, token, task mail, reservation, tmux, Claude / Codex, worktree, and readiness into one launch transaction |
| [`cleanup-child-agent.sh`](../hooks/cleanup-child-agent.sh) | Immediately after the child REPL command started by `spawn_child.sh` ends | Best-effort release of reservations, retirement of remote identity, and removal of managed-list / state / credential / MCP configuration |
| [`monitor_child_agent.sh`](../hooks/monitor_child_agent.sh) | Run once per monitoring interval by a `/delegate` parent | Capture the tmux pane and report completion, session disappearance, permission prompt, stasis, and an optional danger pattern through exit codes |
| [`watch_agent_mail_signals.sh`](../hooks/watch_agent_mail_signals.sh) | Started by launcher registration as a dedicated `mail-watcher` tmux service | Watch ORRERY Mail signals and inject notification text plus `C-m` into the exact matching agent tmux session |

### `record-session-index.py`

From the PostToolUse payload, this helper obtains the numeric ORRERY Mail ID, canonical name, Claude `session_id`, transcript path, and cwd, then writes them to `$AGENTSTACK_RUNTIME_DIR/session_index/<agent_id>.json` using a temporary file plus `os.replace`. Each record has `schema_version: 2` and `binding_kind: "self"`. **It does not write a record when the caller registered another agent, such as when a parent registers a child.** The index is read for both dashboard resume and guard identity resolution, so declining to write leaves less room for misuse than filtering only when reading. The dashboard prefers this exact mapping for session resume and falls back to a heuristic only for old sessions. Invalid input and I/O failures are quiet no-ops that do not interfere with registration.

### Codex CLI session binding helpers

`prepare-codex-session-binding.py` records the launcher's authoritative registration at `$AGENTSTACK_RUNTIME_DIR/codex_launches/<agent_id>.json`. Each launch gets a new `launch_id`; the record also carries `binding_expected` and launch kind. A Codex child explicitly adds `launch_origin: child` and `codex_mcp_profile: inherit | orrery-only`; the recorder copies them into the bound receipt beside numeric ID, name, project, and provider. These fields are non-secret and contain no owner credential. Prepare is a startup precondition: when the helper is missing, the lock cannot be created, or metadata cannot be installed atomically, the launcher does not start the new CLI. This prevents an old launch/receipt from appearing successful for a new run. Both the preregistration helper and dashboard NEW AGENT store the numeric ID in a non-secret sidecar separate from the token, and `spawn_child.sh` adopts it into child state. No database name lookup or general parent environment value establishes identity.

The only runtime identity value `record-codex-session-index.py` consumes is `session_id` from SessionStart stdin. It never uses `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, cwd, time, or candidate count. It writes a `provider: codex` receipt only when the payload path resolves to a regular file whose first `session_meta` ID matches, and when the launch metadata's project, numeric ID, name, program, and the launcher's process-scoped `launch_id` all agree. Provenance is copied only when the launcher supplies `standalone`, or the coherent pair `child` plus a valid MCP profile; contradictory fields make the binding invalid. The receipt sits outside the state, token, and child home removed by normal cleanup, so the dashboard can distinguish a cleaned-up child, a product-launched top-level session, and a session with unknown origin without guessing from its name. Receipts created before provenance existed are not promoted to either origin. The recorder ships beside the trusted plugin runner and derives the index runtime root from the launch metadata path, so a login shell restoring a parent's `AGENTSTACK_RUNTIME_DIR` cannot redirect the receipt. Built-in subagent events are excluded because they are not separate CLI processes and share the root ID.

This recorder runs only when the `agentstack-codex-app` SessionStart hook plugin is installed and enabled and Codex has approved its lifecycle hooks. A core install may update the deployed plugin source, but it does not automatically update or enable the optional marketplace/cache selected by Codex, and it never auto-approves hooks. Run the [plugin-only refresh](codex-app.en.md#refreshing-an-existing-plugin-after-a-core-update) for an existing enabled plugin, review and approve the AgentStack hooks in Codex `/hooks`, then verify the result in a fresh process. Users with an absent or disabled plugin are not opted in automatically.

The launcher and recorder take the same agent-ID lock. Launch metadata makes a one-way claim on the first `session_id`, and detecting any different ID permanently conflicts that generation. Every callback writes a fresh receipt nonce to launch metadata before the index; an index is valid only with the same nonce. Thus a stale receipt cannot pass even when deletion fails, and a different ID cannot recover through `clear`, retry, or `compact` until a new launch. A delayed callback from an older run likewise cannot overwrite the current receipt after a new launch is recorded. The SessionStart hook itself is fail-open and never stops the CLI, but records `no_transcript`, `id_mismatch`, `write_failed`, and similar reasons in launch metadata / stderr where possible. After an approximately ten-second display grace, DECK shows `? UNBOUND` if the current receipt is still absent, or `— NO HISTORY` only when disabled history is explicit. The reason appears only in the History tab.

### `resolve-agent-name.sh`

This source-only helper returns `RESOLVED_AGENT` and the resolution source to its caller. Its precedence is `AGENT_NAME`, then the exact tmux session of the pane explicitly named by `TMUX_PANE`, and finally the session index. It uses the session index only when the caller passes `AGENTSTACK_SESSION_ID`, only for records with `schema_version: 2` and `binding_kind: "self"`, and, when `AGENTSTACK_LOOKUP_PROJECT_KEY` is supplied, only when the record exactly matches that project. If several identities are bound to one session, it returns `identity-conflict` instead of selecting by timestamp. Pane metadata is not authoritative and is used only to confirm a match; a mismatch returns `identity-conflict`. `pending-*`, `warm-*`, `claimed-*`, and `mail-watcher` are not identities. It does not query an ambient tmux session without a specified pane; unresolved cases return an empty string so the caller can apply the boundary.

### `spawn_child.sh`

By default, a target declaration through `--resources` is required, and conflicts are checked before a child starts. It supports Claude / Codex, model selection, a preregistered identity, a child-owned token file, a per-child MCP proxy, and an optional worktree. It waits until the tmux REPL is ready or has exited early, then injects the canonical task. Argument / server / worktree failures exit 1, missing resource declarations exit 2, and reservation conflicts exit 21. It is normally used through [/delegate](launchers.en.md#delegate) or the dashboard rather than called directly.

### `cleanup-child-agent.sh`

This helper is chained after the child's Claude / Codex command and runs only when the REPL returns. It releases all reservations and retires the identity with the child's owner token. Remote release / retirement and managed-list updates remain best-effort and run before local child state is handled.

Claude children, and Codex children configured with `AGENTSTACK_CHILD_RESUME_RETENTION_DAYS=0`, fully delete state, token, MCP configuration, and isolated home. By default a Codex child deletes its MCP configuration, home, and proxy runtime but retains mode-0600 state containing the schema version, `retired_at`, `resume_expires_at`, and non-secret provenance together with the canonical owner credential. If safe retention cannot be finalized, cleanup does not guess and delete the credential; it leaves private state for explicit recovery or purge and exits nonzero. Bound session receipts and transcripts are outside both cleanup and purge scope.

The shared `child_resume.py` helper uses one lock and validation path for fresh-spawn / resume state preparation, private-material checks, rebuilding a child home from the current source home, expiry maintenance, and explicit purge. Operator entry points are `agentstack-purge-child-resume <agent>` and `agentstack-purge-child-resume --expired`. `agentstack-doctor` reports expired entries but never invokes purge.

This is not a Claude Code `SessionEnd` hook. Because `SessionEnd` can occur during a crash or resume, remote identity retirement is not tied to that event.

### `monitor_child_agent.sh`

This is a one-shot monitor, not a resident daemon. The parent invokes it repeatedly at a cadence appropriate to risk. Exit codes are `0` continue, `10` shell return, `11` session disappearance, `20` warning, `30` soft stop, `40` send `SIGSTOP` to the process group, and `50` kill the session.

Dangerous-command pattern checking is enabled only with `AGENTSTACK_MONITOR_DANGER_CHECK=1`. In contrast, repeatedly unchanged pane output always counts as stasis and escalates through Escape / `C-c` → freeze → kill.

### `watch_agent_mail_signals.sh`

It uses event watching when `fswatch` is available and two-second polling otherwise. It does not delete signal files, which are server-owned dirty bits; runtime delivery state and a short lease suppress duplicate injection of the same `(agent, message)`. A periodic scan every 30 seconds recovers missed events.

The delivery target is only the tmux session whose name exactly matches the agent. After sending notification text literally, it submits with a separate `C-m` call, avoiding bare shells and unrelated sessions. tmux calls run in timeout-controlled workers so a server stall cannot stop the entire watcher.

## Differences for Codex

The official Codex CLI `SessionStart` hook is used for the history receipt above, but it is not equivalent to Claude Code's registration `PostToolUse` or reservation `PreToolUse` guards, and `mark-agent-registered.sh` does not run. `agent-start-codex` completes identity registration, tmux rename, and launch expectation during bootstrap; a reserved child/resume or reregistration stops when the response name does not match. Direct spawn instead reports a warning and adopts the response name, while raw MCP registration is not detected automatically. The managed `~/.codex/AGENTS.md` instructs reservation reserve / renew / release behavior. The mail watcher and ORRERY Mail registry are shared by Claude and Codex, so notifications and reservation conflicts are mutually visible.

Codex Desktop uses a further, separate plugin hook / Bridge lifecycle. See [Codex App integration](codex-app.en.md) for details.

## Related documentation

- [Installation](install.en.md)
- [Launchers and identity / Skills](launchers.en.md)
- [Codex App integration](codex-app.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)
