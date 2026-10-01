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

- A first turn that ends with `mcp__orrery-mail__send_message` to the parent needs no notice.
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
