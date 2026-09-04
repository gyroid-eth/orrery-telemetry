# Launchers and identity

> 日本語版: [launchers.md](launchers.md)

[Previous: Installation](install.en.md) · [Back to README](../README.en.md) · [Next: Delegation and child agents](delegation.en.md)

## Launch commands

```bash
export PATH="$HOME/.agentstack/bin:$PATH"

agent-start ~/code/my-project
agent-start-codex ~/code/my-project
```

- `agent-start`: Claude Code
- `agent-start-codex`: Codex CLI

If the directory argument is omitted and `fzf` is available, you can select a directory under `AGENTSTACK_BASE_DIR`. Otherwise, the current directory is used.

```bash
export AGENTSTACK_BASE_DIR="$HOME/Obsidian/MyVault"
agent-start
```

The precedence order is an explicit argument, the `fzf` picker, then the current directory.

## tmux session

When launched from outside tmux, the launcher creates a new named session and replaces the current terminal tab. From inside tmux, it renames the current session and runs the CLI in place with `exec`.

Matching the session name to the agent-mail identity makes the following associations unambiguous:

- dashboard click-to-jump
- inbox signal delivery target
- transcript / history
- token recovery
- graceful EXIT / RESUME

The shell remains after the terminal process exits so that investigation and scrollback can continue.

## Scientist names

A new identity requested by a top-level launcher has the form `Adjective-Scientist` (hyphenated, for example `Windy-Fermi`). **The read-back from the `register_agent` response is authoritative for the actual registered name**, and some server environments remove the separator or coerce the request to another name. If the requested name differs from the read-back, the launcher explicitly reports the substitution and aligns a not-yet-started top-level session with the read-back name. A reserved / resumed identity that already has a task, token, and inbox attached does not adopt a different name and stops instead.

- adjectives are the 134 words in `bin/lib/agentstack-scientists.sh`
- scientists come from `dashboard/scientist_portraits.json`
- the scientist suffix is the portrait key
- only scientists whose names contain ASCII alphabetic characters are candidates

The 134 words are synchronized word-for-word with agent-mail's canonical `SIMPLE_ADJECTIVES` Round 3 list. Strict agent-mail deployments validate generated names against the canonical list, so do not add a custom word only on the AgentStack side.

The launcher, dashboard catalog, suggestion API, and child preregistration share the same adjective and scientist sources. Keeping the naming sources unique prevents drift among portraits, registered names, and server-side validation.

## Name availability and fail-closed behavior

Candidate availability has three states.

| State | Meaning |
| --- | --- |
| `available` | Confirmed that no identity with the same name exists in the project |
| `occupied` | An identity with the same name exists |
| `unknown` | Cannot be checked because of a transport failure, authentication error, timeout, unavailable database, or similar condition |

`unknown` is not treated as an available name. By default, the launcher's availability probe stops after three consecutive `unknown` results. This fail-closed design prevents acquisition of a potentially conflicting identity during a communication failure.

