---
name: delegate
description: Delegate a bounded task to a child Claude or Codex agent, prepare risk-aware instructions, spawn the child, annotate it in the dashboard, monitor progress, and verify completion.
allowed-tools: Bash, CronCreate, CronDelete, CronList, Read, Grep, Glob, mcp__orrery-mail__send_message, mcp__orrery-mail__fetch_inbox, mcp__orrery-mail__set_contact_policy, mcp__orrery-mail__macro_contact_handshake, mcp__orrery-mail__respond_contact, mcp__orrery-mail__file_reservation_paths, mcp__orrery-mail__release_file_reservations, mcp__orrery-mail__renew_file_reservations
user-invocable: true
---

# Delegate A Task

Use this skill when the user asks you to delegate work to another agent, spawn a child agent, or run a parallel implementation/review/research task.

The goal is not only to launch a child. The parent agent remains responsible for scoping the task, reducing collision risk, monitoring the child, reading the result, and reporting a verified outcome to the user.

## Environment

Use these variables instead of hard-coded personal paths:

- `AGENTSTACK_HOME`, defaulting to `$HOME/.agentstack`
- `AGENTSTACK_PROJECT_KEY`, the project or vault path shared by all cooperating agents
- `PROJECT_KEY`, usually injected by `spawn_child.sh` and expected to match `AGENTSTACK_PROJECT_KEY`
- `AGENTSTACK_PORT`, used by the dashboard annotation API
- `AGENTSTACK_PREREGISTER_CHILD`, defaulting to `${AGENTSTACK_HOME}/bin/agentstack-preregister-child`
- `AGENTSTACK_SPAWN_SCRIPT`, defaulting to `${AGENTSTACK_SPAWN_SCRIPT:-$AGENTSTACK_HOME/hooks/spawn_child.sh}`
- `AGENTSTACK_RUNTIME_DIR`, used by monitor state

If the child runs outside the project directory, explicitly tell it to use `$PROJECT_KEY` or `$AGENTSTACK_PROJECT_KEY` wherever an ORRERY Mail call takes a project key, and in its completion message. Do not let the child infer the project from its current working directory. (A child launched with `--embed-task` is already registered and does not call `ensure_project` or `register_agent` at all.)
`AGENTSTACK_PROJECT_KEY` must be set before spawning. It is the ORRERY Mail project identity and may be different from the code worktree or the child's current working directory.

## Naming Rules

- Do not use `create_agent_identity` for delegate children.
- Delegate children are registered by the preregistration helper (`agentstack-preregister-child`, section 3), which calls `ensure_project` and `register_agent(name=<Adjective-Scientist>, program=...)` itself. You do not call those tools. The name has no `cc-` or `cx-` prefix; the program type is recorded in `program`.
- Generate names through the stack picker (`bin/lib/agentstack-register.sh` or `spawn_child.sh`) so the suffix matches a bundled dashboard scientist portrait.
- After spawning, verify that the name the helper returned, the tmux session name and the dashboard entry all match, and later that the completion message comes from that name. A child started with `--embed-task` does not read its inbox at startup, so there is no startup fetch to check.

## Usage

```bash
/delegate "<task>"
/delegate "<task>" --dir <working-directory>
/delegate "<task>" --codex
/delegate "<task>" --codex --codex-mcp orrery-only
/delegate "<task>" --model <model-name> [--effort <level>]
/delegate "<task>" --worktree
/delegate "<task>" --worktree --worktree-base <rev>
/delegate "<task>" --claude-chrome-device <deviceId>
```

### How to read the arguments

Users type this skill tersely, often without flags: `/delegate codex terra fix the flaky test`. Read the words in this order, and never invent a child name from them.

| Word in the arguments | Meaning |
| --- | --- |
| `codex` | `--codex` (a Codex child) |
| `claude` | a Claude child (the default) |
| `sol`, `terra`, `luna`, `astra` | Codex model shorthand: `--codex --model <word>`. The launcher expands them to `gpt-6.1-sol` (`gpt-6-sol` on an older CLI; see the default policy below), `gpt-5.6-terra`, `gpt-6-luna`, `gpt-6-astra` |
| `opus`, `sonnet`, `haiku` | Claude model shorthand: `--model <word>` |
| `low`, `medium`, `high`, `xhigh`, `max`, `ultra` | `--effort <word>` (Codex reasoning effort) |
| `--codex-mcp inherit` | Keep the user's configured MCP servers and plugins in a Codex child (the default) |
| `--codex-mcp orrery-only` | Keep authenticated ORRERY Mail and the session-binding plugin, and disable other inherited MCP servers and plugins |
| `--claude-chrome` | Start a Claude child with `--chrome` (Claude in Chrome). Claude children only |
| `--claude-chrome-device <deviceId>` | Same, and tell the child which connected browser to use |
| anything else | part of the task text |

