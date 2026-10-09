"""The common stdlib client against actual isolated S2a HTTP and admin socket."""

import importlib.util
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
api = runpy.run_path(str(ROOT / "bin/lib/runtime_client.py"))
RuntimeClient, ClientError = api["RuntimeClient"], api["ClientError"]
spec = importlib.util.spec_from_file_location(
    "client_s2a_fixture",
    ROOT / "packages/agentstack_mail/tests/test_global_server_s2a.py",
)
s2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2)


@pytest.fixture
def server(monkeypatch):
    prep = s2.prepared.__wrapped__(monkeypatch)
    state = next(prep)
    service = s2.server.__wrapped__(state)
    try:
        next(service)
        yield state
    finally:
        service.close()
        prep.close()


def context(state, name="client", identity=True, window=True):
    cfg = state["config"]
    root = state["root"] / "agentstack/clients" / name
    root.mkdir(mode=0o700, parents=True)
    root.parent.chmod(0o700)
    root.parent.parent.chmod(0o700)
    (root / "runtime").mkdir(mode=0o700)
    lock = Path(cfg["authority_lock"]).stat()
    value = {
        "kind": api["KIND"],
        "mode": "global",
        "activation_enabled": False,
        "wrapper_root": str(ROOT),
        "isolation_root": str(state["root"]),
        "runtime_root": cfg["runtime_root"],
        "agentstack_home": str(state["root"] / "agentstack"),
        "client_name": name,
        "authority": cfg["authority"],
        "authority_lock": cfg["authority_lock"],
        "lock_identity": [lock.st_dev, lock.st_ino],
        "management_socket": cfg["management_socket"],
        "mcp_url": state["url"],
        "expected_server_instance_id": cfg["mail_instance_id"],
        "candidate_generation": cfg["candidate_generation"],
        "authority_epoch": cfg["authority_epoch"],
        "identity": (
            {"agent_id": 2, "credential_generation": 9, "name": "Beta"}
            if identity
            else None
        ),
    }
    if identity and window:
        with s2.connection(Path(cfg["database"])) as db:
            row = db.execute(
                "SELECT id,window_uuid FROM window_identities WHERE agent_id=2"
            ).fetchone()
        value["identity"].update(
            window_row_id=row["id"], window_uuid=row["window_uuid"]
        )
    path = root / "runtime-client.json"
    api["atomic_json"](path, value)
    if identity:
        api["atomic_json"](
            root / "credential.json",
            {
                "kind": "orrery-global-credential-v1",
                **{
                    k: value[k]
                    for k in (
                        "expected_server_instance_id",
                        "candidate_generation",
                        "authority_epoch",
                    )
                },
                **value["identity"],
                "registration_token": "fixture-owner-two",
            },
        )
    return RuntimeClient(path)


def test_actual_client_refresh_retains_metadata_and_verifies_window(
    server, monkeypatch
):
    monkeypatch.setenv("AGENTSTACK_CONTACT_POLICY", "skip")
    client = context(server)
    observed = client.observe()
    assert (
        observed["window_verification"] == "server-verified-read-only"
        and observed["window_ready"]
    )
    result = client.reconnect()
    assert result["reconnect_mode"] == "refresh-preserving-metadata"
    with s2.connection(Path(server["config"]["database"])) as db:
        row = db.execute(
            "SELECT program,model,registration_token,credential_generation FROM agents WHERE id=2"
        ).fetchone()
        assert tuple(row) == ("fixture", "fixture", "fixture-owner-two", 9)
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 0
        )


def test_lost_response_reuses_uuid_and_exact_preimage(server, monkeypatch):
    client = context(server)
    original = client.rpc
    once = False

    def lose(method, params):
        nonlocal once
        result = original(method, params)
        if (
            not once
            and method == "tools/call"
            and params.get("name") == "set_contact_policy"
        ):
            once = True
            raise ClientError("TRANSPORT_FAILED")
        return result

    monkeypatch.setattr(client, "rpc", lose)
    with pytest.raises(ClientError, match="TRANSPORT_FAILED"):
        client.call("set_contact_policy", {"policy": "auto"})
    pending = api["read_json"](client.outputs["mutation"])
    assert pending["phase"] == "planned"
    fresh = RuntimeClient(client.path)
    with pytest.raises(ClientError, match="MUTATION_PENDING_CONFLICT"):
        fresh.call("set_contact_policy", {"policy": "open"})
    result = fresh.call("set_contact_policy", {"policy": "auto"})
    assert result["request_id"] == pending["request_id"]
    assert not client.outputs["mutation"].exists()
    with s2.connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 1
        )
        row = db.execute(
            "SELECT canonical_request_json FROM global_operation_receipts"
        ).fetchone()[0]
        assert json.loads(row) == pending["canonical_payload"]


