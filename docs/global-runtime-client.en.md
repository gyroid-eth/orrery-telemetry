# Global runtime client PR4a preparation

[日本語](global-runtime-client.md)

PR4a connects a shared client and registration, recovery and await entrypoints to the [isolated S1 server](global-server.en.md). An absent context keeps the legacy path. Installation copies helpers without creating a global context. Activation remains false; installed services, normal HOME and Mail databases are not switched.

## Isolated context

Explicitly select an owned 0600 JSON with `AGENTSTACK_CLIENT_CONFIG`. Cwd, inherited project variables and tmux names cannot select the recipient. Without that variable the wrapper checks its own root's `runtime-client.json`, then uses the unchanged legacy path if absent. An explicit missing, invalid or fenced context never falls back.

Use kind `orrery-runtime-client-s1`, mode `global`, `activation_enabled=false` and the helper's checkout/install `wrapper_root`. `isolation_root` is a dedicated private 0700 temporary directory. Include:

- Private client `runtime_dir` for session/profile bookkeeping, separate from S1's Mail-state `runtime_root`; both remain inside the isolated root.
- S1's `runtime_root`, `authority`, `authority_lock`, `management_socket` and loopback `mcp_url`.
- `expected_server_instance_id`, `candidate_generation` and `authority_epoch`. Ordinary writes change mutation revision, which is not a pinned generation.
- `lock_identity`: the common lock's `[st_dev, st_ino]`, checked even after wrapper restart.
- `credential_file`: private JSON inside the isolated root, kind `orrery-global-credential-v1`, the same three bindings, agent ID, credential generation and registration token. Tokens are never command arguments.
- `identity`: stable `agent_id`, nonnegative `credential_generation` and display name. To reconnect a mapped window, save both confirmed `window_row_id` and `window_uuid`.

Only before new registration, set identity to null and use `agentstack-runtime-client register NAME PROGRAM MODEL`. It handles atomic `NAME_CONFLICT` instead of probing other names with self-only whois. Unknown registration outcomes leave a pending journal and block another attempt. It does not escape by generating another identity. An existing row without a token requires explicit operator claim.

## Entrypoints

`agentstack-reregister` reconnects by stored ID/token and displays the canonical server name. Deprecated name arguments do not route. Explicit program/model arguments, environment settings and provider models are honored and saved after success in private `runtime_dir/registration-metadata.json` (`orrery-global-registration-metadata-v1`). Context stays unchanged, so another process can reregister without stopping an awaiting client. Old context `registration_metadata` remains read-compatible only. An unspecified reconnect resends these saved values. Without saved metadata, S1 whois cannot supply program/model: reconnect does not call register, returns `reconnect_mode=observe-only` / `registration_verified=false`, and preserves the server row. This reregister path prints `observed ... (registration not refreshed; pass program/model)` and exits 3. Only an actual register with explicit or saved metadata prints `registered` and exits 0. Contact policy is called only when advertised; S1 has no such capability. `agentstack-await-reply` reads the bound owner's inbox and retains sender/after-ID/timeout semantics, without read/ack mutation or signal clearing in S1. Transient `TRANSPORT_FAILED` retries until timeout; authority changes fail immediately.

Sourced provider bootstraps export the canonical identity. Explicit global `agent-start`, `agent-start-codex` and `agent-start-gemini` register through the context and execute in the current terminal; this preparation path does not create a new tmux session. Legacy picker/tmux startup remains unchanged. Tests use shell provider stubs, never actual model processes.

Name resolution and session policy validate the same context. New session records use schema 3 / `global-self` with instance, stable agent ID, credential generation, authority epoch and window. A foreign registration response cannot bind the current session. Legacy schema 2 remains on its existing path. Read-only inspect reports mapped windows as `window_verification=local-only-server-unverified`, without ready=true. Reconnect with explicit or saved program/model verifies them through S1 register and reports `server-verified-by-register`. Observe-only reconnect leaves them server-unverified. A read-only server window capability and registration refresh without changing program/model are S2 candidates.

