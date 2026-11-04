# Optional ORRERY MCP adapter (read only)

This is phase 1 of upstream Issue #395. It exposes exactly three read-only tools: `orrery_status`, `orrery_list_agents` and `orrery_get_agent`. It does not change the regular installer, daemon, Dashboard, Mail or UI. It does not implement spawning, messaging, OAuth, launchd or installation. Its only Dashboard calls are `GET /api/version` and `GET /api/agents`.

Python 3.11+ is required. Explicitly set `ORRERY_MCP_MODE=legacy` for the old version contract (no runtime_mode in /api/version), or `ORRERY_MCP_MODE=fresh` for a new Mail instance with fresh identity and capabilities. Experimental global, mismatched, missing and unrecognized contracts fail closed. API generation is never taken as a mode signal.

Fresh agent rows retain `mail_instance_id`, `mail_agent_id` and explicit observation state. Unobserved rows stay in the default list (`running_only=false`) with `running=null`; they are not treated as stopped. Legacy has no stable Mail IDs in the Dashboard contract, so the adapter reports null instead of inventing them.

## Manual operation

From an isolated checkout (no production service is replaced):

```sh
cd /path/to/orrery-telemetry
export PYTHONPATH="$PWD/integrations/chatgpt_mcp/src"
export ORRERY_MCP_MODE=legacy
export ORRERY_MCP_DASHBOARD_URL=http://127.0.0.1:8770
export ORRERY_MCP_PORT=8791
python3 -m orrery_chatgpt_mcp pair --show-url
python3 -m orrery_chatgpt_mcp check
python3 -m orrery_chatgpt_mcp serve
```

Use fresh only when your Dashboard is actually using fresh. The printed URL contains a private 43-character secret. Keep it out of issues, logs and shared documents. `pair --rotate` revokes the old URL. The state directory and pairing file are owner-private (0700/0600). The adapter binds only 127.0.0.1; it cannot be used from other devices without a separately configured secure tunnel.

Before using a Secure MCP Tunnel, verify its target port, who can connect to it, which user/account owns the connection and whether its URL or credentials are shared. Knowledge of the secret URL grants reading access to the exposed state, so do not assume the URL is private merely because it uses a tunnel. Chat confirmation does not constitute server-side user approval.

## Verify read-only behavior

Use the authenticated MCP URL to initialize and call `tools/list`. It must list only the three read tools. `orrery_status` must report `adapter.spawn_enabled_on_server=false` and `adapter.message_enabled_on_server=false`. Calling `orrery_message_commit` directly must yield `isError=true` with `unknown_tool`. An unauthenticated 404 is not proof that writing is disabled.

`check` validates the Dashboard identity, runtime mode and roster, not the live process's configuration or the tunnel's access policy. There is no launchd installation or service status in this phase. Stop a manual `serve` process with Ctrl+C.

## Isolated tests

```sh
PYTHONPATH=integrations/chatgpt_mcp/src:. python3 -m pytest -q integrations/chatgpt_mcp/tests/test_readonly.py
```

Tests use synthetic legacy/fresh Dashboard payloads and temporary loopback HTTP servers; they never modify real agents, Mail, tmux or tunnels. Sending, receipts and actual agent execution are future phases.
