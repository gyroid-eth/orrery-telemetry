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
def server(monkeypatch, request):
    prep = s2.prepared.__wrapped__(monkeypatch)
    state = next(prep)
    if getattr(request, "param", None) is not None:
        state["config"]["operation_receipt_limit"] = request.param
        state["config_path"].write_text(json.dumps(state["config"]))
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


@pytest.mark.parametrize("server", [1], indirect=True)
@pytest.mark.parametrize("condition", ["quota-used", "lost-response-pending"])
def test_default_mapped_reconnect_never_writes_policy_or_changes_pending(
    server, monkeypatch, condition
):
    monkeypatch.delenv("AGENTSTACK_CONTACT_POLICY", raising=False)
    client = context(server)
    original = client.rpc

    def lose(method, params):
        reply = original(method, params)
        if method == "tools/call" and params["name"] == "set_contact_policy":
            raise ClientError("TRANSPORT_FAILED")
        return reply

    if condition == "lost-response-pending":
        monkeypatch.setattr(client, "rpc", lose)
        with pytest.raises(ClientError, match="TRANSPORT_FAILED"):
            client.call("set_contact_policy", {"policy": "auto"})
    else:
        client.call("set_contact_policy", {"policy": "auto"})
    pending_path = client.outputs["mutation"]
    before = pending_path.read_bytes() if pending_path.exists() else None
    observed = RuntimeClient(client.path).reconnect()
    assert observed["window_ready"]
    assert (pending_path.read_bytes() if pending_path.exists() else None) == before
    with s2.connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT contact_policy FROM agents WHERE id=2").fetchone()[0]
            == "auto"
        )


@pytest.mark.parametrize(
    "field,bad", [("policy", "not-a-policy"), ("committed_at", "not-a-date")]
)
def test_output_schema_mismatch_keeps_planned_and_replays_same_uuid(
    server, monkeypatch, field, bad
):
    client = context(server)
    original = client.rpc

    def corrupt(method, params):
        reply = original(method, params)
        if method == "tools/call" and params["name"] == "set_contact_policy":
            result = api["result_value"](reply)
            result[field] = bad
            return {
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]}
            }
        return reply

    monkeypatch.setattr(client, "rpc", corrupt)
    with pytest.raises(ClientError, match="RESPONSE_INVALID"):
        client.call("set_contact_policy", {"policy": "auto"})
    pending = api["read_json"](client.outputs["mutation"])
    assert pending["phase"] == "planned" and pending["receipt"] is None
    replay = RuntimeClient(client.path).call("set_contact_policy", {"policy": "auto"})
    assert replay["request_id"] == pending["request_id"]
    assert not client.outputs["mutation"].exists()
    with s2.connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 1
        )


def test_installed_stdlib_wrapper_uses_canonical_output_validator(server):
    import shutil
    import subprocess
    import sys

    client = context(server)
    installed_root = server["root"] / "installed-wrapper"
    library = installed_root / "bin/lib"
    library.mkdir(parents=True)
    canonical = ROOT / "packages/agentstack_mail/src/agentstack_mail/schema_contract.py"
    shutil.copy2(ROOT / "bin/lib/runtime_client.py", library / "runtime_client.py")
    shutil.copy2(canonical, library / "schema_contract.py")
    shutil.copy2(
        ROOT / "packages/agentstack_mail/fixtures/global-server-s2a.json",
        library / "global-server-s2a.json",
    )
    assert (library / "schema_contract.py").read_bytes() == canonical.read_bytes()
    config = api["read_json"](client.path)
    config["wrapper_root"] = str(installed_root)
    api["atomic_json"](client.path, config)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(library / "runtime_client.py"),
            "--context",
            str(client.path),
            "call",
            "set_contact_policy",
            "policy=auto",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["policy"] == "auto"


