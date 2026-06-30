This machine runs **claude-agent-stack**: Claude Code and Codex agents
coordinate over `mcp_agent_mail` and share file reservations. Follow these
rules before doing project work.

## Session Startup

- The shared project key is `__AGENTSTACK_PROJECT_KEY__`. Use it for
  `ensure_project`, `register_agent`, `fetch_inbox`, and reservations. Do not
  infer a different project from your current directory.
- On SessionStart, `session-start-reminder.sh` resolves an existing identity
  before registration (`AGENT_NAME` -> per-pane metadata -> tmux session name)
  and reminds you to re-register with the same `name`. For cc/cx sessions that
  do not carry `AGENT_NAME`, the tmux session name is the decisive source across
  `/clear`, resume, and compact; when the reminder prints a name, do not
  generate a new one.
- If you were launched with `agent-start`, you should already have
  `AGENT_NAME` exported and your tmux session should be named after it. At
  session start, call `ensure_project(human_key="__AGENTSTACK_PROJECT_KEY__")`,
  then call `register_agent` with `program="claude-code"` and
  `name="$AGENT_NAME"` when `AGENT_NAME` is set. If
  `CHILD_REGISTRATION_TOKEN` is set, pass it as `registration_token`; stock
  agent-mail is token-strict for existing names, so same-name re-registration
  requires the original token. If `CHILD_REGISTRATION_TOKEN` is not set, omit
  `registration_token` rather than inventing one.
- If registration still fails with a name-conflict or token-mismatch error in a
  child/reserved session, do not register under another name; report the
  missing or stale `CHILD_REGISTRATION_TOKEN` and update/restart agent-mail.
- Always call `fetch_inbox(agent_name="$AGENT_NAME")` after registration. If
  `PARENT_AGENT` is set, treat the inbox request as the canonical task and
  report completion to that parent with `send_message`.

## File Reservations

Before editing files under the project, reserve the specific paths you plan to
touch with `file_reservation_paths` or `macro_file_reservation_cycle` using
`project_key="__AGENTSTACK_PROJECT_KEY__"` and your agent name. Renew long
edits with `renew_file_reservations`, and release reservations when finished.

## Bundled Skills

The installed skill sources live under `__AGENTSTACK_HOME__/skills`.

- `delegate`: `__AGENTSTACK_HOME__/skills/delegate/SKILL.md`
- `log`: `__AGENTSTACK_HOME__/skills/log/SKILL.md`
