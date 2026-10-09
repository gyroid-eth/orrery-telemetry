"""S1 acceptance over actual HTTP, Unix control socket and migrated SQLite."""

import asyncio
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from fastmcp import Client
import pytest

from agentstack_mail.global_server import GlobalError, GlobalRuntime, TOOLS
from agentstack_mail.namespace_migration import NamespaceMigration
from agentstack_mail.namespace_plan import FenceEvidence
from agentstack_mail.namespace_state_io import connection
from agentstack_mail.namespace_store import generation

spec = importlib.util.spec_from_file_location(
    "s1_fixture", Path(__file__).parent / "fixtures/namespace_state.py"
)
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


async def invoke(url, operation, arguments=None):
    async with Client(url) as client:
        if operation == "tools":
            return await client.list_tools()
        if operation == "resources":
            return await client.list_resources()
        result = await client.call_tool(operation, arguments or {})
        # Assert the actual JSON wire, independent of client-generated Root
        # classes for the open dictionary output schema.
        return json.loads(
            next(part.text for part in result.content if hasattr(part, "text"))
        )


def call(server, operation, **arguments):
    return asyncio.run(invoke(server["url"], operation, arguments))


def control(server, **values):
    request = {"version": 1, **server["binding"], **values}
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(5)
        stream.connect(str(server["config"]["management_socket"]))
        stream.sendall(json.dumps(request).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            data += stream.recv(16384)
        return json.loads(data)


@pytest.fixture
def server(monkeypatch):
    # Keep Unix socket paths short on macOS, without requiring that directory
    # on Linux/WSL. This fixture always creates its own private directory.
    temporary_root = (
        "/private/tmp" if Path("/private/tmp").is_dir() else tempfile.gettempdir()
    )
    root = Path(tempfile.mkdtemp(prefix="rhs1-", dir=temporary_root))
    root.chmod(0o700)
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source, choices = legacy.build(root / "source")
    migration = NamespaceMigration(source, root / "migration", choices)
    migration.resume(
        fence=lambda gen, roster: FenceEvidence(
            gen, roster, {name: "stopped" for name in roster}, True, True, True, 0
        )
    )
    database = migration.workspace / "candidate/mail.sqlite3"
    with connection(database) as db:
        candidate = generation(db)
        instance = db.execute("SELECT instance_id FROM mail_instances").fetchone()[0]
    epoch = str(uuid.uuid4())
    authority = {
        "kind": "orrery-global-authority-v1",
        "phase": "active",
        "root_status": "active",
        "runtime_root": str(database.parent),
        "mail_instance_id": instance,
        "candidate_generation": candidate,
        "authority_epoch": epoch,
    }
    write(root / "authority.json", authority)
    (root / "authority.lock").touch(mode=0o600)
    config = {
        "kind": "orrery-global-server-s1",
        "activation_enabled": False,
        "isolation_root": str(root),
        "runtime_root": str(database.parent),
        "database": str(database),
        "mail_instance_id": instance,
        "candidate_generation": candidate,
        "authority_epoch": epoch,
        "authority": str(root / "authority.json"),
        "authority_lock": str(root / "authority.lock"),
        "management_socket": str(root / "control.sock"),
    }
    write(root / "config.json", config)
    endpoint = f"http://127.0.0.1:{port()}/mcp"
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("AGENTSTACK_", "AGS_"))
    }
    env.update(
        HOME=str(home),
        AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE="passthrough",
        AGENTSTACK_MAIL_ENV_FILE=str(root / "missing.env"),
        PYTHONPATH=str(Path(__file__).parents[1] / "src"),
    )
    log = (root / "server.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentstack_mail.cli",
            "--host",
            "127.0.0.1",
            "--port",
            endpoint.split(":")[2].split("/")[0],
            "--global-config",
            str(root / "config.json"),
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=root,
    )
    state = {
        "root": root,
        "config": config,
        "url": endpoint,
        "migration": migration,
        "binding": {
            "expected_server_instance_id": instance,
            "candidate_generation": candidate,
            "authority_epoch": epoch,
        },
    }
    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                pytest.fail("server exited: " + (root / "server.log").read_text())
            try:
                call(state, "health_check")
                break
            except Exception:
                if time.monotonic() > deadline:
                    pytest.fail(
                        "server readiness timeout: " + (root / "server.log").read_text()
                    )
                time.sleep(0.1)
        yield state
    finally:
        process.terminate()
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        assert not Path(
            config["management_socket"]
        ).exists(), "control socket not removed on TERM"
        shutil.rmtree(root)