Credential, authority, lock, context, management socket and pending journal paths are checked for equal paths, resolved paths and device/inode identities before and after calls. Aliases fail with `CONTEXT_PATH_ROLE_CONFLICT`; profiles cannot overwrite these roles either. A broken implicit context symlink also fails closed instead of selecting legacy mode. Client context, credential, journals, metadata, profiles and session records cannot target the Mail `runtime_root` subtree, including resolved symlink aliases. Existing destinations must match their role magic/schema, binding and owner; SQLite, arbitrary text and another owner are rejected before network calls. Symlinks and multiple hardlinks are rejected. New private 0600 temporary files use O_EXCL, and destination format is checked again immediately before rename. This does not atomically exclude a noncooperating same-UID actor replacing names between the final check and rename.

Local magic values are `orrery-runtime-client-s1` (context), `orrery-global-credential-v1` (credential), `orrery-global-registration-pending-v1` (registration pending), `orrery-global-enrollment-pending-v1` (recovery pending), `orrery-global-registration-metadata-v1` (metadata), and `orrery-global-client-profile-v1` (profile). Session records use schema 3 / `global-self`. Existing files with another type, binding or owner are never automatically migrated or overwritten; an operator must correct the destination.

Released raw tokens (with an optional identity sidecar), legacy persistent profiles and schema 2 / self session records selected as global output destinations fail before network calls with `LEGACY_FILE_REQUIRES_IMPORT`, preserving their bytes. Explicit operator import belongs to PR7; PR4a never converts them automatically.

## Operator recovery and profiles

`agentstack-enroll --global-context CONFIG inspect` uses S1's management socket. Explicit operator `claim REQUEST_ID EXPECTED_GENERATION` and `recover REQUEST_ID EXPECTED_GENERATION` persist a new credential in a private pending journal before CAS; identical retries reuse it. Authentication errors never trigger automatic enrollment. A replayed receipt alone cannot activate a token: current inspect generation/fingerprint and whois authentication must agree before local credential/context replacement. A dedicated HOME still reconnects the explicitly selected stable identity.

`agentstack-runtime-client save-profile /absolute/private/profile.json` saves project-free kind `orrery-global-client-profile-v1`. `agentstack-persistent inspect|reconnect --profile PROFILE` and `agentstack-daemon inspect --profile PROFILE` validate bindings. Global proxy/daemon execution remains PR4c; PR4a `run` rejects with `GLOBAL_PROXY_RUNTIME_REQUIRES_PR4C` and never reports ready=true. Legacy profiles remain executable without a global context. Mixing an explicit global context with a legacy profile rejects with `LEGACY_PROFILE_CONTEXT_CONFLICT`. Operator `agentstack-daemon create --connection CONFIG --name NAME --journal PROFILE --operator` reuses the same global registration/profile store; it takes no project input. `finalize-create --connection CONFIG --journal PROFILE --agent-id ID --operator` verifies a pending credential against the supplied stable ID without registering again. These commands do not start the daemon.

## Fence and remaining work

Operations retain a shared common lock and validate context and authority before/after communication and at startup. Quiescing, retired roots, stale epoch/root/instance and replaced lock inodes reject. Explicit fenced legacy contexts also stop before legacy startup. Even an active S1 context marked legacy rejects with `LEGACY_CONTEXT_REQUIRES_PR7`: returning to ambient legacy curl after releasing the lock would not be a writer fence. Unconfigured default legacy operation remains unchanged; legacy transport/authority handoff is not implemented here. Unconfigured old wrappers are not thereby excluded. OS supervisor shutdown, all-writer collection and installed pointer handoff remain PR7. Same-UID non-cooperating filesystem name replacement cannot be made atomic by this validation.

S2 messaging/reservations/contacts, PR4b child manifests/resume/cleanup/bundled skills and PR4c proxies/session bindings/daemon/wake/delivery generations remain unconnected. Gemini stdio explicitly rejects global execution with `GLOBAL_STDIO_REQUIRES_PR4C` after context/authority validation. Native Windows validates the explicit context/authority then rejects with `GLOBAL_S1_UNIX_CONTRACT_UNSUPPORTED`; it cannot fall back to legacy project routing. Windows Unix-socket/flock alternatives are a separate platform design. WSL uses the shared Unix client. Work directories/transcript cwd are metadata, never Mail namespace routing.

