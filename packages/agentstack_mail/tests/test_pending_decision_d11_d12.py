"""Hermetic executable evidence for selected D11 and pending D12.

The D11 probes encode the selected upstream-parity requirement. The D12 probes
still freeze observed behavior as evidence for a pending decision, not as an
assertion that the behavior is correct.

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
source_root = Path(os.environ["DECISION_SOURCE_ROOT"]).resolve(strict=True)

# The tested paths do not use an LLM.  Installing the same fail-closed stub as
# the main differential worker lets frozen live import without the optional
# legacy LLM dependency and fails if a scenario accidentally crosses that seam.
llm_stub = types.ModuleType(f"{namespace}.llm")


async def fail_if_llm_called(*_args, **_kwargs):
    raise AssertionError("D11 decision probe entered the disabled LLM seam")


llm_stub.complete_system_user = fail_if_llm_called
sys.modules[f"{namespace}.llm"] = llm_stub
app = importlib.import_module(f"{namespace}.app")
db = importlib.import_module(f"{namespace}.db")
storage = importlib.import_module(f"{namespace}.storage")
for module in (app, db, storage):
    Path(module.__file__).resolve(strict=True).relative_to(source_root)


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
        project_id, _project_slug = connection.execute(
            "select id, slug from projects where human_key = ?", (project_key,)
        ).fetchone()
        retired = connection.execute(
            "select retired_at is not null from agents "
            "where project_id = ? and name = 'BlueLake'",
            (project_id,),
        ).fetchone()[0]
        reservations = connection.execute(
            "select a.name, r.path_pattern, r.exclusive from file_reservations r "
            "join agents a on a.id = r.agent_id "
            "where r.project_id = ? and r.released_ts is null "
            "and datetime(r.expires_ts) > CURRENT_TIMESTAMP "
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
        receipts = connection.execute(
            "select a.name, mr.kind, m.subject, m.ack_required, "
            "mr.read_ts is not null, mr.ack_ts is not null "
            "from message_recipients mr "
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

    return {
        "retired": bool(retired),
        "active_reservations": [
            [name, path_pattern, bool(exclusive)]
            for name, path_pattern, exclusive in reservations
        ],
        "message_count": message_count,
        "recipients": [list(row) for row in recipients],
        "receipts": [
            [name, kind, subject, bool(required), bool(read), bool(ack)]
            for name, kind, subject, required, read, ack in receipts
        ],
    }


async def pending_state_retirement(actor, controller):
    project = await set_up_project(controller, "pending_state_retirement")
    reservation = await require_success(
        actor,
        "file_reservation_paths",
        {
            "project_key": project,
            "agent_name": "BlueLake",
            "paths": ["pending/owned.py"],
            "ttl_seconds": 3600,
            "exclusive": True,
            "reason": "D11 selected pending-state retention",
            "format": "json",
        },
    )
    sent = []
    for subject, ack_required in (
        ("D11 pending normal", False),
        ("D11 pending acknowledgement", True),
    ):
        sent.append(
            await require_success(
                actor,
                "send_message",
                {
                    "project_key": project,
                    "sender_name": "GreenCastle",
                    "to": ["BlueLake"],
                    "subject": subject,
                    "body_md": "Hermetic D11 pending-state retirement.",
                    "importance": "high",
                    "ack_required": ack_required,
                    "sender_token": "d11-green-token",
                    "format": "json",
                },
            )
        )
    before_retire = snapshot(project)
    retirement = await require_success(
        controller,
        "retire_agent",
        {
            "project_key": project,
            "agent_name": "BlueLake",
            "registration_token": "d11-blue-token",
        },
    )
    after_retire = snapshot(project)
    rejected_send = await invoke(
        actor,
        "send_message",
        {
            "project_key": project,
            "sender_name": "GreenCastle",
            "to": ["BlueLake"],
            "subject": "D11 rejected after retirement",
            "body_md": "This delivery must not commit.",
            "sender_token": "d11-green-token",
            "format": "json",
        },
    )
    after_rejected_send = snapshot(project)
    fetched = await require_success(
        actor,
        "fetch_inbox",
        {
            "project_key": project,
            "agent_name": "BlueLake",
            "limit": 1,
            "include_bodies": True,
            "format": "json",
        },
    )
    after_fetch = snapshot(project)
    acknowledged = await require_success(
        actor,
        "acknowledge_message",
        {
            "project_key": project,
            "agent_name": "BlueLake",
            "message_id": sent[-1]["deliveries"][0]["payload"]["id"],
            "format": "json",
        },
    )
    after_acknowledge = snapshot(project)
    return {
        "reservation": reservation,
        "sent": sent,
        "retirement": retirement,
        "rejected_send": rejected_send,
        "fetched": fetched,
        "acknowledged": acknowledged,
        "before_retire": before_retire,
        "after_retire": after_retire,
        "after_rejected_send": after_rejected_send,
        "after_fetch": after_fetch,
        "after_acknowledge": after_acknowledge,
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
    try:
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
        release.set()
        reservation = await asyncio.wait_for(reservation_task, timeout=15)
    finally:
        release.set()
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

    try:
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
        release.set()
        sent = await asyncio.wait_for(send_task, timeout=15)
    finally:
        release.set()
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
        results["pending_state_retirement"] = await pending_state_retirement(
            actor,
            controller,
        )
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


def _run_d11_probe(
    namespace: str,
    frozen_live_checkout: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
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
            "DECISION_SOURCE_ROOT": str(source.resolve()),
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
    transcript = completed.stdout + completed.stderr
    leaked_tokens = [
        token
        for token in ("d11-green-token", "d11-blue-token", "d11-red-token")
        if token in transcript
    ]
    if leaked_tokens:
        pytest.fail(
            f"{namespace} D11 worker leaked a fake registration token",
            pytrace=False,
        )
    if completed.returncode != 0:
        pytest.fail(
            f"{namespace} D11 worker failed ({completed.returncode}):\n"
            f"{transcript[-5000:]}",
            pytrace=False,
        )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, f"{namespace} D11 worker produced no output"
    result = json.loads(output_lines[-1])
    assert result["namespace"] == namespace
    return result


@pytest.fixture(scope="module")
def mail_decision_probes(
    frozen_live_checkout: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, Any]]:
    return {
        namespace: _run_d11_probe(
            namespace,
            frozen_live_checkout,
            tmp_path_factory,
        )
        for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE)
    }


def _pending_state_projection(probe: Mapping[str, Any]) -> dict[str, Any]:
    case = probe["cases"]["pending_state_retirement"]
    before = case["before_retire"]
    after = case["after_retire"]
    after_fetch = case["after_fetch"]
    after_acknowledge = case["after_acknowledge"]
    durable_keys = ("active_reservations", "message_count", "recipients", "receipts")
    seed_receipts = [
        [kind, required, read, ack]
        for _name, kind, _subject, required, read, ack in before["receipts"]
    ]
    acknowledged_receipts = [
        [kind, required, read, ack]
        for _name, kind, _subject, required, read, ack in after_acknowledge[
            "receipts"
        ]
    ]
    return {
        "reservation_grants": len(case["reservation"]["granted"]),
        "successful_seed_sends": len(case["sent"]),
        "before_retired": before["retired"],
        "active_reservation_count": len(before["active_reservations"]),
        "active_reservations_exclusive": all(
            row[2] for row in before["active_reservations"]
        ),
        "seed_message_count": before["message_count"],
        "seed_receipts": seed_receipts,
        "after_retired": after["retired"],
        "retire_preserved_durable_pending_state": all(
            after[key] == before[key] for key in durable_keys
        ),
        "new_send_rejected": case["rejected_send"]["ok"] is False,
        "new_send_retired_diagnostic": (
            "retired" in case["rejected_send"]["error"].lower()
        ),
        "rejected_send_zero_d11_state_delta": (
            case["after_rejected_send"]["retired"] == after["retired"]
            and all(
                case["after_rejected_send"][key] == after[key]
                for key in durable_keys
            )
        ),
        "retired_fetch_count": len(case["fetched"]["result"]),
        "fetch_preserved_pending_state": all(
            after_fetch[key] == after[key] for key in durable_keys
        ),
        "after_fetch_retired": after_fetch["retired"],
        "retired_acknowledge_succeeded": bool(case["acknowledged"]["acknowledged"]),
        "retired_acknowledge_receipts": acknowledged_receipts,
        "acknowledge_preserved_reservation": (
            after_acknowledge["active_reservations"]
            == after_fetch["active_reservations"]
        ),
        "after_acknowledge_retired": after_acknowledge["retired"],
    }


def _retirement_race_projection(probe: Mapping[str, Any]) -> dict[str, Any]:
    cases = probe["cases"]
    reservations = {}
    for mode in (
        "reservation_retire_before_create",
        "reservation_create_before_retire",
    ):
        case = cases[mode]
        reservations[mode] = {
            "ok": case["reservation"]["ok"],
            "grant_count": len(case["reservation"]["result"]["granted"]),
            "retired": case["post_state"]["retired"],
            "active_reservation_count": len(
                case["post_state"]["active_reservations"]
            ),
            "active_reservations_exclusive": all(
                row[2] for row in case["post_state"]["active_reservations"]
            ),
            "message_count": case["post_state"]["message_count"],
            "recipient_count": len(case["post_state"]["recipients"]),
        }
    after_validation = cases["send_retire_after_validation"]
    before_validation = cases["send_retire_before_validation"]
    return {
        "reservations": reservations,
        "send_after_validation": {
            "ok": after_validation["send"]["ok"],
            "retired": after_validation["post_state"]["retired"],
            "active_reservation_count": len(
                after_validation["post_state"]["active_reservations"]
            ),
            "message_count": after_validation["post_state"]["message_count"],
            "recipient_kinds": sorted(
                row[1] for row in after_validation["post_state"]["recipients"]
            ),
            "receipt_states": sorted(
                [kind, required, read, ack]
                for _name, kind, _subject, required, read, ack in after_validation[
                    "post_state"
                ]["receipts"]
            ),
        },
        "send_before_validation": {
            "ok": before_validation["send"]["ok"],
            "retired_diagnostic": (
                "retired" in before_validation["send"]["error"].lower()
            ),
            "retired": before_validation["post_state"]["retired"],
            "active_reservation_count": len(
                before_validation["post_state"]["active_reservations"]
            ),
            "message_count": before_validation["post_state"]["message_count"],
            "recipient_count": len(before_validation["post_state"]["recipients"]),
            "receipt_count": len(before_validation["post_state"]["receipts"]),
        },
    }


def test_d11_selected_parity_preserves_pending_state_and_retired_fetch(
    mail_decision_probes: Mapping[str, dict[str, Any]],
) -> None:
    expected = {
        "reservation_grants": 1,
        "successful_seed_sends": 2,
        "before_retired": False,
        "active_reservation_count": 1,
        "active_reservations_exclusive": True,
        "seed_message_count": 2,
        "seed_receipts": [
            ["to", False, False, False],
            ["to", True, False, False],
        ],
        "after_retired": True,
        "retire_preserved_durable_pending_state": True,
        "new_send_rejected": True,
        "new_send_retired_diagnostic": True,
        "rejected_send_zero_d11_state_delta": True,
        "retired_fetch_count": 1,
        "fetch_preserved_pending_state": True,
        "after_fetch_retired": True,
        "retired_acknowledge_succeeded": True,
        "retired_acknowledge_receipts": [
            ["to", False, False, False],
            ["to", True, True, True],
        ],
        "acknowledge_preserved_reservation": True,
        "after_acknowledge_retired": True,
    }
    live = _pending_state_projection(mail_decision_probes[LIVE_NAMESPACE])
    core = _pending_state_projection(mail_decision_probes[CORE_NAMESPACE])
    assert live == expected
    assert core == expected
    assert core == live


def test_d11_selected_parity_retirement_races_keep_upstream_boundaries(
    mail_decision_probes: Mapping[str, dict[str, Any]],
) -> None:
    expected = {
        "reservations": {
            mode: {
                "ok": True,
                "grant_count": 1,
                "retired": True,
                "active_reservation_count": 1,
                "active_reservations_exclusive": True,
                "message_count": 0,
                "recipient_count": 0,
            }
            for mode in (
                "reservation_retire_before_create",
                "reservation_create_before_retire",
            )
        },
        "send_after_validation": {
            "ok": True,
            "retired": True,
            "active_reservation_count": 0,
            "message_count": 1,
            "recipient_kinds": ["bcc", "to"],
            "receipt_states": [
                ["bcc", True, False, False],
                ["to", True, False, False],
            ],
        },
        "send_before_validation": {
            "ok": False,
            "retired_diagnostic": True,
            "retired": True,
            "active_reservation_count": 0,
            "message_count": 0,
            "recipient_count": 0,
            "receipt_count": 0,
        },
    }
    live = _retirement_race_projection(mail_decision_probes[LIVE_NAMESPACE])
    core = _retirement_race_projection(mail_decision_probes[CORE_NAMESPACE])
    assert live == expected
    assert core == expected
    assert core == live


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
        '"GreenCastle" "D12 completion delivery" "high" "1"\n'
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
    tmp_path: Path,
) -> None:
    signal_payload = {
        "project": "d12-hermetic-project",
        "agent": "BlueLake",
        "message": {
            "id": 12001,
            "subject": "D12 watcher completion",
            "importance": "high",
        },
    }

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