def test_pending_and_held_mutex_do_not_block_liveness(server, monkeypatch):
    import fcntl

    client = context(server)
    original = client.rpc

    def lose(method, params):
        result = original(method, params)
        if method == "tools/call" and params.get("name") == "set_contact_policy":
            raise ClientError("TRANSPORT_FAILED")
        return result

    monkeypatch.setattr(client, "rpc", lose)
    with pytest.raises(ClientError):
        client.call("set_contact_policy", {"policy": "auto"})
    before = client.outputs["mutation"].read_bytes()
    fresh = RuntimeClient(client.path)
    with fresh.outputs["mutex"].open("rb") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert fresh.observe()["window_ready"]
        result = fresh.call(
            "touch_window_identity",
            {k: fresh.identity[k] for k in ("window_row_id", "window_uuid")},
        )
        assert result["ready"]
        with pytest.raises(ClientError, match="MUTATION_PENDING_CONFLICT"):
            fresh.call("set_contact_policy", {"policy": "auto"})
    assert fresh.outputs["mutation"].read_bytes() == before


def test_daemon_create_reconnect_preserves_program_model(server, monkeypatch):
    monkeypatch.setenv("AGENTSTACK_CONTACT_POLICY", "skip")
    client = context(server, name="daemon", identity=False)
    created = client.create("FixtureDaemon", "daemon", "deterministic")
    client = RuntimeClient(client.path)
    client.reconnect()
    with s2.connection(Path(server["config"]["database"])) as db:
        row = db.execute(
            "SELECT program,model FROM agents WHERE id=?", (created["agent_id"],)
        ).fetchone()
        assert tuple(row) == ("daemon", "deterministic")


@pytest.mark.parametrize("role", ["mutation", "mutex"])
def test_foreign_new_role_refuses_before_network(server, monkeypatch, role):
    client = context(server)
    path = client.outputs[role]
    path.write_text("a note, not a client role")
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ClientError, match="OUTPUT_SCHEMA_INVALID"):
        RuntimeClient(client.path)
    assert path.read_bytes() == before


def test_explicit_pending_resolution_checks_digest_and_never_resends(
    server, monkeypatch
):
    import hashlib

    old = context(server, "old-client")
    original = old.rpc

    def lose(method, params):
        result = original(method, params)
        if method == "tools/call" and params["name"] == "set_contact_policy":
            raise ClientError("TRANSPORT_FAILED")
        return result

    monkeypatch.setattr(old, "rpc", lose)
    with pytest.raises(ClientError, match="TRANSPORT_FAILED"):
        old.call("set_contact_policy", {"policy": "auto"})
    pending_path = old.outputs["mutation"]
    saved = pending_path.read_bytes()
    pending = json.loads(saved)
    current = context(server, "current-client")
    proof = {
        "expected_old": {
            key: pending[key]
            for key in ("server_instance_id", "candidate_generation", "authority_epoch")
        },
        "expected_new": {
            "server_instance_id": current.binding["expected_server_instance_id"],
            **{
                key: current.binding[key]
                for key in ("candidate_generation", "authority_epoch")
            },
        },
        "old_agent_id": 2,
        "new_agent_id": 2,
        "confirm": True,
    }
    monkeypatch.setattr(
        old,
        "rpc",
        lambda *_: pytest.fail("resolution must never use the old transport"),
    )
    with pytest.raises(ClientError, match="MUTATION_PENDING_CONFLICT"):
        old.resolve_mutation(current, "0" * 64, **proof)
    assert pending_path.read_bytes() == saved
    result = old.resolve_mutation(current, hashlib.sha256(saved).hexdigest(), **proof)
    assert result["old_outcome"] == "unknown-no-resend"
    assert not pending_path.exists()
    with s2.connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 1
        )


def test_mismatched_mutation_result_preserves_pending_for_valid_replay(
    server, monkeypatch
):
    client = context(server)
    original = client.rpc

    def wrong_owner(method, params):
        reply = original(method, params)
        if method == "tools/call" and params["name"] == "set_contact_policy":
            result = api["result_value"](reply)
            result["agent_id"] = 999
            return {
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]}
            }
        return reply

    monkeypatch.setattr(client, "rpc", wrong_owner)
    with pytest.raises(ClientError, match="IDENTITY_BINDING_MISMATCH"):
        client.call("set_contact_policy", {"policy": "auto"})
    pending = api["read_json"](client.outputs["mutation"])
    assert pending["phase"] == "planned"
    replay = RuntimeClient(client.path).call("set_contact_policy", {"policy": "auto"})
    assert replay["request_id"] == pending["request_id"]
    assert not client.outputs["mutation"].exists()