def owner(server, agent_id=2, token="fixture-owner-two", **extra):
    return {
        **server["binding"],
        "agent_id": agent_id,
        "registration_token": token,
        **extra,
    }


def test_http_catalog_and_three_generations(server):
    listed = asyncio.run(invoke(server["url"], "tools"))
    assert {tool.name for tool in listed} == TOOLS
    contract = json.loads(
        (Path(__file__).parents[1] / "fixtures/global-server-s1.json").read_text()
    )
    assert {tool.name for tool in listed} == set(contract["tools"])
    assert {tool.name: tool.inputSchema for tool in listed} == contract["tool_schemas"]
    for tool in listed:
        if tool.name not in {"health_check", "ensure_project"}:
            assert set(contract["binding_fields"]) <= set(tool.inputSchema["required"])
        if tool.name not in {"health_check", "ensure_project", "register_agent"}:
            assert set(contract["owner_fields"]) <= set(tool.inputSchema["required"])
    assert asyncio.run(invoke(server["url"], "resources")) == []
    health = call(server, "health_check")
    assert (
        health["activation_enabled"] is False and health["resources_supported"] is False
    )
    assert health["authority_epoch"] != health["candidate_generation"]
    assert health["supported_tools"] == sorted(TOOLS)
    for scope in ["/fixture/one", "/unrelated/tmux"]:
        assert call(server, "ensure_project", human_key=scope)["created"] is False
        result = call(
            server, "whois", **owner(server, project_key=scope, agent_name="Alpha")
        )
        assert result["id"] == 2 and result["name"] == "Beta"
    assert "registration_token" not in json.dumps(result)


def test_register_reconnect_stable_token_window_and_revision(server):
    before = call(server, "health_check")
    with connection(Path(server["config"]["database"])) as db:
        window_uuid = db.execute(
            "SELECT window_uuid FROM window_identities WHERE id=22"
        ).fetchone()[0]
    result = call(
        server,
        "register_agent",
        **owner(
            server,
            name="Alpha",
            program="fixture",
            model="fixture",
            window_row_id=22,
            window_uuid=window_uuid,
            project_key="old-env",
        ),
    )
    assert result["id"] == 2 and result["name"] == "Beta"
    assert (
        result["registration_token"] == "fixture-owner-two"
        and result["credential_generation"] == 9
    )
    assert result["window_uuid"] == window_uuid
    after = call(server, "health_check")
    assert after["mutation_revision"] == before["mutation_revision"] + 1
    assert after["authority_epoch"] == before["authority_epoch"]
    assert after["candidate_generation"] == before["candidate_generation"]
    assert control(server, action="inspect", agent_id=2)["name"] == "Beta"
    with pytest.raises(Exception, match="WINDOW_OWNER_MISMATCH"):
        call(
            server,
            "register_agent",
            **owner(
                server,
                program="fixture",
                model="fixture",
                window_row_id=11,
                window_uuid=window_uuid,
            ),
        )
    with pytest.raises(Exception, match="OWNER_REQUIRED"):
        call(
            server,
            "register_agent",
            **owner(server, token="wrong", program="fixture", model="fixture"),
        )


def test_registration_creates_global_identity_and_conflict_is_closed(server):
    values = {
        **server["binding"],
        "name": "NewIdentity",
        "program": "fixture",
        "model": "fixture",
    }
    result = call(server, "register_agent", **values)
    assert result["id"] > 4 and result["credential_generation"] == 1
    with connection(Path(server["config"]["database"])) as db:
        row = db.execute("SELECT * FROM agents WHERE id=?", (result["id"],)).fetchone()
        assert row["legacy_project_id"] is None
    with pytest.raises(Exception, match="NAME_CONFLICT"):
        call(server, "register_agent", **values)


