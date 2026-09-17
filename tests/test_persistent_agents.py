from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "agentstack-persistent"
DELIVER = ROOT / "bin" / "agentstack-persistent-deliver"


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fixture(
    tmp_path: Path,
    *,
    interaction: str,
    provider: str,
    command: list[str],
    local_state: str = "present",
    credential_generation: int = 3,
) -> tuple[Path, Path, Path, dict[str, str]]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    state = tmp_path / "state"
    work = tmp_path / "work"
    work.mkdir()
    project = str(tmp_path / "project")
    connection = tmp_path / "connection.json"
    connection.write_text(
        json.dumps(
            {
                "kind": "orrery-mail-connection-v1",
                "management_socket": str(tmp_path / "mail.sock"),
                "runtime_dir": str(runtime),
                "expected_server_instance_id": "mail-instance-fixture",
                "mcp_url": "http://127.0.0.1:18765/mcp",
                "mail_env": str(tmp_path / "mail.env"),
                "http_bearer_mode": "disabled",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    connection.chmod(0o600)
    profile = {
        "kind": "orrery-persistent-agent-v1",
        "name": "PersistentBot",
        "agent_id": 41,
        "project_key": project,
        "connection": str(connection),
        "provider": provider,
        "parentless": True,
        "lifecycle": "persistent",
        "interaction": interaction,
        "state_dir": str(state),
        "working_directory": str(work),
        "command": command,
        "environment": {},
    }
    if interaction == "headless":
        profile["bridge_contract"] = "orrery-mail-notification-reply-v1"
    source_home = tmp_path / "source-codex"
    if provider == "codex":
        source_home.mkdir()
        (source_home / "config.toml").write_text(
            'model = "gpt-fixture"\n'
            '[mcp_servers."orrery-mail"]\nurl = "http://direct.invalid"\n'
            '[mcp_servers.other]\ncommand = "other"\n',
            encoding="utf-8",
        )
        (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
        profile["source_home"] = str(source_home)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile) + "\n", encoding="utf-8")
    profile_path.chmod(0o600)

    enroll = _write_executable(
        tmp_path / "bin" / "agentstack-enroll",
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({{'ok': True, 'agent_id': 41, 'project_key': {project!r}, "
        f"'name': 'PersistentBot', 'credential_state': 'server-token', "
        f"'connection_pinned': True, 'local_credential_state': {local_state!r}, "
        f"'server_instance_id': 'mail-instance-fixture', "
        f"'credential_generation': {credential_generation}, "
        f"'credential_fingerprint': '0123456789abcdef'}}))\n",
    )
    stack = tmp_path / "stack"
    _write_executable(
        stack / "integrations/codex_app/plugin/scripts/run-mcp.sh",
        "#!/bin/sh\nexit 0\n",
    )
    mail_env = tmp_path / "mail.env"
    mail_env.write_text("MODE=fixture\n", encoding="utf-8")
    env = {
        **os.environ,
        "AGENTSTACK_ENROLL_BIN": str(enroll),
        "AGENTSTACK_HOME": str(stack),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:18765/mcp",
        "AGENTSTACK_MAIL_ENV": str(mail_env),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
    }
    return profile_path, runtime, state, env


def _wait_for(path: Path, process: subprocess.Popen[bytes], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(f"process exited before {path}: {process.returncode}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def test_interactive_profile_exec_preserves_pty_stdin_and_channels_arguments(tmp_path: Path):
    record = tmp_path / "interactive.json"
    fake_claude = _write_executable(
        tmp_path / "bin" / "claude",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "line = sys.stdin.readline().strip()\n"
        "value = {'pid': os.getpid(), 'stdin_tty': os.isatty(0), 'line': line, "
        "'argv': sys.argv, 'agent': os.environ.get('AGENT_NAME'), "
        "'parent': os.environ.get('PARENT_AGENT'), 'config': os.environ.get('AGENTSTACK_PERSISTENT_MCP_CONFIG')}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(value))\n"
        "print('received:' + line, flush=True)\n",
    )
    profile, runtime, state, env = _fixture(
        tmp_path,
        interaction="interactive",
        provider="claude",
        command=[str(fake_claude), "--channels", "telegram", "--state-dir", str(tmp_path / "channels")],
    )
    # The selected connection profile, not an ambient endpoint, controls the
    # bound proxy transport.
    env["AGENTSTACK_MCP_URL"] = "http://wrong-ambient.invalid/mcp"
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(LAUNCHER), "run", "--profile", str(profile)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    try:
        os.write(master, b"cockpit input\n")
        assert process.wait(timeout=15) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            process.terminate()
    value = json.loads(record.read_text(encoding="utf-8"))
    assert value["pid"] == process.pid, "launcher must exec, not hide the REPL as a child"
    assert value["stdin_tty"] is True
    assert value["line"] == "cockpit input"
    assert value["agent"] == "PersistentBot"
    assert value["parent"] is None
    assert value["argv"][1:5] == ["--channels", "telegram", "--state-dir", str(tmp_path / "channels")]
    assert value["argv"][-3] == "--mcp-config"
    assert value["argv"][-1] == "--strict-mcp-config"
    proxy = json.loads(Path(value["config"]).read_text(encoding="utf-8"))
    assert set(proxy["mcpServers"]) >= {"orrery-mail", "agentstack"}
    assert {
        server["env"]["AGENTSTACK_MCP_URL"] for server in proxy["mcpServers"].values()
    } == {"http://127.0.0.1:18765/mcp"}
    manifest = json.loads((runtime / "persistent" / "PersistentBot.json").read_text())
    assert manifest["interaction"] == "interactive"
    assert manifest["provider"] == "claude"
    assert state.joinpath("instance.lock").is_file()


def test_interactive_codex_gets_fresh_binding_each_run_without_parent_pair(tmp_path: Path):
    record = tmp_path / "codex-runs.jsonl"
    fake_codex = _write_executable(
        tmp_path / "bin" / "codex",
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "value = {'binding': os.environ.get('AGENTSTACK_CODEX_LAUNCH_BINDING'), "
        "'launch_id': os.environ.get('AGENTSTACK_CODEX_LAUNCH_ID'), "
        "'parent': os.environ.get('PARENT_AGENT')}\n"
        f"with open({str(record)!r}, 'a', encoding='utf-8') as fh: fh.write(json.dumps(value) + '\\n')\n",
    )
    profile, runtime, _state, env = _fixture(
        tmp_path,
        interaction="interactive",
        provider="codex",
        command=[str(fake_codex)],
    )
    env.update(
        {
            "PARENT_AGENT": "ParentBot",
            "AGENTSTACK_CODEX_LAUNCH_BINDING": "/parent/binding.json",
            "AGENTSTACK_CODEX_LAUNCH_ID": "f" * 32,
        }
    )

    for _ in range(2):
        result = subprocess.run(
            [str(LAUNCHER), "run", "--profile", str(profile)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

    launches = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert len(launches) == 2
    assert launches[0]["launch_id"] != launches[1]["launch_id"]
    assert {entry["binding"] for entry in launches} == {
        str(runtime / "codex_launches" / "41.json")
    }
    assert all(entry["parent"] is None for entry in launches)
    final = json.loads((runtime / "codex_launches" / "41.json").read_text(encoding="utf-8"))
    assert final["launch_id"] == launches[-1]["launch_id"]
    assert final["launch_kind"] == "startup"
    assert final["claimed_session_id"] is None


def test_codex_binding_prepare_failure_blocks_headless_command(tmp_path: Path):
    marker = tmp_path / "command-ran"
    bridge = _write_executable(
        tmp_path / "bin" / "fixture-bridge",
        f"#!/bin/sh\ntouch {str(marker)!r}\n",
    )
    profile, _runtime, _state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="codex",
        command=[str(bridge)],
    )
    hooks = tmp_path / "failing-hooks"
    _write_executable(
        hooks / "prepare-codex-session-binding.py",
        "#!/usr/bin/env python3\nraise SystemExit(1)\n",
    )
    env["AGENTSTACK_HOOKS_DIR"] = str(hooks)
    result = subprocess.run(
        [str(LAUNCHER), "run", "--profile", str(profile)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["reason"] == "codex-binding-prepare-failed"
    assert not marker.exists()


def test_malformed_profile_axis_fails_closed_without_traceback(tmp_path: Path):
    command = _write_executable(tmp_path / "bin" / "bridge", "#!/bin/sh\nexit 0\n")
    profile, _runtime, _state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="claude",
        command=[str(command)],
    )
    value = json.loads(profile.read_text(encoding="utf-8"))
    value["provider"] = []
    profile.write_text(json.dumps(value), encoding="utf-8")
    profile.chmod(0o600)
    result = subprocess.run(
        [str(LAUNCHER), "inspect", "--profile", str(profile)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["reason"] == "profile-invalid"
    assert "Traceback" not in result.stderr


def test_profile_refuses_missing_local_credential_before_exec(tmp_path: Path):
    marker = tmp_path / "should-not-run"
    command = _write_executable(
        tmp_path / "bin" / "bridge",
        f"#!/bin/sh\ntouch {str(marker)!r}\n",
    )
    profile, _runtime, _state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="claude",
        command=[str(command)],
        local_state="missing-token-same-identity",
    )
    result = subprocess.run(
        [str(LAUNCHER), "run", "--profile", str(profile)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr) == {
        "docs": "docs/persistent-agents.md",
        "ok": False,
        "reason": "local-credential-unavailable",
    }
    assert not marker.exists()


def test_profile_accepts_verified_legacy_generation_zero_credential(tmp_path: Path):
    command = _write_executable(tmp_path / "bin" / "bridge", "#!/bin/sh\nexit 0\n")
    profile, _runtime, _state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="claude",
        command=[str(command)],
        local_state="present-legacy-verified",
        credential_generation=0,
    )
    result = subprocess.run(
        [str(LAUNCHER), "inspect", "--profile", str(profile)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    inspected = json.loads(result.stdout)
    assert inspected["ready"] is True
    assert inspected["credential_generation"] == 0


def test_profile_refuses_inspect_result_from_another_pinned_authority(tmp_path: Path):
    marker = tmp_path / "should-not-run"
    command = _write_executable(
        tmp_path / "bin" / "bridge",
        f"#!/bin/sh\ntouch {str(marker)!r}\n",
    )
    profile, _runtime, _state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="claude",
        command=[str(command)],
    )
    enroll = tmp_path / "bin" / "agentstack-enroll"
    enroll.write_text(
        enroll.read_text(encoding="utf-8").replace(
            "'server_instance_id': 'mail-instance-fixture'",
            "'server_instance_id': 'different-mail-instance'",
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(LAUNCHER), "run", "--profile", str(profile)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["reason"] == "enrolled-authority-mismatch"
    assert not marker.exists()


def test_same_profile_cannot_start_twice(tmp_path: Path):
    ready = tmp_path / "ready"
    sleeper = _write_executable(
        tmp_path / "bin" / "bridge",
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "while True: time.sleep(1)\n",
    )
    profile, _runtime, _state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="claude",
        command=[str(sleeper)],
    )
    first = subprocess.Popen(
        [str(LAUNCHER), "run", "--profile", str(profile)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(ready, first)
        second = subprocess.run(
            [str(LAUNCHER), "run", "--profile", str(profile)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert second.returncode == 2
        assert json.loads(second.stderr)["reason"] == "profile-already-running"
    finally:
        first.terminate()
        first.wait(timeout=10)


def test_headless_mail_wake_reaches_bot_and_confirms_reply(tmp_path: Path):
    ready = tmp_path / "bridge-ready"
    handoff = tmp_path / "bot-handoff.json"
    reply = tmp_path / "mail-reply.json"
    bridge = _write_executable(
        tmp_path / "bin" / "fixture-bridge",
        "#!/usr/bin/env python3\n"
        "import json, os, socket\n"
        "path = os.environ['AGENTSTACK_PERSISTENT_WAKE_SOCKET']\n"
        "server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "server.bind(path); os.chmod(path, 0o600); server.listen(1)\n"
        f"open({str(ready)!r}, 'w').write(json.dumps(dict(codex_home=os.environ['CODEX_HOME'], "
        "binding=os.environ['AGENTSTACK_CODEX_LAUNCH_BINDING'], "
        "launch_id=os.environ['AGENTSTACK_CODEX_LAUNCH_ID'], "
        "parent_binding=os.environ.get('PARENT_AGENT'))))\n"
        "client, _ = server.accept()\n"
        "request = json.loads(client.makefile().readline())\n"
        f"open({str(handoff)!r}, 'w').write(json.dumps(request))\n"
        "answer = {'to': request['message']['from'], 'in_reply_to': request['message']['id'], 'body': 'bot answer'}\n"
        f"open({str(reply)!r}, 'w').write(json.dumps(answer))\n"
        "response = {'version': 1, 'contract': request['contract'], 'status': 'replied', 'message_id': request['message']['id']}\n"
        "client.sendall((json.dumps(response) + '\\n').encode()); client.close(); server.close()\n",
    )
    profile, runtime, state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="codex",
        command=[str(bridge)],
    )
    process = subprocess.Popen(
        [str(LAUNCHER), "run", "--profile", str(profile)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(ready, process)
        signal_file = tmp_path / "7.signal"
        signal_file.write_text(
            json.dumps(
                {
                    "message": {
                        "id": 7,
                        "from": "Operator",
                        "subject": "wake fixture",
                        "importance": "high",
                        "body_snippet": "please respond",
                        "body_truncated": False,
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        delivered = subprocess.run(
            [
                str(DELIVER),
                "--runtime-dir",
                str(runtime),
                "--agent-name",
                "PersistentBot",
                "--signal-file",
                str(signal_file),
                "--timeout",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert (delivered.returncode, delivered.stdout, delivered.stderr) == (0, "", "")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    request = json.loads(handoff.read_text(encoding="utf-8"))
    assert request["type"] == "mail-notification"
    assert request["message"]["id"] == 7
    assert json.loads(reply.read_text(encoding="utf-8")) == {
        "to": "Operator",
        "in_reply_to": 7,
        "body": "bot answer",
    }
    launch_environment = json.loads(ready.read_text(encoding="utf-8"))
    codex_home = Path(launch_environment["codex_home"])
    config = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "direct.invalid" not in config
    assert 'mcp_servers."orrery-mail"' in config
    assert "mcp_servers.agentstack" in config
    assert (codex_home / "auth.json").is_symlink()
    binding = Path(launch_environment["binding"])
    assert binding == runtime / "codex_launches" / "41.json"
    launch = json.loads(binding.read_text(encoding="utf-8"))
    assert launch["agent_id"] == 41
    assert launch["agent_name"] == "PersistentBot"
    assert launch["project_key"] == str(tmp_path / "project")
    assert launch["launch_kind"] == "startup"
    assert launch["history_mode"] == "enabled"
    assert launch["launch_id"] == launch_environment["launch_id"]
    assert launch_environment["parent_binding"] is None
    manifest = json.loads((runtime / "persistent" / "PersistentBot.json").read_text())
    assert manifest["interaction"] == "headless"
    assert manifest["bridge_contract"] == "orrery-mail-notification-reply-v1"
    assert state.joinpath("instance.lock").is_file()


def test_delivery_does_not_fall_back_when_headless_bridge_did_not_reply(tmp_path: Path):
    runtime = tmp_path / "runtime"
    manifest_dir = runtime / "persistent"
    manifest_dir.mkdir(parents=True)
    manifest = manifest_dir / "PersistentBot.json"
    manifest.write_text(
        json.dumps(
            {
                "kind": "orrery-persistent-runtime-v1",
                "pid": os.getpid(),
                "name": "PersistentBot",
                "lifecycle": "persistent",
                "parentless": True,
                "interaction": "headless",
                "bridge_contract": "orrery-mail-notification-reply-v1",
                "wake_socket": str(tmp_path / "missing.sock"),
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    signal_file = tmp_path / "1.signal"
    signal_file.write_text(json.dumps({"message": {"id": 1}}), encoding="utf-8")
    result = subprocess.run(
        [
            str(DELIVER),
            "--runtime-dir",
            str(runtime),
            "--agent-name",
            "PersistentBot",
            "--signal-file",
            str(signal_file),
            "--timeout",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 20
    assert signal_file.exists()


def _delivery_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    runtime = tmp_path / "runtime"
    manifest_dir = runtime / "persistent"
    manifest_dir.mkdir(parents=True)
    wake_socket = Path("/tmp") / f"orrery-deliver-{os.getpid()}-{time.time_ns()}.sock"
    manifest = manifest_dir / "PersistentBot.json"
    manifest.write_text(
        json.dumps(
            {
                "kind": "orrery-persistent-runtime-v1",
                "pid": os.getpid(),
                "name": "PersistentBot",
                "lifecycle": "persistent",
                "parentless": True,
                "interaction": "headless",
                "bridge_contract": "orrery-mail-notification-reply-v1",
                "wake_socket": str(wake_socket),
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    signal_file = tmp_path / "12.signal"
    signal_file.write_text(json.dumps({"message": {"id": 12}}), encoding="utf-8")
    return runtime, wake_socket, signal_file


def _deliver(runtime: Path, signal_file: Path, timeout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(DELIVER),
            "--runtime-dir",
            str(runtime),
            "--agent-name",
            "PersistentBot",
            "--signal-file",
            str(signal_file),
            "--timeout",
            timeout,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_delivery_deadline_is_global_across_slow_chunks_then_retry_succeeds(tmp_path: Path):
    runtime, wake_socket, signal_file = _delivery_fixture(tmp_path)
    ready = threading.Event()
    response = (
        json.dumps(
            {
                "version": 1,
                "contract": "orrery-mail-notification-reply-v1",
                "status": "replied",
                "message_id": 12,
            }
        )
        + "\n"
    ).encode()

    def bridge() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(wake_socket))
            os.chmod(wake_socket, 0o600)
            server.listen(2)
            ready.set()
            with server.accept()[0] as client:
                client.makefile().readline()
                for byte in response:
                    try:
                        client.sendall(bytes([byte]))
                    except OSError:
                        break
                    time.sleep(0.06)
            with server.accept()[0] as client:
                client.makefile().readline()
                client.sendall(response)

    thread = threading.Thread(target=bridge, daemon=True)
    thread.start()
    assert ready.wait(2)
    started = time.monotonic()
    first = _deliver(runtime, signal_file, "0.25")
    elapsed = time.monotonic() - started
    assert first.returncode == 20
    assert elapsed < 0.8, "small chunks must not reset the overall reply deadline"
    second = _deliver(runtime, signal_file, "2")
    assert second.returncode == 0
    thread.join(timeout=2)
    assert not thread.is_alive()
    wake_socket.unlink(missing_ok=True)


def test_delivery_allows_normal_reply_near_but_within_deadline(tmp_path: Path):
    runtime, wake_socket, signal_file = _delivery_fixture(tmp_path)
    ready = threading.Event()

    def bridge() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(wake_socket))
            os.chmod(wake_socket, 0o600)
            server.listen(1)
            ready.set()
            with server.accept()[0] as client:
                request = json.loads(client.makefile().readline())
                time.sleep(0.35)
                response = {
                    "version": 1,
                    "contract": request["contract"],
                    "status": "replied",
                    "message_id": request["message"]["id"],
                }
                client.sendall((json.dumps(response) + "\n").encode())

    thread = threading.Thread(target=bridge, daemon=True)
    thread.start()
    assert ready.wait(2)
    started = time.monotonic()
    result = _deliver(runtime, signal_file, "1")
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert 0.3 <= elapsed < 1.0
    thread.join(timeout=2)
    assert not thread.is_alive()
    wake_socket.unlink(missing_ok=True)


def test_expired_lease_old_owner_cannot_release_successor(tmp_path: Path):
    source = (ROOT / "hooks" / "watch_agent_mail_signals.sh").read_text(encoding="utf-8")
    lease_functions = source[
        source.index("acquire_delivery_lease() {") : source.index("is_pid_running() {")
    ]
    fixture = tmp_path / "lease-fixture.sh"
    fixture.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "LEASE_DIR=\"$1\"\n"
        "LEASE_TTL=1\n"
        "mkdir -p \"$LEASE_DIR\"\n"
        + lease_functions
        + "\nowner_one=$(acquire_delivery_lease PersistentBot 12)\n"
        "printf '0\\n' > \"$LEASE_DIR/PersistentBot-12.lock/ts\"\n"
        "owner_two=$(acquire_delivery_lease PersistentBot 12)\n"
        "release_delivery_lease PersistentBot 12 \"$owner_one\"\n"
        "test \"$(cat \"$LEASE_DIR/PersistentBot-12.lock/owner\")\" = \"$owner_two\"\n"
        "release_delivery_lease PersistentBot 12 \"$owner_two\"\n"
        "test ! -d \"$LEASE_DIR/PersistentBot-12.lock\"\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/bash", str(fixture), str(tmp_path / "leases")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_saturation_waits_before_lease_and_rechecks_completed_state(tmp_path: Path):
    source = (ROOT / "hooks" / "watch_agent_mail_signals.sh").read_text(encoding="utf-8")
    handler = source[
        source.index("handle_signal_file() {") : source.index("# deliver_worker:")
    ]
    signal_file = (
        tmp_path / "signals" / "projects" / "fixture" / "agents" / "PersistentBot" / "12.signal"
    )
    signal_file.parent.mkdir(parents=True)
    signal_file.write_text("{}\n", encoding="utf-8")
    fixture = tmp_path / "saturated-worker.sh"
    fixture.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "SIGNAL=$1\nCALLS=$2\nACQUIRED=$3\nDELIVERED=$4\n"
        "MAX_WORKERS=1\nNOTIFY_MIN_IMPORTANCE=low\n"
        "read_signal_meta() { printf '12\\nOperator\\nsubject\\nnormal\\n0\\n\\n0\\n'; }\n"
        "importance_at_least() { return 0; }\n"
        "state_should_attempt() {\n"
        "  local count=0\n"
        "  [[ -f \"$CALLS\" ]] && count=$(cat \"$CALLS\")\n"
        "  count=$((count + 1))\n"
        "  printf '%s\\n' \"$count\" > \"$CALLS\"\n"
        "  [[ \"$count\" -eq 1 ]]\n"
        "}\n"
        "state_mark_result() { :; }\n"
        "acquire_delivery_lease() { touch \"$ACQUIRED\"; printf 'owner\\n'; }\n"
        "deliver_worker() { touch \"$DELIVERED\"; }\n"
        "log() { :; }\n"
        + handler
        + "\n(sleep 0.4) &\n"
        "handle_signal_file \"$SIGNAL\"\n"
        "wait\n"
        "test \"$(cat \"$CALLS\")\" = 2\n"
        "test ! -e \"$ACQUIRED\"\n"
        "test ! -e \"$DELIVERED\"\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "/bin/bash",
            str(fixture),
            str(signal_file),
            str(tmp_path / "state-calls"),
            str(tmp_path / "lease-acquired"),
            str(tmp_path / "delivered"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_watcher_routes_headless_signal_through_bridge_without_tmux(tmp_path: Path):
    ready = tmp_path / "watcher-bridge-ready"
    replied = tmp_path / "watcher-bridge-replied"
    bridge = _write_executable(
        tmp_path / "bin" / "watcher-bridge",
        "#!/usr/bin/env python3\n"
        "import json, os, socket\n"
        "path = os.environ['AGENTSTACK_PERSISTENT_WAKE_SOCKET']\n"
        "server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "server.bind(path); os.chmod(path, 0o600); server.listen(1)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "client, _ = server.accept()\n"
        "request = json.loads(client.makefile().readline())\n"
        f"open({str(replied)!r}, 'w').write(str(request['message']['id']))\n"
        "response = {'version': 1, 'contract': request['contract'], 'status': 'replied', 'message_id': request['message']['id']}\n"
        "client.sendall((json.dumps(response) + '\\n').encode())\n"
        "client.close(); server.close()\n",
    )
    profile, runtime, _state, env = _fixture(
        tmp_path,
        interaction="headless",
        provider="claude",
        command=[str(bridge)],
    )
    agent = subprocess.Popen(
        [str(LAUNCHER), "run", "--profile", str(profile)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    watcher: subprocess.Popen[str] | None = None
    try:
        _wait_for(ready, agent)
        signals = tmp_path / "signals"
        signal_file = signals / "projects" / "fixture" / "agents" / "PersistentBot" / "8.signal"
        signal_file.parent.mkdir(parents=True)
        signal_file.write_text(
            json.dumps(
                {
                    "message": {
                        "id": 8,
                        "from": "Operator",
                        "subject": "watcher headless fixture",
                        "importance": "high",
                    }
                }
            ),
            encoding="utf-8",
        )
        tmux_called = tmp_path / "tmux-called"
        fake_bin = tmp_path / "watcher-bin"
        _write_executable(
            fake_bin / "tmux",
            f"#!/bin/sh\ntouch {str(tmux_called)!r}\nexit 0\n",
        )
        watcher_env = {
            **os.environ,
            "PATH": f"{fake_bin}:{ROOT / '.venv' / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            "AGENTSTACK_SIGNALS_DIR": str(signals),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(tmp_path / "watcher-lock"),
            "AGENTSTACK_PERSISTENT_DELIVER": str(DELIVER),
            "AGENTSTACK_HEADLESS_REPLY_TIMEOUT": "5",
        }
        watcher = subprocess.Popen(
            ["/bin/bash", str(ROOT / "hooks" / "watch_agent_mail_signals.sh")],
            env=watcher_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and signal_file.exists():
            if watcher.poll() is not None:
                raise AssertionError(f"watcher exited early: {watcher.communicate()}")
            time.sleep(0.05)
        assert not signal_file.exists()
        assert replied.read_text(encoding="utf-8") == "8"
        state = json.loads((runtime / "notify-state.json").read_text(encoding="utf-8"))
        assert state["PersistentBot:8"]["last_result"] == "success"
        assert state["PersistentBot:8"]["source"] == "persistent-headless-replied"
        assert not tmux_called.exists()
        assert agent.wait(timeout=10) == 0
    finally:
        if watcher is not None and watcher.poll() is None:
            watcher.terminate()
            watcher.wait(timeout=10)
        if agent.poll() is None:
            agent.terminate()
            agent.wait(timeout=10)


def test_help_routes_enrollment_receipt_to_profile_start():
    result = subprocess.run([str(LAUNCHER), "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "agentstack-enroll inspect" in result.stdout
    assert "claim/recover" in result.stdout
    assert "receipt" in result.stdout
    assert "agentstack-persistent run --profile" in result.stdout


def test_watcher_routes_headless_without_tmux_fallback():
    source = (ROOT / "hooks" / "watch_agent_mail_signals.sh").read_text(encoding="utf-8")
    assert '"$PERSISTENT_DELIVER"' in source
    assert '"headless_reply_failed"' in source
    assert source.index('"$PERSISTENT_DELIVER"') < source.index("tmux has-session")
