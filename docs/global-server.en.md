# Global server S1 preparation

[日本語](global-server.md)

S1 is an isolated real HTTP and Unix management-socket server for #213 client preparation. `agentstack-mail` without extra arguments retains the legacy server and its 25 tools. Only explicit `--global-config` selects S1. Startup requires `AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough`; the CLI rejects the default coerce mode. The PR3 pointer and database activation remain false. No installed service or user HOME is switched.

## Verify the isolated contract yourself

Use a fresh checkout and the [development venv](../CONTRIBUTING.md#test-environment). Run the Python below with `.venv/bin/python` and `PYTHONPATH=.:packages/agentstack_mail/src`. Inputs are the checkout and development dependencies; no installed Mail database, normal HOME settings or running service is used.

The example reuses the [synthetic S1 fixture](../packages/agentstack_mail/tests/test_global_server.py). PR3 converts a synthetic legacy database into a schema-2 candidate and obtains its instance/candidate values. No guessed IDs or real-database queries are needed. The fixture creates a private 0700 temporary root, complete 0600 config/authority files, a dedicated HOME, a free loopback port and a management socket. Its child environment specifies passthrough and an explicitly absent `AGENTSTACK_MAIL_ENV_FILE`, preventing normal .env loading. Finally it sends TERM to its own server and removes the socket and temporary root.

```python
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
from pytest import MonkeyPatch

path = Path("packages/agentstack_mail/tests/test_global_server.py")
spec = importlib.util.spec_from_file_location("s1_check", path)
fixture_module = importlib.util.module_from_spec(spec)
patch = MonkeyPatch()
temporary_home = tempfile.TemporaryDirectory(prefix="s1-home-")
fixture = None
try:
    for key in list(os.environ):
        if key.startswith(("AGENTSTACK_", "AGS_")) or key in {
            "TMUX", "CODEX_HOME", "CLAUDE_CONFIG_DIR"
        }:
            patch.delenv(key, raising=False)
    patch.setenv("HOME", temporary_home.name)
    spec.loader.exec_module(fixture_module)
    fixture = fixture_module.server.__wrapped__(patch)
    state = next(fixture)
    print("config:", json.dumps(state["config"]))
    print("authority:", (state["root"] / "authority.json").read_text())
    fixture_module.test_http_catalog_and_three_generations(state)
    before = fixture_module.call(state, "health_check")
    fixture_module.call(
        state, "register_agent",
        **fixture_module.owner(state, program="fixture", model="fixture")
    )
    after = fixture_module.call(state, "health_check")
    assert after["mutation_revision"] == before["mutation_revision"] + 1
    assert after["authority_epoch"] == before["authority_epoch"]
    assert after["candidate_generation"] == before["candidate_generation"]
    request = {"version": 1, **state["binding"], "action": "inspect", "agent_id": 2}
    print("socket request (one JSON line):", json.dumps(request) + "\n")
    inspected = fixture_module.control(state, action="inspect", agent_id=2)
    token = secrets.token_urlsafe(32)
    receipt = fixture_module.control(
        state, action="recover", request_id="documentation-recovery", agent_id=2,
        expected_generation=inspected["credential_generation"], new_credential=token
    )
    assert receipt["ok"]
    assert fixture_module.call(
        state, "whois", **fixture_module.owner(state, token=token)
    )["id"] == 2
    print("health before:", json.dumps(before))
    print("health after:", json.dumps(after))
    print("receipt (no credential):", json.dumps(receipt))
    print("S1 check: tools=7, resources=0, epoch unchanged, owner recovery OK")
finally:
    if fixture is not None:
        fixture.close()
    patch.undo()
    temporary_home.cleanup()
```

Output includes the complete config and authority actually used. Management uses one JSON line followed by a newline (JSONL), containing version, the three binding fields, action and agent ID. Inspect returns canonical name/generation/fingerprint. Recovery adds request ID, expected generation and new credential. The example does not print that credential; it checks the successful receipt against real HTTP whois. Receipt replay retrieves operation history, not proof of the current credential. Inspect and whois verify the current state.

The fixture uses this startup entrypoint; the example starts and stops it automatically. Here `fixture` and `port` represent the generated temporary root and free port:

```sh
env -i PATH="$PATH" HOME="$fixture/home" \
  AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough \
  AGENTSTACK_MAIL_ENV_FILE="$fixture/missing.env" \
  .venv/bin/python -m agentstack_mail.cli \
  --global-config "$fixture/config.json" --host 127.0.0.1 --port "$port"
```

Run the file below without `-n` for acceptance checks. Every node is in the same [acceptance test file](../packages/agentstack_mail/tests/test_global_server.py). Check pytest exit 0 and its final passed count; on failure also read the FAILED lines.

```sh
check_root="$(mktemp -d)"
mkdir "$check_root/home" "$check_root/tmux"
HOME="$check_root/home" TMUX_TMPDIR="$check_root/tmux" \
  PYTHONPATH=.:packages/agentstack_mail/src \
  .venv/bin/python -m pytest -q packages/agentstack_mail/tests/test_global_server.py
```

| Contract | Node | Expected result |
|---|---|---|
| Legacy mode | `test_default_cli_keeps_real_legacy_tools_database_and_management` | 25 tools; legacy project schema/rows preserved |
| Catalog / three generations | `test_http_catalog_and_three_generations` | All seven schemas match the fixture; no resources; activation=false |
| Rename / window / normal write | `test_register_reconnect_stable_token_window_and_revision` | Canonical ID/name; wrong owner/UUID rejected; only counter advances |
| Own inbox | `test_inbox_privacy_and_owner_auth` | Own recipients only; no BCC peers/tokens; no read/ack mutation |
| Stale binding / root | `test_stale_wire_binding_rejects_http_and_management` / `test_common_authority_fences_current_http_management_and_restarted_wrapper` | STALE_RUNTIME_BINDING / WRITER_FENCED |
| Updater / replaced lock | `test_updater_exclusive_gate_prevents_new_request` / `test_replaced_startup_lock_fences_http_and_management` | Exclusive gate and replaced inode rejected; revision unchanged |
| SQLite sidecar | `test_sqlite_sidecars_cannot_write_outside_isolation` | Startup/HTTP reject unsafe files; outside sentinel unchanged |
| Main DB after startup / precommit | `test_main_database_becoming_unsafe_fences_existing_runtime` / `test_rejected_main_alias_preserves_independently_committed_wal` / `test_sqlite_files_becoming_unsafe_before_commit_roll_back` | HTTP/management reject added links or public modes; alias WAL commit survives; mid-operation changes roll back |
| Enrollment | `test_management_recovery_cas_replay_and_secret_free_receipt` / `test_null_token_claim_is_operator_only_and_audited` | CAS/audit/same receipt on retry; old token/generation rejected |
| Credential | `test_accepted_unicode_credential_authenticates_owner` | New/recovered/preserved token authenticates; different token rejected |


## Wire contract

The source of truth is `fixtures/global-server-s1.json` in the package and the actual signatures in `global_server.py`. Discover capabilities through `tools/list`; do not fall back when a required capability is absent.

Health returns the stable `server_instance_id`, `candidate_generation`, `authority_epoch`, `mutation_revision`, supported tools and unsupported resources. Owner operations require `expected_server_instance_id`, `candidate_generation`, `authority_epoch`, numeric `agent_id` and `registration_token`. Optional legacy `project_key` is ignored. Names, cwd and inherited tmux project settings cannot select the owner.

Registration authenticates an existing ID/token and returns its canonical name and credential generation after an approved rename. Reconnection can supply both `window_row_id` and `window_uuid` for owner verification. An existing identity without an owner token is never claimed by silently generating one. New registration omits the ID and rejects lookup/name conflicts; it creates no project membership. `whois` and inbox expose only the authenticated identity and its recipient rows. BCC peers and credentials are excluded. Inbox does not clear signals or mutate read/ack state in S1. Retirement and restoration require the same owner token. `ensure_project` creates nothing.

Three generations are distinct: candidate generation is the PR3 candidate fingerprint; mutation revision is the database `write_generation` counter; authority epoch belongs to the common runtime fence. Ordinary writes advance only the counter.

## Isolated configuration and fence

Configuration is an owner-private 0600 JSON with `kind=orrery-global-server-s1`, `activation_enabled=false`, `isolation_root`, `runtime_root`, `database`, `mail_instance_id`, `candidate_generation`, `authority_epoch`, `authority`, `authority_lock` and `management_socket`. All paths are absolute. Database, authority, lock and socket must reside in an owner-private 0700 temporary root. The server opens only a PR3 schema-2 candidate and never initializes the legacy ORM schema against it. Normal HOME database paths are rejected.

Authority contains `kind=orrery-global-authority-v1`, `phase=active`, `root_status=active`, `runtime_root` (the target state root within the temporary isolation directory), instance, candidate generation and authority epoch. The updater must hold an exclusive lock on the same private regular lock file when changing authority. The server holds a shared lock across the operation and SQLite transaction and rechecks authority before commit. Quiescing, retired roots, stale epochs and replacement of config/database/lock are rejected.

This fences cooperating writers. OS supervisor shutdown, complete writer inventory, old connection termination and installed pointer handoff remain PR7 work. An unknown process with direct database access is not stopped by this lock alone.

The server pins the validated startup lock dev/inode and checks it at operation entry and before commit. Replacing a lock while the runtime is running is unsupported. A legitimate replacement requires stopping every old writer, updating authority epoch/configuration and starting a new runtime as a PR7 handoff. Replacing the path cannot let an old runtime continue.

The main database and existing `-wal`, `-shm` and `-journal` files are checked before and immediately after the initial SQLite open, and before/after each transaction's open and before commit. Symlinks, multiple hardlinks, different owners or public modes are rejected with `DATABASE_UNSAFE` for the main database or `SQLITE_SIDECAR_UNSAFE` for sidecars. The same conditions apply after startup; changes detected during an operation roll back the database transaction. Credential verification uses constant-time UTF-8 byte comparison without normalization; accepted non-ASCII tokens and tokens preserved by PR3 authenticate with the same value.

These checks cover cooperating writers that honor the common lock. After the validated descriptors close, SQLite reopens paths by name. Validation/open and final validation/commit are not atomic filesystem operations. A non-cooperating process with the same UID can still change links or paths in those intervals. Post-open and precommit checks narrow the intervals and detect changes, but rollback cannot guarantee reversal of writes already made to an outside sidecar. This race itself was not measured in these tests. Stopping all old writers and installed handoff remain PR7 work.

## Management socket

The owner-UID Unix socket implements global management `version=1`; enrollment is absent from MCP. Every request supplies the same instance/candidate/epoch binding as HTTP. `inspect` takes an agent ID and returns canonical name, credential generation and a non-secret fingerprint. `claim`/`recover` take request ID, agent ID, expected credential generation and new credential, distinguish null-token claims from recovery and record accepted CAS/audit atomically. Identical retries return the same non-secret receipt; conflicting contents are rejected. `request_status` returns the recorded receipt.

Historical enrollment records remain intact; global receipts/audit use separate tables. Client CLI/profile recovery in a dedicated HOME is subsequent PR4a work.

## Unsupported capabilities

S1 publishes seven tools: health, ensure, register, whois, inbox, retire and unretire. The other 18 tools are listed in the fixture's `unimplemented` array and remain S2 work: sending/replying, read/ack/search, contacts, summaries/topics, reservations/macros, archive/attachments/Git writers, global signal writing/clearing and generated Git guard integration. No resources are published. Retained legacy non-published bodies do not become new public requirements.

The sequence is S1 → 4a → S2 → 4b → 4c. S1 does not complete global operation or installed cutover. Test legacy regression separately from the seven real HTTP capabilities and management socket.