@pytest.mark.parametrize("route", ["register", "recover", "migrated"])
def test_accepted_unicode_credential_authenticates_owner(server, route):
    token = "界" * 24
    agent_id = 2
    if route == "register":
        result = call(
            server,
            "register_agent",
            **server["binding"],
            name="UnicodeOwner",
            program="fixture",
            model="fixture",
            registration_token=token,
        )
        agent_id = result["id"]
        assert result["registration_token"] == token
    elif route == "recover":
        receipt = control(
            server,
            action="recover",
            agent_id=agent_id,
            request_id="unicode-recovery",
            expected_generation=9,
            new_credential=token,
        )
        assert receipt["ok"] and receipt["new_generation"] == 10
        assert token not in json.dumps(receipt, ensure_ascii=False)
    else:
        # PR3 preserves credentials verbatim; S1 must authenticate them too.
        with connection(Path(server["config"]["database"]), write=True) as db:
            db.execute(
                "UPDATE agents SET registration_token=? WHERE id=?", (token, agent_id)
            )
    assert (
        call(server, "whois", **owner(server, agent_id=agent_id, token=token))["id"]
        == agent_id
    )
    with pytest.raises(Exception, match="OWNER_REQUIRED"):
        call(server, "whois", **owner(server, agent_id=agent_id, token="外" * 24))


def test_inbox_privacy_and_owner_auth(server):
    inbox = call(server, "fetch_inbox", **owner(server))
    assert any(message["id"] == 101 for message in inbox)
    assert all(message["kind"] == "to" for message in inbox)
    assert "fixture-owner" not in json.dumps(inbox)
    hidden = call(server, "fetch_inbox", **owner(server, include_bodies=False, limit=1))
    assert len(hidden) == 1 and "body_md" not in hidden[0]
    with pytest.raises(Exception, match="OWNER_REQUIRED"):
        call(server, "fetch_inbox", **owner(server, token="wrong"))
    with pytest.raises(Exception):
        call(server, "whois", **owner(server, unknown_scope="bad"))
    with pytest.raises(Exception):
        call(server, "send_message", **owner(server))


def test_management_recovery_cas_replay_and_secret_free_receipt(server):
    token = "replacement-owner-token-00001"
    before = control(server, action="inspect", agent_id=2)
    assert before["credential_generation"] == 9
    values = dict(
        action="recover",
        request_id="recover-1",
        agent_id=2,
        expected_generation=9,
        new_credential=token,
    )
    receipt = control(server, **values)
    assert receipt["ok"] and receipt["new_generation"] == 10
    assert control(server, **values) == receipt
    assert token not in json.dumps(receipt)
    assert control(server, action="request_status", request_id="recover-1") == receipt
    assert (
        control(server, **{**values, "new_credential": "different-owner-token-00002"})[
            "reason"
        ]
        == "REQUEST_CONFLICT"
    )
    assert (
        control(server, **{**values, "request_id": "stale-2"})["reason"]
        == "CREDENTIAL_GENERATION_CONFLICT"
    )
    with pytest.raises(Exception, match="OWNER_REQUIRED"):
        call(server, "whois", **owner(server))
    assert (
        call(server, "whois", **owner(server, token=token))["credential_generation"]
        == 10
    )
    with connection(Path(server["config"]["database"])) as db:
        assert db.execute("SELECT COUNT(*) FROM enrollment_requests").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM enrollment_audits").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM global_enrollment_audit").fetchone()[0]
            == 3
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority_epoch", "stale"),
        ("candidate_generation", "stale"),
        ("expected_server_instance_id", "other"),
    ],
)
def test_stale_wire_binding_rejects_http_and_management(server, field, value):
    with pytest.raises(Exception, match="STALE_RUNTIME_BINDING"):
        call(
            server,
            "register_agent",
            **{**owner(server), field: value, "program": "fixture", "model": "fixture"},
        )
    assert (
        control(server, action="inspect", agent_id=2, **{field: value})["reason"]
        == "STALE_RUNTIME_BINDING"
    )