After the dashboard spawn checks availability on the scientist rail, it validates the full name again through `/api/suggest-name`. It normalizes the specified name by removing `-` and rejects it unless its exact status is `available`. See [Dashboard](dashboard.en.md#new-agent) and [API](api.en.md#post-apispawn) for details.

## Identity registration

The launcher registers an identity with agent-mail before starting the CLI.

1. Remove stale `AGENT_NAME`, `PARENT_AGENT`, token, and reserved marker values
2. Generate a candidate name
3. Check agent-mail health
4. Register with project key, program, model, and task metadata
5. Compare the requested name with the returned canonical name. On a mismatch, a top-level launch reports it and renames the tmux session to the returned name; a reserved identity stops
6. Update the managed agent list and clipboard

If `AGENTSTACK_PROJECT_KEY` is unset or agent-mail is unreachable, the CLI itself still starts with the preselected name. Mail, reservations, and project-scoped dashboard features are unavailable, however.

Claude Code hooks also record registration inside the session. Because Codex does not have Claude Code's hook system, `agentstack-codex-bootstrap` handles registration and tmux renaming before startup.

## Registration token

Reregistering an existing identity requires that identity's `registration_token`. A top-level token is stored with mode `0600`.

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/agent_token_<name>
```

A delegated child additionally has child-owned state at:

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/child-agents/<name>.json
```

The parent's token is not given to a preregistered child. Dashboard spawn generates a child-specific token and passes it to `spawn_child.sh --pre-registered` through a temporary mode-`0600` token file. This keeps the token out of transcripts, command-line arguments, and dashboard responses.

The default `/delegate` path is `--pre-registered --embed-task --task-file <path>`. The parent writes the complete task to a temporary mode-`0600` file, and the launcher embeds it into the first Claude / Codex prompt together with the child name, parent name, spawn time, project key, and instruction to use `send_message` at completion. There is no registration, reregistration, or `fetch_inbox` startup ritual. This prompt is the only source of truth, so do not send the same child a separate task mail. `--task-file` takes precedence over the positional task argument and is also the boundary that prevents the shell from interpreting backticks or `$()` in the task.

`CHILD_REGISTRATION_TOKEN` is a historical variable name, but it is also used to reauthenticate top-level identities.

## Reregistration

```bash
AGENTSTACK_PROJECT_KEY=/path/to/project \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

The helper reads the owner token from runtime state and restores the identity with the same name. Do not create a different name when same-name registration fails. A different name separates the inbox, thread, reservations, and audit history.

## `CLAUDECODE` guard

The launcher and child spawner set the following in each tmux session's environment:

```text
CLAUDECODE=1
```

This guard prevents an interactive shell's exit hook from cascading into termination of the entire tmux server.

The value is set with `tmux new-session -e` when the session is created, not in the tmux server's global environment, so that identities from other sessions cannot mix.

## Codex-specific startup

`agent-start-codex` performs the following steps.

- source `agentstack-codex-bootstrap` for registration and renaming
- fix the working directory with `codex -C <dir>`
- pass `--sandbox ${AGENTSTACK_CODEX_SANDBOX:-workspace-write}`
- pass `--ask-for-approval ${AGENTSTACK_CODEX_APPROVAL:-on-request}`
- pass `--add-dir` only when `AGENTSTACK_VAULT` exists
- remove `OPENAI_API_KEY` and prefer ChatGPT OAuth

Because an API key in the environment can override OAuth, it is removed only from the Codex subprocess.

## Mail watcher and REPL injection

When the mail watcher finds an agent-mail signal, it injects notification text into the corresponding tmux session's Claude / Codex REPL.

Text and submission are separate operations.

```bash
tmux send-keys -t "$session" -l "$text"
sleep 0.2
tmux send-keys -t "$session" C-m
```

Codex may not treat the `Enter` keysym as submission, so the watcher uses `C-m`. It avoids accidental injection into a bare shell and runs tmux calls in workers with timeouts.

## Skills (2) and file reservations

The installer places the following skills in the canonical `~/.agentstack/skills` directory and creates absolute symlinks from Claude Code's standard discovery path, `~/.claude/skills/<name>`, to each canonical copy.

- [`/delegate`](../skills/delegate/SKILL.md): declare and reserve resources, then launch and monitor a Claude / Codex child with an optional model and worktree
- [`/log`](../skills/log/SKILL.md): organize session decisions, changes, verification, and next actions into a reusable Markdown log

A Claude Code session that was open before installation does not discover the newly added skills. Run `/exit` once and relaunch with `agent-start <project>` from a new terminal.

### `/delegate`

`/delegate` is not merely a shortcut that launches a child.

AgentStack delegation must be entered with the leading slash as `/delegate ...`. `delegate ...` is an ordinary prompt, not an invocation of this skill. If Claude handles it with a built-in subagent / Agent tool, it may produce an artifact, but it does not create AgentStack identity, reservations, a dedicated tmux session, or dashboard telemetry. Do not use the built-in Agent tool in place of `/delegate` when the objective is to create an AgentStack-monitored child.

| Item | Details |
| --- | --- |
| Trigger | A request to delegate to a child, launch a subagent, or perform parallel work |
| Basic form | `/delegate "<task>" [--dir <path>] [--codex] [--model <model>] [--worktree] [--worktree-base <rev>]` |
| Required prerequisites | The parent's agent-mail identity and canonical project key. Editing tasks require a resource declaration and reservation |
| Optional prerequisites | `--worktree` requires a Git repository; dashboard annotation requires the dashboard service |

The parent agent does not finish when it hands off the task. It remains responsible for deciding scope and risk, making reservations, monitoring, and verifying the artifact. Use `--codex` for a Codex child, `--model` for an allowed model, and `--dir` to choose the child's working directory.

The model generation names in `spawn_child.sh`'s model catalog are canonical. For Claude, an omitted model or `opus` means `claude-opus-5`, and `sonnet` means `claude-sonnet-5`; for Codex, an omitted model or `sol` means `gpt-5.6-sol`. `terra` / `luna` are aliases for the corresponding `gpt-5.6-*` models. Full IDs for older generations remain valid for compatibility, but the warm pool is claimed only for an exact match with the current 200K Opus / Sonnet entries in the catalog.

1. Determine risk and monitoring cadence from the target resources, exclusivity, failure points, and reversibility
2. Create a child-owned token and canonical name with `agentstack-preregister-child`
3. Prepare the file reservation, contact, and mode-`0600` canonical task file
4. Launch Claude / Codex with its model and worktree through `spawn_child.sh --embed-task --task-file` (do not send task mail)
5. Read the agent-mail completion report and `monitor_child_agent.sh`, then verify the artifact yourself
6. Release the reservation before reporting the parent's result

A worktree child's cwd changes to `/tmp/cc-worktrees/<name>`, but its agent-mail project does not change. The task must identify `AGENTSTACK_PROJECT_KEY` / `PROJECT_KEY` as canonical. `--worktree-base <rev>` fixes the baseline for multiple children.

The monitor's dangerous-command detection is passive by default. When enabled with `AGENTSTACK_MONITOR_DANGER_CHECK=1`, a match causes a soft stop. Repeated stasis with unchanged output escalates through soft stop, `C-c`, process-group freeze, then session kill regardless of that setting. See the skill text for the exit codes.

### `/log`

| Item | Details |
| --- | --- |
| Trigger | A request to create a session log, summarize current work, or save decisions, changes, and verification |
| Basic form | `/log <theme> [project]` |
| Required prerequisites | A theme. Ask only when the project is not obvious and cannot be inferred safely |
| Optional prerequisites | Obsidian mode requires `AGENTSTACK_OBSIDIAN_APP` and an `AGENTSTACK_PROJECT_KEY` inside the vault |

`/log` uses Obsidian mode only when `AGENTSTACK_OBSIDIAN_APP` and `AGENTSTACK_PROJECT_KEY` are both set and the project is inside the vault. It connects to existing project `logs/` and daily-note conventions when present, and does not guess a private directory structure when no convention is found.

Otherwise, it writes to:

```text
<git-root-or-cwd>/logs/LOG_<YYYY-MM-DDTHHmm> <Theme>.md
```

The log is not a transcript. It focuses on Goal, Decisions, Work Performed, Verification, Related Notes, and Next Actions.

### Hooks and reservation enforcement

Claude Code hard-blocks `Edit` / `Write` through the `check-file-reservation.sh` PreToolUse hook. Codex has no equivalent hook, so the managed `~/.codex/AGENTS.md` instructs reserve / renew / release discipline. The registry is shared, so Claude and Codex reservations are mutually visible.

See [Hooks and operational helpers](hooks.en.md) for the trigger timing, caller, block conditions, and cleanup lifecycle of the repository's 11 hooks / helpers.

## Related documentation

- [Hooks and operational helpers](hooks.en.md)
- [Codex App integration](codex-app.en.md)
- [Dashboard](dashboard.en.md)
- [Configuration](configuration.en.md)
- [Troubleshooting](troubleshooting.en.md)
