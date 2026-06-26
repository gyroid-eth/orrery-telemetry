This machine runs **claude-agent-stack**: Claude Code and Codex agents
coordinate over `mcp_agent_mail` and share file reservations. Follow these
rules before doing project work.

## Session Startup

- The shared project key is `__AGENTSTACK_PROJECT_KEY__`. Use it for
  `ensure_project`, `register_agent`, `fetch_inbox`, and reservations. Do not
  infer a different project from your current directory.
- If you were launched with `agent-start`, you should already have
  `AGENT_NAME` exported and your tmux session should be named after it. At
  session start, call `ensure_project(human_key="__AGENTSTACK_PROJECT_KEY__")`,
  then call `register_agent` with `program="claude-code"` and
  `name="$AGENT_NAME"` when `AGENT_NAME` is set. This registration is idempotent
  and safe even if the launcher already registered you. If `AGENT_NAME` is not
  set, omit `name`.
- If `register_agent(name="$AGENT_NAME")` reports that the name is already
  taken, retry with a short disambiguator before the scientist suffix, such as
  `cc-a1-Curie`, so the final name still ends with a bundled scientist key.
  Export and use the registered name returned by agent-mail.
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
