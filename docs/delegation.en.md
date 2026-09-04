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
| Identity | None. An internal ID for each call (a hexadecimal value such as `a1798ced…`) | A name registered with agent-mail (an adjective and scientist name such as `Teal-Darwin`) |
| Process | Same process as the parent | Independent tmux session (and optionally a terminal window) |
| Dashboard | **Does not appear** (it does not exist even as a node) | Appears as a node, with a line connecting it to its parent |
| Communication | Only the arguments from the parent and the final returned text | agent-mail. It can communicate bidirectionally with agents other than its parent |
| Lifetime | Only for that single call | Until explicitly ended. More work can be sent later |
| Recovery after interruption | Not possible | The tmux session remains and can be reopened with dashboard jump / resume |
| File coordination | None | File reservations avoid collisions with other agents |
| Progress visibility | Unknown until it finishes | Intermediate progress can be watched in its pane. A monitoring loop can run |
| Best suited for | Short investigations, searches, and one-off decisions | Long implementations, parallel work, and work whose progress people need to see |

## How to tell them apart

The most reliable method is to **look at the dashboard**. A built-in subagent does not register with agent-mail, so it does not appear as a node. It is not merely missing a line: **it does not exist there**.

If you perform a simple communication check and any of the following is true, you are using a built-in subagent:

- No child node appears in NETWORK
- The child's name is a hexadecimal identifier rather than an adjective and scientist name
- The parent's log contains `Agent(...)` / `Task(...)` calls
- The child has no session in `tmux ls`

With a child agent, by contrast, an edge connects the two agents and displays their message count. Opening the edge shows each exchange as an individual mail message.

## Why they are silently substituted

`/delegate` uses the agent-mail MCP tools. **When those tools are absent, a parent may decide on its own to switch to a built-in subagent and finish the work.** The work completes and a report returns, so from a person's perspective it looks like an unqualified success.

This stack prevents that behavior in three layers.

1. **The installer registers agent-mail as an MCP server.** Previously, the registration procedure was undocumented and silently assumed that users had already completed it
2. **`agentstack-doctor` reports missing or inconsistent registration.** It also displays the repair command
3. **The managed instructions (`claude/CLAUDE.md` / `codex/AGENTS.md`) state that delegation must use only `/delegate`, and that when the tools are absent the agent must report the problem and stop instead of substituting another mechanism**

Run `agentstack-selftest` immediately after installation. It verifies **functionality**, not mere presence (registration validation → spawning two actual agents → mail delivery in both directions).

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
