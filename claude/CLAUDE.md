This machine runs **claude-agent-stack**: Claude Code and Codex agents
coordinate over `mcp_agent_mail` and share file reservations. Follow these
rules before doing project work.

## Session Startup

First calls:

1. `ensure_project(human_key="__AGENTSTACK_PROJECT_KEY__")`.
2. If SessionStart says the shell hook already registered your resolved name,
   do not call `register_agent` again. Otherwise call `register_agent` with the
   resolved `name` (`$AGENT_NAME`, or the name printed by the SessionStart
   reminder) and pass `CHILD_REGISTRATION_TOKEN` as `registration_token` when it
   is available.
3. `fetch_inbox(project_key="__AGENTSTACK_PROJECT_KEY__", agent_name="$AGENT_NAME")`.

The SessionStart hook has already registered you when it prints:

```text
mcp-agent-mail server is running. This session is already registered.
あなたは「<name>」です（既存 identity・source: ...）。shell hook で登録済みです。
新しい名前を生成せず、register_agent を呼び直さず、fetch_inbox から始めてください。
```

When you see those lines, skip `register_agent` and go straight to
`fetch_inbox`.

Details and exceptions for the first calls:

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
  `printenv CHILD_REGISTRATION_TOKEN` is non-empty, pass that value as
  `registration_token`; Claude Code is not sandbox-hiding this env in normal
  launcher sessions. `CHILD_REGISTRATION_TOKEN` is not only for child agents:
  it is the re-authentication token for continuing an existing identity. A
  top-level session with no `PARENT_AGENT` still needs it if the tmux/session
  name resolves to an existing identity. If `CHILD_REGISTRATION_TOKEN` is empty,
  do not invent a new token; try the helper below before reporting failure.
- Stock agent-mail is token-strict for existing names. `register_agent` and
  read-only tools such as `fetch_inbox` or `whois` require the original
  registration token unless this MCP session has already authenticated as that
  agent. Reading only is not token-free.
- If a token error such as `requires registration_token` occurs, try manual
  recovery before reporting failure:

  ```bash
  __AGENTSTACK_HOME__/bin/agentstack-reregister "$AGENT_NAME" claude-code
  ```

  Success prints `agentstack-reregister: registered <name>` and exits 0. If it
  succeeds, skip `register_agent` and call `fetch_inbox`.
- The runtime token file is
  `${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/agent_token_<name>`. It is
  acceptable for stack helpers to use this token file. Do not read agent-mail's
  `storage.sqlite3` directly; the DB is outside the recovery boundary and ad
  hoc DB reads risk stale paths, token leakage, and identity splits.
- If registration still fails with a name-conflict or token-mismatch error in a
  child/reserved session, do not register under another name; report the
  missing or stale `CHILD_REGISTRATION_TOKEN` and update/restart agent-mail.
- Always call
  `fetch_inbox(project_key="__AGENTSTACK_PROJECT_KEY__", agent_name="$AGENT_NAME")`
  after registration. If `PARENT_AGENT` is set, treat the inbox request as the
  canonical task and report completion to that parent with `send_message`.
- If `PARENT_AGENT` is not set and registration or inbox access is truly
  unrecoverable, there is no parent to report to. Leave a short operator-facing
  note and continue the user task without inbox coordination; do not stall or
  create a new alias.

## File Reservations

Before editing files under the project, reserve the specific paths you plan to
touch with `file_reservation_paths` or `macro_file_reservation_cycle` using
`project_key="__AGENTSTACK_PROJECT_KEY__"` and your agent name. Renew long
edits with `renew_file_reservations`, and release reservations when finished.

## Bundled Skills

The installed skill sources live under `__AGENTSTACK_HOME__/skills`.

- `delegate`: `__AGENTSTACK_HOME__/skills/delegate/SKILL.md`
- `log`: `__AGENTSTACK_HOME__/skills/log/SKILL.md`
