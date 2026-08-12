"""Selected upstream-parity requirement for product decision D6.

Path A preserves the pre-existing frozen-live behavior for one narrow cohort:
an explicitly registered same-project sender with a caller-supplied non-NULL
token may omit ``sender_token`` when sending to an ``open`` recipient.  The
send succeeds with ``verified_sender=false`` and one delivery.  A wrong-token
control rejects with the observed durable projection unchanged.

The selected scope does not claim matching-token behavior, generated or
unavailable tokens, NULL-token/macro/migrated identities, cross-project sends,
other contact policies, other send entrypoints, concurrency, claim, rotation,
recovery, or future strict enforcement.  Credential non-disclosure is claimed
only for serialized raw MCP results and fixture transcripts.  Those canary
scans do not freeze additive response or error fields.  Signal lifecycle,
inbox-fetch cleanup, archive/Git details, and read/ack state are not projected.
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
_CALLER_TOKENS = (
    "d6-green-owner-token",
    "d6-blue-owner-token",
    "d6-wrong-sender-token",
)


_WORKER = r"""
from __future__ import annotations

import asyncio
import hmac
import importlib
import json
import os
import sqlite3
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
output_path = root / "result.json"
source_root = Path(os.environ["D6_SOURCE_ROOT"]).resolve(strict=True)

sender_name = "GreenCastle"
recipient_name = "BlueLake"
sender_token = "d6-green-owner-token"
recipient_token = "d6-blue-owner-token"
wrong_sender_token = "d6-wrong-sender-token"
subject = "D6 missing sender token"
body = "Known-token sender omitted sender_token."

_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")
Path(app.__file__).resolve(strict=True).relative_to(source_root)


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
    value = result.structuredContent
    if value is None:
        blocks = jsonable(result.content)
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
    value = jsonable(value)
    if isinstance(value, dict) and "result" in value:
        value = value["result"]
    return value


def assert_no_caller_credentials(result):
    raw_result = result.model_dump(mode="json", by_alias=True)
    serialized = json.dumps(raw_result, ensure_ascii=False, sort_keys=True)
    for secret in (sender_token, recipient_token, wrong_sender_token):
        if secret in serialized:
            raise AssertionError("D6 raw MCP result disclosed a caller token")
    return raw_result