The child's name is never taken from the arguments. Only an explicit `--name <Adjective-Scientist>` names a child; otherwise the registration helper picks one. A word such as `terra` is a model, not a name.

Model defaults: Claude children use `claude-opus-5-5`; Codex children prefer `gpt-6.1-sol` at effort `xhigh` when the selected CLI is 0.159.0 or later (fresh catalog evidence is used if its version is unknown), otherwise `gpt-6-sol`. GPT-6.1 Sol requires Codex CLI 0.159.0 or later; doctor reports fallback and update guidance. Pass the same `--model` (and `--effort`) both to the registration helper and to `spawn_child.sh`, so the roster and the running process agree. For Codex, resolve the CLI and model together before registration with the public `resolve` command below (use `""` for omission). It uses the same environment → saved `env.sh` → usable PATH selection and child login shell as the spawner. Pass the returned formal ID to both steps and pin the returned `AGENTSTACK_CODEX_BIN` for the spawn. Do not derive the omitted default from candidate order.

MCP defaults are deliberately backward-compatible: omit `--codex-mcp` or use `--codex-mcp inherit` to preserve the user's configured MCP/plugin surface. Use `--codex-mcp orrery-only` for a Codex child whose task needs shell/files plus ORRERY coordination but no inherited browser, application, or account tools. This is an explicit capability reduction: do not select it when the task depends on a plugin skill or any non-ORRERY MCP server.

## 1. Analyze Risk Before Spawning

Before starting the child, write down a brief risk profile:

- Target resources: exact files, directories, APIs, databases, browser tabs, or external tools the child may touch.
- Exclusivity: whether each resource must be reserved exclusively.
- Likely stuck points: startup, inbox read, dependency setup, long tests, ambiguous decisions, permission prompts, or external services.
- Sensitivity and reversibility: whether the task could overwrite data, delete files, publish content, spend money, or call outside services.
- Monitoring interval: high risk should be watched more frequently than low risk.

Suggested monitoring cadence:

| Risk | Startup checks | Steady-state checks |
| --- | --- | --- |
| high | every 1 minute until stable | every 2 minutes |
| medium | every 1 minute until stable | every 3 minutes |
| low | every 1 minute until stable | every 5 minutes |

For file edits in a shared project, reserve the relevant paths before spawning when an ORRERY Mail reservation tool is available.

## 2. Prepare The Child Task

Give the child a short role label and a concrete task description. Keep them separate:

| Field | Purpose |
| --- | --- |
| `role` | Short dashboard label, such as `api-migrate`, `review`, `tests`, or `docs` |
| `task` | Concrete instruction stored in a task file and embedded in the launch prompt |
| `group` | Shared cluster name for a set of related child agents |

Embedded task template:

```markdown
## Role: <role>
## Task summary: <same task text used for register_agent.task_description>

## Work
<specific request, constraints, files, and expected output>

## Coordination
- Use project_key from `$PROJECT_KEY` or `$AGENTSTACK_PROJECT_KEY`: <value if known>.
- Registration is already complete; do not call ensure_project, register_agent, or fetch_inbox.
- Treat the launch prompt and its embedded task as canonical. There is no task mail.
- Do not modify files outside the declared scope.
- Report completion to <parent-agent-name> with `send_message` at importance `high`, including changed paths, summary, and verification.
```

Use generic task examples such as code review, API migration, test-suite repair, documentation cleanup, or data import validation. Avoid embedding organization-specific project lore in the delegated task unless the current user request requires it.

## 3. Register, Open Contact, Reserve, And Spawn

### Who does what

Registering a child is a separate identity's lifecycle, not a re-registration of
yourself. The rules against running a helper, registering again or reading a
token file are about **your own** identity; they never forbid pre-registering a
child.

