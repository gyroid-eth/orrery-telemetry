"""Hermetic executable evidence for pending product decision D6.

This probe measures whether a tokenized sender can omit ``sender_token`` and
still send.  It records frozen-live and Core behavior without selecting or
implementing a future authentication policy.  A wrong-token attempt is the
rejected-call control and must leave every observed durable surface unchanged.
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
TESTS_ROOT = Path(__file__).resolve().parent
CORE_SOURCE = PACKAGE_ROOT / "src"

_SUBJECT = "D6 missing sender token"
_BODY = "Known-token sender omitted sender_token."
_TOKEN_MISMATCH_FRAGMENT = "sender_token does not match registered token"


_WORKER = r"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from differential_probe import _install_llm_stub


namespace, root_text = sys.argv[1:3]
root = Path(root_text)
project_path = root / "project"
project_path.mkdir(parents=True, exist_ok=True)
project_key = str(project_path)
database = root / "mail.sqlite3"
archive = root / "archive"
signals = root / "signals"
output_path = root / "result.json"

sender_name = "GreenCastle"
recipient_name = "BlueLake"
sender_token = "d6-green-owner-token"
recipient_token = "d6-blue-owner-token"
wrong_sender_token = "d6-wrong-sender-token"
subject = "D6 missing sender token"
body = "Known-token sender omitted sender_token."

_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")


def jsonable(value):
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


def public_payload(result):
    value = result.structured_content
    if value is None:
        value = result.data
    value = jsonable(value)
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value


def normalize_public(value):
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key in {"created_ts", "read_ts", "ack_ts"} and item is not None:
                normalized[key] = "<TIMESTAMP>"
            else:
                normalized[key] = normalize_public(item)
        return normalized
    if isinstance(value, list):
        return [normalize_public(item) for item in value]
    if isinstance(value, str):
        return value.replace(project_key, "<PROJECT>")
    return value


async def capture_call(client, tool_name, arguments):
    try:
        result = await client.call_tool(
            tool_name,
            arguments,
            raise_on_error=False,
        )
    except BaseException as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": normalize_public(str(exc)),
        }
    payload = normalize_public(public_payload(result))
    content = normalize_public(jsonable(result.content))
    if result.is_error:
        return {
            "ok": False,
            "error_type": "tool_result",
            "error": repr({"payload": payload, "content": content}),
        }
    return {"ok": True, "payload": payload}


async def require_success(client, tool_name, arguments):
    result = await capture_call(client, tool_name, arguments)
    if not result["ok"]:
        raise AssertionError(f"setup tool {tool_name} failed: {result!r}")
    return result["payload"]


def git_commit_count():
    completed = subprocess.run(
        ["git", "-C", str(archive), "rev-list", "--count", "HEAD"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        },
        check=True,
        text=True,
        capture_output=True,
    )
    return int(completed.stdout.strip())


def database_state():
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        project_id, project_slug = connection.execute(
            "SELECT id, slug FROM projects WHERE human_key = ?",
            (project_key,),
        ).fetchone()
        messages = [
            {
                "id": row["id"],
                "sender": row["sender"],
                "thread_id": row["thread_id"],
                "topic": row["topic"],
                "subject": row["subject"],
                "body_md": row["body_md"],
                "importance": row["importance"],
                "ack_required": bool(row["ack_required"]),
                "created_ts_present": row["created_ts"] is not None,
                "attachments": json.loads(row["attachments"]),
            }
            for row in connection.execute(
                "SELECT m.id, a.name AS sender, m.thread_id, m.topic, "
                "m.subject, m.body_md, m.importance, m.ack_required, "
                "m.created_ts, m.attachments "
                "FROM messages AS m "
                "JOIN agents AS a ON a.id = m.sender_id "
                "WHERE m.project_id = ? "
                "ORDER BY m.id",
                (project_id,),
            )
        ]
        recipients = [
            {
                "message_id": row["message_id"],
                "agent": row["agent"],
                "kind": row["kind"],
                "read": row["read_ts"] is not None,
                "acknowledged": row["ack_ts"] is not None,
            }
            for row in connection.execute(
                "SELECT mr.message_id, a.name AS agent, mr.kind, "
                "mr.read_ts, mr.ack_ts "
                "FROM message_recipients AS mr "
                "JOIN agents AS a ON a.id = mr.agent_id "
                "JOIN messages AS m ON m.id = mr.message_id "
                "WHERE m.project_id = ? "
                "ORDER BY mr.message_id, a.name",
                (project_id,),
            )
        ]
        counts = {
            "messages": connection.execute(
                "SELECT COUNT(*) FROM messages WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0],
            "recipients": connection.execute(
                "SELECT COUNT(*) "
                "FROM message_recipients AS mr "
                "JOIN messages AS m ON m.id = mr.message_id "
                "WHERE m.project_id = ?",
                (project_id,),
            ).fetchone()[0],
        }
    finally:
        connection.close()
    return project_slug, {
        "counts": counts,
        "messages": messages,
        "recipients": recipients,
    }


def archive_state():
    observations = []
    for path in sorted(archive.rglob("*.md")):
        content = path.read_text(encoding="utf-8")
        if subject not in content:
            continue
        relative = path.relative_to(archive)
        relative_text = relative.as_posix()
        if "/messages/" in f"/{relative_text}":
            role = "canonical"
        elif f"/{sender_name}/outbox/" in f"/{relative_text}":
            role = "sender_outbox"
        elif f"/{recipient_name}/inbox/" in f"/{relative_text}":
            role = "recipient_inbox"
        else:
            role = "unexpected"
        observations.append(
            {
                "role": role,
                "subject_occurrences": content.count(subject),
                "body_occurrences": content.count(body),
                "mentions_sender": sender_name in content,
                "mentions_recipient": recipient_name in content,
            }
        )
    return sorted(observations, key=lambda item: item["role"])


def signal_state(project_slug):
    observations = []
    for path in sorted(signals.rglob("*.signal")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        message = payload.get("message") or {}
        observations.append(
            {
                "recipient": payload.get("agent"),
                "project_matches": payload.get("project") == project_slug,
                "timestamp_present": bool(payload.get("timestamp")),
                "message": {
                    "id": message.get("id"),
                    "from": message.get("from"),
                    "subject": message.get("subject"),
                    "importance": message.get("importance"),
                },
            }
        )
    return observations


def durable_state():
    project_slug, db = database_state()
    return {
        "database": db,
        "archive_bundle": archive_state(),
        "signals": signal_state(project_slug),
        "git_commit_count": git_commit_count(),
    }


async def main():
    from fastmcp import Client

    async with Client(app.build_mcp_server()) as client:
        await require_success(
            client,
            "ensure_project",
            {"human_key": project_key, "format": "json"},
        )
        for name, token in (
            (sender_name, sender_token),
            (recipient_name, recipient_token),
        ):
            await require_success(
                client,
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "d6-hermetic-probe",
                    "model": "fixture-model",
                    "name": name,
                    "task_description": "D6 missing sender-token measurement",
                    "registration_token": token,
                    "format": "json",
                },
            )
        await require_success(
            client,
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": recipient_name,
                "policy": "open",
                "format": "json",
            },
        )

        before_missing = durable_state()
        missing = await capture_call(
            client,
            "send_message",
            {
                "project_key": project_key,
                "sender_name": sender_name,
                "to": [recipient_name],
                "subject": subject,
                "body_md": body,
                "importance": "high",
                "ack_required": True,
                "format": "json",
            },
        )
        after_missing = durable_state()
        inbox = await capture_call(
            client,
            "fetch_inbox",
            {
                "project_key": project_key,
                "agent_name": recipient_name,
                "include_bodies": True,
                "limit": 20,
                "format": "json",
            },
        )
        before_wrong = durable_state()
        wrong = await capture_call(
            client,
            "send_message",
            {
                "project_key": project_key,
                "sender_name": sender_name,
                "to": [recipient_name],
                "subject": "D6 wrong sender token control",
                "body_md": "This rejected control must not persist.",
                "sender_token": wrong_sender_token,
                "format": "json",
            },
        )
        after_wrong = durable_state()

    payload = {
        "before_missing": before_missing,
        "missing": missing,
        "after_missing": after_missing,
        "inbox": inbox,
        "before_wrong": before_wrong,
        "wrong": wrong,
        "after_wrong": after_wrong,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for secret in (sender_token, recipient_token, wrong_sender_token):
        if secret in serialized:
            raise AssertionError("D6 result disclosed a caller token")
    output_path.write_text(serialized + "\n", encoding="utf-8")


asyncio.run(main())
"""


