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
| `SessionStart` | [`session-start-reminder.sh`](../hooks/session-start-reminder.sh) | Same as above, after the title helper | Check agent-mail health and the existing identity, then output same-name reregistration or registration instructions and `fetch_inbox` into session context |
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
- **Behavior:** Resolves identity in the order `AGENT_NAME` → pane metadata → exact tmux session and checks agent-mail liveness. When an owner token and project key are available, it reregisters the same identity from the shell and instructs the session to begin with `fetch_inbox` after success.
- **When reregistration is unavailable:** Displays instructions that pass the resolved same name to `register_agent`; it does not branch into generating another name. When a child-specific MCP proxy injects authentication, it does not make the model read the token file.

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

## Operational helpers (6)

These are not registered directly with events in `settings.template.json`. Their callers and startup conditions are explicit.

| Executable | Caller / startup timing | Primary behavior |
| --- | --- | --- |
| [`record-session-index.py`](../hooks/record-session-index.py) | Started **synchronously** by `mark-agent-registered.sh` with the PostToolUse payload | Atomically write the exact mapping among agent-mail ID, Claude `session_id`, transcript, cwd, `project_key`, and `registered_by`. Do not record a call that registered another agent |
| [`resolve-agent-name.sh`](../hooks/resolve-agent-name.sh) | Sourced by reminder, reservation, and cleanup helpers that need identity | Resolve identity in the order env → exact tmux session → session index (when the caller passes `AGENTSTACK_SESSION_ID`) |
| [`spawn_child.sh`](../hooks/spawn_child.sh) | Explicitly run by `/delegate` or dashboard NEW AGENT when starting a child | Combine identity, token, task mail, reservation, tmux, Claude / Codex, worktree, and readiness into one launch transaction |
| [`cleanup-child-agent.sh`](../hooks/cleanup-child-agent.sh) | Immediately after the child REPL command started by `spawn_child.sh` ends | Best-effort release of reservations, retirement of remote identity, and removal of managed-list / state / credential / MCP configuration |
| [`monitor_child_agent.sh`](../hooks/monitor_child_agent.sh) | Run once per monitoring interval by a `/delegate` parent | Capture the tmux pane and report completion, session disappearance, permission prompt, stasis, and an optional danger pattern through exit codes |
| [`watch_agent_mail_signals.sh`](../hooks/watch_agent_mail_signals.sh) | Started by launcher registration as a dedicated `mail-watcher` tmux service | Watch agent-mail signals and inject notification text plus `C-m` into the exact matching agent tmux session |

### `record-session-index.py`

From the PostToolUse payload, this helper obtains the numeric agent-mail ID, canonical name, Claude `session_id`, transcript path, and cwd, then writes them to `$AGENTSTACK_RUNTIME_DIR/session_index/<agent_id>.json` using a temporary file plus `os.replace`. Each record has `schema_version: 2` and `binding_kind: "self"`. **It does not write a record when the caller registered another agent, such as when a parent registers a child.** The index is read for both dashboard resume and guard identity resolution, so declining to write leaves less room for misuse than filtering only when reading. The dashboard prefers this exact mapping for session resume and falls back to a heuristic only for old sessions. Invalid input and I/O failures are quiet no-ops that do not interfere with registration.

### `resolve-agent-name.sh`

This source-only helper returns `RESOLVED_AGENT` and the resolution source to its caller. Its precedence is `AGENT_NAME`, then the exact tmux session of the pane explicitly named by `TMUX_PANE`, and finally the session index. It uses the session index only when the caller passes `AGENTSTACK_SESSION_ID`, only for records with `schema_version: 2` and `binding_kind: "self"`, and, when `AGENTSTACK_LOOKUP_PROJECT_KEY` is supplied, only when the record exactly matches that project. If several identities are bound to one session, it returns `identity-conflict` instead of selecting by timestamp. Pane metadata is not authoritative and is used only to confirm a match; a mismatch returns `identity-conflict`. `pending-*`, `warm-*`, `claimed-*`, and `mail-watcher` are not identities. It does not query an ambient tmux session without a specified pane; unresolved cases return an empty string so the caller can apply the boundary.

### `spawn_child.sh`

By default, a target declaration through `--resources` is required, and conflicts are checked before a child starts. It supports Claude / Codex, model selection, a preregistered identity, a child-owned token file, a per-child MCP proxy, and an optional worktree. It waits until the tmux REPL is ready or has exited early, then injects the canonical task. Argument / server / worktree failures exit 1, missing resource declarations exit 2, and reservation conflicts exit 21. It is normally used through [/delegate](launchers.en.md#delegate) or the dashboard rather than called directly.

### `cleanup-child-agent.sh`

This helper is chained after the child's Claude / Codex command and runs only when the REPL returns. It releases all reservations, retires identity with the child's owner token, and deletes child state, token, MCP configuration, and isolated Codex home. Remote release / retirement and managed-list updates are attempted best-effort before local child state is removed.

This is not a Claude Code `SessionEnd` hook. Because `SessionEnd` can occur during a crash or resume, remote identity retirement is not tied to that event.

### `monitor_child_agent.sh`

This is a one-shot monitor, not a resident daemon. The parent invokes it repeatedly at a cadence appropriate to risk. Exit codes are `0` continue, `10` shell return, `11` session disappearance, `20` warning, `30` soft stop, `40` send `SIGSTOP` to the process group, and `50` kill the session.

Dangerous-command pattern checking is enabled only with `AGENTSTACK_MONITOR_DANGER_CHECK=1`. In contrast, repeatedly unchanged pane output always counts as stasis and escalates through Escape / `C-c` → freeze → kill.

### `watch_agent_mail_signals.sh`

It uses event watching when `fswatch` is available and two-second polling otherwise. It does not delete signal files, which are server-owned dirty bits; runtime delivery state and a short lease suppress duplicate injection of the same `(agent, message)`. A periodic scan every 30 seconds recovers missed events.

The delivery target is only the tmux session whose name exactly matches the agent. After sending notification text literally, it submits with a separate `C-m` call, avoiding bare shells and unrelated sessions. tmux calls run in timeout-controlled workers so a server stall cannot stop the entire watcher.

## Differences for Codex

Codex CLI does not have Claude Code's `SessionStart` / `PreToolUse` / `PostToolUse` hook system, so `mark-agent-registered.sh` does not run. `agent-start-codex` completes identity registration and tmux rename during bootstrap; a reserved child/resume or reregistration stops when the response name does not match. Direct spawn instead reports a warning and adopts the response name, while raw MCP registration is not detected automatically. These are separate follow-ups and do not justify omitting the mail service's `passthrough` setting. The managed `~/.codex/AGENTS.md` instructs reservation reserve / renew / release behavior. The mail watcher and agent-mail registry are shared by Claude and Codex, so notifications and reservation conflicts are mutually visible.

Codex Desktop uses a further, separate plugin hook / Bridge lifecycle. See [Codex App integration](codex-app.en.md) for details.

## Related documentation

- [Installation](install.en.md)
- [Launchers and identity / Skills](launchers.en.md)
- [Codex App integration](codex-app.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)
