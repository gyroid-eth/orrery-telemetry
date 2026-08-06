"""Hermetic executable evidence for pending product decisions D11 and D12.

D11 runs the frozen, authenticated live source and AgentStack Mail Core in
secret-free subprocesses with worker-owned database, archive, and signal roots.
The injected barriers alter scheduling only; production retirement, reservation,
send, persistence, and notification code remains unchanged.

D12 executes the checked-in watcher delivery state machine without starting its
watch loop.  ``run_to`` is replaced with a recorder, so no real tmux command can
run.  SIGKILL probes observe the durable boundaries around the recorded external
command, success state, lease release, and signal unlink.  A tmux-like external
system applying bytes and then losing the process before returning is inherently
outside this process's transactional observation; the source-order assertion
below deliberately records that remaining unobservable seam.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from differential_source import (
    CORE_NAMESPACE,
    LIVE_NAMESPACE,
    WorkerStateRoots,
    isolated_worker_env,
    reconstruct_live,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CORE_SOURCE = PACKAGE_ROOT / "src"
WATCHER = REPOSITORY_ROOT / "hooks" / "watch_agent_mail_signals.sh"


_D11_WORKER = r"""
import asyncio
import importlib
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

from fastmcp import Client
from fastmcp.exceptions import ToolError

namespace = os.environ["DECISION_NAMESPACE"]
state_root = Path(os.environ["DECISION_STATE_ROOT"])
database_path = Path(os.environ["DECISION_DATABASE"])
signals_root = Path(os.environ["DECISION_SIGNALS"])

# The tested paths do not use an LLM.  Installing the same fail-closed stub as
# the main differential worker lets frozen live import without the optional
# legacy LLM dependency and fails if a scenario accidentally crosses that seam.
llm_stub = types.ModuleType(f"{namespace}.llm")


async def fail_if_llm_called(*_args, **_kwargs):
    raise AssertionError("D11 decision probe entered the disabled LLM seam")


llm_stub.complete_system_user = fail_if_llm_called
sys.modules[f"{namespace}.llm"] = llm_stub
app = importlib.import_module(f"{namespace}.app")


def public_payload(result):
    if result.structured_content is not None:
        return result.structured_content
    return result.data


async def invoke(client, tool_name, arguments):
    try:
        result = await client.call_tool(tool_name, arguments)
    except ToolError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    if result.is_error:
        return {
            "ok": False,
            "error_type": "tool_result",
            "error": repr(public_payload(result)),
        }
    return {"ok": True, "result": public_payload(result)}


async def require_success(client, tool_name, arguments):
    result = await invoke(client, tool_name, arguments)
    if not result["ok"]:
        raise AssertionError(f"{tool_name} unexpectedly failed: {result!r}")
    return result["result"]


async def set_up_project(client, case_name):
    project_key = state_root / case_name / "project"
    project_key.mkdir(parents=True, exist_ok=True)
    project = str(project_key)
    await require_success(
        client,
        "ensure_project",
        {"human_key": project, "format": "json"},
    )
    for name, token in (
        ("GreenCastle", "d11-green-token"),
        ("BlueLake", "d11-blue-token"),
        ("RedStone", "d11-red-token"),
    ):
        await require_success(
            client,
            "register_agent",
            {
                "project_key": project,
                "program": "d11-hermetic-probe",
                "model": "fixture-model",
                "name": name,
                "task_description": "D11 deterministic retirement race",
                "registration_token": token,
                "format": "json",
            },
        )
    for name in ("BlueLake", "RedStone"):
        await require_success(
            client,
            "set_contact_policy",
            {
                "project_key": project,
                "agent_name": name,
                "policy": "open",
                "format": "json",
            },
        )
    return project


