"""Hermetic frozen-live versus Core evidence for pending decision D5.

These probes record today's coercion behavior; they do not select whether an
invalid contact policy should keep coercing or become an error.  Frozen live
and Core execute the exact same worker in fresh interpreters with private
databases, archives, signal directories, and homes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest
from differential_source import (
    CORE_NAMESPACE,
    LIVE_NAMESPACE,
    WorkerStateRoots,
    isolated_worker_env,
    reconstruct_live,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CORE_SOURCE = PACKAGE_ROOT / "src"


_WORKER_SOURCE = r"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import types
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


namespace = os.environ["D5_NAMESPACE"]
database = Path(os.environ["D5_DATABASE"])
storage = Path(os.environ["D5_STORAGE"])
signals = Path(os.environ["D5_SIGNALS"])
project_key = os.environ["D5_PROJECT_KEY"]
output = Path(os.environ["D5_OUTPUT"])


def install_llm_stub() -> None:
    module_name = f"{namespace}.llm"
    stub = types.ModuleType(module_name)

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("D5 probe entered the disabled LLM seam")

    stub.complete_system_user = fail_if_called
    sys.modules[module_name] = stub


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return jsonable(model_dump(mode="json", by_alias=True, exclude_none=True))
    return repr(value)


def public_payload(result: Any) -> Any:
    value = result.structured_content
    if value is None:
        value = result.data
    value = jsonable(value)
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value


async def capture(client: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await client.call_tool(tool_name, arguments)
    except BaseException as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "payload": None,
        }
    return {
        "ok": not result.is_error,
        "error_type": None if not result.is_error else "tool_result",
        "error": None if not result.is_error else repr(public_payload(result)),
        "payload": public_payload(result),
    }


async def require_success(
    client: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    result = await capture(client, tool_name, arguments)
    if not result["ok"]:
        raise AssertionError(f"{tool_name} unexpectedly failed: {result!r}")
    return result["payload"]


def database_state() -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        project_id = connection.execute(
            "select id from projects where human_key = ?", (project_key,)
        ).fetchone()[0]
        counts = {}
        for table in (
            "projects",
            "agents",
            "agent_links",
            "messages",
            "message_recipients",
            "file_reservations",
        ):
            counts[table] = connection.execute(
                f'select count(*) from "{table}"'
            ).fetchone()[0]
        agents = connection.execute(
            "select name, program, model, task_description, attachments_policy, "
            "contact_policy, retired_at is not null from agents "
            "where project_id = ? order by name",
            (project_id,),
        ).fetchall()
        links = connection.execute(
            "select source.name, target.name, links.status, links.reason, "
            "links.expires_ts is not null from agent_links links "
            "join agents source on source.id = links.a_agent_id "
            "join agents target on target.id = links.b_agent_id "
            "where links.a_project_id = ? order by source.name, target.name",
            (project_id,),
        ).fetchall()
        messages = connection.execute(
            "select sender.name, messages.subject, messages.body_md, "
            "messages.importance, messages.ack_required "
            "from messages join agents sender on sender.id = messages.sender_id "
            "where messages.project_id = ? order by messages.id",
            (project_id,),
        ).fetchall()
        recipients = connection.execute(
            "select messages.subject, recipient.name, mr.kind, "
            "mr.read_ts is not null, mr.ack_ts is not null "
            "from message_recipients mr "
            "join messages on messages.id = mr.message_id "
            "join agents recipient on recipient.id = mr.agent_id "
            "where messages.project_id = ? order by mr.message_id, mr.agent_id",
            (project_id,),
        ).fetchall()
        reservations = connection.execute(
            "select owner.name, reservations.path_pattern, reservations.exclusive, "
            "reservations.released_ts is not null "
            "from file_reservations reservations "
            "join agents owner on owner.id = reservations.agent_id "
            "where reservations.project_id = ? order by reservations.id",
            (project_id,),
        ).fetchall()
        return {
            "counts": counts,
            "agents": [list(row) for row in agents],
            "links": [list(row) for row in links],
            "messages": [list(row) for row in messages],
            "recipients": [list(row) for row in recipients],
            "reservations": [list(row) for row in reservations],
        }
    finally:
        connection.close()


def tree_state(root: Path, *, exclude_git: bool = False) -> dict[str, str]:
    if not root.is_dir():
        return {}
    state = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if exclude_git and ".git" in relative.parts:
            continue
        if path.name == ".archive.lock" or path.name.endswith(".lock"):
            continue
        state[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def git_state() -> dict[str, Any]:
    def run(*arguments: str) -> str:
        environment = dict(os.environ)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        completed = subprocess.run(
            ["git", "-C", str(storage), *arguments],
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    return {
        "commit_count": int(run("rev-list", "--count", "HEAD")),
        "status": run("status", "--porcelain=v1"),
    }


def durable_state() -> dict[str, Any]:
    return {
        "database": database_state(),
        "archive": tree_state(storage, exclude_git=True),
        "git": git_state(),
        "signals": tree_state(signals),
    }


def contact_policy(state: dict[str, Any], name: str = "BlueLake") -> str:
    matches = [row for row in state["database"]["agents"] if row[0] == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name} row: {matches!r}")
    return matches[0][5]


install_llm_stub()
app = importlib.import_module(f"{namespace}.app")


async def main() -> None:
    from fastmcp import Client

    server = app.build_mcp_server()
    async with Client(server) as client:
        await require_success(
            client,
            "ensure_project",
            {"human_key": project_key, "format": "json"},
        )
        for name, token in (
            ("GreenCastle", "d5-green-owner-token"),
            ("BlueLake", "d5-blue-owner-token"),
        ):
            await require_success(
                client,
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "pending-decision-d5-probe",
                    "model": "fixture-model",
                    "name": name,
                    "task_description": "D5 invalid contact policy coercion",
                    "registration_token": token,
                    "format": "json",
                },
            )

        await require_success(
            client,
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "open",
                "format": "json",
            },
        )
        await require_success(
            client,
            "send_message",
            {
                "project_key": project_key,
                "sender_name": "GreenCastle",
                "sender_token": "d5-green-owner-token",
                "to": ["BlueLake"],
                "subject": "D5 prior contact seed",
                "body_md": "Establish a recent-contact record before the policy controls.",
                "format": "json",
            },
        )
        await require_success(
            client,
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "contacts_only",
                "format": "json",
            },
        )
        contacts_only_delivery = await capture(
            client,
            "send_message",
            {
                "project_key": project_key,
                "sender_name": "GreenCastle",
                "sender_token": "d5-green-owner-token",
                "to": ["BlueLake"],
                "subject": "D5 contacts-only control",
                "body_md": "This must be blocked without an approved link.",
                "auto_contact_if_blocked": False,
                "format": "json",
            },
        )

        invalid_before = durable_state()
        invalid_result = await capture(
            client,
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "not-a-policy",
                "format": "json",
            },
        )
        invalid_after = durable_state()
        invalid_delivery = await capture(
            client,
            "send_message",
            {
                "project_key": project_key,
                "sender_name": "GreenCastle",
                "sender_token": "d5-green-owner-token",
                "to": ["BlueLake"],
                "subject": "D5 invalid-coercion follow-up",
                "body_md": "The recent contact is accepted after coercion to auto.",
                "auto_contact_if_blocked": False,
                "format": "json",
            },
        )
        invalid_delivery_after = durable_state()

        await require_success(
            client,
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "contacts_only",
                "format": "json",
            },
        )
        empty_before = durable_state()
        empty_result = await capture(
            client,
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "",
                "format": "json",
            },
        )
        empty_after = durable_state()

    payload = {
        "namespace": namespace,
        "contacts_only_delivery": contacts_only_delivery,
        "invalid": {
            "result": invalid_result,
            "policy_before": contact_policy(invalid_before),
            "policy_after": contact_policy(invalid_after),
            "before": invalid_before,
            "after": invalid_after,
        },
        "invalid_delivery": {
            "result": invalid_delivery,
            "after": invalid_delivery_after,
        },
        "empty": {
            "result": empty_result,
            "policy_before": contact_policy(empty_before),
            "policy_after": contact_policy(empty_after),
            "before": empty_before,
            "after": empty_after,
        },
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(payload, destination, ensure_ascii=False, indent=2, sort_keys=True)
        destination.write("\n")


asyncio.run(main())
"""