def test_definite_rejection_clears_pending_and_allows_next_intent(server):
    client = context(server, window=False)
    with pytest.raises(ClientError, match="TARGET_UNAVAILABLE"):
        client.call("request_contact", {"to_agent_id": 999999})
    assert not client.outputs["mutation"].exists()
    assert client.call("set_contact_policy", {"policy": "auto"})["policy"] == "auto"
    assert client.reconnect()["agent_id"] == 2


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("refresh_registration", {"task_description": None}),
        ("refresh_registration", {"task_description": ""}),
        ("set_contact_policy", {"policy": "auto"}),
        (
            "request_contact",
            {"to_agent_id": 3, "ttl_seconds": 1, "to_project": "ignored"},
        ),
        (
            "respond_contact",
            {"from_agent_id": 3, "accept": False, "from_project": "ignored"},
        ),
        ("macro_contact_handshake", {"to_agent_id": 3, "auto_accept": False}),
    ],
)
def test_all_receipt_tools_share_exact_preimage_bytes(
    server, monkeypatch, tool, arguments
):
    client = context(server)
    original = client.rpc

    def lose(method, params):
        value = original(method, params)
        if method == "tools/call" and params.get("name") == tool:
            raise ClientError("TRANSPORT_FAILED")
        return value

    monkeypatch.setattr(client, "rpc", lose)
    with pytest.raises(ClientError, match="TRANSPORT_FAILED"):
        client.call(tool, arguments)
    pending = api["read_json"](client.outputs["mutation"])
    canonical = api["wire_contract"]()["canonical"](pending["canonical_payload"])
    with s2.connection(Path(server["config"]["database"])) as db:
        row = db.execute(
            "SELECT * FROM global_operation_receipts WHERE request_id=?",
            (pending["request_id"],),
        ).fetchone()
    assert row["canonical_request_json"] == canonical
    assert row["request_hash"] == pending["request_hash"]
    fresh = RuntimeClient(client.path)
    assert fresh.call(tool, arguments)["request_id"] == pending["request_id"]
    assert not fresh.outputs["mutation"].exists()


@pytest.mark.parametrize(
    "refusal", ["UNKNOWN_SERVER_REASON", "CREDENTIAL_GENERATION_MISMATCH"]
)
def test_uncertain_existing_request_preserves_exact_pending(
    server, monkeypatch, refusal
):
    client = context(server)
    original = client.rpc

    def lose(method, params):
        response = original(method, params)
        if method == "tools/call" and params.get("name") == "set_contact_policy":
            raise ClientError("TRANSPORT_FAILED")
        return response

    monkeypatch.setattr(client, "rpc", lose)
    with pytest.raises(ClientError, match="TRANSPORT_FAILED"):
        client.call("set_contact_policy", {"policy": "auto"})
    saved = client.outputs["mutation"].read_bytes()

    def reject(method, params):
        if method == "tools/call" and params.get("name") == "set_contact_policy":
            return {
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": refusal}],
                }
            }
        return original(method, params)

    monkeypatch.setattr(client, "rpc", reject)
    with pytest.raises(
        ClientError, match="TOOL_REJECTED|CREDENTIAL_GENERATION_MISMATCH"
    ):
        client.call("set_contact_policy", {"policy": "auto"})
    assert client.outputs["mutation"].read_bytes() == saved
    monkeypatch.setattr(client, "rpc", original)
    client.call("set_contact_policy", {"policy": "auto"})
    assert not client.outputs["mutation"].exists()


@pytest.mark.parametrize(
    "refusal",
    [
        "TARGET_UNAVAILABLE",
        "SELF_CONTACT_NOT_SUPPORTED",
        "CONTACT_TARGET_CONSENT_REQUIRED",
    ],
)
def test_definite_tool_refusals_do_not_poison_next_mutation(server, refusal):
    tool, arguments = {
        "TARGET_UNAVAILABLE": ("request_contact", {"to_agent_id": 999999}),
        "SELF_CONTACT_NOT_SUPPORTED": ("request_contact", {"to_agent_id": 2}),
        "CONTACT_TARGET_CONSENT_REQUIRED": (
            "macro_contact_handshake",
            {"to_agent_id": 3, "auto_accept": True},
        ),
    }[refusal]
    client = context(server)
    with pytest.raises(ClientError, match=refusal):
        client.call(tool, arguments)
    assert not client.outputs["mutation"].exists()
    assert client.call("set_contact_policy", {"policy": "auto"})["policy"] == "auto"
    assert client.reconnect()["window_ready"]


@pytest.mark.parametrize("server", [1], indirect=True)
def test_quota_rejection_has_no_pending_and_preserves_mapped_reconnect(server):
    client = context(server)
    client.call("set_contact_policy", {"policy": "auto"})
    with pytest.raises(ClientError, match="REQUEST_RECEIPT_CAPACITY_REACHED"):
        client.call("set_contact_policy", {"policy": "open"})
    assert not client.outputs["mutation"].exists()
    assert client.reconnect()["window_ready"]