| Operation | Route |
| --- | --- |
| Your own messages, `whois` and reservations | The ORRERY Mail connection you were given. With a bound proxy, use its tools exactly as their schema says; do not add caller identity, project or token fields |
| Registering the new child identity | The shell helper `agentstack-preregister-child` (below). It registers the child with its own new token inside the helper; you never see or pass a token, and it does not use your proxy |
| Starting the child | `spawn_child.sh --pre-registered <name> --child-token-file ... --embed-task --task-file ...` |
| Re-registering yourself | Never as part of delegation. If your own proxy fails, report that; do not switch to a helper or raw tools for yourself |
| Contact approval for a restrictive policy | Only when it is actually needed (step 4), through the contact tools you have or the operator |

**What this skill needs:** a shell (for the helper and the launcher) and your
own send/fetch connection. A bound proxy that has no `register_agent`,
`ensure_project` or contact tools is the normal case and is **not** a reason to
stop: the helper does the registration.

This is the canonical flow, not one option among interchangeable transports.
If the preregistration helper or the launcher is missing or fails,
report the exact failure and stop delegation. Do not inspect mailbox files or
the ORRERY Mail database, start an ad hoc watcher/poll loop, inject the task into
tmux, use a built-in child tool, or invoke the launcher's direct mode as a
substitute.

For a Codex child, resolve the requested model before step 3, in the same shell environment that will run the launcher:

```bash
CODEX_LAUNCH_POLICY="$(python3 "$AGENTSTACK_HOME/dashboard/codex_models.py" resolve "<requested model or empty string>")" || exit 1
CODEX_MODEL="$(printf '%s' "$CODEX_LAUNCH_POLICY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["model"])')" || exit 1
CODEX_BIN="$(printf '%s' "$CODEX_LAUNCH_POLICY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["codex_bin"])')" || exit 1
```

The JSON also reports `cli_version` and `default_source` for diagnosis. An empty `codex_bin` means resolution did not find a usable CLI within its short budget; version-unknown selection then uses fresh catalog evidence. The launcher retains its own usability checks and reports a missing CLI before starting a child.

1. Verify `AGENTSTACK_PROJECT_KEY` is set to the shared project key, not a random cwd.
2. Let the helper name the child. Omit `--name` and it draws a free `Adjective-Scientist` name from the same picker top-level registration uses, so the name always has a dashboard portrait. Only pass `--name` when the caller needs a specific existing identity; an off-list name is accepted with a warning (no portrait), or rejected outright under `AGENTSTACK_STRICT_AGENT_NAMES=1`.
3. Pre-register the child with a child-owned token and write that token to a temporary 0600 file. Prefer the helper so the parent LLM never sees the token:

   ```bash
   CHILD_TOKEN_FILE="$(mktemp "${TMPDIR:-/tmp}/agentstack-child-token.XXXXXX")"
   chmod 600 "$CHILD_TOKEN_FILE"
   trap 'rm -f "$CHILD_TOKEN_FILE"' EXIT

   CHILD_NAME="$("${AGENTSTACK_PREREGISTER_CHILD:-$AGENTSTACK_HOME/bin/agentstack-preregister-child}" \
     --project-key "$AGENTSTACK_PROJECT_KEY" \
     --program "claude-code" \
     --model "<model-name>" \
     --task-description "<task summary>" \
     --token-file-out "$CHILD_TOKEN_FILE")"
   ```

   The helper prints the registered name; use `$CHILD_NAME` from here on rather than a name you chose yourself.
   For a Codex child, pass `--program "codex" --model "$CODEX_MODEL"`, using the resolved formal ID above. Do not pass `--name` just because the user typed a word you do not recognize.
   Do not paste the token into the inbox message, prompt text, shell history, or a command-line argument.
4. Contact policy. The registration helper sets the child's `contact_policy` to `open` by default, but that is best effort and an environment override can change it, and `whois` does not show policies. So you usually **cannot confirm** either side's policy before spawning. That is not a reason to stop:
   - **You know a side is restrictive** (the user or operator said so, or an earlier message to or from this child was rejected by contact policy): complete a contact handshake or approval before spawning, with the contact tools you have or through the operator.
   - **Unknown (the usual case), or both open:** proceed with the spawn. Missing contact tools are not a reason to stop.
   - **A message is actually rejected by contact policy** (the child reports it cannot reach you, or your send fails with a contact-policy error): report that exact error to the operator and resolve it through a handshake or approval. Do not change your own or the child's `contact_policy` on your own, and do not switch to another transport.
