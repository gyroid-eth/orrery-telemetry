This machine runs **claude-agent-stack**: multiple Claude Code and Codex agents
coordinate over `mcp_agent_mail` (inter-agent messaging + a shared file-lock
registry) and appear in a live dashboard. As a Codex agent you are a first-class
participant. Follow the rules below.

## Coordination (agent-mail)

- The shared project key is `__AGENTSTACK_PROJECT_KEY__`. Use it for
  `ensure_project`, `register_agent`, `fetch_inbox`, and reservations — do not
  infer a different project from your current directory.
- On SessionStart, `session-start-reminder.sh` resolves an existing identity
  before registration (`AGENT_NAME` -> per-pane metadata -> tmux session name)
  and reminds you to re-register with the same `name`. For cc/cx sessions that
  do not carry `AGENT_NAME`, the tmux session name is the decisive source across
  `/clear`, resume, and compact; when the reminder prints a name, do not
  generate a new one.
- If you were launched with `agent-start-codex`, `AGENT_NAME` should already be
  exported and your tmux session should be named after you. At session start,
  call `ensure_project(human_key="__AGENTSTACK_PROJECT_KEY__")`, then
  `register_agent` with `program="codex"` and `name="$AGENT_NAME"` when
  `AGENT_NAME` is set. If `CHILD_REGISTRATION_TOKEN` is set, pass it as
  `registration_token`; with that token, re-registration of the
  launcher-created identity is idempotent. If `AGENT_NAME` is not set, omit
  `name`.
- Identity registration is lenient on current agent-mail: re-registering the
  same agent name without a token succeeds and preserves the existing token;
  only supplying a different token for that agent is rejected. If you see an
  identity split, your `~/mcp_agent_mail` clone is likely outdated; update it
  with `git -C ~/mcp_agent_mail pull`, then restart agent-mail.
- If registration still fails with a name-conflict or token-mismatch error in a
  child/reserved session, do not register under another name; report the
  missing or stale `CHILD_REGISTRATION_TOKEN` and update/restart agent-mail.
- If `PARENT_AGENT` is set, you are a child agent: read your task from
  `fetch_inbox` before doing anything, and report completion with `send_message`
  to the parent. Do not invent a task from context.

## File reservations — REQUIRED before editing (Codex is not auto-guarded)

Claude agents are hard-blocked by a PreToolUse hook from editing shared files
without a reservation. **Codex has no such hook, so enforcement is on you.**
Before you Edit/Write any file under the project, take a reservation so another
agent does not clobber it:

- Acquire: `macro_file_reservation_cycle` (or `file_reservation_paths`) with
  `project_key="__AGENTSTACK_PROJECT_KEY__"`, your agent name, and the paths
  (project-relative). Use `ttl_seconds` ≥ 600.
- Renew long edits with `renew_file_reservations`; release when done with
  `release_file_reservations`.
- If a path is already reserved by another agent, coordinate over agent-mail
  instead of editing it.

Skipping this is the main way two Codex agents corrupt each other's work.

## Skills

These slash-command-style workflows live as Markdown you read on demand (Codex
has no skill registry). When a request matches, open the file and follow it:

- **delegate** — spawn and supervise a child Claude/Codex agent
  (`__AGENTSTACK_HOME__/skills/delegate/SKILL.md`). Triggers: "delegate",
  "委任", "spawn a child agent", "run this in parallel". The canonical launcher
  is `__AGENTSTACK_HOME__/hooks/spawn_child.sh`; do not invent a parallel flow.
- **log** — write a structured session log
  (`__AGENTSTACK_HOME__/skills/log/SKILL.md`). Triggers: "log this", "ログ残して".
