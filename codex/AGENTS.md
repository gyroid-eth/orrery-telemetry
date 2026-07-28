This machine runs **claude-agent-stack**: multiple Claude Code and Codex agents
coordinate over `mcp_agent_mail` (inter-agent messaging + a shared file-lock
registry) and appear in a live dashboard. As a Codex agent you are a first-class
participant. Follow the rules below.

## Coordination (agent-mail)

First calls:

1. Confirm your name with `echo "$AGENT_NAME"`. If it is empty, use the
   SessionStart reminder or tmux session name as the identity; do not run the
   helper or `register_agent` with an empty name.
2. Always try the token-safe helper first, even if bootstrap only left you with
   `AGENT_NAME`:
   `AGENTSTACK_PROJECT_KEY="__AGENTSTACK_PROJECT_KEY__" __AGENTSTACK_HOME__/bin/agentstack-reregister "$AGENT_NAME"`.
3. If that succeeds, do not call `register_agent` again.
4. On the first `fetch_inbox`/`whois` call in this MCP session, read
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
  first run:

  ```bash
  AGENTSTACK_PROJECT_KEY="__AGENTSTACK_PROJECT_KEY__" __AGENTSTACK_HOME__/bin/agentstack-reregister "$AGENT_NAME"
  ```

  It restores the owner token from runtime state and re-registers without
  printing the token. If that succeeds, do not call `register_agent` again;
  continue with
  `fetch_inbox(project_key="__AGENTSTACK_PROJECT_KEY__", agent_name="$AGENT_NAME")`.
- `agentstack-reregister` success prints `agentstack-reregister: registered
  <name>` on stdout and exits 0. Failures print an error on stderr and exit
  nonzero.
- Codex workspace-write sandboxes can hide launcher-exported environment
  variables from shell commands: `printenv CHILD_REGISTRATION_TOKEN` may be
  empty even though the launcher created a valid token. Prefer
  `agentstack-reregister`, which restores the token from runtime state instead
  of relying on sandbox-visible env. In Codex, do not try to decide whether the
  token is set by `printenv`; the helper is the check.
- If `agentstack-reregister` is unavailable or fails before registration, call
  `ensure_project(human_key="__AGENTSTACK_PROJECT_KEY__")`, then
  `register_agent(project_key="__AGENTSTACK_PROJECT_KEY__", program="codex",
  name="$AGENT_NAME", ...)` when `AGENT_NAME` is set. If
  `CHILD_REGISTRATION_TOKEN` is visible, pass it as `registration_token`; stock
  agent-mail is token-strict for existing names, so same-name re-registration
  requires the original token. If `CHILD_REGISTRATION_TOKEN` is not visible,
  omit `registration_token` rather than inventing one. If `AGENT_NAME` is not
  set, omit `name`.
- `CHILD_REGISTRATION_TOKEN` is not only for child agents: it is the
  re-authentication token for continuing an existing identity. A top-level
  session with no `PARENT_AGENT` still needs it if the tmux/session name
  resolves to an existing identity.
- Stock agent-mail is token-strict for existing names. `register_agent` and
  read-only tools such as `fetch_inbox` or `whois` require the original
  registration token unless this MCP session has already authenticated as that
  agent. Reading only is not token-free.
- Stack registration helpers set your own `contact_policy` to `open` by default
  after token-backed registration. Set `AGENTSTACK_CONTACT_POLICY=auto`,
  `contacts_only`, `block_all`, or an empty value to override that behavior.
- Top-level sessions store their runtime token at
  `${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/agent_token_<name>`.
  Delegated children also use
  `${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/child-agents/<name>.json`.
  `agentstack-reregister` reads both locations. It is acceptable for stack
  helpers to use these token files. Do not read agent-mail's `storage.sqlite3`
  directly; the DB is outside the recovery boundary and ad hoc DB reads risk
  stale paths, token leakage, and identity splits.

Failure handling:

| What you see | Meaning | Action |
| --- | --- | --- |
| helper missing or not executable | helper did not run | Use MCP fallback: `ensure_project` -> `register_agent` with visible token only -> `fetch_inbox`. |
| `agent name required` or empty `AGENT_NAME` | identity was not resolved | Use the SessionStart reminder or tmux session name; do not pass an empty name. |
| `register_agent failed for <name>`, `requires registration_token`, or token mismatch | persisted token is missing or stale, a wrong token such as a parent token is visible, or this name belongs to another token | Retry `agentstack-reregister`; if it still fails, stop recovery. Do not create a new alias or token; report stale/missing/wrong token to the operator or parent. |

- If registration still fails with a name-conflict or token-mismatch error in a
  child/reserved session, do not register under another name; report the
  missing or stale `CHILD_REGISTRATION_TOKEN` and update/restart agent-mail.
- Treat a session as child/reserved when `PARENT_AGENT` is set, when the tmux
  metadata/session name was preassigned by `spawn_child.sh`, or when your inbox
  task says the name is already reserved. In those cases, never switch names.
- If `PARENT_AGENT` is set, you are a child agent: read your task from
  `fetch_inbox(project_key="__AGENTSTACK_PROJECT_KEY__", agent_name="$AGENT_NAME")`
  before doing anything, and report completion with `send_message` to the
  parent. Do not invent a task from context.
- If `PARENT_AGENT` is not set and registration or inbox access is truly
  unrecoverable, there is no parent to report to. Leave a short operator-facing
  note and continue the user task without inbox coordination; do not stall or
  create a new alias.
- Interpret inbox results precisely: an empty list after successful
  authentication is normal and you may continue; an auth error is a
  registration/token problem to recover first; a response for the wrong name is
  an identity split, so stop and do not register under another alias.

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