def snapshot(project_key):
    connection = sqlite3.connect(database_path)
    try:
        project_id, project_slug = connection.execute(
            "select id, slug from projects where human_key = ?", (project_key,)
        ).fetchone()
        retired = connection.execute(
            "select retired_at is not null from agents "
            "where project_id = ? and name = 'BlueLake'",
            (project_id,),
        ).fetchone()[0]
        reservations = connection.execute(
            "select a.name, r.path_pattern from file_reservations r "
            "join agents a on a.id = r.agent_id "
            "where r.project_id = ? and r.released_ts is null "
            "order by r.id",
            (project_id,),
        ).fetchall()
        recipients = connection.execute(
            "select a.name, mr.kind, m.subject from message_recipients mr "
            "join agents a on a.id = mr.agent_id "
            "join messages m on m.id = mr.message_id "
            "where m.project_id = ? order by mr.message_id, mr.agent_id",
            (project_id,),
        ).fetchall()
        message_count = connection.execute(
            "select count(*) from messages where project_id = ?", (project_id,)
        ).fetchone()[0]
    finally:
        connection.close()

    signals = []
    project_signal_root = signals_root / "projects" / project_slug
    for path in sorted(project_signal_root.rglob("*.signal")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("project") == project_slug
        signals.append(
            {
                "agent": payload.get("agent"),
                "message": payload.get("message"),
                "payload": payload,
            }
        )
    return {
        "retired": bool(retired),
        "active_reservations": [list(row) for row in reservations],
        "message_count": message_count,
        "recipients": [list(row) for row in recipients],
        "signals": signals,
    }


async def reservation_race(actor, controller, mode):
    project = await set_up_project(controller, mode)
    ready = asyncio.Event()
    release = asyncio.Event()
    original_create = app._create_file_reservation

    if mode == "reservation_retire_before_create":
        async def gated_create(*args, **kwargs):
            ready.set()
            await release.wait()
            return await original_create(*args, **kwargs)
    else:
        async def gated_create(*args, **kwargs):
            result = await original_create(*args, **kwargs)
            ready.set()
            await release.wait()
            return result

    app._create_file_reservation = gated_create
    reservation_task = asyncio.create_task(
        invoke(
            actor,
            "file_reservation_paths",
            {
                "project_key": project,
                "agent_name": "BlueLake",
                "paths": [f"race/{mode}.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": mode,
                "format": "json",
            },
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=15)
        retirement = await require_success(
            controller,
            "retire_agent",
            {
                "project_key": project,
                "agent_name": "BlueLake",
                "registration_token": "d11-blue-token",
            },
        )
    finally:
        release.set()
    reservation = await asyncio.wait_for(reservation_task, timeout=15)
    app._create_file_reservation = original_create
    return {
        "reservation": reservation,
        "retirement": retirement,
        "post_state": snapshot(project),
    }


async def send_race(actor, controller, mode):
    project = await set_up_project(controller, mode)
    ready = asyncio.Event()
    release = asyncio.Event()

    if mode == "send_retire_after_validation":
        original = app._create_message

        async def gated_create_message(*args, **kwargs):
            ready.set()
            await release.wait()
            return await original(*args, **kwargs)

        app._create_message = gated_create_message
        restore_name = "_create_message"
    else:
        original = app._get_agent

        async def gated_get_agent(project_record, name):
            if name == "GreenCastle":
                ready.set()
                await release.wait()
            return await original(project_record, name)

        app._get_agent = gated_get_agent
        restore_name = "_get_agent"

    send_task = asyncio.create_task(
        invoke(
            actor,
            "send_message",
            {
                "project_key": project,
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "bcc": ["RedStone"],
                "subject": mode,
                "body_md": "Hermetic D11 retirement race.",
                "importance": "high",
                "ack_required": True,
                "sender_token": "d11-green-token",
                "format": "json",
            },
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=15)
        retirement = await require_success(
            controller,
            "retire_agent",
            {
                "project_key": project,
                "agent_name": "BlueLake",
                "registration_token": "d11-blue-token",
            },
        )
    finally:
        release.set()
    sent = await asyncio.wait_for(send_task, timeout=15)
    setattr(app, restore_name, original)
    return {
        "send": sent,
        "retirement": retirement,
        "post_state": snapshot(project),
    }


async def main():
    server = app.build_mcp_server()
    results = {}
    async with Client(server) as actor, Client(server) as controller:
        for mode in (
            "reservation_retire_before_create",
            "reservation_create_before_retire",
        ):
            results[mode] = await reservation_race(actor, controller, mode)
        for mode in (
            "send_retire_after_validation",
            "send_retire_before_validation",
        ):
            results[mode] = await send_race(actor, controller, mode)
    print(json.dumps({"namespace": namespace, "cases": results}, sort_keys=True))


asyncio.run(main())
"""


@pytest.fixture(scope="module")
def frozen_live_checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("d11-d12-frozen-live"),
    )


@pytest.fixture(scope="module", params=(LIVE_NAMESPACE, CORE_NAMESPACE))
def mail_decision_probe(
    request: pytest.FixtureRequest,
    frozen_live_checkout: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    namespace = str(request.param)
    source = (
        frozen_live_checkout / "src" if namespace == LIVE_NAMESPACE else CORE_SOURCE
    )
    root = tmp_path_factory.mktemp(f"d11-{namespace}")
    roots = WorkerStateRoots.under(root, pythonpath=(source,))
    environment = isolated_worker_env(os.environ, namespace, roots)
    environment.update(
        {
            "DECISION_NAMESPACE": namespace,
            "DECISION_STATE_ROOT": str(root),
            "DECISION_DATABASE": str(roots.database),
            "DECISION_SIGNALS": str(roots.signals),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _D11_WORKER],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"{namespace} D11 worker failed ({completed.returncode}):\n"
            f"{(completed.stdout + completed.stderr)[-5000:]}",
            pytrace=False,
        )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, f"{namespace} D11 worker produced no output"
    result = json.loads(output_lines[-1])
    assert result["namespace"] == namespace
    return result


def test_d11_retirement_races_record_current_post_state(
    mail_decision_probe: Mapping[str, Any],
) -> None:
    cases = mail_decision_probe["cases"]

    for mode in (
        "reservation_retire_before_create",
        "reservation_create_before_retire",
    ):
        case = cases[mode]
        assert case["reservation"]["ok"] is True
        assert len(case["reservation"]["result"]["granted"]) == 1
        assert case["post_state"] == {
            "retired": True,
            "active_reservations": [["BlueLake", f"race/{mode}.py"]],
            "message_count": 0,
            "recipients": [],
            "signals": [],
        }

    after_validation = cases["send_retire_after_validation"]
    assert after_validation["send"]["ok"] is True
    assert after_validation["post_state"]["retired"] is True
    assert after_validation["post_state"]["active_reservations"] == []
    assert after_validation["post_state"]["message_count"] == 1
    assert after_validation["post_state"]["recipients"] == [
        ["BlueLake", "to", "send_retire_after_validation"],
        ["RedStone", "bcc", "send_retire_after_validation"],
    ]
    signals = after_validation["post_state"]["signals"]
    assert len(signals) == 1
    assert signals[0]["agent"] == "BlueLake"
    assert signals[0]["message"]["subject"] == "send_retire_after_validation"
    assert all(signal_record["agent"] != "RedStone" for signal_record in signals)

    before_validation = cases["send_retire_before_validation"]
    assert before_validation["send"]["ok"] is False
    assert "retired" in before_validation["send"]["error"].lower()
    assert before_validation["post_state"] == {
        "retired": True,
        "active_reservations": [],
        "message_count": 0,
        "recipients": [],
        "signals": [],
    }


_WATCHER_FUNCTIONS = (
    "state_should_attempt",
    "state_mark_result",
    "acquire_delivery_lease",
    "release_delivery_lease",
    "deliver_worker",
)


def _extract_shell_function(source: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(?:.*\n)*?^\}}\n",
        source,
        re.MULTILINE,
    )
    assert match is not None, f"watcher function {name} moved or was renamed"
    return match.group(0)


def _instrumented_watcher_functions() -> tuple[str, str]:
    source = WATCHER.read_text(encoding="utf-8")
    functions = {
        name: _extract_shell_function(source, name) for name in _WATCHER_FUNCTIONS
    }
    delivery = functions["deliver_worker"]
    success = '    state_mark_result "$agent_name" "$msg_key" "success" "watcher"'
    release = '    release_delivery_lease "$agent_name" "$msg_key"'
    unlink = '        rm -f "$signal_file" 2>/dev/null || true'
    assert delivery.count(success) == 1
    assert delivery.count(unlink) == 1
    success_at = delivery.index(success)
    release_at = delivery.index(release, success_at)
    assert success_at < release_at < delivery.index(unlink)
    delivery = delivery.replace(
        success,
        '    crash_if "after_external_injection"\n'
        f"{success}\n"
        '    crash_if "after_success_record"',
        1,
    )
    release_at = delivery.index(release, delivery.index(success))
    delivery = (
        delivery[:release_at]
        + release
        + '\n    crash_if "after_lease_release"'
        + delivery[release_at + len(release) :]
    )
    delivery = delivery.replace(
        unlink,
        '        crash_if "before_unlink"\n' + unlink,
        1,
    )
    functions["deliver_worker"] = delivery
    return "\n".join(functions.values()), source


def _watcher_shell(functions: str, *, deliver: bool) -> str:
    action = (
        'acquire_delivery_lease "$TEST_AGENT" "$TEST_MSG_KEY"\n'
        'deliver_worker "$TEST_SIGNAL" "$TEST_AGENT" "$TEST_MSG_KEY" '
        '"GreenCastle" "D11 race delivery" "high" "1"\n'
        if deliver
        else 'state_should_attempt "$TEST_AGENT" "$TEST_MSG_KEY"\n'
    )
    return f"""set -euo pipefail
STATE_FILE="$TEST_STATE_FILE"
LEASE_DIR="$TEST_LEASE_DIR"
RETRY_COOLDOWN=30
LEASE_TTL=120
TMUX_TIMEOUT=1
mkdir -p "$LEASE_DIR"
log() {{ :; }}
crash_if() {{
    if [[ "${{CRASH_POINT:-}}" == "$1" ]]; then
        kill -KILL "$$"
    fi
}}
run_to() {{
    local _timeout="$1"
    shift
    if [[ "$1" != "tmux" ]]; then
        return 97
    fi
    printf '%s\n' "$*" >> "$FAKE_COMMAND_LOG"
    case "$2" in
        has-session) return 0 ;;
        capture-pane) printf 'Claude ❯\n'; return 0 ;;
        send-keys) return 0 ;;
        *) return 98 ;;
    esac
}}
{functions}
{action}"""


def _run_watcher_state_machine(
    root: Path,
    signal_payload: Mapping[str, Any],
    crash_point: str,
    *,
    command_log: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    functions, _source = _instrumented_watcher_functions()
    agent = str(signal_payload["agent"])
    msg_key = str(signal_payload["message"]["id"])
    signal_path = root / "signals" / "agents" / agent / f"{msg_key}.signal"
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    if not signal_path.exists():
        signal_path.write_text(json.dumps(signal_payload), encoding="utf-8")
    state_file = root / "runtime" / "notify-state.json"
    lease_dir = root / "runtime" / "notify-locks"
    command_log = command_log or root / "fake-external-commands.log"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TEST_AGENT": agent,
        "TEST_MSG_KEY": msg_key,
        "TEST_SIGNAL": str(signal_path),
        "TEST_STATE_FILE": str(state_file),
        "TEST_LEASE_DIR": str(lease_dir),
        "FAKE_COMMAND_LOG": str(command_log),
        "CRASH_POINT": crash_point,
    }
    completed = subprocess.run(
        ["/bin/bash", "-c", _watcher_shell(functions, deliver=True)],
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return completed, signal_path, state_file, lease_dir / f"{agent}-{msg_key}.lock"


def _state_should_attempt(
    root: Path,
    signal_payload: Mapping[str, Any],
) -> subprocess.CompletedProcess[str]:
    functions, _source = _instrumented_watcher_functions()
    return subprocess.run(
        ["/bin/bash", "-c", _watcher_shell(functions, deliver=False)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TEST_AGENT": str(signal_payload["agent"]),
            "TEST_MSG_KEY": str(signal_payload["message"]["id"]),
            "TEST_SIGNAL": str(root / "unused.signal"),
            "TEST_STATE_FILE": str(root / "runtime" / "notify-state.json"),
            "TEST_LEASE_DIR": str(root / "runtime" / "notify-locks"),
            "FAKE_COMMAND_LOG": str(root / "unused.log"),
            "CRASH_POINT": "",
        },
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_d12_watcher_crash_windows_are_durable_and_hermetic(
    mail_decision_probe: Mapping[str, Any],
    tmp_path: Path,
) -> None:
    generated_signals = mail_decision_probe["cases"]["send_retire_after_validation"][
        "post_state"
    ]["signals"]
    assert len(generated_signals) == 1
    signal_payload = generated_signals[0]["payload"]
    assert signal_payload["agent"] == "BlueLake"

    expectations = {
        "after_external_injection": (False, True, True),
        "after_success_record": (True, True, True),
        "after_lease_release": (True, True, False),
        "before_unlink": (True, True, False),
        "": (True, False, False),
    }
    for crash_point, (has_success, has_signal, has_lease) in expectations.items():
        case_root = tmp_path / (crash_point or "normal")
        completed, signal_path, state_file, lease_path = _run_watcher_state_machine(
            case_root,
            signal_payload,
            crash_point,
        )
        if crash_point:
            assert completed.returncode == -signal.SIGKILL
        else:
            assert completed.returncode == 0, completed.stderr

        assert signal_path.exists() is has_signal
        assert lease_path.exists() is has_lease
        state = (
            json.loads(state_file.read_text(encoding="utf-8"))
            if state_file.exists()
            else {}
        )
        key = f"BlueLake:{signal_payload['message']['id']}"
        assert (state.get(key, {}).get("last_result") == "success") is has_success
        commands = (
            (case_root / "fake-external-commands.log")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert commands[-1].endswith("send-keys -t BlueLake C-m")
        assert sum("send-keys -t BlueLake C-m" in line for line in commands) == 1

        if crash_point == "after_external_injection":
            # Model lease expiry without waiting 120 seconds.  With no success
            # state, the exact watcher functions accept the same signal again,
            # demonstrating the documented at-least-once/duplicate window.
            (lease_path / "ts").write_text("0\n", encoding="utf-8")
            retried, retried_signal, _state, retried_lease = _run_watcher_state_machine(
                case_root,
                signal_payload,
                "",
                command_log=case_root / "fake-external-commands.log",
            )
            assert retried.returncode == 0, retried.stderr
            assert not retried_signal.exists()
            assert not retried_lease.exists()
            commands = (
                (case_root / "fake-external-commands.log")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            assert sum("send-keys -t BlueLake C-m" in line for line in commands) == 2
        elif crash_point:
            # Once success is durable, state_should_attempt permanently skips
            # the still-present signal.  This is the stale-file crash window.
            skipped = _state_should_attempt(case_root, signal_payload)
            assert skipped.returncode == 1
            assert signal_path.exists()


def test_d12_source_order_exposes_only_the_external_application_seam() -> None:
    """The fake-command return is observable; external application is not.

    No local state can prove whether tmux applied the submitted bytes immediately
    before a process died.  The executable crash test therefore stops at the
    strongest non-invasive boundary: successful return from the exact ``run_to``
    call, followed by the production success/lease/unlink ordering asserted here.
    """

    source = WATCHER.read_text(encoding="utf-8")
    delivery = _extract_shell_function(source, "deliver_worker")
    submit = delivery.index('tmux send-keys -t "$session_name" C-m')
    success = delivery.index(
        'state_mark_result "$agent_name" "$msg_key" "success" "watcher"'
    )
    release = delivery.index('release_delivery_lease "$agent_name" "$msg_key"', success)
    unlink = delivery.index('rm -f "$signal_file"', release)
    assert submit < success < release < unlink
