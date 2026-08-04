---
name: coordinate
description: Coordinate a Codex App task or identify its current agent-mail identity through the session-bound AgentStack bridge.
---

# Coordinate through AgentStack

Use only the bridge-provided, session-bound coordination tools. Fetch message
bodies through the proxy, reserve files before editing, acknowledge or reply to
delivered messages, and report verification results to the requesting agent.

Call `agentstack.bootstrap` once with the current `session_id` before other
coordination tools. A subagent must also pass its hook-provided `agent_id`.
The bootstrap result, not shell or terminal state, is the authoritative current
identity. After bootstrap, do not pass a session ID, agent ID, project key, or
agent name to later tools; the MCP process supplies its fixed binding. Use
`agentstack.runtime_status` with no arguments when asked which agent this task
is. If bootstrap fails, report that identity is unknown and stop coordination.

Continue using the same session binding for all later calls. If a PostToolUse
notice or cold-wake prompt reports pending mail, call
`agentstack.fetch_inbox`. Cold wake resumes only an idle task; an active turn
receives the PostToolUse notice instead of a second concurrent turn.
Stopped subagents are not cold-wake targets; durable external work should be
addressed to the root task.

Never request, print, or copy registration tokens. Do not infer identity from
inherited `AGENT_NAME`, `TMUX`, `TMUX_PANE`, or pane titles.