async def capture_call(client, tool_name, arguments):
    try:
        result = await client.call_tool_mcp(
            tool_name,
            arguments,
        )
    except BaseException as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "token_mismatch": wrong_sender_token in str(exc),
        }
    assert_no_caller_credentials(result)
    payload = public_payload(result)
    if result.isError:
        error_observation = json.dumps(
            {"payload": payload, "content": jsonable(result.content)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return {
            "ok": False,
            "error_type": "tool_result",
            "token_mismatch": "sender_token does not match registered token"
            in error_observation,
        }
    return {"ok": True, "payload": payload}


async def require_success(client, tool_name, arguments):
    result = await capture_call(client, tool_name, arguments)
    if not result["ok"]:
        raise AssertionError(f"setup tool {tool_name} failed: {result!r}")
    return result["payload"]


def database_state():
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        project_id = connection.execute(
            "SELECT id FROM projects WHERE human_key = ?",
            (project_key,),
        ).fetchone()[0]
        expected_tokens = {
            sender_name: sender_token,
            recipient_name: recipient_token,
        }
        agents = [
            {
                "name": row["name"],
                "registration_token_is_non_null": (
                    row["registration_token"] is not None
                ),
                "registration_token_matches_fixture": hmac.compare_digest(
                    row["registration_token"] or "",
                    expected_tokens[row["name"]],
                ),
            }
            for row in connection.execute(
                "SELECT name, registration_token "
                "FROM agents WHERE project_id = ? ORDER BY id",
                (project_id,),
            )
        ]
        recipient_policy = connection.execute(
            "SELECT contact_policy FROM agents "
            "WHERE project_id = ? AND name = ?",
            (project_id, recipient_name),
        ).fetchone()[0]
        messages = [
            {
                "id": row["id"],
                "sender": row["sender"],
                "subject": row["subject"],
                "body_md": row["body_md"],
            }
            for row in connection.execute(
                "SELECT m.id, a.name AS sender, m.subject, m.body_md "
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
            }
            for row in connection.execute(
                "SELECT mr.message_id, a.name AS agent, mr.kind "
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
    return {
        "agents": agents,
        "recipient_policy": recipient_policy,
        "counts": counts,
        "messages": messages,
        "recipients": recipients,
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

        before_missing = database_state()
        missing = await capture_call(
            client,
            "send_message",
            {
                "project_key": project_key,
                "sender_name": sender_name,
                "to": [recipient_name],
                "subject": subject,
                "body_md": body,
                "format": "json",
            },
        )
        after_missing = database_state()
        before_wrong = database_state()
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
        after_wrong = database_state()

    payload = {
        "before_missing": before_missing,
        "missing": missing,
        "after_missing": after_missing,
        "before_wrong": before_wrong,
        "wrong": wrong,
        "after_wrong": after_wrong,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for secret in (sender_token, recipient_token, wrong_sender_token):
        if secret in serialized:
            raise AssertionError("D6 serialized probe result disclosed a caller token")
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
    source = _source_for(namespace, frozen_live_checkout).resolve(strict=True)
    roots = WorkerStateRoots.under(
        root,
        pythonpath=(TESTS_ROOT, source),
    )
    environment = isolated_worker_env(os.environ, namespace, roots)
    environment["D6_SOURCE_ROOT"] = str(source)
    environment["NOTIFICATIONS_ENABLED"] = "false"
    environment["AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED"] = "false"
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER, namespace, str(root)],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    transcript = completed.stdout + completed.stderr
    for secret in _CALLER_TOKENS:
        assert secret not in transcript, f"{namespace} D6 worker leaked a token"
    diagnostic = transcript[-8000:]
    assert completed.returncode == 0, diagnostic
    result_path = root / "result.json"
    assert result_path.is_file(), diagnostic
    serialized = result_path.read_text(encoding="utf-8")
    for secret in _CALLER_TOKENS:
        assert secret not in serialized, f"{namespace} D6 output leaked a token"
    payload = json.loads(serialized)
    assert isinstance(payload, dict)
    return payload


def _assert_selected_d6_behavior(payload: Mapping[str, Any]) -> None:
    before_missing = payload["before_missing"]
    after_missing = payload["after_missing"]
    missing = payload["missing"]

    assert before_missing["counts"] == {
        "messages": 0,
        "recipients": 0,
    }
    assert before_missing["agents"] == [
        {
            "name": "GreenCastle",
            "registration_token_is_non_null": True,
            "registration_token_matches_fixture": True,
        },
        {
            "name": "BlueLake",
            "registration_token_is_non_null": True,
            "registration_token_matches_fixture": True,
        },
    ]
    assert before_missing["recipient_policy"] == "open"
    assert missing["ok"] is True, missing
    assert missing["payload"]["count"] == 1
    assert missing["payload"]["verified_sender"] is False
    assert len(missing["payload"]["deliveries"]) == 1

    assert after_missing == {
        "agents": before_missing["agents"],
        "recipient_policy": "open",
        "counts": {"messages": 1, "recipients": 1},
        "messages": [
            {
                "id": 1,
                "sender": "GreenCastle",
                "subject": _SUBJECT,
                "body_md": _BODY,
            }
        ],
        "recipients": [
            {
                "message_id": 1,
                "agent": "BlueLake",
                "kind": "to",
            }
        ],
    }

    wrong = payload["wrong"]
    assert wrong["ok"] is False
    assert wrong["error_type"] == "tool_result"
    assert wrong["token_mismatch"] is True
    assert payload["before_wrong"] == payload["after_wrong"]
    assert payload["after_wrong"]["counts"] == {
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

    _assert_selected_d6_behavior(live)
    _assert_selected_d6_behavior(core)
