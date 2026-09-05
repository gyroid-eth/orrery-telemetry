# Troubleshooting

> 日本語版: [troubleshooting.md](troubleshooting.md)

[Previous: Configuration](configuration.en.md) · [Back to README](../README.en.md) · [Next: Third-party components](third-party.md)

Start with:

```bash
~/.agentstack/bin/agentstack-doctor
git -C /path/to/orrery-telemetry status --short
```

The core doctor checks the install footprint, required commands, managed blocks, managed agent names, tmux mouse support, tmux global identity environment, dashboard endpoint, and service-manager state. Use `git status` to inspect repository-side changes.

The dashboard treats a correct JSON response from `/api/version` as canonical evidence of what is actually being served and reports it separately from launchd / systemd registration and execution. If the endpoint responds but the manager is not running it, the mode is `unmanaged-background`. Check mail-watcher health separately at `/api/mail-watcher-health`.

## Reporting a bug

```bash
~/.agentstack/bin/agentstack-doctor --report
```

Paste the output verbatim from `--- copy from here ---` through `--- copy to here ---`.

**Every defect found so far came from a difference between the reporter's environment and the development machine**, and locating that difference took several exchanges each time. This output contains only the questions actually asked in those exchanges.

| Item | What it reveals |
|---|---|
| agent-mail commit and distance ahead of origin | Which code is actually running |
| `AGENT_NAME_ENFORCEMENT_MODE` | Whether the requested name passes through unchanged |
| presence of the passthrough patch | Whether that mode is accepted by the installed version |
| requested-name handling | Final decision combining #140 / passthrough / legacy behavior. `unknown` means undetermined |
| presence of the `agents.retired_at` column | Whether dashboard queries can execute |
| open-file limit | Whether the process can exhaust descriptors and exit |
| availability and versions of tmux / python3 / uv / claude / codex | Whether prerequisites are present |

**No tokens or Authorization headers are included.** Values are intentionally omitted and tests lock this behavior so the report can be pasted directly into chat. Add “what you did,” “what you expected,” and “what happened” in the final field. Pasting error text verbatim is fastest.

## Windows Mail archives exceed MAX_PATH

A long canonical project path and message subject can make an archive path
exceed Windows MAX_PATH and fail with `WinError 206`. Archive filesystem access
uses extended-length paths, while Git staging uses Git with `core.longpaths=true`
in the archive repository's local configuration. Project identities, slugs and
filenames are preserved; global Git configuration is not changed.

The Windows regression exercises message persistence, Git staging and startup
recovery beyond 260 characters. It does not establish native Windows installer,
service supervision or Codex Desktop Bridge support. UNC conversion is included
but has not been exercised against a network share.

## `NOT CONFIGURED`

The dashboard service has neither `AGENTSTACK_PROJECT_KEY` nor `AGENTSTACK_VAULT`.

Check:

1. `~/.agentstack/env.sh`
2. environment in the launchd plist / systemd unit
3. `/api/graph` after restarting the service

Repair:

```bash
export AGENTSTACK_PROJECT_KEY=/absolute/project/path
./scripts/install.sh
```

DECK tmux state remains visible without this configuration. It is an intentional degraded mode where only mail edges, history / replay, and spawn are unavailable. Output still searches the cwd / Git-root `logs/` fallback.

## Output is empty or not linked

Output files must be named `LOG_*.md`, and frontmatter `agent:` must match the dashboard's canonical agent name.

1. If `AGENTSTACK_DELIVERABLE_ROOTS` is set, confirm the service process can read each `:`-separated directory
2. Otherwise check `AGENTSTACK_PROJECT_KEY/logs/`, then the vault and cwd / Git-root fallbacks
3. If only `env.sh` changed, rerun the installer to update launchd / systemd environment
4. A non-link item is correct outside `AGENTSTACK_VAULT`; only items inside the vault receive `obsidian://` links

## launchd does not start

```bash
label=org.agentstack.agentdashboard
plist="$HOME/Library/LaunchAgents/$label.plist"
target="gui/$(id -u)/$label"

launchctl enable "$target"
launchctl bootout "$target" 2>/dev/null || true
while launchctl print "$target" >/dev/null 2>&1; do sleep 0.1; done
launchctl bootstrap "gui/$(id -u)" "$plist"
tail -f ~/.agentstack/dashboard/dashboard.log
```

Run `enable` before `bootstrap`. Bootstrapping a disabled label first can produce macOS `Input/output error`. Because `bootout` is asynchronous, wait until `launchctl print` no longer sees the job before registering it again. The installer and `agentctl.sh start` perform this order automatically, with the existing supervised-background fallback when the GUI domain itself is unavailable.

Common causes:

- another process uses port `8770`
- the plist has stale Python / PATH values
- `~/.agentstack` was moved
- only `env.sh` changed after installation
- dashboard files were copied incompletely

To change the port:

```bash
AGENTSTACK_PORT=8771 ./scripts/install.sh --port 8771
```

When the installer detects an occupied port, it stops before service registration.

## No service on Linux / WSL

**Linux and WSL are unverified.** The systemd user path and supervised-background fallback are implemented, but development uses macOS and has not exercised registration or timer startup on a real Linux host. CI on ubuntu-latest stubs `systemctl` and checks only unit generation and call order. The following is the expected design; if it fails, report the environment and output in an issue.

When a systemd user session is available:

```bash
systemctl --user status agentstack-dashboard.service
systemctl --user daemon-reload
```

In environments without systemd user support and on WSL, the installer falls back to `nohup` plus a pidfile. Ghostty click-to-jump is unavailable, but the localhost dashboard and browser terminal can work.

## `EPERM` on macOS

Suspect TCC under Desktop / Documents / Downloads.

1. Start the root agent from a terminal with Full Disk Access
2. End the existing tmux server / session
3. Recreate it from the same terminal
4. Or move the project outside protected locations

`chmod` may not repair this because the originating application identity, not file mode, is evaluated. See [Installation](install.en.md#macos-tcc--full-disk-access) for details.

## Notification text enters Codex but is not submitted

Do not combine text and submit in one `send-keys` call.

```bash
tmux send-keys -t "$session" -l "$text"
sleep 0.2
tmux send-keys -t "$session" C-m
```

Use `C-m` because the `Enter` keysym does not always submit in the Codex REPL.

Additional checks:

- whether the target pane is a Codex / Claude REPL rather than a bare shell
- backlog under `AGENTSTACK_SIGNALS_DIR`
- `watcher_running` from `/api/mail-watcher-health`
- `last_success_age_s` and `recent_results`

## Mail watcher is yellow / red

```bash
curl -s http://127.0.0.1:8770/api/mail-watcher-health
```

- no watcher process
- signals remain and the most recent success is old
- invalid agent-mail endpoint / bearer token
- target tmux session is missing

Confirm that `AGENTSTACK_MAIL_HOME` and `AGENTSTACK_SIGNALS_DIR` match between service and launcher.

## Dashboard spawn disappears immediately

1. Read the end of `dashboard/logs/spawn.log`
2. Check `tmux has-session -t '<child-name>'`
3. Check that `~/.local/bin/claude`, `codex`, or the selected CLI is visible from the service's `PATH`
4. Check `AGENTSTACK_SPAWN_SCRIPT` and working directory
5. Check `AGENTSTACK_PROJECT_KEY` / `AGENTSTACK_VAULT`
6. Check `HTTP_BEARER_TOKEN` in `AGENTSTACK_MAIL_ENV`
7. Check token state with `agentstack-reregister '<child-name>'`

The dashboard waits up to 120 seconds for the launcher's own readiness / early-death verdict, then probes the exact tmux session after the launcher succeeds. A launchd-minimal PATH often lacks `~/.local/bin`, so the spawn path prepends it.

For Codex, also check `AGENTSTACK_CODEX_MODELS` against the requested model and the effort allowlist.

## Spawn name is rejected

After removing hyphens, an explicit name must be a 2–64-character alphabetic name beginning with an ASCII letter.

- `occupied`: existing identity
- `unknown`: database / auth / transport failure
- `available`: usable

`unknown` cannot be used. Repair agent-mail and the project key before attempting another name, preserving identity continuity.

## Registered under a different name than requested

agent-mail rejected the name and replaced it with a generated one. The agent continues working, so this can remain unnoticed until another agent cannot address it.

```bash
~/.agentstack/bin/agentstack-doctor --report \
  | grep -E 'passthrough patch|requested-name handling'
```

`requested-name handling: replaced` is known legacy behavior that rejects requested names. The installer also warns in advance that a generated name will replace the requested one. `unknown` means the source is unreadable or naming behavior is unrecognized, so it does not infer either support or lack of support. Do not decide from the patch line alone: `passthrough patch: absent` can still be `honored` when #140's `validate_explicit_agent_id` exists.

The installation-time decision and evidence remain in `install-state.json` under `agent_mail.requested_name_honoring`. Bundled ORRERY Mail starts configured to accept requested names. If a legacy service still responds, stop it with `--retire-legacy-mail`, rerun the installer, and switch to the bundled service.

When `unsupported database URL` occurs on a machine whose legacy service returns a relative database URL such as `sqlite+aiosqlite:///./storage.sqlite3`, use `./scripts/install.sh --retire-legacy-mail ...` if a known legacy launchd label is loaded. After verifying that the label and plist really refer to the mail service, the installer retires it before listener-reuse checks. Without the flag, the error displays the detected label. If no label appears, it is another listener; do not guess the database path, and identify its owner with `agentstack-doctor --report`.

### Consequences of registration under another name

The agent itself works normally. Only its **addressable name** breaks.

| | |
|---|---|
| What works | Sending, receiving, file reservations, tmux session, dashboard display |
| What breaks | **The name used as a destination by parents and other agents.** `send_message` to the requested name does not arrive |
| Portrait | **Absent.** A fallback name such as `GreenLake` does not end in a scientist name, so the dashboard cannot select a face |

The missing portrait is not a defect; it is **the only visual clue for recognizing this state**. Portraits are therefore not assigned mechanically by a hash or similar method, because doing so would make a failed registration indistinguishable from a successful one.

Because expecting people to infer the problem from a missing face is too weak, **the requested name itself appears on screen**.

- **DECK**: `↯ asked for <requested name>` below the name
- **NETWORK**: the agent's name label turns amber, and hover says `requested <requested name>, registered as <actual name>`

The record is `$AGENTSTACK_RUNTIME_DIR/name-substitutions.json` and is written only when registration substitutes a name.

To recover, configure a server that accepts the name and **restart the agent**. An already registered identity cannot be renamed later.

## Registration / inbox authentication fails

Without creating another name, run:

```bash
AGENTSTACK_PROJECT_KEY=/absolute/project/path \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

Inspect:

```text
$AGENTSTACK_RUNTIME_DIR/agent_token_<name>
$AGENTSTACK_RUNTIME_DIR/child-agents/<name>.json
```

If the token is missing / stale / owned by another identity, report it to the parent or operator. Do not paste a token into chat, logs, or process arguments.

## Hook blocks with `AGENT NOT REGISTERED`

Claude Code's `check-agent-registered.sh` blocks Edit / Write / Bash until successful `register_agent` is recorded for the current `session_id`. After `/clear`, resume, or compact, read the SessionStart-hook reminder and reregister an existing identity without creating a different name.

**First distinguish which situation you are in.**

Having `AGENT_NAME` does not bypass every check. The guard evaluates **identity conflict → invalid endpoint → stopped service (outage policy) → `AGENT_NAME` exemption → flag**. `AGENT_NAME` exempts only the **flag requirement**. If there is no `AGENT_NAME`, read the next section rather than using the recovery command below.

Preferred recovery (**only for a session with the mail MCP**):

```bash
AGENTSTACK_PROJECT_KEY=/absolute/project/path \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

This command repairs remote registration but **does not write the flag file**. Only PostToolUse for the `register_agent` MCP tool (`mark-agent-registered.sh`) can write the flag. Therefore, clearing the block requires calling that MCP tool from inside the session.

After success, run your own `fetch_inbox`. If the tmux session remains `pending-*`, registration read-back and rename did not complete. Use the canonical name returned by the server, and do not automatically kill an existing same-name tmux session.

## First Edit/Write/Bash is blocked (client bypassed launcher / service stopped)

First check **whether the mail service responds**. The branch differs here.

```bash
# endpoint はインストールごとに違います。install が使っている値を見てから叩きます。
grep AGENTSTACK_MCP_URL ~/.agentstack/env.sh
~/.agentstack/bin/agentstack-doctor --report
```

`agentstack-mailctl` may be absent in some environments; it was absent on this repository's development machine for a period. **Use only commands that actually exist.**

### When the service does not respond

**Registration is impossible in principle.** Only PostToolUse for `register_agent` writes the flag, and that call cannot pass through an unresponsive endpoint. The same is true inside tmux. `agentstack-reregister` does not write the flag and is not a recovery path either.

With the default `AGENTSTACK_MAIL_OUTAGE_POLICY=warn-open`, editing in this state **continues with a warning**. Because reservations are unavailable, collisions with other agents sharing the project cannot be detected. A record remains in `~/.agentstack/runtime/logs/unmanaged_sessions.jsonl`.

Recovery is on the service side.

```bash
# 実在するものだけを絶対パスで叩きます（hook の案内も同じ基準です）
[ -x ~/.agentstack/bin/agentstack-mailctl ] && ~/.agentstack/bin/agentstack-mailctl start
[ -x ~/.agentstack/bin/agentstack-doctor ] && ~/.agentstack/bin/agentstack-doctor --report
```

With `AGENTSTACK_MAIL_OUTAGE_POLICY=block`, editing itself is rejected in this state, choosing not to permit writes when coordination is unavailable.

### When the service responds

Registration is possible, so the guard requires it. If the client has the mail MCP, calling `register_agent` clears the block; successful registration from an IDE agent panel has been observed.

If an MCP-less client is deliberately used outside coordination, set this explicitly in that client's environment.

```bash
export AGENTSTACK_UNMANAGED_SESSION_POLICY=warn-open
```

**The default is block.** Without this setting, a session with no identity must register.

## A removed hook returns after rerunning the installer

It does not return, as of 2026-08-22. The installer records its entries in `<runtime>/settings-installed-entries.json`, and **if one is absent from settings on the next run, it treats the removal as intentional and does not re-add it**. The merge output reports this in `respected_removals`.

Only when intentionally restoring it, pass:

```bash
agentstack-merge-settings ... --restore-removed
```

Without a record, as on first install or after deleting the record file, every template entry is installed as before. Absence proves intentional removal **only when there is a record that this installer previously added it**.

## Hook blocks with `FILE RESERVATION REQUIRED`

For Edit / Write under a protected root, the hook establishes exact identity and checks an existing reservation under both relative and absolute paths with renew-only semantics. It never auto-acquires. When blocked:

1. Confirm `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY` matches the project where the reservation was made
2. Confirm `AGENT_NAME`, or the tmux session explicitly selected by `TMUX_PANE`, refers to the canonical identity. A pane-metadata mismatch is `AGENT IDENTITY CONFLICT` and must be repaired first. A client outside tmux resolves identity from the session index after `register_agent`. Multiple identities tied to one session are also `AGENT IDENTITY CONFLICT`; decide which remains and reregister
3. Reserve the exact path or smallest glob with `file_reservation_paths`
4. If a conflict is returned, contact the holder through agent-mail and wait for release or expiry

The owner `registration_token` is not sent in this hook's tool arguments and is separate from the legacy HTTP bearer. `isError` succeeds only when omitted or boolean `false`. After exact identity and protected scope are established, only transport unreachability on the first query fails open. HTTP/MCP/schema rejection, malformed response, and transport failure after definitive zero block. A missing path or path outside protected roots is outside enforcement and exits 0.

Deploy the strict version only after every client is restarted/rebound at cutover C5. Raw non-tmux Claude resolves identity when `register_agent` creates a self binding in the session index. Clients that cannot register because their startup path lacks the mail MCP, and old sessions without an identity source, follow `AGENTSTACK_UNMANAGED_SESSION_POLICY`; restart through `agent-start` when coordination is required. Do not disable the guard or adopt an untargeted tmux session or stale metadata as identity.

## Spawned child cannot read its own inbox

Run the core doctor.

```bash
~/.agentstack/bin/agentstack-doctor
```

If it warns `child MCP proxy missing` or about an incomplete source tree, rerun `./scripts/install.sh`. A child with a proxy does not load its owner token into model context; the child-scoped stdio connection authenticates on its behalf. Do not mix proxy and fallback-to-shared-endpoint states.

## Codex App Bridge / cold wake does not work

Codex Desktop integration has its own doctor, runtime state, and failure classifications separate from the core doctor. See [Common failures in Codex App integration](codex-app.en.md#common-failures). A Codex CLI session's absence from the Bridge is an intentional surface filter.

## Agent appears twice on the dashboard

Check whether the tmux session name matches the agent-mail identity.

```bash
tmux list-sessions
printf '%s\n' "$AGENT_NAME"
```

If stale top-level environment may have been inherited, relaunch from a new terminal with `agent-start` / `agent-start-codex`. The launcher removes `AGENT_NAME`, `PARENT_AGENT`, tokens, and reserved markers before registration.

## History cannot be found

`/api/history` searches Claude / Codex transcripts based on agent program, then falls back to the other when absent.

- whether agent-mail program is correct
- whether the transcript remains on disk
- whether session and agent names match
- whether child and parent transcripts were confused

An agent with no transcript may show only its mail timeline.

## Terminal does not open

- check `AGENTSTACK_TERMINAL`
- check Ghostty / iTerm2 / Terminal.app installation paths
- `tmux has-session -t '<name>'`
- for a browser terminal, check `ttyd` is on PATH
- inspect the error from `/api/ptty?session=<name>`

`AGENTSTACK_TERMINAL=none` performs no OS-terminal open.

## tmux scrollback does not work

In `~/.tmux.conf`:

```tmux
set -g mouse on
set -g history-limit 50000
```

Or enter copy mode with `Ctrl+b [`. `agentstack-doctor` also checks mouse mode.

## Uninstall stops

`install-state.json` is required.

```bash
ls -l ~/.agentstack/install-state.json
~/.agentstack/bin/agentstack-uninstall --dry-run
```

Without a manifest, it does not guess what to delete, preventing accidental removal of settings or mail data.

## Related documentation

- [Installation](install.en.md)
- [Launchers and identity](launchers.en.md)
- [Hooks and operational helpers](hooks.en.md)
- [Codex App integration](codex-app.en.md)
- [Dashboard](dashboard.en.md)
- [Configuration](configuration.en.md)
