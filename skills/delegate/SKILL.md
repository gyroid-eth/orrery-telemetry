---
name: delegate
description: Delegate a bounded task to a child Claude or Codex agent, prepare risk-aware instructions, spawn the child, annotate it in the dashboard, monitor progress, and verify completion.
allowed-tools: Bash, CronCreate, CronDelete, CronList, Read, Grep, Glob, mcp__mcp-agent-mail__send_message, mcp__mcp-agent-mail__fetch_inbox, mcp__mcp-agent-mail__register_agent, mcp__mcp-agent-mail__ensure_project, mcp__mcp-agent-mail__set_contact_policy, mcp__mcp-agent-mail__macro_contact_handshake, mcp__mcp-agent-mail__respond_contact, mcp__mcp-agent-mail__file_reservation_paths, mcp__mcp-agent-mail__release_file_reservations, mcp__mcp-agent-mail__renew_file_reservations
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

If the child runs outside the project directory, explicitly tell it to use `$PROJECT_KEY` or `$AGENTSTACK_PROJECT_KEY` for `ensure_project`, `register_agent`, `fetch_inbox`, and completion messages. Do not let the child infer the project from its current working directory.
`AGENTSTACK_PROJECT_KEY` must be set before spawning. It is the agent-mail project identity and may be different from the code worktree or the child's current working directory.

## Naming Rules

- Do not use `create_agent_identity` for delegate children.
- Delegate children must be explicitly registered with `register_agent(name=<Adjective-Scientist>, program=...)`. The name has no `cc-` or `cx-` prefix; the program type is recorded in `program`.
- Generate names through the stack picker (`bin/lib/agentstack-register.sh` or `spawn_child.sh`) so the suffix matches a bundled dashboard scientist portrait.
- After spawning, verify the tmux session name, dashboard entry, and inbox-read startup all refer to the registered child name.

## Usage

```bash
/delegate "<task>"
/delegate "<task>" --dir <working-directory>
/delegate "<task>" --codex
/delegate "<task>" --model <model-name>
/delegate "<task>" --worktree
/delegate "<task>" --worktree --worktree-base <rev>
```

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

For file edits in a shared project, reserve the relevant paths before spawning when an agent-mail reservation tool is available.

## 2. Prepare The Child Task

Give the child a short role label and a concrete task description. Keep them separate:

| Field | Purpose |
| --- | --- |
| `role` | Short dashboard label, such as `api-migrate`, `review`, `tests`, or `docs` |
| `task` | Concrete instruction used in `register_agent.task_description` and the inbox message |
| `group` | Shared cluster name for a set of related child agents |

Task message template:

```markdown
## Role: <role>
## Task summary: <same task text used for register_agent.task_description>

## Work
<specific request, constraints, files, and expected output>

## Coordination
- Use project_key from `$PROJECT_KEY` or `$AGENTSTACK_PROJECT_KEY`: <value if known>.
- First call ensure_project, register_agent, then fetch_inbox for your own name.
- Treat this inbox message as the canonical task.
- Do not modify files outside the declared scope.
- Report completion to <parent-agent-name> with changed paths, summary, and verification.
```

Use generic task examples such as code review, API migration, test-suite repair, documentation cleanup, or data import validation. Avoid embedding organization-specific project lore in the delegated task unless the current user request requires it.

## 3. Register, Open Contact, Reserve, And Send

Preferred flow: do the coordination through MCP tools first, then let `spawn_child.sh` create the tmux session.

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
   For a Codex child, pass `--program "codex" --model "gpt-5.5"`.
   Do not paste the token into the inbox message, prompt text, shell history, or a command-line argument.
4. Ensure the child can receive the first task message. The stack registration helper sets the child's `contact_policy` to `open` by default. If `AGENTSTACK_CONTACT_POLICY` disables that default, complete a contact handshake or approval before `send_message`.
5. Reserve file paths if the task edits shared resources.
6. `send_message(project_key, sender_name=<parent>, to=[<child>], subject=..., body_md=..., importance="high")`.
   In that message, tell the child to send its **completion report** at `importance="high"` too, and to leave
   everything else — progress notes, questions that can wait — at the default. A notification is typed
   straight into the recipient's prompt, so an operator may have set
   `AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE=high` to stop routine chatter from interrupting a conversation.
   Under that setting a report sent at the default still reaches the inbox but does not announce itself, and
   the parent goes on waiting for something that already happened.

