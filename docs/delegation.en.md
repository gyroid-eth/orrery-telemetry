# Delegation and child agents

> 日本語版: [delegation.md](delegation.md)

[Previous: Launchers and identity](launchers.en.md) · [Back to README](../README.en.md) · [Next: Hooks](hooks.en.md)

Both Claude Code and Codex Desktop have built-in subagent mechanisms. The **child agents** created by this stack are different. Their names are similar, and **one may silently substitute for the other when the intended mechanism is unavailable**, so real incidents have occurred where people concluded that delegation was working without knowing the distinction.

This page explains the differences between the two and how to tell which one you are currently using.

## In one sentence

- A **built-in subagent** is a **call** opened inside its parent, returning an answer and then closing. It leaves nothing externally visible.
- A **child agent** is a **counterpart** with an identity, its own tmux session, and the ability to receive mail. It can be addressed by name as a peer of its parent, and its history remains available afterward.

The former is enough for a short investigation. The latter is necessary when **people or other agents need to know that the work exists**.

## Differences

| | Built-in subagent | Child agent (this stack) |
|---|---|---|
| How it is created | The parent calls the `Agent` / `Task` tool | `/delegate` (which uses `spawn_child.sh` internally) |
| Identity | None. An internal ID for each call (a hexadecimal value such as `a1798ced…`) | A name registered with ORRERY Mail (an adjective and scientist name such as `Teal-Darwin`) |
| Process | Same process as the parent | Independent tmux session (and optionally a terminal window) |
| Dashboard | **Does not appear** (it does not exist even as a node) | Appears as a node, with a line connecting it to its parent |
| Communication | Only the arguments from the parent and the final returned text | agent-mail. It can communicate bidirectionally with agents other than its parent |
| Lifetime | Only for that single call | Until explicitly ended. More work can be sent later |
| Recovery after interruption | Not possible | The tmux session remains and can be reopened with dashboard jump / resume |
| File coordination | None | File reservations avoid collisions with other agents |
| Progress visibility | Unknown until it finishes | Intermediate progress can be watched in its pane. A monitoring loop can run |
| Best suited for | Short investigations, searches, and one-off decisions | Long implementations, parallel work, and work whose progress people need to see |

## How to tell them apart

The most reliable method is to **look at the dashboard**. A built-in subagent does not register with ORRERY Mail, so it does not appear as a node. It is not merely missing a line: **it does not exist there**.

If you perform a simple communication check and any of the following is true, you are using a built-in subagent:

- No child node appears in NETWORK
- The child's name is a hexadecimal identifier rather than an adjective and scientist name
- The parent's log contains `Agent(...)` / `Task(...)` calls
- The child has no session in `tmux ls`

With a child agent, by contrast, an edge connects the two agents and displays their message count. Opening the edge shows each exchange as an individual mail message.

## Why they are silently substituted

`/delegate` uses the ORRERY Mail MCP tools. **When those tools are absent, a parent may decide on its own to switch to a built-in subagent and finish the work.** The work completes and a report returns, so from a person's perspective it looks like an unqualified success.

This stack prevents that behavior in three layers.

1. **The installer registers ORRERY Mail as an MCP server.** Previously, the registration procedure was undocumented and silently assumed that users had already completed it
2. **`agentstack-doctor` reports missing or inconsistent registration.** It also displays the repair command
3. **The managed instructions (`claude/CLAUDE.md` / `codex/AGENTS.md`) state that delegation must use only `/delegate`, and that when the tools are absent the agent must report the problem and stop instead of substituting another mechanism**

Run `agentstack-selftest` immediately after installation. It verifies **functionality**, not mere presence (registration validation → spawning two actual agents → mail delivery in both directions).

## Codex children and MCP approvals

Codex children default to `--ask-for-approval never` because they run unattended. MCP tools have an approval setting separate from shell-command approvals, so calling an inherited server without an explicit allow rule immediately fails with `MCP tool call requires approval, but approval policy is never`. To give every Codex child the same rule, create a TOML fragment and persist it with installer flag `--codex-child-overlay /absolute/path/to/overlay.toml`.

The minimal server-wide approval is:

```toml
[mcp_servers.chrome-devtools]
default_tools_approval_mode = "approve"
```

When changing the endpoint too, an overlay array replaces the inherited value in full:

```toml
[mcp_servers.chrome-devtools]
args = ["--browserUrl", "http://127.0.0.1:<port>"]
default_tools_approval_mode = "approve"
```

