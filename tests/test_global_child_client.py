"""Real S2c HTTP and actual bash3 hook inputs in a temporary HOME."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/agentstack_mail/tests"))
sys.path.insert(0, str(ROOT / "tests"))
import test_global_server_s2c as s2c
import test_global_runtime_client_s2a as cfixture

api = runpy.run_path(str(ROOT / "bin/lib/runtime_client.py"))
Client = api["RuntimeClient"]
Error = api["ClientError"]


@pytest.fixture
def server(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    prep = s2c.prepared.__wrapped__(monkeypatch)
    state = next(prep)
    service = s2c.server.__wrapped__(state)
    try:
        next(service)
        yield state
    finally:
        service.close()
        prep.close()


def client(server, name="parent"):
    existing = cfixture.context(server, name, window=False)
    return Client(existing.path)


def shell(c, rel, args=(), payload=None, extra=None):
    env = {
        **os.environ,
        "AGENTSTACK_CLIENT_CONFIG": str(c.path),
        "HOME": str(c.isolation / "home"),
        "AGENTSTACK_HOME": str(c.isolation / "agentstack"),
    }
    env.update(extra or {})
    return subprocess.run(
        ["/bin/bash", str(ROOT / rel), *args],
        input=json.dumps(payload or {}),
        text=True,
        capture_output=True,
        env=env,
        timeout=40,
    )


def reserve(c, ttl=1800):
    path = str(c.isolation / "work file.py")
    row = c.call("file_reservation_paths", {"paths": [path], "ttl_seconds": ttl})
    return path, row["granted"][0]["id"]


def receipt_count(state):
    with s2c.connection(Path(state["config"]["database"])) as db:
        return db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[
            0
        ]


def test_real_preregister_and_retry_preserves_metadata(server):
    c = client(server)
    args = [
        "--child-client-name",
        "child_01",
        "--name",
        "ChildAlpha",
        "--program",
        "codex",
        "--model",
        "explicit-model",
        "--task-description",
        "task text",
        "--prepare-only",
    ]
    first = shell(c, "bin/agentstack-preregister-child", args)
    assert first.returncode == 0, first.stderr
    row = json.loads(first.stdout)
    assert row["runtime_ready"] is False and "registration_token" not in first.stdout
    second = shell(c, "bin/agentstack-preregister-child", args)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["agent_id"] == row["agent_id"]
    with s2c.connection(Path(server["config"]["database"])) as db:
        records = db.execute(
            "SELECT program,model,task_description FROM agents WHERE name='ChildAlpha'"
        ).fetchall()
        assert [tuple(x) for x in records] == [("codex", "explicit-model", "task text")]


@pytest.mark.parametrize("delivery", ["unsent", "committed"])
def test_registration_unknown_retries_same_name_token(server, monkeypatch, delivery):
    c = client(server)
    original = Client.rpc
    lost = False

    def request(self, method, params):
        nonlocal lost
        if params.get("name") == "register_agent" and not self.identity and not lost:
            lost = True
            if delivery == "committed":
                original(self, method, params)
            raise Error("TRANSPORT_FAILED")
        return original(self, method, params)

    monkeypatch.setattr(Client, "rpc", request)
    options = {
        "child_client_name": "child_01",
        "name": "ChildAlpha",
        "program": "codex",
        "model": "model",
        "prepare_only": True,
    }
    with pytest.raises(Error, match="TRANSPORT_FAILED"):
        c.prepare_child(options)
    pending_path = c.clients_parent / "child_01" / api["CLIENT_LAYOUT"]["registration"]
    token = api["read_json"](pending_path)["registration_token"]
    row = c.prepare_child(options)
    credential = api["read_json"](c.clients_parent / "child_01/credential.json")
    assert credential["registration_token"] == token
    assert row["agent_id"] == credential["agent_id"]
    with s2c.connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM agents WHERE name='ChildAlpha'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("role", ["credential", "context", "metadata"])
def test_child_partial_activation_recovers_same_pending(server, monkeypatch, role):
    c = client(server)
    original = Client.write_output
    once = False

    def write(self, path, value, kind):
        nonlocal once
        original(self, path, value, kind)
        if self.config["client_name"] == "child_01" and kind == role and not once:
            once = True
            raise OSError("fixture interruption")

    monkeypatch.setattr(Client, "write_output", write)
    opts = {
        "child_client_name": "child_01",
        "name": "ChildAlpha",
        "program": "codex",
        "model": "model",
        "prepare_only": True,
    }
    with pytest.raises(OSError):
        c.prepare_child(opts)
    pending = api["read_json"](
        c.clients_parent / "child_01" / api["CLIENT_LAYOUT"]["registration"]
    )
    resumed = c.prepare_child(opts)
    assert resumed["agent_id"] == pending["row"]["agent_id"]


def test_prepare_only_and_execution_refusal_before_registration(server):
    c = client(server)
    result = shell(c, "hooks/spawn_child.sh", ["--child-client-name", "new_child"])
    assert (
        result.returncode == 2 and "GLOBAL_CHILD_RUNTIME_REQUIRES_PR4C" in result.stderr
    )
    assert not (c.clients_parent / "new_child").exists()


def test_guard_no_receipts_until_threshold_then_one_renew(server):
    c = client(server)
    path, key = reserve(c, 1800)
    baseline = receipt_count(server)
    payload = {
        "session_id": "sessionA",
        "cwd": str(c.isolation),
        "tool_name": "Edit",
        "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"},
    }
    for _ in range(3):
        out = shell(c, "hooks/check-file-reservation.sh", payload=payload)
        assert out.returncode == 0, out.stderr
    assert receipt_count(server) == baseline
    c.call("release_file_reservations", {"file_reservation_ids": [key]})
    path, key = reserve(c, 60)
    baseline = receipt_count(server)
    out = shell(c, "hooks/check-file-reservation.sh", payload=payload)
    assert out.returncode == 0, out.stderr
    assert receipt_count(server) == baseline + 1
    out = shell(c, "hooks/check-file-reservation.sh", payload=payload)
    assert out.returncode == 0, out.stderr
    assert receipt_count(server) == baseline + 1


def test_renew_response_lost_next_pretool_replays_exact_uuid(server, monkeypatch):
    c = client(server)
    path, key = reserve(c, 60)
    original = c.rpc
    lost = False

    def request(method, params):
        nonlocal lost
        value = original(method, params)
        if params.get("name") == "renew_file_reservations" and not lost:
            lost = True
            raise Error("TRANSPORT_FAILED")
        return value

    monkeypatch.setattr(c, "rpc", request)
    payload = {"cwd": str(c.isolation), "tool_input": {"file_path": path}}
    with pytest.raises(Error, match="TRANSPORT_FAILED"):
        c.guard_edit(payload)
    pending = api["read_json"](c.outputs["mutation"])
    count = receipt_count(server)
    out = shell(c, "hooks/check-file-reservation.sh", payload=payload)
    assert out.returncode == 0, out.stderr
    assert receipt_count(server) == count and not c.outputs["mutation"].exists()
    with s2c.connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM global_operation_receipts WHERE request_id=?",
                (pending["request_id"],),
            ).fetchone()[0]
            == 1
        )


def test_message_intent_keeps_body_and_attachments_replays_without_caller(
    server, monkeypatch
):
    c = client(server)
    original = c.rpc
    lost = False

    def request(method, params):
        nonlocal lost
        value = original(method, params)
        if params.get("name") == "send_message" and not lost:
            lost = True
            raise Error("TRANSPORT_FAILED")
        return value

    monkeypatch.setattr(c, "rpc", request)
    args = {
        "to_agent_ids": [1],
        "subject": "subject",
        "body_md": "本文",
        "attachments": [
            {
                "filename": "file.bin",
                "media_type": "application/octet-stream",
                "content_base64": "eA==",
            }
        ],
    }
    with pytest.raises(Error, match="TRANSPORT_FAILED"):
        c.call("send_message", args)
    saved = api["read_json"](c.outputs["mutation"])
    assert saved["canonical_intent"]["body_md"] == "本文"
    fresh = Client(c.path)
    result = fresh.replay_pending()
    assert (
        result["request_id"] == saved["request_id"]
        and not c.outputs["mutation"].exists()
    )


@pytest.mark.parametrize("policy,code", [("", 0), ("warn-open", 0), ("block", 2)])
def test_actual_pretool_outage_policy(server, policy, code):
    c = client(server)
    config = dict(c.config)
    config["mcp_url"] = "http://127.0.0.1:1/mcp"
    api["atomic_json"](c.path, config)
    out = shell(
        c,
        "hooks/check-file-reservation.sh",
        payload={
            "session_id": "outage",
            "cwd": str(c.isolation),
            "tool_input": {"file_path": "file.py"},
        },
        extra={"AGENTSTACK_MAIL_OUTAGE_POLICY": policy} if policy else {},
    )
    assert out.returncode == code, out.stderr
    if code == 0:
        assert "another agent" in out.stderr + out.stdout


def test_same_id_other_session_prevents_all_end_paths(server, monkeypatch):
    c = client(server)
    path, key = reserve(c)
    # The real subprocess probe is separately verified outside the sandbox;
    # tri-state decisions are deterministic in this unit boundary.
    globals_ = Client.record_live.__globals__
    monkeypatch.setitem(globals_, "process_start", lambda pid: "same-birth")
    c.record_live({"session_id": "A", "provider_pid": 123})
    c.record_live({"session_id": "B", "provider_pid": 124})
    for _ in range(2):
        assert c.end_session({"session_id": "A"})["end"] == "other-session-live"
    with s2c.connection(Path(server["config"]["database"])) as db:
        assert (
            db.execute(
                "SELECT released_ts FROM file_reservations WHERE id=?", (key,)
            ).fetchone()[0]
            is None
        )
    monkeypatch.setitem(globals_, "process_start", lambda pid: None)
    assert c.end_session({"session_id": "A"})["end"] == "unknown"


def test_global_posttool_registration_of_child_never_marks_parent(server):
    c = client(server)
    payload = {
        "session_id": "parent-session",
        "tool_input": {"name": "ChildAlpha"},
        "tool_response": {"agent_id": 123, "name": "ChildAlpha"},
    }
    result = shell(c, "hooks/mark-agent-registered.sh", payload=payload)
    assert result.returncode == 0, result.stderr
    assert not (c.runtime_dir / "session_index/self.json").exists()
    assert not (c.isolation / "home" / "agent_name_parent-session").exists()


@pytest.mark.parametrize("state", ["absent", "unknown", "alive"])
def test_actual_sessionend_cleanup_chain_preserves_other_session(
    server, monkeypatch, state
):
    c = client(server)
    path, key = reserve(c)
    globals_ = Client.record_live.__globals__
    monkeypatch.setitem(globals_, "process_start", lambda pid: "birth")
    c.record_live({"session_id": "A", "provider_pid": 123})
    c.record_live({"session_id": "B", "provider_pid": 124})
    monkeypatch.setitem(
        globals_,
        "process_start",
        lambda pid: {"absent": "absent", "unknown": None, "alive": "birth"}[state],
    )
    executable = c.isolation / "stubs"
    executable.mkdir()
    ps = executable / "ps"
    ps.write_text(
        '#!/bin/sh\ncase "$PS_STATE" in absent) exit 1;; alive) echo birth;; *) echo denied >&2; exit 1;; esac\n'
    )
    ps.chmod(0o700)
    for current in ["A", "B"]:
        for hook in [
            "hooks/release-all-reservations.sh",
            "hooks/cleanup-child-agent.sh",
        ]:
            output = shell(
                c,
                hook,
                payload={"session_id": current},
                extra={
                    "PATH": str(executable) + os.pathsep + os.environ["PATH"],
                    "PS_STATE": state,
                },
            )
            assert output.returncode == 0, output.stderr
            result = json.loads(output.stdout)
            assert result["changed"] is (state == "absent")
    with s2c.connection(Path(server["config"]["database"])) as db:
        released = db.execute(
            "SELECT released_ts FROM file_reservations WHERE id=?", (key,)
        ).fetchone()[0]
        assert (released is not None) is (state == "absent")


def test_empty_release_and_explicit_ids_keep_server_rejection(server):
    c = client(server)
    result = c.call(
        "release_file_reservations", {"paths": [str(c.isolation / "missing.py")]}
    )
    assert result["released"] == []
    with pytest.raises(Error, match="LEASE_NOT_FOUND"):
        c.call("release_file_reservations", {"file_reservation_ids": [999999]})
    assert not c.outputs["mutation"].exists()


def test_root_growth_and_role_collision_are_structural(server, monkeypatch):
    c = client(server)
    directory = c.client_root / "runtime/provider"
    directory.mkdir(mode=0o700)
    for index in range(1100):
        (directory / f"{index}.txt").write_text("provider history")
    assert Client(c.path).observe()["agent_id"] == 2
    c.outputs["child"].symlink_to(c.authority)
    calls = []
    monkeypatch.setattr(Client, "rpc", lambda *args: calls.append(args))
    with pytest.raises(Error, match="CONTEXT_PATH_ROLE_CONFLICT"):
        Client(c.path)
    assert calls == []


def test_schema_invalid_receipt_keeps_raw_intent(server, monkeypatch):
    c = client(server)
    original = c.rpc

    def request(method, params):
        result = original(method, params)
        if params.get("name") == "send_message":
            result["result"]["structuredContent"]["request_id"] = str(
                __import__("uuid").uuid4()
            )
        return result

    monkeypatch.setattr(c, "rpc", request)
    with pytest.raises(Error, match="RESPONSE_INVALID"):
        c.call(
            "send_message",
            {"to_agent_ids": [1], "subject": "topic", "body_md": "retry bytes"},
        )
    saved = api["read_private"](c.outputs["mutation"])
    assert json.loads(saved)["canonical_intent"]["body_md"] == "retry bytes"
    fresh = Client(c.path)
    fresh.replay_pending()
    assert not c.outputs["mutation"].exists()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_actual_http_rejection_never_warns_open(server, status):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    c = client(server)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        cfg = dict(c.config)
        cfg["mcp_url"] = f"http://127.0.0.1:{httpd.server_port}/mcp"
        api["atomic_json"](c.path, cfg)
        result = shell(
            c,
            "hooks/check-file-reservation.sh",
            payload={"cwd": str(c.isolation), "tool_input": {"file_path": "file.py"}},
        )
        assert result.returncode == 2 and "HTTP_REJECTED" in result.stderr
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def test_broken_context_never_creates_old_hook_flags(server):
    c = client(server)
    c.path.unlink()
    c.path.symlink_to(c.client_root / "missing")
    result = shell(
        c,
        "hooks/check-agent-registered.sh",
        payload={"session_id": "broken", "cwd": str(c.isolation)},
    )
    assert result.returncode == 2 and "PRIVATE_FILE_UNAVAILABLE" in result.stderr


def test_s2c_welcome_uses_shared_raw_intent(server, monkeypatch):
    c = client(server)
    original = c.rpc
    once = False

    def request(method, params):
        nonlocal once
        value = original(method, params)
        if params.get("name") == "macro_contact_handshake" and not once:
            once = True
            raise Error("TRANSPORT_FAILED")
        return value

    monkeypatch.setattr(c, "rpc", request)
    with pytest.raises(Error, match="TRANSPORT_FAILED"):
        c.call(
            "macro_contact_handshake",
            {
                "to_agent_id": 1,
                "welcome_subject": "hello",
                "welcome_body": "raw welcome",
            },
        )
    saved = api["read_json"](c.outputs["mutation"])
    assert saved["canonical_intent"]["welcome_body"] == "raw welcome"
    assert Client(c.path).replay_pending()["request_id"] == saved["request_id"]


def test_debounce_worker_stale_generation_cannot_release_new_lease(server, monkeypatch):
    c = client(server)
    path, key = reserve(c)
    monkeypatch.setenv("FILE_RESERVATION_RELEASE_DELAY_SECONDS", "0")
    old = "00000000-0000-4000-8000-000000000001"
    new = "00000000-0000-4000-8000-000000000002"
    c.write_output(
        c.outputs["release"],
        {
            "kind": "orrery-global-release-debounce-v1",
            **c.binding,
            "agent_id": 2,
            "generation": new,
            "ids": [key],
        },
        "release",
    )
    assert c.release_worker(old)["stale"] is True
    assert c.call("check_file_reservations", {"paths": [path]})["covered"] is True
    c.release_worker(new)
    assert c.call("check_file_reservations", {"paths": [path]})["covered"] is False


def test_child_end_resume_purge_keeps_identity_and_refuses_unknown(server, monkeypatch):
    from datetime import datetime, timedelta, timezone

    c = client(server)
    prepared = c.prepare_child(
        {"child_client_name": "child_01", "name": "ChildAlpha", "prepare_only": True}
    )
    child = Client(prepared["client_config"])
    row = child.resume_child({"prepare_only": True})
    assert row["agent_id"] == prepared["agent_id"] and row["runtime_ready"] is False
    globals_ = Client.record_live.__globals__
    monkeypatch.setitem(globals_, "process_start", lambda pid: "birth")
    child.record_live({"session_id": "child", "provider_pid": 123})
    assert child.end_session({"session_id": "child"})["end"] == "last-session"
    state = api["read_json"](child.outputs["child"])
    child.write_output(
        child.outputs["child"],
        {
            **state,
            "resume_expires_at": (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(),
        },
        "child",
    )
    child.record_live({"session_id": "unknown", "provider_pid": 124})
    monkeypatch.setitem(globals_, "process_start", lambda pid: None)
    with pytest.raises(Error, match="SESSION_LIVENESS_UNKNOWN"):
        child.purge_child()
    with pytest.raises(Error, match="CHILD_RETENTION_EXPIRED"):
        child.resume_child({"prepare_only": True})
    monkeypatch.setitem(globals_, "process_start", lambda pid: "absent")
    assert child.purge_child()["credential_retained"] is True
    assert child.purge_child()["purged"] is True
    assert child.outputs["credential"].exists()
    with pytest.raises(Error, match="CHILD_PURGED"):
        child.resume_child({"prepare_only": True})
    assert (
        api["read_json"](child.outputs["credential"])["agent_id"]
        == prepared["agent_id"]
    )


@pytest.mark.parametrize("boundary", ["before-unretire", "lost-unretire"])
def test_resume_partial_state_retries_same_identity(server, monkeypatch, boundary):
    parent = client(server)
    prepared = parent.prepare_child(
        {"child_client_name": "child_01", "name": "ChildAlpha", "prepare_only": True}
    )
    child = Client(prepared["client_config"])
    monkeypatch.setitem(
        Client.record_live.__globals__, "process_start", lambda pid: "birth"
    )
    child.record_live({"session_id": "s", "provider_pid": 123})
    child.end_session({"session_id": "s"})
    token = api["read_private"](child.credential)
    if boundary == "before-unretire":
        original = child.write_output

        def write(path, value, role):
            original(path, value, role)
            if role == "child" and value["phase"] == "resume-planned":
                raise OSError("fixture interruption")

        monkeypatch.setattr(child, "write_output", write)
        expected = OSError
    else:
        original = child.rpc

        def request(method, params):
            result = original(method, params)
            if params.get("name") == "unretire_agent":
                raise Error("TRANSPORT_FAILED")
            return result

        monkeypatch.setattr(child, "rpc", request)
        expected = Error
    with pytest.raises(expected):
        child.resume_child({"prepare_only": True})
    assert api["read_json"](child.outputs["child"])["phase"] == "resume-planned"
    fresh = Client(child.path)
    assert (
        fresh.resume_child({"prepare_only": True})["agent_id"] == prepared["agent_id"]
    )
    assert api["read_private"](child.credential) == token
    result = shell(
        fresh, "bin/lib/agentstack-register.sh", ["cancel-child-resume", "--operator"]
    )
    assert result.returncode == 0, result.stderr
    assert (
        fresh.management("inspect", agent_id=fresh.identity["agent_id"])["retired_at"]
        is not None
    )


@pytest.mark.parametrize(
    "failure",
    [
        {"tool_response": {"status": "FAILED"}},
        {"tool_response": "PreToolUse: blocked"},
        {"tool_result": "PostToolUse: failed"},
    ],
)
def test_failed_actual_posttool_never_schedules_release(server, failure):
    c = client(server)
    path, key = reserve(c)
    output = shell(
        c,
        "hooks/release-file-reservation.sh",
        payload={
            "session_id": "s",
            "cwd": str(c.isolation),
            "tool_input": {"file_path": path},
            **failure,
        },
    )
    assert output.returncode == 0, output.stderr
    assert not c.outputs["release"].exists()
    assert c.call("check_file_reservations", {"paths": [path]})["covered"] is True


@pytest.mark.parametrize("policy", ["", "warn-open", "block"])
def test_actual_posttool_outage_warns_and_audits_without_undo(server, policy):
    c = client(server)
    config = dict(c.config)
    config["mcp_url"] = "http://127.0.0.1:1/mcp"
    api["atomic_json"](c.path, config)
    output = shell(
        c,
        "hooks/release-file-reservation.sh",
        payload={
            "session_id": "s",
            "cwd": str(c.isolation),
            "tool_input": {"file_path": "file.py"},
            "tool_response": "success",
        },
        extra={"AGENTSTACK_MAIL_OUTAGE_POLICY": policy} if policy else {},
    )
    assert output.returncode == 0
    assert ("another agent" in output.stdout + output.stderr) is (policy != "block")
    assert (
        c.isolation / "home/.agentstack/runtime/logs/unmanaged_sessions.jsonl"
    ).is_file()


def test_s2c_contact_receipt_wrong_peer_keeps_single_intent(server, monkeypatch):
    c = client(server)
    original = c.rpc

    def request(method, params):
        reply = original(method, params)
        if params.get("name") == "macro_contact_handshake":
            value = api["result_value"](reply)
            value["contact"]["to_agent_id"] = 99999
            return {"result": {"isError": False, "structuredContent": value}}
        return reply

    monkeypatch.setattr(c, "rpc", request)
    with pytest.raises(Error, match="RESPONSE_INVALID"):
        c.call("macro_contact_handshake", {"to_agent_id": 1})
    saved = api["read_json"](c.outputs["mutation"])
    assert saved["phase"] == "planned"
    assert Client(c.path).replay_pending()["request_id"] == saved["request_id"]