@pytest.fixture(scope="module")
def frozen_live_checkout(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-d6-frozen-live"),
    )


def _source_for(namespace: str, frozen_live_checkout: Path) -> Path:
    if namespace == LIVE_NAMESPACE:
        return frozen_live_checkout / "src"
    if namespace == CORE_NAMESPACE:
        return CORE_SOURCE
    raise AssertionError(f"unexpected namespace {namespace!r}")


def _run_worker(
    namespace: str,
    frozen_live_checkout: Path,
    root: Path,
) -> dict[str, Any]:
    roots = WorkerStateRoots.under(
        root,
        pythonpath=(TESTS_ROOT, _source_for(namespace, frozen_live_checkout)),
    )
    environment = isolated_worker_env(os.environ, namespace, roots)
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER, namespace, str(root)],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    diagnostic = (completed.stdout + completed.stderr)[-8000:]
    assert completed.returncode == 0, diagnostic
    result_path = root / "result.json"
    assert result_path.is_file(), diagnostic
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _assert_observed_d6_behavior(payload: Mapping[str, Any]) -> None:
    before_missing = payload["before_missing"]
    after_missing = payload["after_missing"]
    missing = payload["missing"]

    assert before_missing["database"]["counts"] == {
        "messages": 0,
        "recipients": 0,
    }
    assert missing["ok"] is True, missing
    assert missing["payload"]["count"] == 1
    assert missing["payload"]["verified_sender"] is False
    assert len(missing["payload"]["deliveries"]) == 1
    delivery = missing["payload"]["deliveries"][0]
    assert delivery["project"] == "<PROJECT>"
    assert delivery["payload"]["id"] == 1
    assert delivery["payload"]["from"] == "GreenCastle"
    assert delivery["payload"]["to"] == ["BlueLake"]
    assert delivery["payload"]["subject"] == _SUBJECT
    assert delivery["payload"]["body_md"] == _BODY
    assert delivery["payload"]["importance"] == "high"
    assert delivery["payload"]["ack_required"] is True

    assert after_missing["database"] == {
        "counts": {"messages": 1, "recipients": 1},
        "messages": [
            {
                "id": 1,
                "sender": "GreenCastle",
                "thread_id": None,
                "topic": None,
                "subject": _SUBJECT,
                "body_md": _BODY,
                "importance": "high",
                "ack_required": True,
                "created_ts_present": True,
                "attachments": [],
            }
        ],
        "recipients": [
            {
                "message_id": 1,
                "agent": "BlueLake",
                "kind": "to",
                "read": False,
                "acknowledged": False,
            }
        ],
    }
    assert [item["role"] for item in after_missing["archive_bundle"]] == [
        "canonical",
        "recipient_inbox",
        "sender_outbox",
    ]
    for item in after_missing["archive_bundle"]:
        assert item["subject_occurrences"] >= 1
        assert item["body_occurrences"] == 1
        assert item["mentions_sender"] is True
        assert item["mentions_recipient"] is True
    assert after_missing["signals"] == [
        {
            "recipient": "BlueLake",
            "project_matches": True,
            "timestamp_present": True,
            "message": {
                "id": 1,
                "from": "GreenCastle",
                "subject": _SUBJECT,
                "importance": "high",
            },
        }
    ]
    assert (
        after_missing["git_commit_count"]
        == before_missing["git_commit_count"] + 1
    )

    inbox = payload["inbox"]
    assert inbox["ok"] is True, inbox
    assert len(inbox["payload"]) == 1
    inbox_message = inbox["payload"][0]
    assert inbox_message["id"] == 1
    assert inbox_message["from"] == "GreenCastle"
    assert inbox_message["subject"] == _SUBJECT
    assert inbox_message["body_md"] == _BODY
    assert inbox_message["importance"] == "high"
    assert inbox_message["ack_required"] is True

    wrong = payload["wrong"]
    assert wrong["ok"] is False
    assert _TOKEN_MISMATCH_FRAGMENT in wrong["error"]
    assert payload["before_wrong"] == payload["after_wrong"]
    assert payload["after_wrong"]["database"]["counts"] == {
        "messages": 1,
        "recipients": 1,
    }


def test_d6_frozen_live_and_core_match_missing_sender_token_behavior(
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    live = _run_worker(
        LIVE_NAMESPACE,
        frozen_live_checkout,
        tmp_path / "live",
    )
    core = _run_worker(
        CORE_NAMESPACE,
        frozen_live_checkout,
        tmp_path / "core",
    )

    _assert_observed_d6_behavior(live)
    _assert_observed_d6_behavior(core)
    assert core == live
