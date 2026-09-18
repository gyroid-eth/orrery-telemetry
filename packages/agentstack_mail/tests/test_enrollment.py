from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, ParamSpec

import pytest
from fastmcp import Client
from sqlalchemy import select

from agentstack_mail import db
from agentstack_mail.app import build_mcp_server
from agentstack_mail.config import get_settings
from agentstack_mail.enrollment import EnrollmentControlServer
from agentstack_mail import enrollment_cli
from agentstack_mail.models import Agent, EnrollmentAudit, EnrollmentRequest, Project


CANARY = "enrollment-secret-canary-000000000000"
P = ParamSpec("P")


def _sync_async_test(
    test: Callable[P, Awaitable[None]],
) -> Callable[P, None]:
    """Run one async fixture without depending on a pytest async plugin."""

    @functools.wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


async def _setup_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None = None,
) -> tuple[Path, int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_root = tmp_path / "mail-state"
    state_root.mkdir()
    database = state_root / "storage.sqlite3"
    archive = state_root / "archive"
    signals = state_root / "signals"
    management_socket = state_root / "management.sock"
    database_url = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(state_root / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_DATABASE_URL", database_url)
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(archive))
    monkeypatch.setenv("AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR", str(signals))
    monkeypatch.setenv("AGENTSTACK_MAIL_MANAGEMENT_SOCKET", str(management_socket))
    db.reset_database_state()
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.database.url == database_url
    assert Path(settings.storage.root) == archive
    assert Path(settings.notifications.signals_dir) == signals
    assert settings.management_socket_path == str(management_socket)
    await db.ensure_schema(settings)
    async with db.get_session() as session:
        async with session.begin():
            project = Project(slug=f"fixture-{uuid.uuid4().hex}", human_key=str(tmp_path / "project"))
            session.add(project)
            await session.flush()
            agent = Agent(
                project_id=project.id,
                name="PersistentBot",
                program="claude-code",
                model="test",
                registration_token=token,
            )
            session.add(agent)
            await session.flush()
            agent_id = int(agent.id)
    return database, agent_id


async def _start_server(tmp_path: Path) -> EnrollmentControlServer:
    short_root = Path("/private/tmp") / f"orrery-enroll-tests-{os.getuid()}"
    short_root.mkdir(mode=0o700, exist_ok=True)
    short_root.chmod(0o700)
    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    server = EnrollmentControlServer(short_root / f"{suffix}.sock")
    await server.start()
    return server


async def _rpc(socket_path: Path, payload: dict[str, Any], *, read_response: bool = True) -> dict[str, Any] | None:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(json.dumps(payload, sort_keys=True).encode() + b"\n")
    await writer.drain()
    if not read_response:
        writer.close()
        await writer.wait_closed()
        return None
    raw = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(raw)


def _connection_profile(
    tmp_path: Path,
    server: EnrollmentControlServer,
    *,
    name: str = "local-connection",
    runtime_name: str = "runtime",
    pinned: bool = True,
) -> tuple[Path, Path]:
    runtime = tmp_path / runtime_name
    profile = tmp_path / f"{name}.json"
    payload = {
        "kind": "orrery-mail-connection-v1",
        "management_socket": str(server.socket_path),
        "runtime_dir": str(runtime),
    }
    if pinned:
        payload["expected_server_instance_id"] = server.instance_id
    profile.write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )
    profile.chmod(0o600)
    return profile, runtime


async def _run_cli(*arguments: str) -> tuple[int, str, str, list[str]]:
    executable = Path(sys.executable).with_name("agentstack-enroll")
    argv = [str(executable), *arguments]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode(), argv


def _mutation(
    server: EnrollmentControlServer,
    project_key: str,
    agent_id: int,
    *,
    action: str,
    token: str = CANARY,
    generation: int = 0,
    request_id: str | None = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "action": action,
        "request_id": request_id or str(uuid.uuid4()),
        "expected_server_instance_id": instance_id or server.instance_id,
        "project_key": project_key,
        "agent_id": agent_id,
        "expected_name": "PersistentBot",
        "expected_generation": generation,
        "new_credential": token,
    }