@pytest.mark.parametrize(
    "field,value",
    [("phase", "quiescing"), ("root_status", "retired"), ("authority_epoch", "next")],
)
def test_common_authority_fences_current_http_management_and_restarted_wrapper(
    server, field, value
):
    path = Path(server["config"]["authority"])
    original = json.loads(path.read_text())
    write(path, {**original, field: value})
    with pytest.raises(Exception, match="WRITER_FENCED"):
        call(
            server,
            "register_agent",
            **owner(server, program="fixture", model="fixture"),
        )
    assert control(server, action="inspect", agent_id=2)["reason"] == "WRITER_FENCED"
    with pytest.raises(GlobalError, match="WRITER_FENCED"):
        GlobalRuntime(server["root"] / "config.json")
    write(path, original)


def test_updater_exclusive_gate_prevents_new_request(server):
    fd = os.open(server["config"]["authority_lock"], os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Exception, match="WRITER_FENCED"):
            call(
                server,
                "register_agent",
                **owner(server, program="fixture", model="fixture"),
            )
    finally:
        os.close(fd)


@pytest.mark.parametrize("exclusive", [False, True])
def test_replaced_startup_lock_fences_http_and_management(server, exclusive):
    path = Path(server["config"]["authority_lock"])
    fd = os.open(path, os.O_RDONLY)
    try:
        if exclusive:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        replacement = path.with_name("replacement.lock")
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        replacement.replace(path)
        with connection(Path(server["config"]["database"])) as db:
            revision = db.execute(
                "SELECT value FROM namespace_metadata WHERE key='write_generation'"
            ).fetchone()[0]
        with pytest.raises(Exception, match="AUTHORITY_LOCK_REPLACED"):
            call(
                server,
                "register_agent",
                **owner(server, program="fixture", model="fixture"),
            )
        assert (
            control(server, action="inspect", agent_id=2)["reason"]
            == "AUTHORITY_LOCK_REPLACED"
        )
        # Releasing the updater's old lock must not restore this old runtime.
        fcntl.flock(fd, fcntl.LOCK_UN)
        with pytest.raises(Exception, match="AUTHORITY_LOCK_REPLACED"):
            call(server, "whois", **owner(server))
        with connection(Path(server["config"]["database"])) as db:
            assert db.execute(
                "SELECT value FROM namespace_metadata WHERE key='write_generation'"
            ).fetchone()[0] == revision
    finally:
        os.close(fd)


def test_retirement_requires_owner_and_unretire_preserves_token(server):
    assert call(server, "retire_agent", **owner(server))["status"] == "retired"
    with pytest.raises(Exception, match="OWNER_REQUIRED"):
        call(server, "fetch_inbox", **owner(server))
    assert call(server, "unretire_agent", **owner(server))["status"] == "active"
    assert call(server, "whois", **owner(server))["id"] == 2


def test_null_token_claim_is_operator_only_and_audited(server):
    with connection(Path(server["config"]["database"]), write=True) as db:
        db.execute(
            "UPDATE agents SET registration_token=NULL,credential_generation=0 WHERE id=3"
        )
    with pytest.raises(Exception, match="OWNER_REQUIRED"):
        call(
            server,
            "register_agent",
            **owner(
                server,
                agent_id=3,
                token="new-owner-token-0000001",
                program="codex",
                model="fixture",
            ),
        )
    result = control(
        server,
        action="claim",
        agent_id=3,
        request_id="claim-3",
        expected_generation=0,
        new_credential="new-owner-token-0000001",
    )
    assert result["ok"] and result["new_generation"] == 1
    assert (
        control(server, action="inspect", agent_id=3)["credential_state"]
        == "server-token"
    )