5. Reserve file paths if the task edits shared resources.
6. Write the complete task to a mode `0600` temporary file without shell interpolation. A quoted heredoc delimiter keeps backticks and `$()` literal:

   ```bash
   TASK_FILE="$(mktemp "${TMPDIR:-/tmp}/agentstack-task.XXXXXX")"
   chmod 600 "$TASK_FILE"
   trap 'rm -f "$CHILD_TOKEN_FILE" "$TASK_FILE"' EXIT
   cat > "$TASK_FILE" <<'AGENTSTACK_TASK'
   <complete task text>
   AGENTSTACK_TASK
   ```

   Do not also call `send_message` with a task request. In embedded mode the launch prompt is the sole canonical task; task mail would create a second, potentially divergent source of authority. The child still uses `send_message` for its **completion report** at `importance="high"`. Leave routine progress notes and non-urgent questions at the default importance.

Then spawn the pre-registered child:

```bash
PARENT_AGENT="<parent-name>" bash "${AGENTSTACK_SPAWN_SCRIPT:-$AGENTSTACK_HOME/hooks/spawn_child.sh}" \
  --pre-registered "<child-name>" \
  --child-token-file "$CHILD_TOKEN_FILE" \
  --embed-task --task-file "$TASK_FILE" \
  "<working-directory>"
```

For a Codex child, repeat the model (and effort) the user asked for; without `--model` the launcher resolves the current catalog default (`gpt-6.1-sol` with CLI 0.159.0+, otherwise `gpt-6-sol`; unknown versions use fresh catalog evidence) regardless of what was registered. Resolve omission before registration with `resolve ""`, pass `$CODEX_MODEL` to both steps, and pass `$CODEX_BIN` to this launch:

```bash
AGENTSTACK_CODEX_BIN="$CODEX_BIN" PARENT_AGENT="<parent-name>" bash "${AGENTSTACK_SPAWN_SCRIPT:-$AGENTSTACK_HOME/hooks/spawn_child.sh}" \
  --pre-registered "<child-name>" --codex --model "$CODEX_MODEL" --effort medium \
  --child-token-file "$CHILD_TOKEN_FILE" \
  --embed-task --task-file "$TASK_FILE" \
  "<working-directory>"
```

When the user selected `/delegate --codex-mcp orrery-only`, add the same `--codex-mcp orrery-only` to that launcher command. Omit it for the backward-compatible `inherit` default.

When the user selected `/delegate --claude-chrome` or `--claude-chrome-device <deviceId>` for a Claude child, add the same flag to the launcher command. Never pick a deviceId yourself: pass only the one the user gave. Without either flag the launch is unchanged and the child inherits the user's Claude settings. The deviceId is an instruction the child follows, not a lock on other browsers; see docs/delegation.en.md.

`spawn_child.sh --embed-task` requires `--pre-registered`, and `--task-file` takes precedence over a positional task. The launcher reads the file before starting tmux, adds the child and parent names, spawn time, project key, and completion-report instruction, then injects the same canonical prompt into Claude or Codex. It warns on stderr not to send task mail.

`spawn_child.sh --pre-registered` will refuse to start without a child-owned token file or existing `AGENTSTACK_RUNTIME_DIR/child-agents/<child-name>.json` state. It intentionally ignores any ambient `CHILD_REGISTRATION_TOKEN` from the parent so the parent's owner token is not forwarded to the child.

## 4. Worktree Mode

Use `--worktree` when the child should edit in an isolated git worktree instead of the parent's working tree.

Behavior:

- The child runs in a persistent worktree directory below `${AGENTSTACK_WORKTREE_ROOT:-${AGENTSTACK_HOME:-$HOME/.agentstack}/worktrees}`.
- The child uses a new branch such as `exp/<child-name>`.
- The parent decides later whether to merge, cherry-pick, or discard the result.
- The worktree is outside the normal project directory, so the child must be told to use `$PROJECT_KEY` or `$AGENTSTACK_PROJECT_KEY` for ORRERY Mail project identity.

Use `--worktree-base <rev>` when spawning several children that must share the same baseline:

```bash
/delegate "approach A" --worktree --worktree-base main
/delegate "approach B" --worktree --worktree-base main
```

After verification, clean up from the source repository:

```bash
git worktree remove "${AGENTSTACK_WORKTREE_ROOT:-${AGENTSTACK_HOME:-$HOME/.agentstack}/worktrees}/<child-name>"
git branch -d exp/<child-name>
```

Use `-D` only when intentionally discarding the branch.

## 5. Annotate The Dashboard

After registering the child, add role metadata to the dashboard. This is best-effort; spawning can continue if the dashboard is down.

Endpoint:

```text
http://127.0.0.1:${AGENTSTACK_PORT:-8770}/api/annotate
```

Single child:

```bash
curl -s -f --max-time 2 -X POST "http://127.0.0.1:${AGENTSTACK_PORT:-8770}/api/annotate" \
  -H "Content-Type: application/json" \
  -d '{"name":"<child-name>","role":"api-migrate","emoji":"code","group":"api-v2"}' \
  || echo "[warn] dashboard annotation skipped" >&2
```

Several children in the same group:

```bash
for spec in \
  'BlueLake|schema|db|api-v2' \
  'GreenStone|client|web|api-v2' \
  'RedField|tests|test|api-v2'
do
  IFS='|' read -r name role emoji group <<< "$spec"
  curl -s -f --max-time 2 -X POST "http://127.0.0.1:${AGENTSTACK_PORT:-8770}/api/annotate" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"name":"%s","role":"%s","emoji":"%s","group":"%s"}' "$name" "$role" "$emoji" "$group")" \
    || echo "[warn] annotation skipped for $name" >&2
done
```

You may also annotate the parent so the dashboard groups the parent and children together:

```bash
curl -s -f --max-time 2 -X POST "http://127.0.0.1:${AGENTSTACK_PORT:-8770}/api/annotate" \
  -H "Content-Type: application/json" \
  -d '{"name":"<parent-name>","role":"lead","emoji":"lead","group":"api-v2"}' \
  || true
```

If you need to clear a label, send an empty value to `http://127.0.0.1:${AGENTSTACK_PORT:-8770}/api/annotate`.

## 6. Monitor Progress

Primary completion signal: the child sends an ORRERY Mail message to the parent. It reaches you as a notification in your session; if the notification says the body is complete, act on it without a `fetch_inbox`.

### Waiting for the child

Do not improvise a wait. A `fetch_inbox` polling loop consumes the push notification's dirty bit (the reply then sits unread), and a pane-diff or `sleep` loop keeps firing after the child is done. Choose one of three patterns:

1. **Send and move on** (default): keep working; the completion report arrives as a notification.
2. **Background await**: when you must be woken by the reply but have other work, run the bundled primitive in the background and let its completion wake you:

   ```bash
   "$AGENTSTACK_HOME/bin/agentstack-await-reply" --agent-name "$AGENT_NAME" \
     --from "$CHILD_NAME" --after-id <id of the task message you sent> --timeout 1800
   ```

   It prints the reply as JSON and exits 0, or exits 124 on timeout.
3. **Blocking await**: the same command in the foreground, only when your next step depends on the reply's content.

### Startup race

This applies only when the task travels by mail (a child launched without the embedded task). Such a child can start before its task message is committed and see an empty inbox. Tell it in the launch prompt to wait ten seconds and fetch once more before reporting "no task"; if you still receive that report, resend the task with `send_message` rather than letting the child guess from its cwd.

### Backup monitor

Schedule monitor checks with the installed monitor script:

```bash
bash "$AGENTSTACK_HOME/hooks/monitor_child_agent.sh" \
  --child "<child-name>" \
  --risk "<low|medium|high>" \
  --resources "<resource-csv>" \
  --parent "<parent-name>" \
  --mode auto
```

Monitor exit codes:

| Code | Meaning | Parent response |
| --- | --- | --- |
| 0 | healthy | keep watching |
| 10 | shell prompt returned | fetch inbox and verify completion |
| 11 | session missing | inspect inbox and tmux history |
| 20 | warning only | report or intervene if repeated |
| 30 | soft stop sent | inspect the child before continuing |
| 40 | process group frozen | decide whether to resume or terminate |
| 50 | session killed | report failure and recover manually |

Dangerous command detection in the monitor is passive by default. It runs only when `AGENTSTACK_MONITOR_DANGER_CHECK=1`.

Known false positives — check the pane before acting on a code:

