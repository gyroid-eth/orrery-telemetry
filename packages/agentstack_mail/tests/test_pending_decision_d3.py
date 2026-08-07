"""Hermetic executable evidence for pending product decision D3.

This probe records current cross-project intro, send, and reply identity
behavior.  Passing means frozen live and AgentStack Mail Core still agree; it
does not select or endorse that behavior.  Each namespace runs in a private,
secret-free database/archive/signal root, and phase two starts in a fresh
Python process so the reply route is also measured across a process restart.
"""

from __future__ import annotations

import json
import os
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
import re
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

from fastmcp import Client
from fastmcp.exceptions import ToolError

namespace = os.environ["DECISION_NAMESPACE"]
phase = os.environ["D3_PHASE"]
state_root = Path(os.environ["DECISION_STATE_ROOT"])
database_path = Path(os.environ["DECISION_DATABASE"])
storage_root = Path(os.environ["DECISION_STORAGE"])
signals_root = Path(os.environ["DECISION_SIGNALS"])
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


def public_payload(result):
    if result.structured_content is not None:
        return result.structured_content
    return result.data


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
    try:
        result = await client.call_tool(tool_name, arguments)
    except ToolError as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": normalize_error(exc),
        }
    if result.is_error:
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
            }
            for row in connection.execute(
                "select id, project_id, name, registration_token "
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
        links = [
            {
                "a_project": ids_to_labels[row[0]],
                "a_agent_id": row[1],
                "b_project": ids_to_labels[row[2]],
                "b_agent_id": row[3],
                "status": row[4],
                "reason": row[5],
            }
            for row in connection.execute(
                "select a_project_id, a_agent_id, b_project_id, b_agent_id, "
                "status, reason from agent_links order by id"
            )
        ]
    finally:
        connection.close()
    return {
        "projects": [
            {"label": labels[path], "id": record["id"]}
            for path, record in sorted(projects.items(), key=lambda item: item[1]["id"])
        ],
        "agents": agents,
        "messages": messages,
        "recipients": recipients,
        "links": links,
    }


def parse_archive_message(path, project):
    content = path.read_text(encoding="utf-8")
    marker = "\n---\n\n"
    if not content.startswith("---json\n") or marker not in content:
        raise AssertionError(f"unexpected archived message format: {path}")
    frontmatter_text, body = content[len("---json\n") :].split(marker, 1)
    frontmatter = json.loads(frontmatter_text)
    selected_frontmatter = {
        key: frontmatter.get(key)
        for key in (
            "id",
            "thread_id",
            "from",
            "to",
            "cc",
            "bcc",
            "subject",
            "importance",
            "ack_required",
            "attachments",
        )
    }
    selected_frontmatter["project"] = project_label(frontmatter.get("project"))
    return selected_frontmatter, body.rstrip("\n")


def archive_snapshot():
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "select human_key, slug from projects order by id"
        ).fetchall()
    finally:
        connection.close()

    archived_messages = {}
    profiles = []
    thread_digests = []
    for project_path, slug in rows:
        project = project_label(project_path)
        project_root = storage_root / "projects" / slug
        for path in sorted(project_root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(project_root)
            parts = relative.parts
            if len(parts) == 3 and parts[0] == "agents" and parts[2] == "profile.json":
                profile = json.loads(path.read_text(encoding="utf-8"))
                profiles.append(
                    {
                        "project": project,
                        "name": parts[1],
                        "program": profile.get("program"),
                        "model": profile.get("model"),
                        "task_description": profile.get("task_description"),
                    }
                )
                continue
            if len(parts) == 3 and parts[:2] == ("messages", "threads"):
                thread_digests.append(
                    {"project": project, "thread_id": path.stem}
                )
                continue

            surface = None
            if len(parts) >= 4 and parts[0] == "messages":
                surface = "canonical"
            elif len(parts) >= 6 and parts[0] == "agents" and parts[2] in {
                "inbox",
                "outbox",
            }:
                surface = f"{parts[1]}/{parts[2]}"
            if surface is None or path.suffix != ".md":
                continue
            match = re.search(r"__(\d+)\.md$", path.name)
            if match is None:
                raise AssertionError(f"archived message id missing from {path}")
            message_id = int(match.group(1))
            frontmatter, body = parse_archive_message(path, project)
            record = archived_messages.setdefault(
                message_id,
                {
                    "project": project,
                    "frontmatter": frontmatter,
                    "body_md": body,
                    "copies": [],
                },
            )
            if record["frontmatter"] != frontmatter or record["body_md"] != body:
                raise AssertionError(
                    f"archive copies disagree for message {message_id}"
                )
            record["copies"].append(surface)

    completed = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=storage_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "git_commit_count": int(completed.stdout.strip()),
        "profiles": sorted(profiles, key=lambda item: (item["project"], item["name"])),
        "messages": [
            {
                "id": message_id,
                **record,
                "copies": sorted(record["copies"]),
            }
            for message_id, record in sorted(archived_messages.items())
        ],
        "thread_digests": sorted(
            thread_digests, key=lambda item: (item["project"], item["thread_id"])
        ),
    }


