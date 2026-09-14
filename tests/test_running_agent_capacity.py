"""Coverage for the shared, runtime-scoped running-agent limit."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "hooks" / "running_agent_capacity.py"


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="running-agent capacity uses POSIX process and file-locking APIs",
)


def _run(
    runtime: Path,
    *arguments: str,
    limit: str = "2",
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    environment = os.environ.copy()
    environment.update({
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_MAX_RUNNING_AGENTS": limit,
        "AGENTSTACK_MANAGED_AGENTS_FILE": str(runtime / "managed_agents.txt"),
    })
    if extra_env:
        environment.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(HELPER), "--output", "json", *arguments],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            "running-agent capacity emitted no JSON "
            f"(exit {result.returncode}): {(result.stderr or '').strip()}"
        ) from exc
    return result, payload


def test_reservation_is_bounded_and_release_frees_the_slot(tmp_path: Path):
    runtime = tmp_path / "runtime"

    first, first_data = _run(runtime, "reserve", "--purpose", "first", limit="2")
    second, second_data = _run(runtime, "reserve", "--purpose", "second", limit="2")
    full, full_data = _run(runtime, "reserve", "--purpose", "third", limit="2")

    assert first.returncode == 0
    assert second.returncode == 0
    assert first_data["lease_id"] != second_data["lease_id"]
    assert full.returncode == 3
    assert full_data == {
        "ok": False,
        "code": "running_agent_limit_reached",
        "error": "running-agent limit reached: 2 running, limit 2",
        "running": 2,
        "limit": 2,
        "external_sessions": [],
    }

    released, released_data = _run(
        runtime, "release", "--lease", first_data["lease_id"], limit="2"
    )
    retry, retry_data = _run(runtime, "reserve", "--purpose", "retry", limit="2")

    assert released.returncode == 0
    assert released_data == {"ok": True, "released": True}
    assert retry.returncode == 0
    assert retry_data["lease_id"]


def test_invalid_value_fails_closed(tmp_path: Path):
    result, data = _run(tmp_path / "runtime", "reserve", limit="two")

    assert result.returncode == 4
    assert data["ok"] is False
    assert data["code"] == "invalid_running_agent_limit"


def test_parallel_requests_never_admit_more_than_the_limit(tmp_path: Path):
    runtime = tmp_path / "runtime"

    def reserve_once(_: int) -> tuple[int, dict]:
        result, data = _run(runtime, "reserve", "--purpose", "parallel", limit="2")
        return result.returncode, data

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(reserve_once, range(6)))

    admitted = [data for code, data in results if code == 0]
    rejected = [data for code, data in results if code == 3]
    assert len(admitted) == 2
    assert len(rejected) == 4
    assert all(data["code"] == "running_agent_limit_reached" for data in rejected)


def test_existing_managed_tmux_agent_counts_without_a_lease(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "managed_agents.txt").write_text("Parent\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "ps": "#!/bin/sh\nprintf '700 1 sh\\n701 700 codex\\n'\n",
        "tmux": "#!/bin/sh\nprintf 'Parent\\t700\\n'\n",
    }.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    result, data = _run(
        runtime,
        "reserve",
        "--purpose",
        "child",
        limit="1",
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 3
    assert data["code"] == "running_agent_limit_reached"
    assert data["running"] == 1
    assert data["external_sessions"] == ["Parent"]


def test_macos_absent_tmux_server_is_an_empty_inventory(tmp_path: Path):
    """macOS says ``error connecting ... no such file`` when tmux is absent."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "managed_agents.txt").write_text("Parent\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "ps": "#!/bin/sh\nprintf '700 1 sh\\n'\n",
        "tmux": (
            "#!/bin/sh\n"
            "printf 'error connecting to /private/tmp/tmux-501/default "
            "(no such file or directory)\\n' >&2\n"
            "exit 1\n"
        ),
    }.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    result, data = _run(
        runtime,
        "reserve",
        "--purpose",
        "child",
        limit="1",
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    assert data["running"] == 0


def test_expired_lease_does_not_hide_a_reused_managed_session(tmp_path: Path):
    """A dead old lease must not make a new process with its name invisible."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "managed_agents.txt").write_text("Parent\n", encoding="utf-8")
    (runtime / "running-agents.json").write_text(
        json.dumps({
            "version": 1,
            "leases": [{
                "id": "abcdefghijklmno",
                "state": "running",
                "purpose": "old-launch",
                "created_at": time.time() - 600,
                "updated_at": time.time() - 600,
                "expires_at": time.time() - 500,
                "session": "Parent",
                "program": "codex",
                "pane_pid": 999,
            }],
        }),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "ps": "#!/bin/sh\nprintf '700 1 sh\\n701 700 codex\\n'\n",
        "tmux": "#!/bin/sh\nprintf 'Parent\\t700\\n'\n",
    }.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    result, data = _run(
        runtime,
        "reserve",
        "--purpose",
        "child",
        limit="1",
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 3
    assert data["running"] == 1
    assert data["external_sessions"] == ["Parent"]
