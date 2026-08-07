"""Selected upstream-parity requirement for product decision D3.

Path A preserves the measured frozen-live cross-project identity topology:
contact intros carry a source-project sender row, approved sends create a
target-local null-token alias, and replies after a process restart reach that
alias rather than the source identity.  The selected comparator projects only
those D3 effects; contact expiry, no-pending response, policy coercion, and
sender-token omission remain outside this requirement.

Each namespace runs in a private database/archive/signal root, imports from an
authenticated selected source, scans raw MCP result channels for credential
canaries, and starts phase two in a fresh Python process.
"""

from __future__ import annotations

import json
import os
import secrets
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
CORE_SOURCE = PACKAGE_ROOT / "src"


_D3_WORKER = r'''
import asyncio
import importlib
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

from fastmcp import Client
namespace = os.environ["DECISION_NAMESPACE"]
phase = os.environ["D3_PHASE"]
state_root = Path(os.environ["DECISION_STATE_ROOT"])
database_path = Path(os.environ["DECISION_DATABASE"])
source_root = Path(os.environ["DECISION_SOURCE_ROOT"]).resolve(strict=True)
caller_tokens = json.loads(os.environ["D3_CALLER_TOKENS"])
project_a_path = state_root / "project-a"
project_b_path = state_root / "project-b"
project_a_path.mkdir(parents=True, exist_ok=True)
project_b_path.mkdir(parents=True, exist_ok=True)
project_a = str(project_a_path.resolve())
project_b = str(project_b_path.resolve())

llm_stub = types.ModuleType(f"{namespace}.llm")


async def fail_if_llm_called(*_args, **_kwargs):
    raise AssertionError("D3 decision probe entered the disabled LLM seam")


llm_stub.complete_system_user = fail_if_llm_called
sys.modules[f"{namespace}.llm"] = llm_stub
app = importlib.import_module(f"{namespace}.app")
imported_app_path = Path(app.__file__).resolve(strict=True)
assert imported_app_path.is_relative_to(source_root), (
    "D3 worker imported its app outside the authenticated selected source"
)


def public_payload(result):
    value = result.structuredContent
    if value is None:
        blocks = result.model_dump(mode="json", by_alias=True)["content"]
        if (
            isinstance(blocks, list)
            and len(blocks) == 1
            and isinstance(blocks[0], dict)
            and isinstance(blocks[0].get("text"), str)
        ):
            try:
                value = json.loads(blocks[0]["text"])
            except json.JSONDecodeError:
                value = blocks
        else:
            value = blocks
    return value


def assert_no_caller_credentials(result):
    channels = result.model_dump(mode="json", by_alias=True)
    serialized = json.dumps(channels, sort_keys=True, ensure_ascii=False)
    if any(token in serialized for token in caller_tokens.values()):
        raise AssertionError("D3 tool result leaked a caller credential")

    allowed_redactions = {None, "", "***", "<redacted>", "[REDACTED]"}

    def inspect(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).strip().lower().lstrip("_")
                if (
                    normalized.endswith("token")
                    or normalized.endswith("secret")
                    or normalized.endswith("credential")
                    or normalized in {"authorization", "api_key", "apikey"}
                ) and nested not in allowed_redactions:
                    raise AssertionError(
                        "D3 tool result exposed a credential-bearing field"
                    )
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(channels)


def unwrap(value):
    while isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value


def project_label(value):
    if value == project_a:
        return "A"
    if value == project_b:
        return "B"
    return value


def normalize_error(value):
    return str(value).replace(project_a, "<PROJECT_A>").replace(
        project_b, "<PROJECT_B>"
    )


async def invoke(client, tool_name, arguments):
    result = await client.call_tool_mcp(tool_name, arguments)
    assert_no_caller_credentials(result)
    if result.isError:
        return {
            "ok": False,
            "error_type": "tool_result",
            "error": normalize_error(repr(public_payload(result))),
        }
    return {"ok": True, "result": unwrap(public_payload(result))}


async def require_success(client, tool_name, arguments):
    result = await invoke(client, tool_name, arguments)
    if not result["ok"]:
        raise AssertionError(f"{tool_name} unexpectedly failed: {result!r}")
    return result["result"]


def select_message(item):
    return {
        key: item.get(key)
        for key in (
            "id",
            "project_id",
            "sender_id",
            "thread_id",
            "topic",
            "subject",
            "importance",
            "ack_required",
            "from",
            "to",
            "cc",
            "bcc",
            "body_md",
        )
        if key in item
    }


def select_inbox(value):
    value = unwrap(value)
    if not isinstance(value, list):
        raise AssertionError(f"fetch_inbox response is not a list: {value!r}")
    return [select_message(item) for item in value]


def select_delivery(value):
    value = unwrap(value)
    deliveries = []
    for delivery in value.get("deliveries", []):
        deliveries.append(
            {
                "project": project_label(delivery.get("project")),
                "payload": select_message(delivery.get("payload") or {}),
            }
        )
    selected = {
        "count": value.get("count"),
        "deliveries": deliveries,
    }
    if "verified_sender" in value:
        selected["verified_sender"] = value.get("verified_sender")
    if "reply_to" in value:
        selected["reply_to"] = value.get("reply_to")
    if "thread_id" in value:
        selected["thread_id"] = value.get("thread_id")
    return selected


def project_rows(connection):
    return {
        row[1]: {"id": row[0], "slug": row[2]}
        for row in connection.execute(
            "select id, human_key, slug from projects order by id"
        )
    }


def database_snapshot():
    connection = sqlite3.connect(database_path)
    try:
        projects = project_rows(connection)
        labels = {project_a: "A", project_b: "B"}
        ids_to_labels = {
            record["id"]: labels[path] for path, record in projects.items()
        }
        agents = [
            {
                "id": row[0],
                "project": ids_to_labels[row[1]],
                "name": row[2],
                "registration_token_is_null": row[3] is None,
                "program": row[4],
                "model": row[5],
                "task_description": row[6],
            }
            for row in connection.execute(
                "select id, project_id, name, registration_token, program, "
                "model, task_description "
                "from agents order by id"
            )
        ]
        agent_projects = {row["id"]: row["project"] for row in agents}
        messages = [
            {
                "id": row[0],
                "project": ids_to_labels[row[1]],
                "sender_id": row[2],
                "sender_project": agent_projects[row[2]],
                "thread_id": row[3],
                "topic": row[4],
                "subject": row[5],
                "body_md": row[6],
                "importance": row[7],
                "ack_required": bool(row[8]),
            }
            for row in connection.execute(
                "select id, project_id, sender_id, thread_id, topic, subject, "
                "body_md, importance, ack_required from messages order by id"
            )
        ]
        agent_names = {row["id"]: row["name"] for row in agents}
        recipients = [
            {
                "message_id": row[0],
                "agent_id": row[1],
                "agent_project": agent_projects[row[1]],
                "agent_name": agent_names[row[1]],
                "kind": row[2],
            }
            for row in connection.execute(
                "select message_id, agent_id, kind from message_recipients "
                "order by message_id, agent_id"
            )
        ]
    finally:
        connection.close()
    return {
        "agents": agents,
        "messages": messages,
        "recipients": recipients,
    }


async def phase_one():
    server = app.build_mcp_server()
    async with Client(server) as client:
        for project in (project_a, project_b):
            await require_success(
                client,
                "ensure_project",
                {"human_key": project, "format": "json"},
            )
        await require_success(
            client,
            "register_agent",
            {
                "project_key": project_a,
                "program": "d3-hermetic-probe",
                "model": "fixture-model",
                "name": "GreenCastle",
                "task_description": "D3 source identity",
                "registration_token": caller_tokens["GreenCastle"],
                "format": "json",
            },
        )
        await require_success(
            client,
            "register_agent",
            {
                "project_key": project_b,
                "program": "d3-hermetic-probe",
                "model": "fixture-model",
                "name": "BlueLake",
                "task_description": "D3 target identity",
                "registration_token": caller_tokens["BlueLake"],
                "format": "json",
            },
        )
        await require_success(
            client,
            "request_contact",
            {
                "project_key": project_a,
                "from_agent": "GreenCastle",
                "to_agent": "BlueLake",
                "to_project": project_b,
                "reason": "D3 cross-project contact",
                "register_if_missing": False,
                "ttl_seconds": 604800,
                "format": "json",
            },
        )
        blue_intro_inbox = await require_success(
            client,
            "fetch_inbox",
            {
                "project_key": project_b,
                "agent_name": "BlueLake",
                "include_bodies": True,
                "limit": 20,
                "format": "json",
            },
        )
        intro_messages = select_inbox(blue_intro_inbox)
        intro_id = next(
            item["id"]
            for item in intro_messages
            if item["subject"] == "Contact request from GreenCastle"
        )
        intro_reply = await invoke(
            client,
            "reply_message",
            {
                "project_key": project_b,
                "message_id": intro_id,
                "sender_name": "BlueLake",
                "body_md": "D3 reply to contact intro",
                "format": "json",
            },
        )
        post_intro_database = database_snapshot()
        await require_success(
            client,
            "respond_contact",
            {
                "project_key": project_b,
                "to_agent": "BlueLake",
                "from_agent": "GreenCastle",
                "from_project": project_a,
                "accept": True,
                "ttl_seconds": 604800,
                "format": "json",
            },
        )
        post_approval_database = database_snapshot()
        sent = await require_success(
            client,
            "send_message",
            {
                "project_key": project_a,
                "sender_name": "GreenCastle",
                "sender_token": caller_tokens["GreenCastle"],
                "to": [f"project:{project_b}#BlueLake"],
                "subject": "D3 approved cross-project message",
                "body_md": "D3 normal cross-project body",
                "importance": "high",
                "ack_required": True,
                "topic": "d3-cross-project",
                "format": "json",
            },
        )
    return {
        "intro_reply": intro_reply,
        "post_intro_database": post_intro_database,
        "post_approval_database": post_approval_database,
        "normal_send": select_delivery(sent),
    }


async def phase_two():
    connection = sqlite3.connect(database_path)
    try:
        normal_message_id = connection.execute(
            "select id from messages where subject = ?",
            ("D3 approved cross-project message",),
        ).fetchone()[0]
    finally:
        connection.close()

    server = app.build_mcp_server()
    async with Client(server) as client:
        reply = await require_success(
            client,
            "reply_message",
            {
                "project_key": project_b,
                "message_id": normal_message_id,
                "sender_name": "BlueLake",
                "body_md": "D3 reply after process restart",
                "format": "json",
            },
        )
        source_green_inbox = await require_success(
            client,
            "fetch_inbox",
            {
                "project_key": project_a,
                "agent_name": "GreenCastle",
                "include_bodies": True,
                "limit": 20,
                "format": "json",
            },
        )
        alias_green_inbox = await require_success(
            client,
            "fetch_inbox",
            {
                "project_key": project_b,
                "agent_name": "GreenCastle",
                "include_bodies": True,
                "limit": 20,
                "format": "json",
            },
        )
    return {
        "post_restart_reply": select_delivery(reply),
        "source_green_inbox": select_inbox(source_green_inbox),
        "alias_green_inbox": select_inbox(alias_green_inbox),
        "final_database": database_snapshot(),
    }


async def main():
    if phase == "one":
        result = await phase_one()
    elif phase == "two":
        result = await phase_two()
    else:
        raise AssertionError(f"unsupported D3 phase: {phase}")
    print(
        json.dumps(
            {"namespace": namespace, "phase": phase, "observation": result},
            sort_keys=True,
        )
    )


asyncio.run(main())
'''