@_sync_async_test
async def test_fixture_new_identity_must_use_existing_creation_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    try:
        response = await _rpc(
            server.socket_path,
            _mutation(server, str(tmp_path / "missing-project"), 9999, action="claim"),
        )
        assert response["ok"] is False
        assert response["receipt"]["reason"] == "project-not-found"
        async with db.get_session() as session:
            agents = (await session.execute(select(Agent))).scalars().all()
            assert len(agents) == 1
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_server_null_claim_migrates_and_restart_keeps_id_and_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, agent_id = await _setup_database(tmp_path, monkeypatch)
    project_key = str(tmp_path / "project")
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    try:
        code, stdout, stderr, argv = await _run_cli(
            "claim", "--agent-id", str(agent_id), "--project-key", project_key,
            "--connection", str(profile), "--name", "PersistentBot",
        )
        assert (code, stderr) == (0, "")
        receipt = json.loads(stdout)
        assert receipt["result"] == "accepted"
        assert receipt["new_generation"] == 1
        active = runtime / "agent_token_PersistentBot"
        first_credential = active.read_text(encoding="utf-8").strip()
        assert first_credential and first_credential not in stdout
        assert first_credential not in stderr
        assert first_credential not in "\0".join(argv)
        first_instance = server.instance_id
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()

    monkeypatch.setenv("AGENTSTACK_MAIL_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    await db.ensure_schema(get_settings())
    restarted = await _start_server(tmp_path)
    try:
        assert restarted.instance_id == first_instance
        inspected = await _rpc(
            restarted.socket_path,
            {"version": 1, "action": "inspect", "project_key": project_key, "agent_id": agent_id, "expected_name": "PersistentBot"},
        )
        assert inspected["agent_id"] == agent_id
        assert inspected["credential_generation"] == 1
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token == first_credential
    finally:
        await restarted.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_server_token_local_loss_recover_then_inspect_normal_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch, token="unavailable-old-token-000000000")
    project_key = str(tmp_path / "project")
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    try:
        code, stdout, stderr, _argv = await _run_cli(
            "recover", "--agent-id", str(agent_id), "--project-key", project_key,
            "--connection", str(profile), "--name", "PersistentBot",
        )
        assert (code, stderr) == (0, "")
        assert json.loads(stdout)["reason"] == "operator-recover"
        active = runtime / "agent_token_PersistentBot"
        recovered = active.read_text(encoding="utf-8").strip()
        assert recovered != "unavailable-old-token-000000000"

        code, stdout, stderr, _argv = await _run_cli(
            "inspect", "--agent-id", str(agent_id), "--project-key", project_key,
            "--connection", str(profile), "--name", "PersistentBot",
        )
        assert (code, stderr) == (0, "")
        state = json.loads(stdout)
        assert state["credential_state"] == "server-token"
        assert state["local_credential_state"] == "present"

        code, _stdout, stderr, _argv = await _run_cli(
            "recover", "--agent-id", str(agent_id), "--project-key", project_key,
            "--connection", str(profile), "--name", "PersistentBot",
        )
        assert code == 2
        assert json.loads(stderr)["reason"] == "local-credential-exists"
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_disconnect_retry_uses_same_request_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    request = _mutation(server, str(tmp_path / "project"), agent_id, action="claim")
    try:
        # Close without reading: the server still owns commit/reply ordering.
        await _rpc(server.socket_path, request, read_response=False)
        for _ in range(50):
            status = await _rpc(
                server.socket_path,
                {"version": 1, "action": "request_status", "request_id": request["request_id"]},
            )
            if status["reason"] != "request-not-found":
                break
            await asyncio.sleep(0.01)
        assert status["receipt"]["result"] == "accepted"
        replay = await _rpc(server.socket_path, request)
        assert replay["reason"] == "replayed"
        assert replay["receipt"] == status["receipt"]
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_cas_conflict_wrong_authority_and_wrong_id_leave_credential_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    project_key = str(tmp_path / "project")
    server = await _start_server(tmp_path)
    try:
        first_request = _mutation(server, project_key, agent_id, action="claim")
        second_request = _mutation(
            server, project_key, agent_id, action="claim", token="different-secret-canary-0000000"
        )
        first, second = await asyncio.gather(
            _rpc(server.socket_path, first_request),
            _rpc(server.socket_path, second_request),
        )
        winner, loser = sorted((first, second), key=lambda item: item["ok"], reverse=True)
        assert winner["ok"] is True
        assert loser["receipt"]["reason"] == "generation-conflict"
        winning_token = (
            first_request["new_credential"]
            if winner["receipt"]["request_id"] == first_request["request_id"]
            else second_request["new_credential"]
        )
        wrong_authority_request = _mutation(
            server, project_key, agent_id, action="recover", generation=1,
            token="authority-canary-secret-00000000", instance_id=str(uuid.uuid4()),
        )
        wrong_authority = await _rpc(server.socket_path, wrong_authority_request)
        assert wrong_authority["receipt"]["reason"] == "server-identity-mismatch"
        wrong_authority_replay = await _rpc(server.socket_path, wrong_authority_request)
        assert wrong_authority_replay["reason"] == "replayed"
        assert wrong_authority_replay["receipt"] == wrong_authority["receipt"]
        wrong_id = await _rpc(
            server.socket_path,
            _mutation(server, project_key, agent_id + 9000, action="recover", generation=1),
        )
        assert wrong_id["receipt"]["reason"] == "agent-not-found"
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token == winning_token
            assert agent.credential_generation == 1
            audits = (await session.execute(select(EnrollmentAudit))).scalars().all()
            assert [row.result for row in audits].count("accepted") == 1
            assert [row.result for row in audits].count("rejected") == 3
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_active_save_failure_resumes_from_pending_without_new_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    project_key = str(tmp_path / "project")
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    arguments = [
        "claim", "--agent-id", str(agent_id), "--project-key", project_key,
        "--connection", str(profile), "--name", "PersistentBot",
    ]
    real_write = enrollment_cli._atomic_active_write
    try:
        monkeypatch.setattr(
            enrollment_cli,
            "_atomic_active_write",
            lambda _path, _secret: (_ for _ in ()).throw(OSError("fixture-boundary")),
        )
        with pytest.raises(SystemExit) as failed:
            await asyncio.to_thread(enrollment_cli.main, arguments)
        assert failed.value.code == 2
        pending_path = next((runtime / "enrollment" / "pending").glob("*.json"))
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        first_secret = pending["new_credential"]

        monkeypatch.setattr(enrollment_cli, "_atomic_active_write", real_write)
        await asyncio.to_thread(enrollment_cli.main, arguments)
        assert (runtime / "agent_token_PersistentBot").read_text(encoding="utf-8").strip() == first_secret
        assert not pending_path.exists()
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_committed_request_status_failure_keeps_pending_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    project_key = str(tmp_path / "project")
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    arguments = [
        "claim", "--agent-id", str(agent_id), "--project-key", project_key,
        "--connection", str(profile), "--name", "PersistentBot",
    ]
    real_write = enrollment_cli._atomic_active_write
    real_request = enrollment_cli._request
    try:
        monkeypatch.setattr(
            enrollment_cli,
            "_atomic_active_write",
            lambda _path, _secret: (_ for _ in ()).throw(OSError("fixture-boundary")),
        )
        with pytest.raises(SystemExit) as failed:
            await asyncio.to_thread(enrollment_cli.main, arguments)
        assert failed.value.code == 2
        pending_path = next((runtime / "enrollment" / "pending").glob("*.json"))
        pending = json.loads(pending_path.read_text(encoding="utf-8"))

        monkeypatch.setattr(enrollment_cli, "_atomic_active_write", real_write)

        def fail_status(socket_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
            if payload.get("action") == "request_status":
                return {"version": 1, "ok": False, "reason": "management-operation-failed"}
            return real_request(socket_path, payload)

        monkeypatch.setattr(enrollment_cli, "_request", fail_status)
        with pytest.raises(SystemExit) as unavailable:
            await asyncio.to_thread(enrollment_cli.main, arguments)
        assert unavailable.value.code == 3
        assert pending_path.exists()
        assert not (runtime / "agent_token_PersistentBot").exists()

        monkeypatch.setattr(enrollment_cli, "_request", real_request)
        await asyncio.to_thread(enrollment_cli.main, arguments)
        assert (runtime / "agent_token_PersistentBot").read_text(encoding="utf-8").strip() == pending["new_credential"]
        assert not pending_path.exists()
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_uncommitted_request_status_failure_keeps_pending_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    project_key = str(tmp_path / "project")
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    arguments = [
        "claim", "--agent-id", str(agent_id), "--project-key", project_key,
        "--connection", str(profile), "--name", "PersistentBot",
    ]
    real_request = enrollment_cli._request

    def fail_before_commit(socket_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "claim":
            raise enrollment_cli.EnrollmentCliError("mail-control-unavailable")
        if payload.get("action") == "request_status":
            return {"version": 1, "ok": False, "reason": "management-operation-failed"}
        return real_request(socket_path, payload)

    try:
        monkeypatch.setattr(enrollment_cli, "_request", fail_before_commit)
        with pytest.raises(SystemExit) as unavailable:
            await asyncio.to_thread(enrollment_cli.main, arguments)
        assert unavailable.value.code == 3
        pending_path = next((runtime / "enrollment" / "pending").glob("*.json"))
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        assert pending_path.exists()
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token is None

        monkeypatch.setattr(enrollment_cli, "_request", real_request)
        await asyncio.to_thread(enrollment_cli.main, arguments)
        assert (runtime / "agent_token_PersistentBot").read_text(encoding="utf-8").strip() == pending["new_credential"]
        assert not pending_path.exists()
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_interruption_after_pending_save_retries_same_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    project_key = str(tmp_path / "project")
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    arguments = [
        "claim", "--agent-id", str(agent_id), "--project-key", project_key,
        "--connection", str(profile), "--name", "PersistentBot",
    ]
    real_request = enrollment_cli._request
    interrupted = False

    def interrupt_before_mutation(socket_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal interrupted
        if payload.get("action") == "claim" and not interrupted:
            interrupted = True
            raise enrollment_cli.EnrollmentCliError("mail-control-unavailable")
        return real_request(socket_path, payload)

    try:
        monkeypatch.setattr(enrollment_cli, "_request", interrupt_before_mutation)
        with pytest.raises(SystemExit) as failed:
            await asyncio.to_thread(enrollment_cli.main, arguments)
        assert failed.value.code == 3
        pending_path = next((runtime / "enrollment" / "pending").glob("*.json"))
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        first_request_id = pending["request_id"]
        first_secret = pending["new_credential"]

        monkeypatch.setattr(enrollment_cli, "_request", real_request)
        await asyncio.to_thread(enrollment_cli.main, arguments)
        active = (runtime / "agent_token_PersistentBot").read_text(encoding="utf-8").strip()
        assert active == first_secret
        async with db.get_session() as session:
            audit = (
                await session.execute(
                    select(EnrollmentAudit).where(EnrollmentAudit.request_id == first_request_id)
                )
            ).scalar_one()
            assert audit.result == "accepted"
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
@pytest.mark.parametrize(
    ("trigger_name", "trigger_sql"),
    (
        (
            "fail_enrollment_update",
            """CREATE TRIGGER fail_enrollment_update BEFORE UPDATE OF registration_token ON agents
            BEGIN SELECT RAISE(ABORT, 'fixed-fixture-failure'); END""",
        ),
        (
            "fail_enrollment_receipt",
            """CREATE TRIGGER fail_enrollment_receipt BEFORE INSERT ON enrollment_requests
            BEGIN SELECT RAISE(ABORT, 'fixed-fixture-failure'); END""",
        ),
        (
            "fail_enrollment_audit",
            """CREATE TRIGGER fail_enrollment_audit BEFORE INSERT ON enrollment_audits
            BEGIN SELECT RAISE(ABORT, 'fixed-fixture-failure'); END""",
        ),
    ),
)
async def test_fixture_db_exception_rolls_back_without_secret_leak_and_same_request_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    trigger_name: str,
    trigger_sql: str,
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    request = _mutation(server, str(tmp_path / "project"), agent_id, action="claim")
    loop_contexts: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_contexts.append(context))
    try:
        async with db.get_engine().begin() as connection:
            await connection.exec_driver_sql(trigger_sql)
        failed = await _rpc(server.socket_path, request)
        await asyncio.sleep(0)
        assert failed == {
            "version": 1,
            "ok": False,
            "reason": "management-operation-failed",
        }
        assert CANARY not in json.dumps(failed)
        assert CANARY not in caplog.text
        assert not loop_contexts
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token is None
            assert await session.get(EnrollmentRequest, request["request_id"]) is None
            assert (await session.execute(select(EnrollmentAudit))).scalars().all() == []

        async with db.get_engine().begin() as connection:
            await connection.exec_driver_sql(f"DROP TRIGGER {trigger_name}")
        retried = await _rpc(server.socket_path, request)
        assert retried["receipt"]["result"] == "accepted"
        assert CANARY not in json.dumps(retried)
    finally:
        loop.set_exception_handler(previous_handler)
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_db_failure_keeps_pending_for_same_request_cli_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    arguments = (
        "claim", "--agent-id", str(agent_id), "--project-key", str(tmp_path / "project"),
        "--connection", str(profile), "--name", "PersistentBot",
    )
    try:
        async with db.get_engine().begin() as connection:
            await connection.exec_driver_sql(
                """CREATE TRIGGER fail_cli_update BEFORE UPDATE OF registration_token ON agents
                BEGIN SELECT RAISE(ABORT, 'fixed-fixture-failure'); END"""
            )
        code, _stdout, stderr, _argv = await _run_cli(*arguments)
        assert code == 3
        assert json.loads(stderr)["reason"] == "management-operation-failed"
        pending_path = next((runtime / "enrollment" / "pending").glob("*.json"))
        pending = json.loads(pending_path.read_text(encoding="utf-8"))

        async with db.get_engine().begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER fail_cli_update")
        code, stdout, stderr, _argv = await _run_cli(*arguments)
        assert (code, stderr) == (0, "")
        assert json.loads(stdout)["request_id"] == pending["request_id"]
        assert (runtime / "agent_token_PersistentBot").read_text(encoding="utf-8").strip() == pending["new_credential"]
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_unpinned_connection_requires_explicit_inspect_pin_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    profile, _runtime = _connection_profile(tmp_path, server, pinned=False)
    common = (
        "--agent-id", str(agent_id), "--project-key", str(tmp_path / "project"),
        "--connection", str(profile), "--name", "PersistentBot",
    )
    try:
        code, stdout, stderr, _argv = await _run_cli("inspect", *common)
        assert (code, stderr) == (0, "")
        assert json.loads(stdout)["connection_pinned"] is False
        code, _stdout, stderr, _argv = await _run_cli("claim", *common)
        assert code == 2
        assert json.loads(stderr)["reason"] == "connection-not-pinned"

        code, stdout, stderr, _argv = await _run_cli(
            "inspect", *common, "--pin-server", server.instance_id
        )
        assert (code, stderr) == (0, "")
        assert json.loads(stdout)["connection_pinned"] is True
        assert json.loads(profile.read_text(encoding="utf-8"))["expected_server_instance_id"] == server.instance_id
        code, _stdout, stderr, _argv = await _run_cli("claim", *common)
        assert (code, stderr) == (0, "")
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_pinned_connection_rejects_socket_swap_to_another_mail_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database_a, _agent_a = await _setup_database(tmp_path / "first", monkeypatch)
    server_a = await _start_server(tmp_path / "first")
    profile, _runtime = _connection_profile(tmp_path, server_a)
    pinned_instance = server_a.instance_id
    await server_a.close()
    await db.dispose_database_for_shutdown()

    replacement_root = tmp_path / "replacement"
    _database_b, agent_b = await _setup_database(replacement_root, monkeypatch)
    server_b = await _start_server(replacement_root)
    try:
        swapped = json.loads(profile.read_text(encoding="utf-8"))
        swapped["management_socket"] = str(server_b.socket_path)
        profile.write_text(json.dumps(swapped) + "\n", encoding="utf-8")
        profile.chmod(0o600)
        code, _stdout, stderr, _argv = await _run_cli(
            "claim", "--agent-id", str(agent_b),
            "--project-key", str(replacement_root / "project"),
            "--connection", str(profile), "--name", "PersistentBot",
        )
        assert code == 2
        assert json.loads(stderr)["reason"] == "server-identity-mismatch"
        assert server_b.instance_id != pinned_instance
        async with db.get_session() as session:
            assert (await session.get(Agent, agent_b)).registration_token is None
    finally:
        await server_b.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_stale_accepted_receipt_cannot_restore_superseded_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    profile_a, runtime_a = _connection_profile(
        tmp_path, server, name="connection-a", runtime_name="runtime-a"
    )
    profile_b, runtime_b = _connection_profile(
        tmp_path, server, name="connection-b", runtime_name="runtime-b"
    )
    common = ("--agent-id", str(agent_id), "--project-key", str(tmp_path / "project"), "--name", "PersistentBot")
    arguments_a = ["claim", *common, "--connection", str(profile_a)]
    real_write = enrollment_cli._atomic_active_write
    try:
        monkeypatch.setattr(
            enrollment_cli,
            "_atomic_active_write",
            lambda _path, _secret: (_ for _ in ()).throw(OSError("fixture-boundary")),
        )
        with pytest.raises(SystemExit):
            await asyncio.to_thread(enrollment_cli.main, arguments_a)
        pending_a = next((runtime_a / "enrollment" / "pending").glob("*.json"))
        stale = json.loads(pending_a.read_text(encoding="utf-8"))
        monkeypatch.setattr(enrollment_cli, "_atomic_active_write", real_write)

        code, _stdout, stderr, _argv = await _run_cli(
            "recover", *common, "--connection", str(profile_b)
        )
        assert (code, stderr) == (0, "")
        latest = (runtime_b / "agent_token_PersistentBot").read_text(encoding="utf-8").strip()
        assert latest != stale["new_credential"]

        code, _stdout, stderr, _argv = await _run_cli(*arguments_a)
        assert code == 2
        assert json.loads(stderr)["reason"] == "receipt-no-longer-current"
        assert not (runtime_a / "agent_token_PersistentBot").exists()
        assert not pending_a.exists()
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token == latest
            assert agent.credential_generation == 2
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_parallel_cli_activation_serializes_and_keeps_current_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    arguments = (
        "claim", "--agent-id", str(agent_id), "--project-key", str(tmp_path / "project"),
        "--connection", str(profile), "--name", "PersistentBot",
    )
    try:
        first, second = await asyncio.gather(_run_cli(*arguments), _run_cli(*arguments))
        assert sorted((first[0], second[0])) == [0, 2]
        active = (runtime / "agent_token_PersistentBot").read_text(encoding="utf-8").strip()
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token == active
            assert agent.credential_generation == 1
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_active_identity_metadata_blocks_other_authority_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "server-current-token-000000000000"
    _database, agent_id = await _setup_database(tmp_path, monkeypatch, token=original)
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    active = runtime / "agent_token_PersistentBot"
    identity = active.with_name(f"{active.name}.identity.json")
    runtime.mkdir(parents=True, mode=0o700)
    active.write_text("other-authority-token-000000000000\n", encoding="utf-8")
    active.chmod(0o600)
    identity.write_text(
        json.dumps(
            {
                "kind": "orrery-enrollment-active-v1",
                "server_instance_id": str(uuid.uuid4()),
                "project_key": "/other/project",
                "agent_id": agent_id,
                "agent_name": "PersistentBot",
                "request_id": str(uuid.uuid4()),
                "credential_generation": 1,
                "credential_fingerprint": "other",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    identity.chmod(0o600)
    common = (
        "--agent-id", str(agent_id), "--project-key", str(tmp_path / "project"),
        "--connection", str(profile), "--name", "PersistentBot",
    )
    try:
        code, stdout, stderr, _argv = await _run_cli("inspect", *common)
        assert (code, stderr) == (0, "")
        assert json.loads(stdout)["local_credential_state"] == "identity-conflict"
        code, _stdout, stderr, _argv = await _run_cli("recover", *common)
        assert code == 2
        assert json.loads(stderr)["reason"] == "local-identity-conflict"
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token == original
            assert agent.credential_generation == 0
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_token_only_loss_with_same_identity_sidecar_can_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    profile, runtime = _connection_profile(tmp_path, server)
    common = (
        "--agent-id", str(agent_id), "--project-key", str(tmp_path / "project"),
        "--connection", str(profile), "--name", "PersistentBot",
    )
    active = runtime / "agent_token_PersistentBot"
    identity_path = active.with_name(f"{active.name}.identity.json")
    try:
        code, _stdout, stderr, _argv = await _run_cli("claim", *common)
        assert (code, stderr) == (0, "")
        first_credential = active.read_text(encoding="utf-8").strip()
        first_identity = json.loads(identity_path.read_text(encoding="utf-8"))
        assert first_identity["credential_generation"] == 1

        active.unlink()
        code, stdout, stderr, _argv = await _run_cli("inspect", *common)
        assert (code, stderr) == (0, "")
        assert json.loads(stdout)["local_credential_state"] == "missing-token-same-identity"

        code, stdout, stderr, _argv = await _run_cli("recover", *common)
        assert (code, stderr) == (0, "")
        assert json.loads(stdout)["new_generation"] == 2
        recovered_credential = active.read_text(encoding="utf-8").strip()
        recovered_identity = json.loads(identity_path.read_text(encoding="utf-8"))
        assert recovered_credential != first_credential
        assert recovered_identity["credential_generation"] == 2
        async with db.get_session() as session:
            agent = await session.get(Agent, agent_id)
            assert agent.registration_token == recovered_credential
            assert agent.credential_generation == 2
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


@_sync_async_test
async def test_fixture_secret_canary_absent_from_result_audit_and_public_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _database, agent_id = await _setup_database(tmp_path, monkeypatch)
    server = await _start_server(tmp_path)
    try:
        response = await _rpc(
            server.socket_path,
            _mutation(server, str(tmp_path / "project"), agent_id, action="claim"),
        )
        assert CANARY not in json.dumps(response)
        async with db.get_session() as session:
            audits = (await session.execute(select(EnrollmentAudit))).scalars().all()
            assert CANARY not in repr([row.model_dump() for row in audits])
        assert CANARY not in caplog.text
        # The in-process public-catalog fixture must not inherit the developer's
        # live management socket. It exercises MCP dispatch, while the isolated
        # control socket above exercises enrollment.
        monkeypatch.delenv("AGENTSTACK_MAIL_MANAGEMENT_SOCKET", raising=False)
        get_settings.cache_clear()
        mcp = build_mcp_server()
        published = mcp.published_tool_names
        assert not {"inspect_enrollment", "claim_agent", "recover_agent", "agentstack_enroll"} & published
        async with Client(mcp) as client:
            with pytest.raises(Exception, match="Unknown tool"):
                await client.call_tool("agentstack_enroll", {})
    finally:
        await server.close()
        await db.dispose_database_for_shutdown()


def test_help_explains_operator_workflow_without_loading_connection(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        enrollment_cli.main(["--help"])
    assert exited.value.code == 0
    output = capsys.readouterr().out
    assert "inspect -> claim" in output
    assert "receipt" in output
    assert "persistent profile" in output


def test_fixture_mail_stopped_is_a_fixed_non_secret_failure(tmp_path: Path) -> None:
    profile = tmp_path / "offline.json"
    profile.write_text(
        json.dumps(
            {
                "kind": "orrery-mail-connection-v1",
                "management_socket": str(tmp_path / "missing.sock"),
                "runtime_dir": str(tmp_path / "runtime"),
            }
        ),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    with pytest.raises(SystemExit) as exited:
        enrollment_cli.main(
            [
                "inspect", "--agent-id", "1", "--project-key", str(tmp_path / "project"),
                "--connection", str(profile),
            ]
        )
    assert exited.value.code == 2
