---
name: coordinate
description: Coordinate a Codex App task through the AgentStack bridge after it has been bound to an agent-mail identity.
---

# Coordinate through AgentStack

Use only the bridge-provided, session-bound coordination tools. Fetch message
bodies through the proxy, reserve files before editing, acknowledge or reply to
delivered messages, and report verification results to the requesting agent.

Never request, print, or copy registration tokens. Do not infer identity from
inherited `AGENT_NAME`, `TMUX`, `TMUX_PANE`, or pane titles.
