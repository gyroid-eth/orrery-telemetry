# AgentStack Codex App integration

This directory is the source of truth for the optional Codex App bridge. It
keeps experimental app-server control isolated from agent-mail identities and
from the dashboard's tmux runtime path.

The current P3 implementation provides:

- a synchronous JSON-RPC client for `codex app-server` over stdio;
- versioned runtime-event and binding schemas plus delivery-state migration;
- a private Bridge socket, fail-open hook spool, durable identity bindings,
  separately protected owner tokens, and sanitized dashboard snapshots;
- server-assigned root and subagent names: fresh registrations omit `name`,
  then atomically adopt the canonical agent-mail response into the binding;
- a fail-closed App-surface filter backed by matching `Codex Desktop` rollout
  metadata; transcript-less sessions are skipped because cold wake requires a
  resumable rollout and such identities would be unusable;
- the same rollout eligibility check at daemon ingress and retry replay, plus
  bounded registration retries with exponential backoff and legacy-row
  migration, so pre-filter CLI spool rows cannot recreate identities;
- startup recovery for interrupted hook/retry drain files and sanitized
  launcher/daemon lifecycle diagnostics on launchd stderr;
- a session-bound, allowlisted MCP proxy for inbox, messaging, acknowledgement,
  reservations, and sanitized runtime/lineage status;
- PostToolUse pending-mail notices for active turns;
- SQLite delivery leases with message-level idempotency, two-second
  coalescing, bounded exponential retry, an hourly wake limit, and
  dead-letter state;
- metadata-only cold wake through an argv-safe `codex exec resume` adapter,
  using the hook-reported workspace cwd, without message bodies or dangerous
  bypass flags;
- bounded, token-redacted resume diagnostics; untrusted workspaces fail once
  as `blocked / untrusted_workspace` instead of silently exhausting retries;
- single-run approval policy for only the eight session-bound `agentstack`
  proxy tools; the wake invocation disables only the plugin-bundled proxy
  server and re-registers the same local launcher under the same server name
  because the deployed Codex CLI 0.144.4 does not apply plugin-provided MCP
  tool policy during headless execution. This does not change shell, sandbox,
  other MCP, or global approval policy;
- sanitized `wake_failed`, blocked, pending, and dead-letter telemetry for the
  Dashboard provider;
- fail-closed stopped-subagent handling: durable cold wake targets root tasks;
  blocked child delivery exposes only its parent root external ID for a future
  count-only escalation path;
- injectable agent-mail transport and a Codex App runtime provider;
- fake-server protocol tests that do not start Codex or require tmux.

## Identity lifecycle

The Bridge does not maintain an agent-name pool. A fresh root or subagent
binding starts with a local-only `Pending-<external-id-hash>` label and calls
`register_agent` without `name`, allowing agent-mail to choose from its
canonical name and portrait namespace. The response name is immediately
adopted by the durable binding and its runtime snapshot. The provisional label
is also withheld during registration retries, so it cannot become a remote
identity.

Once a binding has a server-confirmed name, startup reconciliation and later
SessionStart/SubagentStart re-authentication send that persisted name together
with its existing owner token. This preserves inbox and delivery continuity
without allocating a replacement identity.

## Development

```sh
python -m pytest -q integrations/codex_app/tests
```

To opt into the read-only real app-server smoke test:

```sh
AGENTSTACK_RUN_CODEX_INTEGRATION=1 python -m pytest -q -m integration \
  integrations/codex_app/tests/test_protocol_integration.py
```

The real `codex exec resume` smoke test is separately opt-in and requires an
explicit disposable session ID:

```sh
AGENTSTACK_RUN_CODEX_WAKE_INTEGRATION=1 \
AGENTSTACK_CODEX_WAKE_SESSION_ID=<session-id> \
python -m pytest -q -m integration \
  integrations/codex_app/tests/test_wake.py
```

## Packaging

The installer keeps source and generated state separate:

- source is copied to `~/.agentstack/integrations/codex_app/`;
- runtime state stays under `~/.agentstack/runtime/codex-app/`;
- `env.sh` is generated with mode `0600` and contains references, not bearer
  tokens;
- the local marketplace snapshot contains a self-contained plugin cache bundle
  with its Python source and schemas;
- on macOS the installer treats `launchctl bootstrap`/`enable`/`kickstart` as
  the capability probe; launchd uses `RunAtLoad` and `KeepAlive` when they
  succeed;
- when the GUI launchd domain is unavailable (including headless SSH or a
  sleeping GUI session), the installer removes the live plist and starts a
  supervised background Bridge instead. The supervisor restarts a failed
  Bridge child and keeps its pidfile and logs in the runtime directory;
- the install manifest and doctor report and verify the service mode that is
  actually running. `--no-service` disables both launchd and the fallback.

Preview an install without touching Codex or launchd:

```sh
scripts/install-codex-app-integration.sh \
  --dry-run --no-service --no-plugin \
  --project-key /absolute/project \
  --agent-mail-url http://127.0.0.1:18765/api/
```

After an approved install, diagnose it with:

```sh
~/.agentstack/integrations/codex_app/bin/doctor-codex-app-integration
```

Waiting runtimes become `dormant` after one hour without a lifecycle event by
default. The installer accepts `--stale-after SECONDS` (minimum five minutes)
for environments with a different observed idle cadence.

Transient agent-mail registration failures retry at most 12 calls over a
maximum one-hour lifetime, with a five-minute backoff cap. The corresponding
installer options are `--retry-max-attempts`, `--retry-max-age`, and
`--retry-max-backoff`. Non-Desktop or transcript-less rows are deterministic
drops and are never re-spooled.

To explicitly retire Bridge-owned identities whose matching Codex Desktop
rollout no longer exists, then purge their local binding and snapshot:

```sh
~/.agentstack/integrations/codex_app/bin/doctor-codex-app-integration \
  --allow-stopped --cleanup-orphan-bindings
```

Cleanup is isolated per binding. Already-retired remote identities are purged
locally even when a legacy owner token no longer matches; active identities
whose retirement fails remain local and are reported after the remaining
bindings have been processed.

Codex repository trust is enforced by default. For a deliberately reviewed
non-git workspace only, the installer accepts the explicit
`--skip-git-check` opt-in, which writes
`AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK=1`; it is never enabled implicitly.

To explicitly replay one failed or dead-lettered root delivery after fixing
its cause:

```sh
~/.agentstack/integrations/codex_app/bin/doctor-codex-app-integration \
  --allow-stopped --requeue-message 123 --agent-name ExampleAgent
```

Uninstall retains delivery/binding runtime state unless `--purge-data` is
explicitly supplied. To build a public-review artifact through the mechanical
allowlist and privacy gate:

```sh
scripts/export-component.sh codex-app /absolute/export/destination
```