@pytest.mark.parametrize("prior_unknown", [False, True])
def test_credential_rotation_refusal_distinguishes_old_unknown_commit(
    server, monkeypatch, prior_unknown
):
    client = context(server)
    original = client.rpc
    saved = None
    if prior_unknown:

        def lose(method, params):
            value = original(method, params)
            if method == "tools/call" and params.get("name") == "set_contact_policy":
                raise ClientError("TRANSPORT_FAILED")
            return value

        monkeypatch.setattr(client, "rpc", lose)
        with pytest.raises(ClientError, match="TRANSPORT_FAILED"):
            client.call("set_contact_policy", {"policy": "auto"})
        saved = client.outputs["mutation"].read_bytes()
        monkeypatch.setattr(client, "rpc", original)
    result = client.management(
        "recover",
        agent_id=2,
        request_id="rotate-for-refusal",
        expected_generation=9,
        new_credential="fixture-new-owner-token-0000002",
    )
    assert result["new_generation"] == 10
    with pytest.raises(ClientError, match="OWNER_REQUIRED"):
        client.call("set_contact_policy", {"policy": "auto"})
    if prior_unknown:
        assert client.outputs["mutation"].read_bytes() == saved
    else:
        assert not client.outputs["mutation"].exists()


@pytest.mark.parametrize("lost_conflict_response", [False, True])
def test_cross_root_uuid_conflict_preserves_pending_until_operator_resolution(
    server, monkeypatch, lost_conflict_response
):
    import hashlib
    import subprocess
    import sys
    import uuid

    first = context(server, "committed-root")
    second = context(server, "conflicting-root")
    request_id = str(uuid.uuid4())
    first.call("set_contact_policy", {"policy": "open", "request_id": request_id})
    dbpath = Path(server["config"]["database"])

    def receipt_state():
        with s2.connection(dbpath) as db:
            return (
                [
                    dict(row)
                    for row in db.execute("SELECT * FROM global_operation_receipts")
                ],
                s2.revision(db),
            )

    before = receipt_state()
    saved = []
    save = second.save_mutation

    def record(pending):
        save(pending)
        saved.append(second.outputs["mutation"].read_bytes())

    monkeypatch.setattr(second, "save_mutation", record)
    original = second.rpc
    arguments = {"policy": "auto", "request_id": request_id}
    if lost_conflict_response:

        def lose(method, params):
            value = original(method, params)
            if method == "tools/call" and params.get("name") == "set_contact_policy":
                raise ClientError("TRANSPORT_FAILED")
            return value

        monkeypatch.setattr(second, "rpc", lose)
        with pytest.raises(ClientError, match="TRANSPORT_FAILED"):
            second.call("set_contact_policy", arguments)
        monkeypatch.setattr(second, "rpc", original)
    with pytest.raises(ClientError, match="REQUEST_ID_CONFLICT"):
        second.call("set_contact_policy", arguments)
    assert second.outputs["mutation"].read_bytes() == saved[0]
    assert receipt_state() == before
    with pytest.raises(ClientError, match="MUTATION_PENDING_CONFLICT"):
        second.call("set_contact_policy", {"policy": "contacts_only"})
    assert second.outputs["mutation"].read_bytes() == saved[0]
    binding = {
        "server_instance_id": first.binding["expected_server_instance_id"],
        **{k: first.binding[k] for k in ("candidate_generation", "authority_epoch")},
    }
    proof = {
        "expected_old": binding,
        "expected_new": binding,
        "old_agent_id": 2,
        "new_agent_id": 2,
        "confirm": True,
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin/lib/runtime_client.py"),
            "--context",
            str(second.path),
            "resolve-mutation",
            hashlib.sha256(saved[0]).hexdigest(),
            str(first.path),
            json.dumps(proof),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["old_outcome"] == "unknown-no-resend"
    assert not second.outputs["mutation"].exists()
    assert receipt_state() == before
    outcome = second.call("set_contact_policy", {"policy": "auto"})
    assert outcome["request_id"] != request_id
    assert receipt_state()[0][0] == before[0][0]