## Verification

Use a fresh checkout and [development venv](../CONTRIBUTING.md#test-environment). `tests/test_global_runtime_client.py` reuses S1's canonical fixture: PR3 migrates a synthetic legacy DB into a candidate, then real HTTP/socket tests run with a dedicated HOME and free port. No installed DB input is needed. Finally every server receives TERM, then kill if needed, and its socket/root are removed.

```sh
check_root=$(mktemp -d)
mkdir -m 700 "$check_root/home" "$check_root/tmux"
HOME="$check_root/home" TMUX_TMPDIR="$check_root/tmux" \
  PYTHONPATH=.:packages/agentstack_mail/src .venv/bin/python -m pytest -q \
  tests/test_global_runtime_client.py tests/test_await_reply.py \
  tests/test_reregister_diagnostics.py tests/test_persistent_agents.py
```

Evaluate new contracts, legacy defaults, provider stubs, generation/owner/window checks and recovery retries separately. Run the full suite once per fixed head in a separate environment.

## Verify actual HTTP and management socket yourself

Run this example from the checkout root with `PYTHONPATH=.:packages/agentstack_mail/src .venv/bin/python`. It reuses the canonical synthetic fixture and verifies the preserved token and stable ID against the real server. It never prints the token and finally removes its own server/socket/root.

```python
import importlib.util
import os
from pathlib import Path
import tempfile
from pytest import MonkeyPatch

patch = MonkeyPatch()
private_home = tempfile.TemporaryDirectory(prefix='client-check-home-')
fixture = None
try:
    for key in list(os.environ):
        if key.startswith(('AGENTSTACK_', 'AGS_')) or key in {'TMUX', 'TMUX_PANE', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'}:
            patch.delenv(key, raising=False)
    patch.setenv('HOME', private_home.name)
    patch.setenv('AGENTSTACK_MAIL_ENV_FILE', str(Path(private_home.name) / 'absent.env'))
    path = Path('tests/test_global_runtime_client.py')
    spec = importlib.util.spec_from_file_location('client_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.prepared.__wrapped__(patch)
    state = next(fixture)
    client = module.client(state)
    observed = client.reconnect()
    inspected = client.management('inspect', agent_id=observed['agent_id'])
    assert observed['agent_id'] == inspected['agent_id']
    assert client.local_owner() == state['token']
    assert client.call('health_check')['activation_enabled'] is False
    print('stable ID / preserved token / actual HTTP and socket: OK')
    print('client context:', state['client_path'])
    print('canonical name:', observed['name'])
finally:
    if fixture is not None:
        fixture.close()
    patch.undo()
    private_home.cleanup()
```

## Entrypoints and subsequent ownership

| Path | PR4a support | Remaining work |
|---|---|---|
| Project context / launch / shared register, names and contacts | Explicit context precedes legacy env loading; atomic registration replaces name preflight; absent contact capability is skipped | Protected work folders remain ordinary cwd metadata |
| Register/reregister / Claude, Codex and Gemini bootstrap / await | Stable ID/token and authority checks; dedicated HOME, rename and server window check during registration | Child preregistration/manifests: PR4b |
| Resolve name / identity policy / session index | Same context; authenticated self responses saved as schema 3 | Model guards/reminders/skills: PR4b; proxy external bindings: PR4c |
| Persistent/daemon profiles / enrollment | Shared global profile save/inspect/reconnect; explicit operator CAS | Proxy/daemon execution and wake: PR4c |
| Gemini stdio | Authority validation and fixed rejection reason | Stdio proxy: PR4c |
| Native Windows launcher | Context/authority inspection and fixed Unix-contract rejection | Global transport needs separate platform design; WSL uses the Unix client |

Observe-only reconnect leaves them server-unverified. A read-only server window capability and registration refresh without changing program/model are S2 candidates. PR4a does not change the S1 fixture or wire for its own convenience.