def test_inbox_since_ts_compares_instants_with_offsets(server):
    with connection(Path(server["config"]["database"]), write=True) as db:
        db.execute(
            "UPDATE messages SET created_ts='2026-10-08T23:00:00+14:00' WHERE id=101"
        )
    inbox = call(
        server, "fetch_inbox", **owner(server, since_ts="2026-10-08T09:01:00Z")
    )
    assert 101 not in [row["id"] for row in inbox]


def test_authority_changed_inside_transaction_rolls_back(server):
    runtime = GlobalRuntime(server["root"] / "config.json")
    path = Path(server["config"]["authority"])
    old = json.loads(path.read_text())
    with pytest.raises(GlobalError, match="WRITER_FENCED"):
        with runtime.transaction(write=True) as db:
            db.execute("UPDATE agents SET model='must-not-commit' WHERE id=2")
            write(path, {**old, "phase": "quiescing"})
    write(path, old)
    assert call(server, "whois", **owner(server))["id"] == 2
    with connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute("SELECT model FROM agents WHERE id=2").fetchone()[0]
            != "must-not-commit"
        )


def test_new_runtime_root_fences_old_process_with_same_candidate_and_epoch(server):
    path = Path(server["config"]["authority"])
    original = json.loads(path.read_text())
    write(path, {**original, "runtime_root": str(server["root"] / "another-root")})
    with pytest.raises(Exception, match="WRITER_FENCED"):
        call(server, "whois", **owner(server))
    write(path, original)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("layout", ["hardlink", "symlink", "public-mode"])
@pytest.mark.parametrize("entry", ["http", "startup"])
def test_sqlite_sidecars_cannot_write_outside_isolation(
    server, tmp_path, suffix, layout, entry
):
    database = Path(server["config"]["database"])
    with connection(database, write=True) as db:
        db.execute("PRAGMA journal_mode=WAL")
    outside = tmp_path / "outside-sentinel"
    assert server["root"] not in outside.resolve().parents
    before = b"UNCHANGED" + b"X" * 8192
    outside.write_bytes(before)
    outside.chmod(0o600)
    sidecar = Path(str(database) + suffix)
    assert not sidecar.exists()
    if layout == "hardlink":
        os.link(outside, sidecar)
    elif layout == "symlink":
        sidecar.symlink_to(outside)
    else:
        sidecar.write_bytes(before)
        sidecar.chmod(0o644)
    try:
        with pytest.raises(Exception, match="SQLITE_SIDECAR_UNSAFE"):
            if entry == "startup":
                GlobalRuntime(server["root"] / "config.json")
            else:
                call(
                    server,
                    "register_agent",
                    **server["binding"],
                    name="SidecarOwner",
                    program="fixture",
                    model="fixture",
                )
        assert outside.read_bytes() == before
        assert outside.stat().st_size == len(before)
    finally:
        sidecar.unlink(missing_ok=True)


def test_private_wal_sidecars_allow_normal_registration(server):
    database = Path(server["config"]["database"])
    with connection(database, write=True) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        # Keep a cooperating connection open so SQLite's regular sidecars exist.
        db.execute("SELECT id FROM agents").fetchall()
        for suffix in ("-wal", "-shm"):
            path = Path(str(database) + suffix)
            assert path.is_file() and path.stat().st_nlink == 1
            assert path.stat().st_mode & 0o077 == 0
        result = call(
            server,
            "register_agent",
            **server["binding"],
            name="PrivateWalOwner",
            program="fixture",
            model="fixture",
        )
        assert result["id"] > 4


def test_unmigrated_database_rejected_without_schema_changes(server):
    import sqlite3

    database = server["root"] / "migration" / "not-migrated.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
    database.chmod(0o600)
    before = database.read_bytes()
    config = server["root"] / "unmigrated.json"
    write(
        config,
        {
            **server["config"],
            "runtime_root": str(database.parent),
            "database": str(database),
        },
    )
    with pytest.raises(GlobalError, match="CANDIDATE_SCHEMA_REQUIRED"):
        GlobalRuntime(config)
    assert database.read_bytes() == before