- **Exit 10 between tool calls.** The shell-return check matches a trailing `❯`, and the Claude Code REPL prompt is also `❯`, so a healthy child can return 10 on every tick. Treat 10 as "verify", not "done": completion is the inbox report plus your own inspection of the output.
- **Exit 30 on a benign `rm -rf`.** Build steps delete `__pycache__`, `dist/`, `node_modules/` and similar. Read the actual command in the pane; if it is a build artifact, delete the cron entry, tell the child to continue, and do not escalate.
- **Text sitting in the child's input box** may be Claude Code's automatic prompt suggestion, which `capture-pane` renders like typed input. Do not diagnose an unsubmitted `Enter` from that alone; if it matters, send one harmless character and see whether it appends.

### Manage the child's context

The child is busy with its task and will not choose a good moment to compact. That is the parent's job. Both Claude Code and Codex auto-compact, so a low `Context NN% left` in the pane footer is not an emergency and is never a reason to interrupt work in progress. Compact deliberately at task boundaries instead: when the child is idle at its prompt, its results are persisted (mail report, files, log), and the next task benefits from a fresh head (new code to review, a different subsystem, after a long trial-and-error). `/compact` is a REPL command, so this is the one case where a keystroke is sent to the pane:

```bash
tmux send-keys -t "$CHILD_NAME" -l "/compact"; sleep 0.3; tmux send-keys -t "$CHILD_NAME" C-m
```

Then send the next task with `send_message`; the child re-orients from the files and mail the task points to.

## 7. Completion Handling

When completion is detected:

1. Stop any cron monitor for the child.
2. Fetch the child's completion message.
3. Read or inspect the changed artifacts yourself.
4. Run focused verification when feasible.
5. For implementation changes, run a doc-sync pass and update README or
   managed docs (`claude/CLAUDE.md`, `codex/AGENTS.md`) when behavior changed.
6. Release file reservations.
7. Report the verified outcome to the user.

If the child reports uncertainty, partial completion, or skipped tests, preserve that information in your report.

### Reuse before you retire

Do not retire a child the moment it reports. Sending a related follow-up task to the same child with `send_message` skips the spawn cost (tmux window, registration, bootstrap), keeps the context of the previous work, and keeps the parent-child edge on the dashboard. Reuse while the child still has roughly half its context; below that, compact it at the boundary (above) or spawn a fresh child for the next task. Reviewer children in particular stay alive so additional review angles go to the agent that already read the code.

When a child is truly finished, end its tmux session; `cleanup-child-agent.sh` retires the registration and removes its runtime files. Retire only agents you spawned and named explicitly.

## 8. Codex Child Notes

Codex children differ from Claude Code children in a few operational details:

- Codex may use a different REPL prompt, so monitor logic must avoid treating a visible input prompt as proof of completion.
- Instructions, follow-ups and corrections go to the child over `send_message`, never by typing into its pane: keystrokes can land in the input box without submitting, nothing records that they arrived, and a human then has to press Enter for you. The only pane keystroke the parent sends is `/compact` (section 6), as text and `C-m` in separate calls.
- Codex may be sandboxed differently from Claude Code; include test commands and allowed paths explicitly in the task.
- With `--embed-task` (this skill's flow) the launch prompt is the canonical task, exactly as for a Claude child: there is no startup inbox ritual. The child reads its inbox later only when it is told a message is waiting, or when the task needs it. Reservations taken through your proxy belong to you; a child that edits files takes its own.

## 9. Shared Resource Coordination

For files, prefer ORRERY Mail file reservations.

For non-file resources such as a single browser tab, hardware device, local service, or database writer, use a simple acquire/release protocol over ORRERY Mail:

- Send `<RESOURCE>_ACQUIRE: <key>` to the relevant agents.
- Check recent inbox messages for an unreleased acquire from another agent.
- Back off when the resource is held.
- Send `<RESOURCE>_RELEASE: <key>` when done.

Use acquire/release pairing rather than a short time window. Long legitimate operations should not be mistaken for stale locks.

## Principles

- Delegate only bounded work with clear ownership.
- Keep project identity stable with `$AGENTSTACK_PROJECT_KEY`.
- Messages, never raw tmux injection (the sole exception is `/compact`).
- Wait with `agentstack-await-reply` or not at all; never with a loop of your own.
- A child is reused before it is retired, and compacted at task boundaries, not mid-task.
- Watch startup closely, then adjust cadence by risk.
- Verify the child's output before reporting it as done.