@pytest.fixture(scope="module")
def frozen_live_checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-d3-frozen-live"),
    )


def _run_phase(
    namespace: str,
    phase: str,
    roots: WorkerStateRoots,
    environment: Mapping[str, str],
    caller_tokens: Mapping[str, str],
) -> dict[str, Any]:
    phase_environment = dict(environment)
    phase_environment["D3_PHASE"] = phase
    completed = subprocess.run(
        [sys.executable, "-c", _D3_WORKER],
        cwd=roots.cwd,
        env=phase_environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    transcript = completed.stdout + completed.stderr
    if any(token in transcript for token in caller_tokens.values()):
        pytest.fail(
            f"{namespace} D3 phase {phase} leaked a caller credential "
            "into its process transcript",
            pytrace=False,
        )
    if completed.returncode != 0:
        pytest.fail(
            f"{namespace} D3 phase {phase} worker failed "
            f"({completed.returncode}):\n"
            f"{transcript[-5000:]}",
            pytrace=False,
        )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, f"{namespace} D3 phase {phase} produced no output"
    result = json.loads(output_lines[-1])
    serialized_result = json.dumps(result, sort_keys=True, ensure_ascii=False)
    assert not any(token in serialized_result for token in caller_tokens.values())
    assert result["namespace"] == namespace
    assert result["phase"] == phase
    return result["observation"]


@pytest.fixture(scope="module")
def d3_observations(
    frozen_live_checkout: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, Any]]:
    observations = {}
    for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE):
        source = (
            frozen_live_checkout / "src"
            if namespace == LIVE_NAMESPACE
            else CORE_SOURCE
        )
        root = tmp_path_factory.mktemp(f"d3-{namespace}")
        roots = WorkerStateRoots.under(root, pythonpath=(source,))
        caller_tokens = {
            "GreenCastle": secrets.token_urlsafe(32),
            "BlueLake": secrets.token_urlsafe(32),
        }
        environment = isolated_worker_env(os.environ, namespace, roots)
        environment.update(
            {
                "DECISION_NAMESPACE": namespace,
                "DECISION_STATE_ROOT": str(root),
                "DECISION_DATABASE": str(roots.database),
                "DECISION_SOURCE_ROOT": str(source.resolve(strict=True)),
                "D3_CALLER_TOKENS": json.dumps(caller_tokens, sort_keys=True),
            }
        )
        phase_one = _run_phase(
            namespace, "one", roots, environment, caller_tokens
        )
        phase_two = _run_phase(
            namespace, "two", roots, environment, caller_tokens
        )
        observations[namespace] = {**phase_one, **phase_two}
    return observations