def test_real_home_boundary_is_not_selected_by_tmpdir(server, monkeypatch):
    from types import SimpleNamespace
    import agentstack_mail.global_server as module

    # Simulate an operator HOME at the fixture root; do not read/create any
    # actual HOME file to test this boundary.
    monkeypatch.setattr(
        module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(server["root"]))
    )
    with pytest.raises(GlobalError, match="REAL_HOME_REFUSED"):
        GlobalRuntime(server["root"] / "config.json")


def test_hardlinked_database_cannot_bypass_isolation(server):
    duplicate = server["root"] / "another.sqlite3"
    os.link(server["config"]["database"], duplicate)
    try:
        with pytest.raises(GlobalError, match="DATABASE_UNSAFE"):
            GlobalRuntime(server["root"] / "config.json")
    finally:
        duplicate.unlink()


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("activation_enabled", True, "ISOLATED_CONFIG_REQUIRED"),
        ("candidate_generation", "wrong", "CANDIDATE_BINDING_MISMATCH"),
        ("database", "/not-an-isolated-database", "CONFIG_PATH_OUTSIDE_ISOLATION"),
    ],
)
def test_invalid_explicit_config_is_rejected_without_fallback(
    server, key, value, reason
):
    target = server["root"] / "rejected-config.json"
    write(target, {**server["config"], key: value})
    with pytest.raises(GlobalError, match=reason):
        GlobalRuntime(target)


def test_default_cli_keeps_real_legacy_tools_database_and_management(server):
    from agentstack_mail.contract import COMPATIBILITY_TOOLS

    root = server["root"] / "legacy"
    root.mkdir(mode=0o700)
    endpoint = f"http://127.0.0.1:{port()}/mcp"
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("AGENTSTACK_", "AGS_"))
    }
    env.update(
        HOME=str(root),
        PYTHONPATH=str(Path(__file__).parents[1] / "src"),
        AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE="passthrough",
        AGENTSTACK_MAIL_ENV_FILE=str(root / "absent.env"),
        AGENTSTACK_MAIL_DATABASE_URL="sqlite+aiosqlite:///"
        + str(root / "legacy.sqlite3"),
        AGENTSTACK_MAIL_STORAGE_ROOT=str(root / "archive"),
        AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=str(root / "signals"),
        AGENTSTACK_MAIL_MANAGEMENT_SOCKET=str(root / "legacy.sock"),
    )
    log = (root / "server.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentstack_mail.cli",
            "--host",
            "127.0.0.1",
            "--port",
            endpoint.split(":")[2].split("/")[0],
        ],
        env=env,
        cwd=root,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    old = {"url": endpoint}
    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                pytest.fail((root / "server.log").read_text())
            try:
                call(old, "health_check")
                break
            except Exception:
                if time.monotonic() > deadline:
                    pytest.fail((root / "server.log").read_text())
                time.sleep(0.1)
        assert {
            tool.name for tool in asyncio.run(invoke(endpoint, "tools"))
        } == COMPATIBILITY_TOOLS
        call(old, "ensure_project", human_key="/fixture/legacy")
        token = "legacy-owner-token-00001"
        registered = call(
            old,
            "register_agent",
            project_key="/fixture/legacy",
            name="LegacyIdentity",
            program="codex",
            model="fixture",
            registration_token=token,
        )
        assert registered["name"] == "LegacyIdentity"
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(5)
            stream.connect(str(root / "legacy.sock"))
            stream.sendall(
                json.dumps(
                    {
                        "version": 1,
                        "action": "inspect",
                        "project_key": "/fixture/legacy",
                        "agent_id": registered["id"],
                    }
                ).encode()
                + b"\n"
            )
            reply = json.loads(stream.recv(16384))
            assert reply["ok"] and reply["project_key"] == "/fixture/legacy"
        with connection(root / "legacy.sqlite3") as db:
            assert "project_id" in {
                row[1] for row in db.execute("PRAGMA table_info(agents)")
            }
            assert "namespace_metadata" not in {
                row[0] for row in db.execute("SELECT name FROM sqlite_master")
            }
    finally:
        process.terminate()
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        assert not (root / "legacy.sock").exists()
