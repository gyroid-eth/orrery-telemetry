---
name: coordinate
description: Coordinate a Codex App task or identify its current ORRERY Mail identity through the session-bound AgentStack bridge.
---
## Explicit global preparation

When `AGENTSTACK_CLIENT_CONFIG` selects global mode, use the fixed client context and stable IDs. Do not apply the legacy project/name/raw-token registration procedure below. Child preparation uses `agentstack-preregister-child --child-client-name child_01 --name ChildAlpha --program codex --model explicit-model --task-description "Task summary" --prepare-only`; repeat the same command to recover its saved registration intent. Registration dispatch remains in `bin/lib/agentstack-register.sh`. Global child execution/proxy/wake is pending4c: ordinary spawn/resume refuses, and prepare-only never means a running child. Do not use another launcher to bypass that refusal. Message writes use typed JSON through `agentstack-runtime-client call-json send_message` with `to_agent_ids`; replay saved message bytes with `replay-pending`. Follow the actual bound proxy schema when proxy tools are provided. See `docs/global-child-client.md` / `docs/global-child-client.en.md`.



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
