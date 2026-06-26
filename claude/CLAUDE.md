This machine runs **claude-agent-stack**: Claude Code and Codex agents
coordinate over `mcp_agent_mail` and share file reservations. Follow these
rules before doing project work.

## Session Startup

- The shared project key is `__AGENTSTACK_PROJECT_KEY__`. Use it for
  `ensure_project`, `register_agent`, `fetch_inbox`, and reservations. Do not
  infer a different project from your current directory.
- If you were launched with `agent-start`, you should already have
  `AGENT_NAME` exported and your tmux session should be named after it. If
  `AGENT_NAME` is missing or you are not registered, call
  `ensure_project(human_key="__AGENTSTACK_PROJECT_KEY__")`, then
  `register_agent` with `program="claude-code"` and `name="$AGENT_NAME"` when
  a name is present.
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
