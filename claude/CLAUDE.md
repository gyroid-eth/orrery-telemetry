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
3. Tokens: **if your agent-mail MCP server runs through the local proxy, you
   never touch a token.** Spawned children are configured that way, and the
   SessionStart reminder says so ("この接続はローカル MCP proxy 経由で既に認証済み
   です"). The proxy holds your token and authenticates every call, so do not
   read `agent_token_<name>` — that only costs an approval prompt and puts the
   secret in your context.

   Only when the proxy is absent (a top-level session, or an install without
   it) does the old rule apply: on the first `fetch_inbox`/`whois` call, read
   `${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/agent_token_<name>` and
   pass its value as `registration_token`. Later calls in the authenticated MCP
   session may omit it.

The SessionStart hook has already registered you when it prints:

```text
mcp-agent-mail server is running. This session is already registered.
あなたは「<name>」です（既存 identity・source: ...）。shell hook で登録済みです。
新しい名前を生成せず、register_agent を呼び直さず、fetch_inbox から始めてください。
```

When you see those lines, skip `register_agent` and go straight to
`fetch_inbox`, passing the persisted owner token on that first MCP call. The
hook's shell-side HTTP registration does not authenticate the model's separate
MCP tool session.

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
- Stack registration helpers set your own `contact_policy` to `open` by default
  after token-backed registration. Set `AGENTSTACK_CONTACT_POLICY=auto`,
  `contacts_only`, `block_all`, or an empty value to override that behavior.
- If a token error such as `requires registration_token` occurs, try manual
  recovery before reporting failure:

  ```bash
  __AGENTSTACK_HOME__/bin/agentstack-reregister "$AGENT_NAME" claude-code
  ```

  Success prints `agentstack-reregister: registered <name>` and exits 0. If it
  succeeds, skip `register_agent` and call `fetch_inbox`.
- Top-level sessions store their runtime token at
  `${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/agent_token_<name>`.
  Delegated children also use
  `${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/child-agents/<name>.json`.
  `agentstack-reregister` reads both locations. It is acceptable for stack
  helpers to use these token files. Do not read agent-mail's `storage.sqlite3`
  directly; the DB is outside the recovery boundary and ad hoc DB reads risk
  stale paths, token leakage, and identity splits.
- If `CHILD_REGISTRATION_TOKEN` is present but re-registration still fails,
  suspect a wrong token, including a parent token accidentally mixed into a
  pre-registered child. Retry with `agentstack-reregister`; if it cannot restore
  the correct token from runtime state, report the mismatch instead of creating
  a new alias.
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
