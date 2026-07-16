---
name: coordinate
description: Coordinate a Codex App task through the AgentStack bridge after it has been bound to an agent-mail identity.
---

# Coordinate through AgentStack

Use only the bridge-provided, session-bound coordination tools. Fetch message
bodies through the proxy, reserve files before editing, acknowledge or reply to
delivered messages, and report verification results to the requesting agent.

Call `agentstack.bootstrap` once with the current `session_id` before other
coordination tools. A subagent must also pass its hook-provided `agent_id`.
Continue using the same session binding for all later calls. If a PostToolUse
notice or cold-wake prompt reports pending mail, call
`agentstack.fetch_inbox`. Cold wake resumes only an idle task; an active turn
receives the PostToolUse notice instead of a second concurrent turn.
Stopped subagents are not cold-wake targets; durable external work should be
addressed to the root task.

Never request, print, or copy registration tokens. Do not infer identity from
inherited `AGENT_NAME`, `TMUX`, `TMUX_PANE`, or pane titles.
