"""Typed local inputs and exceptional registration recovery through real S2c."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_global_child_client import api, client, shell, server as fixture_server

server = fixture_server


def test_tools_task_and_typed_json_use_existing_shell_front_door(server):
    parent = client(server)
    prepared = shell(
        parent,
        "bin/agentstack-preregister-child",
        [
            "--child-client-name",
            "child_01",
            "--name",
            "ChildAlpha",
            "--base",
            "mail-only",
            "--tools",
            "mcp:example",
            "--prepare-only",
        ],
    )
    assert prepared.returncode == 0, prepared.stderr
    result = json.loads(prepared.stdout)
    child = api["RuntimeClient"](result["client_config"])
    state = api["read_json"](child.outputs["child"])
    assert state["tools_selection"] == {
        "base": "mail-only",
        "tools": {"mcp": ["example"]},
    }
    task = parent.isolation / "task.txt"
    task.write_text("literal task with `shell` and $(syntax)\n")
    args = [
        "--child-client-name",
        "child_01",
        "--prepare-only",
        "--embed-task",
        "--task-file",
        str(task),
    ]
    for _ in range(2):
        output = shell(parent, "hooks/spawn_child.sh", args)
        assert output.returncode == 0, output.stderr
        assert json.loads(output.stdout)["runtime_ready"] is False
    assert child.outputs["task"].read_bytes() == task.read_bytes()
    task.write_text("changed task")
    output = shell(parent, "hooks/spawn_child.sh", args)
    assert output.returncode == 2 and "TASK_PENDING_CONFLICT" in output.stderr
    library = Path(api["ROOT"]) / "bin/lib/agentstack-register.sh"
    output = subprocess.run(
        ["/bin/bash", str(library), "call-json", "file_reservation_paths"],
        input=json.dumps(
            {
                "paths": [str(parent.isolation / "a file.py")],
                "exclusive": True,
                "ttl_seconds": 1800,
            }
        ),
        env={**os.environ, "AGENTSTACK_CLIENT_CONFIG": str(parent.path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert output.returncode == 0, output.stderr
    assert json.loads(output.stdout)["granted"][0]["exclusive"] is True


def test_foreign_registration_requires_explicit_digest_abandon(server):
    parent = client(server)
    with pytest.raises(api["ClientError"], match="OWNER_REQUIRED"):
        parent.prepare_child(
            {"child_client_name": "child_01", "name": "Alpha", "prepare_only": True}
        )
    child = api["RuntimeClient"](parent.clients_parent / "child_01/runtime-client.json")
    raw = api["read_private"](child.outputs["registration"])
    with pytest.raises(api["ClientError"], match="REGISTRATION_PENDING_CONFLICT"):
        child.abandon_registration("0" * 64)
    assert api["read_private"](child.outputs["registration"]) == raw
    result = child.abandon_registration(hashlib.sha256(raw).hexdigest())
    assert result["terminal"] is True
    assert (
        api["read_json"](child.outputs["registration"])["registration_token"]
        == json.loads(raw)["registration_token"]
    )
    with pytest.raises(api["ClientError"], match="REGISTRATION_OWNER_CONFLICT"):
        parent.prepare_child(
            {"child_client_name": "child_01", "name": "Alpha", "prepare_only": True}
        )


def test_resume_checks_parent_before_unretire(server):
    parent = client(server)
    parent.prepare_child(
        {"child_client_name": "child_01", "name": "ChildAlpha", "prepare_only": True}
    )
    other = client(server, "other")
    output = shell(
        other,
        "bin/lib/agentstack-register.sh",
        ["resume-child", "--child-client-name", "child_01", "--prepare-only"],
    )
    assert output.returncode == 2 and "CHILD_PARENT_MISMATCH" in output.stderr
    output = shell(
        parent,
        "bin/lib/agentstack-register.sh",
        ["resume-child", "--child-client-name", "../other", "--prepare-only"],
    )
    assert output.returncode == 2 and "CLIENT_NAME_INVALID" in output.stderr


def test_explicit_context_never_falls_back_when_register_library_missing(server):
    parent = client(server)
    root = parent.isolation / "partial"
    (root / "hooks").mkdir(parents=True)
    (root / "bin/lib").mkdir(parents=True, exist_ok=True)
    (root / "bin/lib/global-client-loader.sh").write_bytes(
        (Path(api["ROOT"]) / "bin/lib/global-client-loader.sh").read_bytes()
    )
    hook = root / "hooks/cleanup-child-agent.sh"
    hook.write_bytes((Path(api["ROOT"]) / "hooks/cleanup-child-agent.sh").read_bytes())
    output = shell(parent, str(hook), payload={"session_id": "s"})
    assert output.returncode == 2 and "GLOBAL_ENTRY_UNAVAILABLE" in output.stderr


def test_real_ps_other_session_keeps_leases_until_process_exits(server):
    """Run outside macOS sandbox: ps must observe the owned fixture process."""
    parent = client(server)
    lease = parent.call(
        "file_reservation_paths", {"paths": [str(parent.isolation / "owned.py")]}
    )["granted"][0]["id"]
    process = subprocess.Popen(["sleep", "30"])
    try:
        parent.record_live({"session_id": "A", "provider_pid": os.getpid()})
        record = parent.record_live({"session_id": "B", "provider_pid": process.pid})
        assert record["process_start"] not in (None, "absent")
        assert parent.end_session({"session_id": "A"})["end"] == "other-session-live"
        process.terminate()
        process.wait(timeout=5)
        assert parent.end_session({"session_id": "A"})["end"] == "last-session"
        assert all(row["id"] != lease for page in parent.all_leases() for row in page)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_explicit_global_refuses_an_old_library_without_dispatch(server):
    parent = client(server)
    root = parent.isolation / "old-install"
    (root / "hooks").mkdir(parents=True)
    (root / "bin/lib").mkdir(parents=True, exist_ok=True)
    (root / "bin/lib/global-client-loader.sh").write_bytes(
        (Path(api["ROOT"]) / "bin/lib/global-client-loader.sh").read_bytes()
    )
    (root / "bin/lib").mkdir(parents=True, exist_ok=True)
    hook = root / "hooks/cleanup-child-agent.sh"
    hook.write_bytes((Path(api["ROOT"]) / "hooks/cleanup-child-agent.sh").read_bytes())
    (root / "bin/lib/agentstack-register.sh").write_text(
        "ags_register() { return 0; }\n"
    )
    output = shell(parent, str(hook), payload={"session_id": "s"})
    assert output.returncode == 2 and "GLOBAL_ENTRY_UNAVAILABLE" in output.stderr


def test_context_equals_works_without_ambient_global_selector(server):
    parent = client(server)
    output = shell(
        parent,
        "bin/agentstack-preregister-child",
        [
            "--context=" + str(parent.path),
            "--child-client-name",
            "equal",
            "--name",
            "ChildEqual",
            "--prepare-only",
        ],
        extra={"AGENTSTACK_CLIENT_CONFIG": ""},
    )
    assert output.returncode == 0, output.stderr
    assert json.loads(output.stdout)["runtime_ready"] is False


def test_gemini_lifecycle_helper_passes_typed_paths_without_legacy_fields(server):
    parent = client(server)
    helper = Path(api["ROOT"]) / "bin/agentstack-gemini-child-mail"
    paths = json.dumps([str(parent.isolation / "with space.py")])
    env = {**os.environ, "AGENTSTACK_CLIENT_CONFIG": str(parent.path)}
    for action in ("reserve", "release"):
        output = subprocess.run(
            [
                sys.executable,
                str(helper),
                action,
                "--paths-json",
                paths,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert output.returncode == 0, output.stderr
        value = json.loads(output.stdout)
        assert value["granted" if action == "reserve" else "released"]


@pytest.mark.parametrize(
    "old_library",
    ["ags_register() { return 0; }\n", "ags_global_entry() { return 125; }\n"],
)
def test_resume_global_partial_library_never_accepts_empty_success(server, old_library):
    parent = client(server)
    root = parent.isolation / "partial-resume"
    (root / "hooks").mkdir(parents=True)
    (root / "bin/lib").mkdir(parents=True, exist_ok=True)
    (root / "bin/lib/global-client-loader.sh").write_bytes(
        (Path(api["ROOT"]) / "bin/lib/global-client-loader.sh").read_bytes()
    )
    (root / "bin/lib").mkdir(parents=True, exist_ok=True)
    for rel in ("bin/agentstack-resume", "hooks/child_tools.py"):
        (root / rel).write_bytes((Path(api["ROOT"]) / rel).read_bytes())
    (root / "bin/lib/agentstack-register.sh").write_text(old_library)
    output = subprocess.run(
        [
            sys.executable,
            str(root / "bin/agentstack-resume"),
            "ChildAlpha",
        ],
        env={**os.environ, "AGENTSTACK_CLIENT_CONFIG": str(parent.path)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert output.returncode == 2 and "GLOBAL_ENTRY_UNAVAILABLE" in output.stderr
    assert not output.stdout


def test_resume_cli_uses_existing_global_front_door(server):
    parent = client(server)
    parent.prepare_child(
        {"child_client_name": "child_01", "name": "ChildAlpha", "prepare_only": True}
    )
    output = subprocess.run(
        [
            sys.executable,
            str(Path(api["ROOT"]) / "bin/agentstack-resume"),
            "--client-name",
            "child_01",
            "--prepare-only",
            "--detached",
        ],
        env={**os.environ, "AGENTSTACK_CLIENT_CONFIG": str(parent.path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert output.returncode == 0, output.stderr
    assert json.loads(output.stdout)["runtime_ready"] is False