`default_tools_approval_mode` is Codex's server-wide key. To narrow approval to one tool, set `approval_mode = "approve"` under a table such as `[mcp_servers.chrome-devtools.tools.take_screenshot]`. Tables merge recursively; overlay scalars and arrays replace inherited values. To preserve the child's authenticated identity, the ORRERY Mail proxy entries under `mcp_servers` and `plugins.*.mcp_servers.agentstack` are protected: attempted overlay changes are ignored and named in a stderr warning.

The overlay currently applies only to macOS/Linux `spawn_child.sh`. WSL2 uses that path, but the community-lane native-Windows launcher does not apply it.

### Codex children in a worktree and hook trust

Codex records that a project's hooks (`<checkout>/.codex/hooks.json`) are trusted in the user's `~/.codex/config.toml`, keyed by the path of that `hooks.json` (`[hooks.state."<path>:<event>:<i>:<j>"]` with a `trusted_hash`). A `--worktree` child reads the same hooks at another path, finds no record, and stops on the "N hooks need review" screen; none of those hooks run meanwhile.

The launcher therefore adds, in the child's own `CODEX_HOME` `config.toml` only, the trust the user gave to the hooks of the source checkout, keyed by the worktree path. The source checkout is the one whose `.git` names the same git directory as the worktree (also when that git directory is kept elsewhere).

- The `trusted_hash` is a hash of the hook's content, which Codex checks at every start: only identical hooks become trusted, and a changed hook goes back to review.
- The user's `~/.codex/config.toml` is not changed, and no hook the user has not trusted is added.

### Minimizing MCP for a Codex child

Explicitly use `/delegate "<task>" --codex --codex-mcp orrery-only` to make the child-owned `config.toml` keep only authenticated ORRERY Mail and the AgentStack plugin needed for session binding. Other inherited MCP servers and plugins are set to `enabled = false`. Shell and file operations remain available, but plugin-provided skills and app tools are disabled too, so do not use this profile when the task depends on them.

The default is `inherit`, preserving the user's MCP/plugin configuration exactly as before. This avoids silently removing capabilities from existing child workflows. In one maintainer macOS measurement, the unused but eagerly started `chrome-devtools`, `node_repl`, and Rhino processes accounted for about 208 MB RSS per child. RSS double-counts shared pages and varies by environment, so this is a profile-selection estimate, not a guarantee of physical memory reclaimed.

#### When another MCP is needed partway through a task

If a child started with `orrery-only` needs another tool, ask the parent agent for help (or the operator in standalone mode). The parent can take over that part of the work or launch a new child through the regular `/delegate` procedure with the required MCP enabled, passing the necessary context explicitly. The new child does not automatically continue the original conversation.

In one limited interactive measurement with Codex CLI 0.154.0, a preconfigured stdio MCP was changed from disabled to enabled. Although `/mcp` then refreshed its inventory and started the server, no tool call was confirmed, and one control that resumed the same conversation did not succeed either. A fresh process with a new conversation did succeed, so the current guidance does not treat a config change, `/mcp`, or resume as a reliable mid-session switch. This result is not generalized to other versions, HTTP/OAuth or plugin-provided servers, or newly added MCP servers.

## Claude children and browser control (Claude in Chrome)

Explicitly passing `/delegate "<task>" --claude-chrome-device <deviceId>` (or `--claude-chrome`) starts the Claude child's `claude` with `--chrome`, which gives it Claude in Chrome browser control. It applies only to Claude children and cannot be combined with `--codex`. It is separate from any OS desktop control.