def _only(
    rows: list[Mapping[str, Any]],
    description: str,
    **criteria: Any,
) -> Mapping[str, Any]:
    matches = [
        row
        for row in rows
        if all(row.get(key) == value for key, value in criteria.items())
    ]
    assert len(matches) == 1, (
        f"expected one {description} matching {criteria!r}, found {len(matches)}"
    )
    return matches[0]


def _d3_selected_projection(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    post_intro = observation["post_intro_database"]
    post_approval = observation["post_approval_database"]
    final = observation["final_database"]

    source_before_send = _only(
        post_intro["agents"], "source agent", project="A", name="GreenCastle"
    )
    target = _only(
        post_intro["agents"], "target agent", project="B", name="BlueLake"
    )
    intro = _only(
        post_intro["messages"],
        "contact intro",
        subject="Contact request from GreenCastle",
    )
    intro_recipient = _only(
        post_intro["recipients"],
        "contact intro recipient",
        message_id=intro["id"],
        kind="to",
    )
    alias_absent_before_send = not any(
        agent["project"] == "B" and agent["name"] == "GreenCastle"
        for agent in post_approval["agents"]
    )
    intro_reply = observation["intro_reply"]
    expected_sender_error = f"Agent id '{intro['sender_id']}' not found for project"

    alias = _only(
        final["agents"], "target-local alias", project="B", name="GreenCastle"
    )
    normal_message = _only(
        final["messages"],
        "approved cross-project message",
        subject="D3 approved cross-project message",
    )
    normal_recipient = _only(
        final["recipients"],
        "approved cross-project recipient",
        message_id=normal_message["id"],
        kind="to",
    )
    normal_delivery = _only(
        observation["normal_send"]["deliveries"],
        "approved cross-project delivery",
        project="B",
    )

    reply_message = _only(
        final["messages"],
        "post-restart reply",
        subject="Re: D3 approved cross-project message",
    )
    reply_recipient = _only(
        final["recipients"],
        "post-restart reply recipient",
        message_id=reply_message["id"],
        kind="to",
    )
    reply_delivery = _only(
        observation["post_restart_reply"]["deliveries"],
        "post-restart reply delivery",
        project="B",
    )
    alias_inbox_reply = _only(
        observation["alias_green_inbox"],
        "reply in target-local alias inbox",
        id=reply_message["id"],
    )
    expected_thread = str(normal_message["id"])
    metadata_fields = ("program", "model", "task_description")

    return {
        "foreign_source_row_intro": {
            "message_project": intro["project"],
            "sender_is_source_agent": intro["sender_id"]
            == source_before_send["id"],
            "sender_project": intro["sender_project"],
            "sender_name": source_before_send["name"],
            "recipient_is_target_agent": intro_recipient["agent_id"]
            == target["id"],
            "recipient_project": intro_recipient["agent_project"],
            "recipient_name": intro_recipient["agent_name"],
        },
        "immediate_target_reply": {
            "foreign_sender_unresolvable": (
                intro_reply["ok"] is False
                and expected_sender_error in intro_reply["error"]
                and "<PROJECT_B>" in intro_reply["error"]
            ),
        },
        "approved_explicit_send": {
            "alias_absent_before_send": alias_absent_before_send,
            "alias_project": alias["project"],
            "alias_name": alias["name"],
            "alias_token_is_null": alias["registration_token_is_null"],
            "alias_metadata_copied_from_source": all(
                alias[field] == source_before_send[field]
                for field in metadata_fields
            ),
            "message_project": normal_message["project"],
            "message_sender_is_alias": normal_message["sender_id"] == alias["id"],
            "delivery_sender_is_alias": normal_delivery["payload"]["sender_id"]
            == alias["id"],
            "recipient_is_target_agent": normal_recipient["agent_id"]
            == target["id"],
        },
        "fresh_process_reply": {
            "message_project": reply_message["project"],
            "message_sender_is_target_agent": reply_message["sender_id"]
            == target["id"],
            "delivery_sender_is_target_agent": reply_delivery["payload"]["sender_id"]
            == target["id"],
            "delivery_from_is_target_agent": reply_delivery["payload"]["from"]
            == target["name"],
            "recipient_is_target_local_alias": reply_recipient["agent_id"]
            == alias["id"],
            "delivery_targets_alias_name": reply_delivery["payload"]["to"]
            == [alias["name"]],
            "source_inbox_empty": observation["source_green_inbox"] == [],
            "alias_inbox_contains_reply": alias_inbox_reply["id"]
            == reply_message["id"],
            "alias_inbox_sender_is_target_agent": alias_inbox_reply["sender_id"]
            == target["id"],
            "alias_inbox_from_is_target_agent": alias_inbox_reply["from"]
            == target["name"],
            "thread_identity_preserved": (
                observation["post_restart_reply"]["reply_to"]
                == normal_message["id"]
                and observation["post_restart_reply"]["thread_id"]
                == expected_thread
                and reply_message["thread_id"] == expected_thread
                and reply_delivery["payload"]["thread_id"] == expected_thread
                and alias_inbox_reply["thread_id"] == expected_thread
            ),
        },
    }


def test_d3_selected_upstream_parity_requirement(
    d3_observations: Mapping[str, Mapping[str, Any]],
) -> None:
    expected = {
        "foreign_source_row_intro": {
            "message_project": "B",
            "sender_is_source_agent": True,
            "sender_project": "A",
            "sender_name": "GreenCastle",
            "recipient_is_target_agent": True,
            "recipient_project": "B",
            "recipient_name": "BlueLake",
        },
        "immediate_target_reply": {
            "foreign_sender_unresolvable": True,
        },
        "approved_explicit_send": {
            "alias_absent_before_send": True,
            "alias_project": "B",
            "alias_name": "GreenCastle",
            "alias_token_is_null": True,
            "alias_metadata_copied_from_source": True,
            "message_project": "B",
            "message_sender_is_alias": True,
            "delivery_sender_is_alias": True,
            "recipient_is_target_agent": True,
        },
        "fresh_process_reply": {
            "message_project": "B",
            "message_sender_is_target_agent": True,
            "delivery_sender_is_target_agent": True,
            "delivery_from_is_target_agent": True,
            "recipient_is_target_local_alias": True,
            "delivery_targets_alias_name": True,
            "source_inbox_empty": True,
            "alias_inbox_contains_reply": True,
            "alias_inbox_sender_is_target_agent": True,
            "alias_inbox_from_is_target_agent": True,
            "thread_identity_preserved": True,
        },
    }
    frozen_live = _d3_selected_projection(d3_observations[LIVE_NAMESPACE])
    core = _d3_selected_projection(d3_observations[CORE_NAMESPACE])

    assert frozen_live == expected
    assert core == frozen_live
