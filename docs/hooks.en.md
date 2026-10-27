# Hooks and operational helpers

> 日本語版: [hooks.md](hooks.md)

[Previous: Delegation and child agents](delegation.en.md) · [Back to README](../README.en.md) · [Next: Codex App integration](codex-app.en.md)

`hooks/` contains hooks that run automatically from Claude Code lifecycle events, operational helpers called explicitly by launchers, the dashboard, and skills, and the internal libraries and workers sourced or started by event hooks. There are nine Claude Code event hooks.

[`hooks/settings.template.json`](../hooks/settings.template.json) defines event-to-command mappings, and [`hooks/README.md`](../hooks/README.md) defines the safety policy for settings merges. This document is the user reference explaining when each component actually runs and what it guarantees.


`agentstack-resume` changes a stopped child through `/api/jump`; `child_resume.py` owns the change generation and generated-setting rollback. See [tool changes](delegation.en.md#resume-a-stopped-child-with-different-tools).

## Claude Code event hooks (9)

After the installer merges `settings.template.json` into `~/.claude/settings.json`, the following events run automatically.

| Event / matcher | Executable | Trigger timing | Primary behavior |
| --- | --- | --- | --- |
| `SessionStart` | [`set-ghostty-title.sh`](../hooks/set-ghostty-title.sh) | Immediately after startup / resume / `/clear` / compact | Apply a known identity to pane metadata, the tmux session, and the managed agent list |
| `SessionStart` | [`session-start-reminder.sh`](../hooks/session-start-reminder.sh) | Same as above, after the title helper | Check ORRERY Mail health and the existing identity, then output route-aware guidance for embedded tasks, bound proxies, and raw/direct connections into session context |
| `PreToolUse` / `Edit|Write` | [`check-file-reservation.sh`](../hooks/check-file-reservation.sh) | Immediately before Claude Code edits a file | Check an existing exact-path reservation inside a protected root with renew-only semantics. Retry zero results once, then block with exit 2 if the result remains zero |
| `PreToolUse` / `Edit|Write|Bash` | [`check-agent-registered.sh`](../hooks/check-agent-registered.sh) | Immediately before an edit, write, or shell command | Check a session flag to confirm that the current Claude session has called `register_agent`. Block an unregistered session with exit 2 |
| `PreToolUse` / `Bash` | [`deny-llm-key-read.sh`](../hooks/deny-llm-key-read.sh) | Before shell execution | Refuse value reads matching model credential references |
| `PreToolUse` / reservation tools | [`invalidate-release-debounce.sh`](../hooks/invalidate-release-debounce.sh) | Immediately before acquiring or renewing a file reservation | Invalidate the token of an older release worker for the same agent/path, preventing a race that would remove the new reservation immediately |
| `PostToolUse` / `Edit|Write` | [`release-file-reservation.sh`](../hooks/release-file-reservation.sh) | Immediately after a successful file edit | After the default 90-second grace period, release the reservation using the same project, identity, and relative/absolute path as the guard |
| `PostToolUse` / `register_agent` | [`mark-agent-registered.sh`](../hooks/mark-agent-registered.sh) | Immediately after a response from `mcp__orrery-mail__register_agent` or a compatible tool | Validate the response and update the session flag, session index, and pane / tmux metadata only after an exact match with an explicitly requested name |
| `SessionEnd` | [`release-all-reservations.sh`](../hooks/release-all-reservations.sh) | When the Claude session ends | Release every file reservation for the current identity. Do not retire the identity itself |

Both PreToolUse hooks run for `Edit` / `Write`. A registered session cannot write without a reservation, and a session with a reservation cannot write if it is unregistered. The PostToolUse release is armed only after success and does nothing after failure, blocking, or an edit outside a protected root.

The installer distributes the endpoint and transport credential selector in the same generated `env.sh`. ORRERY Mail generates `AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled`, and hooks plus `spawn_child.sh` / `cleanup-child-agent.sh` connect to the endpoint without an Authorization header. The agent owner token is a separate identity credential and does not change the boundary between child token files and tool arguments. The old Keychain service remains only as a fallback read path for existing environments.

### `set-ghostty-title.sh`

- **Trigger:** `SessionStart`. It is also called in the background after `mark-agent-registered.sh` obtains the current session's own canonical name.
- **Behavior:** Uses `AGENT_NAME` or an argument and writes identity metadata for each `TMUX_PANE` to the runtime directory. tmux rename is allowed only for `pending-*` sessions, so an established parent session is not overwritten with a child name.
- **On collision:** Does not kill a same-name tmux session; it refuses the rename and fails closed. If the name cannot be resolved, it does nothing.

### `session-start-reminder.sh`

- **Trigger:** Every `SessionStart` source, including startup, resume, `/clear`, and after compaction.
- **Behavior:** Resolves identity in the order `AGENT_NAME` → pane metadata → exact tmux session and checks ORRERY Mail liveness. When an owner token and project key are available, it reregisters the same identity from the shell. It then tells the model to use only the first matching route: an embedded task that explicitly says registration is complete and no ritual is required, the bound proxy shown by the provided tool schema, or raw/direct MCP. An embedded task is not excluded merely because there is no parent. Shell registration and authentication of a raw model-side MCP tool session are separate.
- **Route boundary:** A child proxy configuration artifact is only a hint that makes the reminder more specific; the descriptions and argument schemas of the tools actually provided to the model remain authoritative. A bound proxy is never told to run the helper, register again, or read a token file, and a proxy failure does not trigger automatic raw/helper fallback. Only an existing identity on confirmed raw/direct MCP uses the token-safe helper, separately from new raw registration. A generic registration failure is not labelled as a stale token.

### `check-file-reservation.sh`

- **Trigger:** Immediately before `Edit` / `Write`. For managed launches, enforcement applies to the computed `AGENTSTACK_PROTECTED_ROOTS`: deliberate `AGENTSTACK_EXTRA_PROTECTED_ROOTS` in configured order, then the actual launch workspace. The first matching root determines the relative reservation name. Managed hooks compare canonical file/root paths; legacy/direct sessions keep their existing root and path-resolution behavior, including the project-root fallback when no roots are set. A path-shaped namespace is not an extra protected root for a new managed launch. See [protection migration](configuration.en.md#reservation-protection-and-migration) before restarting older sessions.
- **Project key:** Resolved in the order `AGENTSTACK_PROJECT_KEY` → `PROJECT_KEY` → `AGENTSTACK_PROJECT_KEY` in `${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh` → cwd from hook input. The installed `env.sh` is not sourced as shell code; only the relevant `export AGENTSTACK_PROJECT_KEY=...` is read literally. The registration guard, session reminder, child cleanup, and `agentstack-await-reply` share this resolver, so an editor session that bypasses the launcher still refers to the same project selected at installation.
- **Identity:** Prefers `AGENT_NAME`. Otherwise it explicitly obtains the target pane's tmux session through `TMUX_PANE` and uses pane metadata only to confirm a match. If metadata and session differ, either is a placeholder, or resolution fails, it blocks with exit 2 before sending HTTP. It never uses an untargeted ambient tmux session. A client outside tmux can resolve identity from hook input `session_id` if it called `register_agent` and was recorded under `<runtime>/session_index/` (precedence: env → tmux → session index). A `session_id` containing anything outside `[a-zA-Z0-9_-]` is not used, and symlinked index entries are not read. Sessions with no identity source follow the “unmanaged session” rules below.
- **Behavior:** Checks existing reservations by both relative and absolute path using renew-only semantics, then reads active foreign exclusive reservations and refuses overlaps with their holders in the reason. It neither reads the owner's `registration_token` nor sends it in tool arguments. A legacy HTTP bearer is a separate transport credential and is not sent to a native endpoint whose generated selector is `disabled`. A zero result is retried once to account for asynchronous commits; the guard never auto-acquires a reservation.
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
- **Writes through Bash or scripts:** Claude's reservation hooks cover only `Edit` / `Write`; use those tools for reserved files. Writes through `Bash`, scripts, or other tools are neither checked nor automatically released by these hooks. Release the reserved files yourself immediately after writing: bound proxy `release_reservations`, raw/direct `release_file_reservations`.
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
| [`cleanup-child-agent.sh`](../hooks/cleanup-child-agent.sh) | Immediately after the child REPL command started by `spawn_child.sh` ends | Confirm reservation release and identity retirement before removing generated state; preserve state and credentials and return nonzero on failure |
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

`AGENTSTACK_CHILD_DEFAULT_TOOLS` applies only to CLI launches without `--base` / `--tools` / `--codex-mcp`. The final directory and provider determine which default items are usable; omissions appear in spawn output and first instructions. Explicit selections and resume retain their fail-closed path. See [defaults](delegation.en.md#installer-defaults).

### `cleanup-child-agent.sh`

This helper is chained after the child's Claude / Codex command and runs only when the REPL returns. It releases all reservations and retires the identity with the child's owner token. Remote release / retirement and managed-list updates remain best-effort and run before local child state is handled.

Claude / Codex children configured with `AGENTSTACK_CHILD_RESUME_RETENTION_DAYS=0` fully delete state, token, MCP configuration, and isolated home. By default a Claude / Codex child deletes its MCP configuration, home, and proxy runtime but retains mode-0600 state containing the schema version, `retired_at`, `resume_expires_at`, and non-secret provenance together with the canonical owner credential. If safe retention cannot be finalized, cleanup does not guess and delete the credential; it leaves private state for explicit recovery or purge and exits nonzero. Bound session receipts and transcripts are outside both cleanup and purge scope.

The shared `child_resume.py` helper uses one lock and validation path for fresh-spawn / resume state preparation, private-material checks, rebuilding a child home from the current source home, expiry maintenance, and explicit purge. Before building a Codex child home, it creates `sessions/`, `history.jsonl`, and `session_index.jsonl` in the user's Codex home (`CODEX_HOME`, or `~/.codex` when unset), then shares them through symlinks. Existing records are never overwritten. Records survive cleanup even on a fresh machine that only runs Codex through children. If a legacy child home contains a real `sessions/` directory, cleanup warns on stderr that conversation history may be lost and shows the path before deleting it. Operator entry points are `agentstack-purge-child-resume <agent>` and `agentstack-purge-child-resume --expired`. `agentstack-doctor` reports expired entries but never invokes purge.

This is not a Claude Code `SessionEnd` hook. Because `SessionEnd` can occur during a crash or resume, remote identity retirement is not tied to that event.

### `monitor_child_agent.sh`

This is a one-shot monitor, not a resident daemon. The parent invokes it repeatedly at a cadence appropriate to risk. Exit codes are `0` continue, `10` shell return, `11` session disappearance, `20` warning, `30` soft stop, `40` send `SIGSTOP` to the process group, and `50` kill the session.

Dangerous-command pattern checking is enabled only with `AGENTSTACK_MONITOR_DANGER_CHECK=1`. In contrast, repeatedly unchanged pane output always counts as stasis and escalates through Escape / `C-c` → freeze → kill.

### `watch_agent_mail_signals.sh`

It uses event watching when `fswatch` is available and two-second polling otherwise. Runtime delivery state and a short lease suppress duplicate injection of the same `(agent, message)`. On success the watcher deletes per-message signals; `fetch_inbox` clears legacy signals. A successful session inventory that confirms an absent recipient removes that notification signal and stops retries; Mail messages and inboxes are unchanged. Timeouts, denied or uncertain lookups, and failed headless delivery remain retryable. A legacy signal refreshed after reading is preserved. Lease guards share 256 fixed buckets. On each scan, the standard `mail-watcher.log` is checked: above 5 MiB its last 5 MiB replace `.1`, then the open log is truncated on the same inode. A periodic scan (30 seconds on macOS, two seconds on Linux) recovers missed events. See [troubleshooting](troubleshooting.en.md#mail-watcher-is-yellow--red) for health classification and optional cleanup of old signals with absent recipients.

When updating the guard scheme, stop the old watcher and its delivery workers through their usual manager before starting the new watcher. Old guard files are not removed automatically; do not delete locks while old workers still run.

The delivery target is only the tmux session whose name exactly matches the agent. After sending notification text literally, it submits with a separate `C-m` call, avoiding bare shells and unrelated sessions. tmux calls run in timeout-controlled workers so a server stall cannot stop the entire watcher.

## Differences for Codex

The optional ORRERY Codex CLI plugin includes a `PreToolUse` / `^apply_patch$` reservation guard. Its adapter extracts every Add / Update / Delete path and both endpoints of a Move from the patch in `tool_input.command`, resolves them against the payload's absolute cwd, and passes them to [`check-file-reservation.sh`](../hooks/check-file-reservation.sh). Header and filename extraction follows the state rules of Codex 0.159.2: leading filename spaces are preserved, indented Add/Delete headers are recognized, and Update context lines remain context. Environment-selection patch headers are refused. Delete and Move sources check the directory entry (resolving its parent), while writes check the physical target. Global mode refuses deletion of a final symlink because its server currently normalizes that entry to the target.

Only path extraction is Codex-specific: identity, protected roots, renew-only ownership checks, the single definitive-zero retry, and unmanaged/outage policy use the Claude implementation. In legacy mode the shared guard uses the same `renew_file_reservations` response to verify ownership and overlapping ACTIVE foreign exclusive leases, naming their holders. Mail renews and checks conflicts in one transaction; `conflict_check` includes the version, project, checked paths, and conflicts.

Pattern semantics live only in Mail's `_patterns_overlap`. The hook has no copy of that matcher. Real Mail HTTP tests cover directory patterns, double stars, slash normalization, character classes, and Unicode. Rejections from a reachable Mail server, malformed results, and scope mismatches fail closed. Renew does not follow redirects; body-read failures after the first HTTP response also refuse. Unreachable Mail before a response follows the existing `AGENTSTACK_MAIL_OUTAGE_POLICY`, with or without an identity: `warn-open` allows with a visible warning, while `block` refuses. A retry failure after definitive zero still refuses. Unmanaged policy and existing global-mode policy stay unchanged. Shared context parsing uses the configured `AGENTSTACK_PYTHON` and never treats a startup or parse failure as permission. Global mode uses its existing common client and server coverage/exclusivity rules.

**Deployment order:** update Mail before deploying these hooks. A new hook with old Mail refuses a missing `conflict_check` and asks for a Mail update. An old hook (before #269) with new Mail ignores the additive field and behaves as before. Resources remain unpublished; no reservation listing is exposed for the hook.

Codex treats exit 2 and stderr as a refusal before editing, so one unreserved target blocks the entire patch. The adapter bounds all path checks to 25 seconds and refuses on failure. Shell writes remain outside this reservation guard in both CLIs. Codex reservation release remains an explicit model action; this change adds no automatic release.

Protection requires the plugin to be installed and enabled, and **a human to review and approve the current hooks in `/hooks`, followed by a new Codex process**. After a core update, use [plugin-only refresh](codex-app.en.md) and review the updated hooks. Startup and updates do not approve hooks automatically. `agentstack-doctor` warns when the guard is missing, disabled, unreviewed, or unknown; it reports current trusted/enabled protection only after a read-only `hooks/list` observation. Older plugins without the guard report missing. When warned, confirm reservations explicitly before editing and do not assume `apply_patch` is guarded.

The official `SessionStart` hook still records history receipts. It is separate from Claude's registration `PostToolUse`; `mark-agent-registered.sh` does not run. `agent-start-codex` completes identity registration, tmux rename, and launch expectation during bootstrap; reserved child/resume and reregistration stop on a response-name mismatch. Direct spawn warns and adopts the response name, while raw MCP registration is not detected automatically. The managed `~/.codex/AGENTS.md` reserve / renew / release instructions remain necessary. Mail watcher and the registry are shared by both CLIs.

Codex Desktop uses a further, separate plugin hook / Bridge lifecycle. See [Codex App integration](codex-app.en.md) for details.

## Related documentation

- [Installation](install.en.md)
- [Launchers and identity / Skills](launchers.en.md)
- [Codex App integration](codex-app.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)

## Local entry for deterministic daemons

The operator-only CLI in [daemon-agents.en.md](daemon-agents.en.md) reuses the bound proxy and canonical await-reply primitive. Ordinary lifecycle hooks and child registration are unchanged; live deployment requires separate approval.

The [absolute reservation candidate](absolute-reservations.en.md) supplies explicit guard/release adapters and a shared normalizer. Existing hooks remain in legacy mode and do not automatically call the candidate helper. Applying reservations to all folders of participating sessions and connecting the real binding resolver belongs to the later cutover PR.

For explicit isolated launch, delivery and operator recovery, see [global delivery client](global-delivery-client.en.md). Legacy remains the default.

SessionStart reports only legacy-block counts. Bare sessions do not register with Mail; editing guards keep their policy. [Details](instruction-migration.en.md#legacy-blocks).

For new isolated-global launches, SessionEnd does not prove provider exit. The wait runner saves the actual outcome and completes the single report intent before cleanup. Existing standalone hooks retain their behavior. See [dedicated launch and termination recovery](global-delivery-client.en.md#dedicated-launch-and-termination-recovery).

## Keep model credential reads out of the agent shell

Claude Bash and the Codex plugin's exec_command / shell_command / shell use the
same `deny-llm-key-read.py` decision. After updating the Codex plugin, a person
must review/approve `/hooks` and start a new process; installation does not approve hooks.

The guard reads references, never credential values. It defaults to
`$AGENTSTACK_HOME/integrations/dsh/backend.json` (`backends.*.keychain`). For an
existing definition elsewhere, set `AGENTSTACK_LLM_KEY_DEFINITIONS` in the launch
environment to that backend JSON, an R3 TOML profile, or a directory of those profiles.
R3 `claude/codex.auth_command` references are supported. This points to existing
configuration rather than duplicating a credential list. No definitions means no-op.

The shared matcher handles macOS `security find-generic-password -w/-g` and
Linux/WSL `secret-tool lookup`. Omitted or variable selectors that could select
the protected key are refused. Attribute-only queries, other keys and config
grep are allowed. Direct auth-helper execution through a model shell tool is
refused, while launcher/key_cmd subprocesses do not invoke this hook.

Inspection failures deny credential-extraction shapes only; unrelated shell
work continues. This limits key exposure without making broken configuration
stop every command. Diagnostics never echo commands or credential references.
Native dsh/Hermes adapters also pass tool names and inputs to this same decision. It accepts lowercase `bash` / `terminal` and `read` / `read_file`. `AGENTSTACK_LLM_KEY_FILES` is a JSON array of absolute provider/credential file references; ordinary shell/native reads of those paths are refused without opening their contents. Relative paths use the payload cwd.

Known limits: encoded commands, arbitrary script internals and reads through tools that bypass the adapter are not analyzed. This is a mistake guard, not OS isolation.

The scan finds extraction forms throughout the command, including control-flow and brace prefixes. Known limits also include conservative refusals of quoted heredoc examples and credential-looking text when the interpreter is unavailable.
