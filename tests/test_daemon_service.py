"""One native Mail process, temp HOME/DB/socket/port, synthetic identities only."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integrations/codex_app/src"))
sys.path.insert(0, str(ROOT / "packages/agentstack_mail/src"))
from agentstack_codex_app.daemon_runtime import (
    DaemonRuntime,
    DaemonError,
    KIND,
    create_identity,
    finalize_creation,
)
from agentstack_codex_app.agent_mail_client import (
    AgentMailClient,
    HttpJsonRpcTransport,
    AgentMailError,
)
from agentstack_mail import enrollment_cli as enroll


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def native(tmp_path):
    tmp_path.chmod(0o700)
    # Keep the management socket below the Unix path-length limit, including
    # when TMPDIR points at a long per-user directory on macOS.
    scratch = tempfile.TemporaryDirectory(prefix="daemon-", dir="/tmp")
    control = Path(scratch.name) / "m.sock"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port not in {8765, 8770, 8791, 9250, 18765}
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT / "packages/agentstack_mail/src"),
        "AGENTSTACK_MAIL_ENV_FILE": str(tmp_path / "missing.env"),
        "AGENTSTACK_MAIL_DATABASE_URL": "sqlite+aiosqlite:///"
        + str(tmp_path / "mail.sqlite"),
        "AGENTSTACK_MAIL_STORAGE_ROOT": str(tmp_path / "archive"),
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": str(tmp_path / "signals"),
        "AGENTSTACK_MAIL_MANAGEMENT_SOCKET": str(control),
        "AGENTSTACK_MAIL_HTTP_HOST": "127.0.0.1",
        "AGENTSTACK_MAIL_HTTP_PORT": str(port),
        "AGENTSTACK_MAIL_HTTP_PATH": "/api/",
        "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE": "passthrough",
        "AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED": "false",
        "AGENTSTACK_MAIL_LOG_LEVEL": "WARNING",
        "AGENTSTACK_MAIL_HTTP_REQUEST_LOG_ENABLED": "false",
    }
    log = open(tmp_path / "mail.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentstack_mail.cli"],
        env=env,
        cwd=tmp_path,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    client = AgentMailClient(
        HttpJsonRpcTransport(
            f"http://127.0.0.1:{port}/api/", timeout=1, allow_redirects=False
        )
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, "isolated Mail process exited"
            try:
                health = client._call_tool_object("health_check", {})
                if control.exists():
                    break
            except AgentMailError:
                pass
            assert time.monotonic() < deadline, "isolated Mail startup deadline"
            time.sleep(0.1)
        instance = health["server_instance_id"]
        connection = {
            "kind": "orrery-mail-connection-v1",
            "expected_server_instance_id": instance,
            "mcp_url": f"http://127.0.0.1:{port}/api/",
            "management_socket": str(control),
            "runtime_dir": str(tmp_path),
            "http_bearer_mode": "disabled",
        }
        conn = write(tmp_path / "connection.json", connection)
        project = str(tmp_path / "project")
        client._call_tool_object("ensure_project", {"human_key": project})
        peer_token = "synthetic-peer-owner-token-0000000000000000"
        peer = client._call_tool_object(
            "register_agent",
            {
                "project_key": project,
                "name": "ExamplePeer",
                "program": "fixture-peer",
                "model": "deterministic",
                "registration_token": peer_token,
            },
        )
        receipt = create_identity(
            conn,
            project_key=project,
            name="BlueLake",
            journal_path=tmp_path / "creation.json",
        )
        profile = {
            "kind": KIND,
            "name": "BlueLake",
            "agent_id": receipt["agent_id"],
            "project_key": project,
            "connection": str(conn),
            "peer_name": "ExamplePeer",
            "peer_id": peer["id"],
            "pause_path": str(tmp_path / "paused.json"),
            "lock_path": str(tmp_path / "daemon.lock"),
        }
        path = write(tmp_path / "daemon.json", profile)
        yield {
            "path": path,
            "profile": profile,
            "connection": connection,
            "client": client,
            "peer_token": peer_token,
            "receipt": receipt,
            "root": tmp_path,
            "control": control,
        }
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)
        log.close()
        scratch.cleanup()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_native_binding_notification_ack_send_restart_and_duplicate_lock(native):
    x = native
    runtime = DaemonRuntime.open(x["path"])
    try:
        status = runtime.call("runtime_status", {})
        assert status["instance"] == x["receipt"]["server_instance_id"]
        inspected = enroll._request(
            x["control"],
            {
                "version": 1,
                "action": "inspect",
                "project_key": x["profile"]["project_key"],
                "agent_id": status["agent_id"],
                "expected_name": "BlueLake",
            },
        )
        assert inspected["server_instance_id"] == status["instance"]
        with pytest.raises(BlockingIOError):
            DaemonRuntime.open(x["path"])
        mid = x["client"].send_message(
            project_key=x["profile"]["project_key"],
            agent_name="ExamplePeer",
            registration_token=x["peer_token"],
            to=["BlueLake"],
            subject="synthetic",
            body_md="synthetic",
        )
        assert runtime.wait_for_notification(1) is True
        messages = runtime.call("fetch_inbox", {"limit": 1000, "include_bodies": True})
        assert len(messages) == 1 and messages[0]["body_md"] == "synthetic"
        runtime.call("acknowledge_message", {"message_id": messages[0]["id"]})
        result = runtime.call(
            "send_message",
            {
                "to": ["ExamplePeer"],
                "subject": "synthetic reply",
                "body_md": "synthetic reply",
                "thread_id": str(uuid.uuid4()),
            },
        )
        assert result["deliveries"][0]["payload"]["id"] > messages[0]["id"]
    finally:
        runtime.close()
    reopened = DaemonRuntime.open(x["path"])
    try:
        assert reopened.call("runtime_status", {}) == status
        marker = Path(x["profile"]["pause_path"])
        marker.write_text("{}")
        with pytest.raises(DaemonError, match="paused"):
            reopened.wait_for_notification(1)
        marker.unlink()
    finally:
        reopened.close()


def test_native_close_cancels_wait_and_cli_receipt_redacts_credentials(native):
    x = native
    env = {
        "HOME": str(x["root"]),
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin/agentstack-daemon"),
            "inspect",
            "--profile",
            str(x["path"]),
        ],
        env=env,
        cwd=x["root"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["agent_id"] == x["receipt"]["agent_id"]
    assert x["peer_token"] not in result.stdout + result.stderr
    assert "credential" not in result.stdout
    runtime = DaemonRuntime.open(x["path"])
    finished = []
    thread = threading.Thread(
        target=lambda: finished.append(runtime.wait_for_notification(60))
    )
    thread.start()
    time.sleep(0.2)
    start = time.monotonic()
    runtime.close()
    thread.join(5)
    assert not thread.is_alive() and finished == [False]
    assert time.monotonic() - start < 5


def test_unknown_creation_is_finalized_only_for_the_same_numeric_row(native):
    x = native
    active = x["root"] / "agent_token_BlueLake"
    active.unlink()
    journal_path = x["root"] / "creation.json"
    journal = json.loads(journal_path.read_text())
    journal["outcome"] = "unknown"
    write(journal_path, journal)
    with pytest.raises(DaemonError, match="row-mismatch"):
        finalize_creation(
            x["profile"]["connection"],
            journal_path=journal_path,
            agent_id=x["profile"]["peer_id"],
        )
    assert not active.exists()
    for _ in range(2):
        receipt = finalize_creation(
            x["profile"]["connection"],
            journal_path=journal_path,
            agent_id=x["receipt"]["agent_id"],
        )
        assert receipt == x["receipt"]
    runtime = DaemonRuntime.open(x["path"])
    runtime.close()
