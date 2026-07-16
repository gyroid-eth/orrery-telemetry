# AgentStack Codex App integration

This directory is the source of truth for the optional Codex App bridge. It
keeps experimental app-server control isolated from agent-mail identities and
from the dashboard's tmux runtime path.

The current P1 implementation provides:

- a synchronous JSON-RPC client for `codex app-server` over stdio;
- versioned runtime-event and binding schemas plus delivery-state migration;
- a private Bridge socket, fail-open hook spool, durable identity bindings,
  separately protected owner tokens, and sanitized dashboard snapshots;
- minimal, injectable agent-mail registration and a Codex App runtime provider;
- fake-server protocol tests that do not start Codex or require tmux.

`wake.py` deliberately raises `NotImplementedError`. P1 performs no inbox
polling, prompt injection, automatic resume, or cold wake.

## Development

```sh
python -m pytest -q integrations/codex_app/tests
```

To opt into the read-only real app-server smoke test:

```sh
AGENTSTACK_RUN_CODEX_INTEGRATION=1 python -m pytest -q -m integration \
  integrations/codex_app/tests/test_protocol_integration.py
```