@pytest.fixture(scope="session")
def frozen_live_checkout_d5(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-d5-frozen-live"),
    )


def _source_for(namespace: str, frozen_live_checkout: Path) -> Path:
    if namespace == LIVE_NAMESPACE:
        return frozen_live_checkout / "src"
    if namespace == CORE_NAMESPACE:
        return CORE_SOURCE
    raise AssertionError(f"unsupported namespace: {namespace}")


def _run_worker(
    *,
    namespace: str,
    root: Path,
    project_key: Path,
    frozen_live_checkout: Path,
) -> dict[str, Any]:
    source = _source_for(namespace, frozen_live_checkout).resolve()
    roots = WorkerStateRoots.under(root, pythonpath=(source,))
    environment = isolated_worker_env(os.environ, namespace, roots)
    output = root / "d5-output.json"
    environment.update(
        {
            "D5_NAMESPACE": namespace,
            "D5_DATABASE": str(roots.database),
            "D5_STORAGE": str(roots.storage),
            "D5_SIGNALS": str(roots.signals),
            "D5_PROJECT_KEY": str(project_key),
            "D5_OUTPUT": str(output),
        }
    )
    if namespace == LIVE_NAMESPACE:
        environment["MESSAGING_AUTO_HANDSHAKE_ON_BLOCK"] = "false"
    else:
        environment["AGENTSTACK_MAIL_MESSAGING_AUTO_HANDSHAKE_ON_BLOCK"] = "false"

    completed = subprocess.run(
        [sys.executable, "-c", _WORKER_SOURCE],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        transcript = (completed.stdout + completed.stderr)[-6000:]
        pytest.fail(
            f"{namespace} D5 worker exited {completed.returncode}:\n{transcript}",
            pytrace=False,
        )
    assert output.is_file()
    assert output.stat().st_mode & 0o077 == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["namespace"] == namespace
    return payload


def _expected_policy_only_change(
    before: Mapping[str, Any],
    *,
    policy: str,
) -> dict[str, Any]:
    expected = deepcopy(before)
    matches = [
        row for row in expected["database"]["agents"] if row[0] == "BlueLake"
    ]
    assert len(matches) == 1
    matches[0][5] = policy
    return expected


def _stable_measurement(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contacts_only_delivery": payload["contacts_only_delivery"],
        "invalid_result": payload["invalid"]["result"],
        "invalid_policy_before": payload["invalid"]["policy_before"],
        "invalid_policy_after": payload["invalid"]["policy_after"],
        "invalid_db_after": payload["invalid"]["after"]["database"],
        "invalid_delivery_ok": payload["invalid_delivery"]["result"]["ok"],
        "invalid_delivery_db_after": payload["invalid_delivery"]["after"]["database"],
        "empty_result": payload["empty"]["result"],
        "empty_policy_before": payload["empty"]["policy_before"],
        "empty_policy_after": payload["empty"]["policy_after"],
        "empty_db_after": payload["empty"]["after"]["database"],
    }


def test_d5_invalid_and_empty_policy_coerce_to_auto_with_matching_side_effects(
    frozen_live_checkout_d5: Path,
    tmp_path: Path,
) -> None:
    project_key = (tmp_path / "project").resolve()
    project_key.mkdir()
    results = {
        namespace: _run_worker(
            namespace=namespace,
            root=tmp_path / namespace,
            project_key=project_key,
            frozen_live_checkout=frozen_live_checkout_d5,
        )
        for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE)
    }

    for namespace, payload in results.items():
        contacts_only = payload["contacts_only_delivery"]
        assert contacts_only["ok"] is False, namespace
        assert "Contact approval required" in contacts_only["error"], namespace

        for case_name in ("invalid", "empty"):
            case = payload[case_name]
            assert case["result"] == {
                "ok": True,
                "error_type": None,
                "error": None,
                "payload": {"agent": "BlueLake", "policy": "auto"},
            }, (namespace, case_name)
            assert case["policy_before"] == "contacts_only", (namespace, case_name)
            assert case["policy_after"] == "auto", (namespace, case_name)
            assert case["after"] == _expected_policy_only_change(
                case["before"], policy="auto"
            ), (namespace, case_name)

        invalid_delivery = payload["invalid_delivery"]
        assert invalid_delivery["result"]["ok"] is True, namespace
        database = invalid_delivery["after"]["database"]
        assert database["counts"]["messages"] == 2, namespace
        assert database["counts"]["message_recipients"] == 2, namespace
        assert [row[1] for row in database["messages"]] == [
            "D5 prior contact seed",
            "D5 invalid-coercion follow-up",
        ], namespace
        assert database["links"] == [], namespace

    live_measurement = _stable_measurement(results[LIVE_NAMESPACE])
    core_measurement = _stable_measurement(results[CORE_NAMESPACE])
    assert core_measurement == live_measurement