def signal_snapshot():
    connection = sqlite3.connect(database_path)
    try:
        labels = {
            slug: project_label(human_key)
            for human_key, slug in connection.execute(
                "select human_key, slug from projects"
            )
        }
    finally:
        connection.close()
    signals = []
    for path in sorted(signals_root.rglob("*.signal")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        project_slug = payload.get("project")
        signals.append(
            {
                "project": labels[project_slug],
                "agent": payload.get("agent"),
                "message": payload.get("message"),
            }
        )
    return signals


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
                "registration_token": "d3-green-source-token",
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
                "registration_token": "d3-blue-target-token",
                "format": "json",
            },
        )
        request = await require_success(
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
        signals_after_intro = signal_snapshot()
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
        approval = await require_success(
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
        sent = await require_success(
            client,
            "send_message",
            {
                "project_key": project_a,
                "sender_name": "GreenCastle",
                "sender_token": "d3-green-source-token",
                "to": [f"project:{project_b}#BlueLake"],
                "subject": "D3 approved cross-project message",
                "body_md": "D3 normal cross-project body",
                "importance": "high",
                "ack_required": True,
                "topic": "d3-cross-project",
                "format": "json",
            },
        )
        signals_after_normal_send = signal_snapshot()
    return {
        "request_contact": {
            "from": request.get("from"),
            "from_project": project_label(request.get("from_project")),
            "to": request.get("to"),
            "to_project": project_label(request.get("to_project")),
            "status": request.get("status"),
        },
        "signals_after_intro": signals_after_intro,
        "blue_intro_inbox": intro_messages,
        "intro_reply": intro_reply,
        "post_intro_database": post_intro_database,
        "approval": {
            "from": approval.get("from"),
            "to": approval.get("to"),
            "approved": approval.get("approved"),
            "updated": approval.get("updated"),
        },
        "normal_send": select_delivery(sent),
        "signals_after_normal_send": signals_after_normal_send,
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
        signals_before_fetch = signal_snapshot()
        blue_inbox = await require_success(
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
        "signals_before_fetch": signals_before_fetch,
        "blue_inbox": select_inbox(blue_inbox),
        "source_green_inbox": select_inbox(source_green_inbox),
        "alias_green_inbox": select_inbox(alias_green_inbox),
        "final_database": database_snapshot(),
        "archive": archive_snapshot(),
        "signals_after_fetch": signal_snapshot(),
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
    if completed.returncode != 0:
        pytest.fail(
            f"{namespace} D3 phase {phase} worker failed "
            f"({completed.returncode}):\n"
            f"{(completed.stdout + completed.stderr)[-5000:]}",
            pytrace=False,
        )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, f"{namespace} D3 phase {phase} produced no output"
    result = json.loads(output_lines[-1])
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
        environment = isolated_worker_env(os.environ, namespace, roots)
        environment.update(
            {
                "DECISION_NAMESPACE": namespace,
                "DECISION_STATE_ROOT": str(root),
                "DECISION_DATABASE": str(roots.database),
                "DECISION_STORAGE": str(roots.storage),
                "DECISION_SIGNALS": str(roots.signals),
            }
        )
        phase_one = _run_phase(namespace, "one", roots, environment)
        phase_two = _run_phase(namespace, "two", roots, environment)
        observations[namespace] = {**phase_one, **phase_two}
    return observations


def test_d3_frozen_live_and_core_have_identical_normalized_observations(
    d3_observations: Mapping[str, Mapping[str, Any]],
) -> None:
    assert d3_observations[LIVE_NAMESPACE] == d3_observations[CORE_NAMESPACE]


def test_d3_records_foreign_intro_sender_and_target_local_reply(
    d3_observations: Mapping[str, Mapping[str, Any]],
) -> None:
    observation = d3_observations[LIVE_NAMESPACE]
    assert observation["request_contact"] == {
        "from": "GreenCastle",
        "from_project": "A",
        "to": "BlueLake",
        "to_project": "B",
        "status": "pending",
    }
    assert observation["approval"] == {
        "from": "GreenCastle",
        "to": "BlueLake",
        "approved": True,
        "updated": 1,
    }
    post_intro = observation["post_intro_database"]
    assert post_intro["agents"] == [
        {
            "id": 1,
            "project": "A",
            "name": "GreenCastle",
            "registration_token_is_null": False,
        },
        {
            "id": 2,
            "project": "B",
            "name": "BlueLake",
            "registration_token_is_null": False,
        },
    ]
    assert post_intro["messages"] == [
        {
            "id": 1,
            "project": "B",
            "sender_id": 1,
            "sender_project": "A",
            "thread_id": None,
            "topic": None,
            "subject": "Contact request from GreenCastle",
            "body_md": "D3 cross-project contact",
            "importance": "normal",
            "ack_required": True,
        }
    ]
    assert post_intro["recipients"] == [
        {
            "message_id": 1,
            "agent_id": 2,
            "agent_project": "B",
            "agent_name": "BlueLake",
            "kind": "to",
        }
    ]
    assert post_intro["links"] == [
        {
            "a_project": "A",
            "a_agent_id": 1,
            "b_project": "B",
            "b_agent_id": 2,
            "status": "pending",
            "reason": "D3 cross-project contact",
        }
    ]
    assert observation["intro_reply"]["ok"] is False
    assert "Agent id '1' not found for project '<PROJECT_B>'" in observation[
        "intro_reply"
    ]["error"]

    final_database = observation["final_database"]
    assert final_database["agents"][-1] == {
        "id": 3,
        "project": "B",
        "name": "GreenCastle",
        "registration_token_is_null": True,
    }
    assert final_database["messages"] == [
        post_intro["messages"][0],
        {
            "id": 2,
            "project": "B",
            "sender_id": 3,
            "sender_project": "B",
            "thread_id": None,
            "topic": "d3-cross-project",
            "subject": "D3 approved cross-project message",
            "body_md": "D3 normal cross-project body",
            "importance": "high",
            "ack_required": True,
        },
        {
            "id": 3,
            "project": "B",
            "sender_id": 2,
            "sender_project": "B",
            "thread_id": "2",
            "topic": "d3-cross-project",
            "subject": "Re: D3 approved cross-project message",
            "body_md": "D3 reply after process restart",
            "importance": "high",
            "ack_required": True,
        },
    ]
    assert final_database["recipients"] == [
        {
            "message_id": 1,
            "agent_id": 2,
            "agent_project": "B",
            "agent_name": "BlueLake",
            "kind": "to",
        },
        {
            "message_id": 2,
            "agent_id": 2,
            "agent_project": "B",
            "agent_name": "BlueLake",
            "kind": "to",
        },
        {
            "message_id": 3,
            "agent_id": 3,
            "agent_project": "B",
            "agent_name": "GreenCastle",
            "kind": "to",
        },
    ]
    assert final_database["links"] == [
        {
            "a_project": "A",
            "a_agent_id": 1,
            "b_project": "B",
            "b_agent_id": 2,
            "status": "approved",
            "reason": "D3 cross-project contact",
        }
    ]
    assert observation["normal_send"]["count"] == 1
    assert observation["normal_send"]["verified_sender"] is True
    assert observation["normal_send"]["deliveries"] == [
        {
            "project": "B",
            "payload": {
                "id": 2,
                "project_id": 2,
                "sender_id": 3,
                "thread_id": None,
                "topic": "d3-cross-project",
                "subject": "D3 approved cross-project message",
                "importance": "high",
                "ack_required": True,
                "from": "GreenCastle",
                "to": ["BlueLake"],
                "cc": [],
                "bcc": [],
                "body_md": "D3 normal cross-project body",
            },
        }
    ]
    assert observation["post_restart_reply"]["count"] == 1
    assert observation["post_restart_reply"]["reply_to"] == 2
    assert observation["post_restart_reply"]["thread_id"] == "2"
    assert observation["post_restart_reply"]["deliveries"] == [
        {
            "project": "B",
            "payload": {
                "id": 3,
                "project_id": 2,
                "sender_id": 2,
                "thread_id": "2",
                "topic": "d3-cross-project",
                "subject": "Re: D3 approved cross-project message",
                "importance": "high",
                "ack_required": True,
                "from": "BlueLake",
                "to": ["GreenCastle"],
                "cc": [],
                "bcc": [],
                "body_md": "D3 reply after process restart",
            },
        }
    ]
    assert observation["source_green_inbox"] == []
    assert [item["id"] for item in observation["alias_green_inbox"]] == [3]
    assert observation["archive"]["git_commit_count"] == 7
    assert observation["archive"]["profiles"] == [
        {
            "project": "A",
            "name": "GreenCastle",
            "program": "d3-hermetic-probe",
            "model": "fixture-model",
            "task_description": "D3 source identity",
        },
        {
            "project": "B",
            "name": "BlueLake",
            "program": "d3-hermetic-probe",
            "model": "fixture-model",
            "task_description": "D3 target identity",
        },
        {
            "project": "B",
            "name": "GreenCastle",
            "program": "d3-hermetic-probe",
            "model": "fixture-model",
            "task_description": "D3 source identity",
        },
    ]
    assert [item["id"] for item in observation["archive"]["messages"]] == [
        1,
        2,
        3,
    ]
    assert observation["archive"]["messages"][0]["copies"] == [
        "BlueLake/inbox",
        "GreenCastle/outbox",
        "canonical",
    ]
    assert observation["archive"]["messages"][2]["copies"] == [
        "BlueLake/outbox",
        "GreenCastle/inbox",
        "canonical",
    ]
    assert observation["archive"]["thread_digests"] == [
        {"project": "B", "thread_id": "2"}
    ]
    assert observation["signals_after_intro"] == [
        {
            "project": "B",
            "agent": "BlueLake",
            "message": {
                "id": 1,
                "from": "GreenCastle",
                "subject": "Contact request from GreenCastle",
                "importance": "normal",
            },
        },
    ]
    assert observation["signals_after_normal_send"] == [
        {
            "project": "B",
            "agent": "BlueLake",
            "message": {
                "id": 2,
                "from": "GreenCastle",
                "subject": "D3 approved cross-project message",
                "importance": "high",
            },
        },
    ]
    assert observation["signals_before_fetch"] == [
        {
            "project": "B",
            "agent": "BlueLake",
            "message": {
                "id": 2,
                "from": "GreenCastle",
                "subject": "D3 approved cross-project message",
                "importance": "high",
            },
        },
        {
            "project": "B",
            "agent": "GreenCastle",
            "message": {
                "id": 3,
                "from": "BlueLake",
                "subject": "Re: D3 approved cross-project message",
                "importance": "high",
            },
        },
    ]
    assert observation["signals_after_fetch"] == []