- **The default is inherit.** A child started without the flag has the same launch command as before, with neither `--chrome` nor `--no-chrome`. Whether Chrome is available is decided by the user's own Claude settings (such as `claudeInChromeDefaultEnabled`). The default does not mean "browser disabled".
- **A deviceId is a selection policy.** When several browsers are connected to the same account (for example Chrome on a Mac and Brave on Windows), it tells the child which one to use. The child checks with `list_connected_browsers` that the deviceId is connected and creates its own tab only after `select_browser` succeeds. If the deviceId is missing or disconnected, the child stops browser work and reports it instead of switching to the first or the local browser. Without a deviceId, the child uses no browser tool except `list_connected_browsers` and asks its parent for one. **This is an instruction to the child, not a technical isolation**: Claude Code has no CLI flag that pins Claude in Chrome to one browser. For tests that must not touch another browser, disconnect the extension in the browsers you are not testing so fewer candidates remain.
- **What is guaranteed is a fresh cold launch.** Both cold start and the legacy path add `--chrome`, and the first prompt tells the child how to choose the browser. A warm-pool session was started without `--chrome`, so it is not claimed and the child cold-starts.
- **Resume is a best-effort extra.** The launch record is bound to the conversation (session ID), not to the agent name. Before starting tmux, the launcher writes a provisional record for that launch. The child's first SessionStart hook turns it into the record for its own session ID (`AGENTSTACK_RUNTIME_DIR/child-agents/<name>.claude-launch.<session-id>.json`). A later launch under the same name, or a launch that fails, does not change an earlier conversation's record (a failed launch removes its own provisional record). Resume from the dashboard decides in exactly three ways; conversation text (prompts or tool output) never takes part:
  - A valid record exists for the session ID being resumed: `--chrome` and the browser policy are restored from it, and the launch ID is passed on (a `/clear` after the resume stays bound to the same launch).
  - A record exists but is corrupt, belongs to another agent or session, or is not 0600: the resume stops and reports why.
  - No record exists: the conversation resumes as before (inherit). **Whether it was started without the flag or its record failed to save cannot be told apart. Neither restoring the chosen browser nor keeping the child off the browser is guaranteed.**
