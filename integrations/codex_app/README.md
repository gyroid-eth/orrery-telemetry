# AgentStack Codex App integration

This directory is the source of truth for the optional Codex App bridge. It
keeps experimental app-server control isolated from agent-mail identities and
from the dashboard's tmux runtime path.

The current P1 scaffold provides:

- a synchronous JSON-RPC client for `codex app-server` over stdio;
- versioned runtime-event and binding schemas plus delivery-state migration;
- plugin, bridge, MCP proxy, identity, and snapshot module boundaries;
- fake-server protocol tests that do not start Codex or require tmux.

`hook_entry.py` and `wake.py` deliberately raise `NotImplementedError`. App-side
lifecycle hook behavior and cold wake remain gated on the in-App P0 checks.

## Development

```sh
python -m pytest -q integrations/codex_app/tests
```

To opt into the read-only real app-server smoke test:

```sh
AGENTSTACK_RUN_CODEX_INTEGRATION=1 python -m pytest -q -m integration \
  integrations/codex_app/tests/test_protocol_integration.py
```