Then spawn the pre-registered child:

```bash
PARENT_AGENT="<parent-name>" bash "${AGENTSTACK_SPAWN_SCRIPT:-$AGENTSTACK_HOME/hooks/spawn_child.sh}" \
  --pre-registered "<child-name>" \
  --child-token-file "$CHILD_TOKEN_FILE" \
  "<task summary>" "<working-directory>"
```

For a Codex child:

```bash
PARENT_AGENT="<parent-name>" bash "${AGENTSTACK_SPAWN_SCRIPT:-$AGENTSTACK_HOME/hooks/spawn_child.sh}" \
  --pre-registered "<child-name>" --codex \
  --child-token-file "$CHILD_TOKEN_FILE" \
  "<task summary>" "<working-directory>"
```

`spawn_child.sh --pre-registered` will refuse to start without a child-owned token file or existing `AGENTSTACK_RUNTIME_DIR/child-agents/<child-name>.json` state. It intentionally ignores any ambient `CHILD_REGISTRATION_TOKEN` from the parent so the parent's owner token is not forwarded to the child.

For fallback direct mode when MCP tools are unavailable:

```bash
bash "${AGENTSTACK_SPAWN_SCRIPT:-$AGENTSTACK_HOME/hooks/spawn_child.sh}" \
  --resources "<resource-csv>" \
  "<task summary>" "<working-directory>"
```

## 4. Worktree Mode

Use `--worktree` when the child should edit in an isolated git worktree instead of the parent's working tree.

Behavior:

- The child runs in a temporary worktree directory.
- The child uses a new branch such as `exp/<child-name>`.
- The parent decides later whether to merge, cherry-pick, or discard the result.
- The worktree is outside the normal project directory, so the child must be told to use `$PROJECT_KEY` or `$AGENTSTACK_PROJECT_KEY` for agent-mail project identity.

Use `--worktree-base <rev>` when spawning several children that must share the same baseline:

```bash
/delegate "approach A" --worktree --worktree-base main
/delegate "approach B" --worktree --worktree-base main
```

After verification, clean up from the source repository:

```bash
git worktree remove /tmp/cc-worktrees/<child-name>
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

Primary completion signal: the child sends an agent-mail message to the parent. Read it with `fetch_inbox`, then verify the claimed output.

Backup signal: schedule monitor checks with the installed monitor script:

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

## 8. Codex Child Notes

Codex children differ from Claude Code children in a few operational details:

- Codex may use a different REPL prompt, so monitor logic must avoid treating a visible input prompt as proof of completion.
- Injecting text with `tmux send-keys` is a last resort. Prefer `send_message`. If you must inject text, send text and `C-m` as separate calls.
- Codex may be sandboxed differently from Claude Code; include test commands and allowed paths explicitly in the task.
- The child must still read its inbox and treat the inbox task as canonical.

## 9. Shared Resource Coordination

For files, prefer agent-mail file reservations.

For non-file resources such as a single browser tab, hardware device, local service, or database writer, use a simple acquire/release protocol over agent-mail:

- Send `<RESOURCE>_ACQUIRE: <key>` to the relevant agents.
- Check recent inbox messages for an unreleased acquire from another agent.
- Back off when the resource is held.
- Send `<RESOURCE>_RELEASE: <key>` when done.

Use acquire/release pairing rather than a short time window. Long legitimate operations should not be mistaken for stale locks.

## Principles

- Delegate only bounded work with clear ownership.
- Keep project identity stable with `$AGENTSTACK_PROJECT_KEY`.
- Prefer messages over raw tmux injection.
- Watch startup closely, then adjust cadence by risk.
- Verify the child's output before reporting it as done.