- **Known limitation.** A session whose record could not be saved (for example, a new session ID after `/clear` that could not be written) is told by the hook, at that moment, not to use the browser. A later resume of that session ID is treated as having no record (inherit). To continue browser work from a conversation whose record cannot be confirmed, start a new cold child with a deviceId. The SessionStart hook repeats the policy at every startup, resume and compaction only for conversations that have a record.
- **WSL.** The official documentation ([Claude in Chrome](https://code.claude.com/docs/en/chrome)) lists WSL as unsupported. With Claude Code 2.1.283, `claude -p --chrome` in WSL controlled a Windows browser connected through the account on one test machine. Do not treat this as supported, and check again after upgrading.
- **Env defaults.** Setting `AGENTSTACK_CLAUDE_CHILD_CHROME=1` and `AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE=<id>` applies the same request to every Claude child started by `spawn_child.sh`. CLI flags take precedence over env. Codex children ignore them, and they are removed from the environment when a Codex child starts or resumes, so a Codex child does not hand them on to children it starts. The dashboard's NEW AGENT uses only the value chosen in the form, never the env defaults.

## How a Claude child receives its task

The launcher (`spawn_child.sh`) gives a Claude child its first task as the `claude [prompt]` argument, so it is the user's first message. The same launch command passes `--append-system-prompt` with operator configuration saying:

- this session is ORRERY child X, started by the launcher because parent P delegated a task to it;
- the first message is the task P delegated (to be carried out with the usual judgment);
- reports go through `mcp__orrery-mail__send_message`.

It makes no claim the launcher has not checked, such as that a person asked for the task.
The task used to be pasted into the input box. Claude Code wraps a paste in `<pasted_content>` and follows instructions inside it only when the user's own message asks it to. On 2026-10-01, Sonnet 5 children outside the vault (no CLAUDE.md with the managed block) handled a task shaped like the incident as follows:

| Delivery | Result |
|---|---|
| pasted | declined 6 times in 6, however it was worded |
| argument only | declined 2 times in 3 |
| argument and system prompt | started 3 times in 3 |

**Checking that the child started:** after the launch, the launcher reads the child's transcript in the background and looks at how its first turn ends. It reads one transcript, once found, and only what is appended to it.

- A first turn that ends after ORRERY Mail accepted a `mcp__orrery-mail__send_message` to the parent needs no notice (a send that failed, or whose result never came back, is reported). The transcript is the one whose first message was written after this launch, so an earlier conversation of the same name is not mistaken for it.
- The parent is told by ORRERY Mail when the first turn:
  - ends in text alone (declined, or asked for confirmation);
  - called tools but ended without the report (checked and then declined, or forgot to report);
  - has no answer within `AGENTSTACK_CHILD_START_WAIT_SECONDS` (default 180), or its transcript cannot be found.
- A first turn still being written at 180 s (still thinking) gets a separate "still in its first answer" notice.
- A child that starts after an early notice gets one more message saying it started.
- The message arrives under the child's name, but its subject starts with `[launcher]` and its first line says the launcher sent it, not the child.
- Every outcome is logged in `spawn_incidents.log`. `AGENTSTACK_CHILD_START_CHECK=0` turns the check off (for tests).

A task too long for one argument (128 KiB per argument on Linux and WSL) is kept in a file only the child can read, and the argument only says to read that file. A task passed as an argument is visible in the process list (`ps`) while the child runs, as it already was for Codex children.

A warm-pool session (`hooks/warm_pool.sh`) is already running, so its task is still pasted. If you use a warm pool, give it the same system prompt when it is pre-started. Codex children already received their task as an argument.

**Run after a model changes:** `scripts/canary-embed-task.sh` starts short-lived children for each model and place (inside or outside the vault) and tabulates them as started, reported outside Mail, declined, or timed out. It is run by hand, not in CI. Every child is ended and retired at the end. It creates temporary identities in the live ORRERY Mail, so it asks before starting.

```bash
scripts/canary-embed-task.sh --models opus,sonnet,haiku --places vault,outside --runs 3 --parent <your name>
```

## Choosing the tools a child gets (`--base` / `--tools`)

You can choose a child's tools when you start it, for example `/delegate "<task>" --base mail-only --tools screen:read:windows-mcp`. In `/api/spawn` the fields are `base` and `tools` ([API](api.en.md#post-apispawn)). `hooks/child_tools.py` is the single interpreter of both.

- **`--base` omitted or `default`**: the launch is the same as before (same command and arguments). A Claude child's only MCP server is ORRERY Mail, while the browser (Claude in Chrome) and the macOS computer use still depend on the user's Claude settings and the child's working directory. A Codex child inherits the user's MCP servers. Whatever `--tools` selects is added on top
- **`--base mail-only`**: the child gets ORRERY Mail and only what `--tools` selects. A Claude child also gets `--no-chrome` unless a browser was selected. A Codex child gets the same disabling as `--codex-mcp orrery-only`, and then only the selected servers are enabled (combining it with `--codex-mcp inherit` is rejected)

| `--tools` | Claude child | Codex child |
|---|---|---|
| `browser` / `browser:<deviceId>` | `--chrome` plus the deviceId instruction (the same as [Claude in Chrome](#claude-children-and-browser-control-claude-in-chrome); rejected when it names a different deviceId than `--claude-chrome-device`) | Enables the user's chrome-devtools server from `config.toml` (named `chrome-devtools`, or the one server that runs `chrome-devtools-mcp`), exposes only the tools in the table below and approves them one by one. A deviceId is Claude-only and is rejected. The launch stops when there is no such server or a wrapper script keeps the tools from being classified |
| `screen` / `screen:operate` | The built-in macOS computer use. Starts only when it is enabled for the project of the child's directory (listed in `projects[<dir>].enabledMcpServers` of `~/.claude.json` and not in `disabledMcpServers`); otherwise the launch stops | Not available (a server name is required) |
| `screen:<read\|operate>:<server>` | Copies the user's screen server (such as Windows-MCP on WSL) into the strict config | Enables that server in the child's `config.toml` |
| `mcp:<server>` | Copies the user's server definition into the strict config (no tool approved) | Enables that server in the child's `config.toml` (no tool approved) |
| `mcp:<server>:all` | The same, and approves every tool the table knows | The same, and approves every tool the table knows, tool by tool |

### The classification table: read only and operate

What a screen selection exposes and approves is decided by the classification table (`TOOL_TABLE`). It currently lists Windows-MCP 0.8.6 (20 tools) and chrome-devtools-mcp (all 30 tools of 1.10.1), each split into classes.

| Class | Windows-MCP 0.8.6 tools |
|---|---|
| Read (`read`) | Screenshot, Snapshot |
| Operate the screen (`operate`) | App, Click, DisplayInventory, Move, MultiEdit, MultiSelect, Scroll, Shortcut, Type, Wait, WaitFor |
| Not screen tools | Clipboard, FileSystem, Notification, PowerShell, Process, Registry, Scrape |

- `screen:read` exposes and approves only the read tools; `screen:operate` the read and operate tools. The rest is hidden (Claude `--disallowed-tools`, Codex left out of `enabled_tools`). Selecting the screen never passes PowerShell or the registry
- Only a separate `mcp:<server>:all` passes the non-screen tools too. It approves every tool of that server, which on Windows means **running arbitrary code on the host without a person approving it**. The child's first prompt says so as well
- The version is read from the server's command line, which must pin it (such as `uvx windows-mcp@0.8.6`; a package only added with `--with` does not count). With a wrapper script, an unpinned definition or a version that is not in the table, `screen:read` and `mcp:<server>:all` stop the launch, and `screen:operate` copies or enables the server without approving any tool

### The chrome-devtools classes (Codex `--tools browser`)

| Class | chrome-devtools-mcp tools |
|---|---|
| Read (`read`) | get_console_message, get_css_styles, list_console_messages, list_network_requests, list_pages, performance_analyze_insight, select_page, wait_for |
| Operate (`operate`) | click, close_page, drag, emulate, fill, fill_form, handle_dialog, hover, navigate_page, new_page, press_key, resize_page, type_text, evaluate_script, upload_file, take_heapsnapshot, performance_start_trace, performance_stop_trace, lighthouse_audit, get_network_request, take_screenshot, take_snapshot |

- `browser` (Codex) exposes and approves the read and operate tools; `screen:read:chrome-devtools` only the read tools. The read selection leaves all other tools out of `enabled_tools`
- The real definition is usually the unpinned `npx chrome-devtools-mcp@latest`, so this one server is classified by tool **name** even when its version is unknown. Only the names in the table are approved, one by one; a tool a newer version adds is neither exposed nor approved. This differs from the Windows-MCP version pin because the known names define the selected scope, including arbitrary execution, while new names stay out
- Selecting Codex `browser` or Chrome `screen:operate` approves all 30 known tools, including arbitrary JavaScript (`evaluate_script` and `navigate_page.initScript`), saving host files and uploading local files. Approval covers the whole tool, including optional output paths. To narrow this scope, select `screen:read:<server>` or use an overlay without selecting browser/operate (their selection replaces the tool allowlist). Read excludes `take_snapshot.filePath`, `take_screenshot.filePath`, and `get_network_request.requestFilePath` / `responseFilePath`.
- Claude Chrome MCP screen selection requires a known pinned version; unknown versions stop because Claude cannot hide unknown tools with an allowlist. A known pinned read version receives `--disallowed-tools` for the remaining tools. Claude in Chrome browser is unchanged.
- Classification requires a direct `npx [-y|--yes] chrome-devtools-mcp[@version]` invocation. A wrapper or `npx --package chrome-devtools-mcp@version other-program` is not classified. Browser and a different screen server may be selected together; each keeps its own approval scope.
- `mcp:chrome-devtools:all` still needs a known pinned definition (`chrome-devtools-mcp@1.10.1`); Codex browser/operate approves the known names even without a version pin
- This is Codex's tool approval only. The Chrome-side `--autoConnect` connection permission is separate, and a person still answers the Chrome connection prompt

- The built-in macOS computer use and the macOS Codex screen and browser (which go through `node_repl`, a tool that runs arbitrary JS) cannot be read only, because their reading and operating are not separate tools

### Approval

Approval is given only inside the child's own launch, tool by tool, for tools the table knows. The user's `settings.json`, `~/.codex/config.toml` and the global approval policy (Codex `never`) are not changed, and a whole server (Claude `mcp__<server>`, Codex `default_tools_approval_mode`) is never approved.

- Claude gets `--allowed-tools`; Codex gets `approval_mode = "approve"` per tool
- A server selected with `mcp:<server>` (without `:all`) and a server of unknown version are only copied or enabled. To call their tools, the user approves them in their own settings (for Codex, the installer's overlay). Without approval an unattended child stops at its first call (measured with Claude on WSL: the call is denied without an allow rule, and works with `--allowed-tools`)
- Approval is decided again at every launch and resume from the server's version at that time and the table. If the server was upgraded to a version the table does not know, a resume approves less than the launch did (`screen:read` and `:all` stop); it never approves more

### Which servers can be copied (Claude)

Only servers in the user scope of `~/.claude.json` and in the project scope of the child's directory can be copied (the project scope wins). Servers from a project `.mcp.json`, from plugins and claude.ai connectors cannot. These servers cannot be selected, and the launch stops:

- a name that is ORRERY Mail (the child always gets its own authenticated Mail)
- a definition with `env` or `headers` values (copying them would add secrets to the child's config file)
- a stdio `command` that is not an absolute path, or a relative `cwd` (they break with the child's PATH and directory)

### When the launch stops (fail closed)

A child started with `--base mail-only` or any `--tools` is stopped before tmux starts when the selection cannot be applied as asked. It is never started silently with everything or with nothing.

- the ORRERY Mail proxy is unavailable (a child without a selection still falls back to the shared endpoint)
- a selected server is missing or fails the checks above, or is not in the Codex `config.toml`
- `mail-only` without the computer use selected, in a project where the computer use is enabled (macOS). The strict config does not remove the computer use, and a way to remove it has not been verified yet. Start the child in another directory or select `screen:operate`
- `screen:read` or `mcp:<server>:all` with a server or version that is not in the table

A `--worktree` child runs in its own directory (its own project), so the macOS computer use normally does not reach it. Only one session on the Mac can use the computer use at a time: while a child has it, the parent cannot use it.

### Screen on WSL and the Windows session

A Windows process started from WSL runs in the Windows session that WSL runs in. WSL started over ssh is in session 0 (no desktop): Windows-MCP starts, but Screenshot fails with `screen grab failed`. Inside a tmux server started from an RDP or console session it works. When a screen server is selected on WSL, the launcher asks PowerShell for its session number and warns when it is 0. It does not stop the launch, because the launcher and the child's tmux server can be in different sessions.

### Records and resume

- Claude: the selection is kept as `base` and `tools` in the same launch record as [Claude in Chrome](#claude-children-and-browser-control-claude-in-chrome) (`<name>.claude-launch.<session-id>.json`, version 3), bound to the conversation. A dashboard resume rebuilds the same strict config and flags from this record and the current user settings. When it cannot (a server was removed, the computer use was enabled, ...), or when the child has no state so strict cannot be applied, the resume stops and returns the reason. The SessionStart hook repeats the given tools to the child at every start, resume and compaction
- Codex: the selection is kept in `AGENTSTACK_RUNTIME_DIR/child-agents/<name>.tools.json` (0600), and `child_resume.py build-home` applies it to the child's `config.toml` at every launch and resume. A damaged record builds no home, so the launch or resume stops

### What is promised

`mail-only` restricts MCP servers, the browser and the computer use. The tools built into Claude Code or Codex (shell, files, WebFetch, ...) and the user's hooks and skills stay.

This is a policy for the child, not technical isolation. The child runs with the user's permissions and can edit its own config files and records, and the dashboard API does not authenticate its callers. Put the real guard for screen control where it is enforced: the per-application permission of the macOS computer use (granted by a person on screen), and the side that starts Windows-MCP (which tools it publishes).

Adding tools to a running child, changing tools on resume (`tools` in `/api/jump`), showing tools in the roster and DECK, and a NEW AGENT field do not exist yet.

## Choosing between them

There are cases where a built-in subagent is correct: a short search where only the answer matters, or a read-only investigation that should not consume the parent's context. These are jobs that end after one call and that nobody needs to refer to later.

A child agent is needed in cases like these:

- **A person wants to watch progress.** The implementation is long and may need redirection partway through
- **Several tasks should run in parallel.** For example, one agent researches literature, another summarizes experimental results, and the parent remains responsible for synthesis
- **The parent should remain available for conversation with the person.** Each derived task can be handed to a child without interrupting the parent's conversation
- **Children need to communicate directly with each other.** Built-in subagents do not know about one another
- **They may touch the same files.** Reservations are needed
- **The history needs to remain available.** The record of who asked whom to do what is retained

**Giving each agent one responsibility improves the quality of its work.** Dividing roles matters for quality, not merely for the efficiency of parallel execution.

**Child agents are also appropriate when you want to preserve context.** Even after an agent finishes, you can find it through dashboard search and resume it, retaining “the counterpart with this context” for later use. A built-in subagent disappears when its call closes and cannot be used this way.

## When notifications interrupt the conversation

A child's report is typed directly into the parent's input field. With several children running, progress reports can arrive while a person is talking to the parent.

```bash
export AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE=high
```

Messages at `normal` importance or lower will no longer interrupt. **The mail is not deleted.** The signal remains, so the next `fetch_inbox` call reads it normally. This removes the right to interrupt, not the right to arrive.

The likely pattern is to ensure that completion reports always arrive while allowing intermediate progress to accumulate. `/delegate` sends task requests with `importance="high"` and **instructs children to return completion reports at `high` importance as well**. Intermediate reports keep their default importance, accumulate, and can be read at a convenient stopping point.

When you run a child with instructions you wrote yourself, explain this distinction to it. If a child sends a completion report at the default importance in an environment with a higher threshold, **the mail has arrived but the parent will continue waiting**.

## Related documentation

- [Launchers and identity](launchers.en.md) — how names and tokens are determined
- [Dashboard](dashboard.en.md) — how to read nodes and edges in NETWORK
- [Troubleshooting](troubleshooting.en.md) — what to check when delegation does not work
