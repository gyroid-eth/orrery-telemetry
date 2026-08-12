"""Selected upstream-parity requirement for D2 contact-link expiry.

Path A deliberately preserves frozen-live semantics: ``expires_ts`` is stored
and refreshed by same-project contact response, but it is not an authorization
boundary in the measured response, local-send, explicit cross-project send, or
explicit cross-project reply paths.  A normal local reply does not query
``AgentLink`` at all and is therefore explicitly outside this D2 parity claim.

Both namespaces run in fresh subprocesses with private database, archive, and
signal roots.  The test seeds past, future, and NULL expiry values directly in
the private SQLite fixture, validates full per-side evidence, then compares a
D2-only effect projection so incidental D3-D6 behavior remains free to change.
Pending-status controls prove that an approved link, rather than an unrelated
heuristic, enables each send route.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
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
from test_differential import _first_difference

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
CORE_SOURCE = PACKAGE_ROOT / "src"

_GREEN = "GreenCastle"
_BLUE = "BlueLake"
_RED = "RedStone"
_EXPIRY_VALUES = {
    "past": "2000-01-01 00:00:00.000000",
    "future": "2099-01-01 00:00:00.000000",
    "null": None,
}

_WORKER_SOURCE = r'''
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from differential_probe import _install_llm_stub, _jsonable, _snapshot


namespace = os.environ["D2_NAMESPACE"]
database = Path(os.environ["D2_DATABASE"])
storage = Path(os.environ["D2_STORAGE"])
signals = Path(os.environ["D2_SIGNALS"])
source_root = Path(os.environ["D2_SOURCE_ROOT"]).resolve(strict=True)
project_a = os.environ["D2_PROJECT_A"]
project_b = os.environ["D2_PROJECT_B"]
output = Path(os.environ["D2_OUTPUT"])
secrets = json.loads(os.environ["D2_SECRETS"])
expiry_values = json.loads(os.environ["D2_EXPIRY_VALUES"])

Path(project_a).mkdir(parents=True, exist_ok=True)
Path(project_b).mkdir(parents=True, exist_ok=True)

_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")
Path(app.__file__).resolve(strict=True).relative_to(source_root)

state = {
    "database_path": str(database),
    "archive_root": str(storage),
    "signals_root": str(signals),
}
cases: list[dict[str, Any]] = []


def payload(result: Any) -> Any:
    value = result.structuredContent
    if value is None:
        blocks = _jsonable(result.content)
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
    value = _jsonable(value)
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value


def assert_no_caller_credentials(result: Any) -> str:
    channels = result.model_dump(mode="json", by_alias=True)
    serialized = json.dumps(channels, sort_keys=True, ensure_ascii=False)
    if any(token in serialized for token in secrets.values()):
        raise AssertionError("D2 tool result leaked a caller credential")

    allowed_redactions = {None, "", "***", "<redacted>", "[REDACTED]"}

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).strip().lower()
                if (
                    normalized.endswith("_token")
                    or normalized.endswith("_secret")
                    or normalized.endswith("_credential")
                    or normalized in {"authorization", "api_key", "apikey"}
                ) and nested not in allowed_redactions:
                    raise AssertionError(
                        "D2 tool result exposed a credential-bearing field"
                    )
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(channels)
    return serialized


def link_row(
    a_project: str,
    a_agent: str,
    b_project: str,
    b_agent: str,
) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT link.*
            FROM agent_links AS link
            JOIN projects AS ap ON ap.id = link.a_project_id
            JOIN agents AS aa ON aa.id = link.a_agent_id
            JOIN projects AS bp ON bp.id = link.b_project_id
            JOIN agents AS ba ON ba.id = link.b_agent_id
            WHERE ap.human_key = ? AND aa.name = ?
              AND bp.human_key = ? AND ba.name = ?
            """,
            (a_project, a_agent, b_project, b_agent),
        ).fetchone()
        if row is None:
            raise AssertionError("D2 fixture link does not exist")
        return {key: row[key] for key in row.keys()}
    finally:
        connection.close()


def seed_link(
    *,
    a_project: str,
    a_agent: str,
    b_project: str,
    b_agent: str,
    status: str,
    expiry_case: str,
) -> dict[str, Any]:
    before = link_row(a_project, a_agent, b_project, b_agent)
    connection = sqlite3.connect(database)
    try:
        changed = connection.execute(
            "UPDATE agent_links SET status = ?, expires_ts = ? WHERE id = ?",
            (status, expiry_values[expiry_case], before["id"]),
        ).rowcount
        connection.commit()
    finally:
        connection.close()
    if changed != 1:
        raise AssertionError(f"D2 fixture updated {changed} rows instead of one")
    seeded = link_row(a_project, a_agent, b_project, b_agent)
    if seeded["id"] != before["id"] or seeded["created_ts"] != before["created_ts"]:
        raise AssertionError("D2 fixture replaced the link instead of updating it")
    if seeded["status"] != status or seeded["expires_ts"] != expiry_values[expiry_case]:
        raise AssertionError("D2 fixture seed was not persisted exactly")
    return seeded


def create_link(
    *,
    a_project: str,
    a_agent: str,
    b_project: str,
    b_agent: str,
) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    try:
        def identity(project_key: str, agent_name: str) -> tuple[int, int]:
            row = connection.execute(
                """
                SELECT project.id, agent.id
                FROM projects AS project
                JOIN agents AS agent ON agent.project_id = project.id
                WHERE project.human_key = ? AND agent.name = ?
                """,
                (project_key, agent_name),
            ).fetchone()
            if row is None:
                raise AssertionError("D2 direct link identity does not exist")
            return int(row[0]), int(row[1])

        a_project_id, a_agent_id = identity(a_project, a_agent)
        b_project_id, b_agent_id = identity(b_project, b_agent)
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
        connection.execute(
            """
            INSERT INTO agent_links (
                a_project_id, a_agent_id, b_project_id, b_agent_id,
                status, reason, created_ts, updated_ts, expires_ts
            ) VALUES (?, ?, ?, ?, 'approved', 'D2 direct fixture', ?, ?, ?)
            """,
            (
                a_project_id,
                a_agent_id,
                b_project_id,
                b_agent_id,
                now,
                now,
                expiry_values["future"],
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return link_row(a_project, a_agent, b_project, b_agent)


def message_id(response: Any) -> int:
    if not isinstance(response, dict):
        raise AssertionError("message response is not an object")
    deliveries = response.get("deliveries")
    if isinstance(deliveries, list) and deliveries:
        first = deliveries[0]
        if isinstance(first, dict) and isinstance(first.get("payload"), dict):
            value = first["payload"].get("id")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    value = response.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise AssertionError("message response contains no integer id")


async def main() -> None:
    from fastmcp import Client

    async with Client(app.build_mcp_server()) as client:
        async def call(
            name: str,
            arguments: dict[str, Any],
            *,
            expect_error: bool = False,
            expected_error_class: str | None = None,
        ) -> tuple[dict[str, Any], dict[str, str]]:
            started_at = datetime.now(timezone.utc).isoformat()
            result = await client.call_tool_mcp(name, arguments)
            finished_at = datetime.now(timezone.utc).isoformat()
            raw_result = assert_no_caller_credentials(result)
            if bool(result.isError) != expect_error:
                raise AssertionError(
                    f"{name} error state {result.isError!r} != expected {expect_error!r}: "
                    f"{payload(result)!r}"
                )
            public = {"is_error": bool(result.isError)}
            # Error prose contains an intentional live/Core help-text divergence.
            # Retain only a stable route-specific semantic fingerprint.
            if result.isError:
                markers = {
                    "local_contact_required": (
                        "contact_required",
                        "contact approval required",
                    ),
                    "external_approved_link_required": (
                        "external recipients missing approved contact links",
                    ),
                    "explicit_recipient_unroutable_with_pending_link": (
                        "external recipients missing approved contact links",
                        "not found in project",
                    ),
                }
                expected_markers = markers.get(expected_error_class or "")
                if expected_markers is None or not any(
                    marker in raw_result.lower() for marker in expected_markers
                ):
                    raise AssertionError(
                        f"{name} did not fail with {expected_error_class!r}"
                    )
                public["error_class"] = expected_error_class
            else:
                if expected_error_class is not None:
                    raise AssertionError("success call declared an error class")
                public["value"] = payload(result)
            return public, {
                "started_at": started_at,
                "finished_at": finished_at,
            }

        async def required(name: str, arguments: dict[str, Any]) -> Any:
            public, _window = await call(name, arguments)
            return public["value"]

        async def exercise(
            label: str,
            *,
            a_project: str,
            a_agent: str,
            b_project: str,
            b_agent: str,
            status: str,
            expiry_case: str,
            tool_name: str,
            arguments: dict[str, Any],
            expect_error: bool = False,
        ) -> Any:
            seeded = seed_link(
                a_project=a_project,
                a_agent=a_agent,
                b_project=b_project,
                b_agent=b_agent,
                status=status,
                expiry_case=expiry_case,
            )
            durable_before = _snapshot(state, secrets)
            public, window = await call(
                tool_name,
                arguments,
                expect_error=expect_error,
                expected_error_class=(
                    "local_contact_required"
                    if label == "local_send_pending_control"
                    else "explicit_recipient_unroutable_with_pending_link"
                    if label == "cross_reply_pending_control"
                    else "external_approved_link_required"
                    if expect_error
                    else None
                ),
            )
            durable_after = _snapshot(state, secrets)
            cases.append(
                {
                    "event": label,
                    "route": tool_name,
                    "fixture": {
                        "status": status,
                        "expiry_case": expiry_case,
                        "link": seeded,
                    },
                    "result": public,
                    "call_window": window,
                    "before": durable_before,
                    "after": durable_after,
                }
            )
            return public.get("value")

        await required("ensure_project", {"human_key": project_a, "format": "json"})
        for name in ("GreenCastle", "BlueLake"):
            await required(
                "register_agent",
                {
                    "project_key": project_a,
                    "program": "d2-parity",
                    "model": "frozen-live",
                    "name": name,
                    "task_description": "D2 upstream parity",
                    "registration_token": secrets[name],
                    "format": "json",
                },
            )
        await required(
            "set_contact_policy",
            {
                "project_key": project_a,
                "agent_name": "BlueLake",
                "policy": "contacts_only",
                "format": "json",
            },
        )

        for expiry_case in ("past", "future", "null"):
            await required(
                "request_contact",
                {
                    "project_key": project_a,
                    "from_agent": "GreenCastle",
                    "to_agent": "BlueLake",
                    "reason": f"D2 {expiry_case} pending response",
                    "register_if_missing": False,
                    "ttl_seconds": 600,
                    "format": "json",
                },
            )
            await exercise(
                f"response_{expiry_case}",
                a_project=project_a,
                a_agent="GreenCastle",
                b_project=project_a,
                b_agent="BlueLake",
                status="pending",
                expiry_case=expiry_case,
                tool_name="respond_contact",
                arguments={
                    "project_key": project_a,
                    "to_agent": "BlueLake",
                    "from_agent": "GreenCastle",
                    "accept": True,
                    "ttl_seconds": 600,
                    "format": "json",
                },
            )

        local_arguments = {
            "project_key": project_a,
            "sender_name": "GreenCastle",
            "sender_token": secrets["GreenCastle"],
            "to": ["BlueLake"],
            "body_md": "D2 local expiry parity",
            "auto_contact_if_blocked": False,
            "format": "json",
        }
        for expiry_case in ("past", "future", "null"):
            await exercise(
                f"local_send_{expiry_case}",
                a_project=project_a,
                a_agent="GreenCastle",
                b_project=project_a,
                b_agent="BlueLake",
                status="approved",
                expiry_case=expiry_case,
                tool_name="send_message",
                arguments={
                    **local_arguments,
                    "subject": f"D2 local {expiry_case}",
                },
            )
        await exercise(
            "local_send_pending_control",
            a_project=project_a,
            a_agent="GreenCastle",
            b_project=project_a,
            b_agent="BlueLake",
            status="pending",
            expiry_case="past",
            tool_name="send_message",
            arguments={**local_arguments, "subject": "D2 local pending control"},
            expect_error=True,
        )

        await required("ensure_project", {"human_key": project_b, "format": "json"})
        await required(
            "register_agent",
            {
                "project_key": project_b,
                "program": "d2-parity",
                "model": "frozen-live",
                "name": "RedStone",
                "task_description": "D2 external recipient",
                "registration_token": secrets["RedStone"],
                "format": "json",
            },
        )
        await required(
            "set_contact_policy",
            {
                "project_key": project_b,
                "agent_name": "RedStone",
                "policy": "contacts_only",
                "format": "json",
            },
        )
        create_link(
            a_project=project_a,
            a_agent="GreenCastle",
            b_project=project_b,
            b_agent="RedStone",
        )
        create_link(
            a_project=project_b,
            a_agent="RedStone",
            b_project=project_a,
            b_agent="GreenCastle",
        )

        external_message_id = None
        for expiry_case in ("past", "future", "null"):
            sent = await exercise(
                f"cross_send_{expiry_case}",
                a_project=project_a,
                a_agent="GreenCastle",
                b_project=project_b,
                b_agent="RedStone",
                status="approved",
                expiry_case=expiry_case,
                tool_name="send_message",
                arguments={
                    "project_key": project_a,
                    "sender_name": "GreenCastle",
                    "sender_token": secrets["GreenCastle"],
                    "to": [f"project:{project_b}#RedStone"],
                    "subject": f"D2 cross send {expiry_case}",
                    "body_md": "D2 explicit cross-project expiry parity",
                    "auto_contact_if_blocked": False,
                    "format": "json",
                },
            )
            external_message_id = message_id(sent)
        await exercise(
            "cross_send_pending_control",
            a_project=project_a,
            a_agent="GreenCastle",
            b_project=project_b,
            b_agent="RedStone",
            status="pending",
            expiry_case="past",
            tool_name="send_message",
            arguments={
                "project_key": project_a,
                "sender_name": "GreenCastle",
                "sender_token": secrets["GreenCastle"],
                "to": [f"project:{project_b}#RedStone"],
                "subject": "D2 cross send pending control",
                "body_md": "must not deliver",
                "auto_contact_if_blocked": False,
                "format": "json",
            },
            expect_error=True,
        )

        if external_message_id is None:
            raise AssertionError("cross send produced no message id for reply")

        for expiry_case in ("past", "future", "null"):
            await exercise(
                f"cross_reply_{expiry_case}",
                a_project=project_b,
                a_agent="RedStone",
                b_project=project_a,
                b_agent="GreenCastle",
                status="approved",
                expiry_case=expiry_case,
                tool_name="reply_message",
                arguments={
                    "project_key": project_b,
                    "message_id": external_message_id,
                    "sender_name": "RedStone",
                    "body_md": f"D2 cross reply {expiry_case}",
                    "to": [f"project:{project_a}#GreenCastle"],
                    "format": "json",
                },
            )
        await exercise(
            "cross_reply_pending_control",
            a_project=project_b,
            a_agent="RedStone",
            b_project=project_a,
            b_agent="GreenCastle",
            status="pending",
            expiry_case="past",
            tool_name="reply_message",
            arguments={
                "project_key": project_b,
                "message_id": external_message_id,
                "sender_name": "RedStone",
                "body_md": "D2 cross reply pending control",
                "to": [f"project:{project_a}#GreenCastle"],
                "format": "json",
            },
            expect_error=True,
        )

    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(
            {"namespace": namespace, "cases": cases},
            destination,
            indent=2,
            sort_keys=True,
        )
        destination.write("\n")


asyncio.run(main())
'''


@pytest.fixture(scope="session")
def frozen_live_checkout_d2(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-d2-frozen-live"),
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
    project_a: Path,
    project_b: Path,
    frozen_live_checkout: Path,
    caller_tokens: Mapping[str, str],
) -> dict[str, Any]:
    source = _source_for(namespace, frozen_live_checkout).resolve()
    roots = WorkerStateRoots.under(root, pythonpath=(TESTS_ROOT, source))
    environment = isolated_worker_env(os.environ, namespace, roots)
    project_a = project_a.resolve()
    project_b = project_b.resolve()
    output = root / "d2-output.json"
    if namespace == LIVE_NAMESPACE:
        environment.update(
            {
                "CONTACT_ENFORCEMENT_ENABLED": "true",
                "CONTACT_AUTO_RETRY_ENABLED": "false",
                "MESSAGING_AUTO_HANDSHAKE_ON_BLOCK": "false",
                "MESSAGING_AUTO_REGISTER_RECIPIENTS": "false",
            }
        )
    else:
        environment.update(
            {
                "AGENTSTACK_MAIL_CONTACT_ENFORCEMENT_ENABLED": "true",
                "AGENTSTACK_MAIL_CONTACT_AUTO_RETRY_ENABLED": "false",
                "AGENTSTACK_MAIL_MESSAGING_AUTO_HANDSHAKE_ON_BLOCK": "false",
                "AGENTSTACK_MAIL_MESSAGING_AUTO_REGISTER_RECIPIENTS": "false",
            }
        )
    environment.update(
        {
            "D2_NAMESPACE": namespace,
            "D2_DATABASE": str(roots.database),
            "D2_STORAGE": str(roots.storage),
            "D2_SIGNALS": str(roots.signals),
            "D2_SOURCE_ROOT": str(source),
            "D2_PROJECT_A": str(project_a),
            "D2_PROJECT_B": str(project_b),
            "D2_OUTPUT": str(output),
            "D2_SECRETS": json.dumps(dict(caller_tokens), sort_keys=True),
            "D2_EXPIRY_VALUES": json.dumps(_EXPIRY_VALUES, sort_keys=True),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER_SOURCE],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    transcript = completed.stdout + completed.stderr
    if any(token in transcript for token in caller_tokens.values()):
        pytest.fail(f"{namespace} D2 worker leaked a caller token", pytrace=False)
    if completed.returncode != 0:
        pytest.fail(
            f"{namespace} D2 worker exited {completed.returncode}:\n{transcript[-6000:]}",
            pytrace=False,
        )
    assert output.is_file()
    assert output.stat().st_mode & 0o077 == 0
    value = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False)
    assert not any(token in serialized for token in caller_tokens.values())
    assert "<UNEXPECTED_TOKEN_VALUE>" not in serialized
    return value


def _table(case: Mapping[str, Any], phase: str, table: str) -> list[dict[str, Any]]:
    return case[phase]["database"]["tables"][table]


def _git_commits(case: Mapping[str, Any], phase: str) -> int:
    return case[phase]["git"]["log"].count("--COMMIT--")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_by_id(rows: list[dict[str, Any]], row_id: int) -> dict[str, Any]:
    matches = [row for row in rows if row["id"] == row_id]
    assert len(matches) == 1
    return matches[0]


def _assert_snapshot_integrity(snapshot: Mapping[str, Any]) -> None:
    database = snapshot["database"]
    assert database["exists"] is True
    assert database["integrity_check"] == ["ok"]
    assert database["foreign_key_violations"] == []
    git = snapshot["git"]
    assert git["exists"] is True
    assert git["status_returncode"] == 0
    assert git["status"] == ""
    assert git["fsck_returncode"] == 0
    assert git["log_returncode"] == 0


def _assert_d2_semantics(output: Mapping[str, Any]) -> None:
    cases = output["cases"]
    by_event = {case["event"]: case for case in cases}
    assert len(by_event) == len(cases) == 15

    for case in cases:
        _assert_snapshot_integrity(case["before"])
        _assert_snapshot_integrity(case["after"])

    for expiry_case in _EXPIRY_VALUES:
        case = by_event[f"response_{expiry_case}"]
        result = case["result"]
        assert result["is_error"] is False
        assert result["value"]["approved"] is True
        assert result["value"]["updated"] == 1
        before_link = case["fixture"]["link"]
        after_link = _row_by_id(
            _table(case, "after", "agent_links"), before_link["id"]
        )
        started_at = _utc(case["call_window"]["started_at"])
        finished_at = _utc(case["call_window"]["finished_at"])
        seeded_expiry = before_link["expires_ts"]
        if expiry_case == "null":
            assert seeded_expiry is None
        elif expiry_case == "past":
            assert seeded_expiry is not None
            assert _utc(seeded_expiry) < started_at
        else:
            assert expiry_case == "future"
            assert seeded_expiry is not None
            assert _utc(seeded_expiry) > finished_at
        assert after_link["id"] == before_link["id"]
        assert after_link["created_ts"] == before_link["created_ts"]
        assert after_link["updated_ts"] != before_link["updated_ts"]
        assert after_link["status"] == "approved"
        assert after_link["expires_ts"] != before_link["expires_ts"]
        updated_at = _utc(after_link["updated_ts"])
        persisted_expiry = _utc(after_link["expires_ts"])
        returned_expiry = _utc(result["value"]["expires_ts"])
        assert started_at <= updated_at <= finished_at
        assert persisted_expiry - updated_at == timedelta(seconds=600)
        assert returned_expiry == persisted_expiry
        assert len(_table(case, "after", "messages")) == len(
            _table(case, "before", "messages")
        )
        assert len(_table(case, "after", "message_recipients")) == len(
            _table(case, "before", "message_recipients")
        )
        assert case["after"]["archive"] == case["before"]["archive"]
        assert case["after"]["signals"] == case["before"]["signals"]
        assert case["after"]["git"] == case["before"]["git"]

    for route in ("local_send", "cross_send", "cross_reply"):
        for expiry_case in _EXPIRY_VALUES:
            case = by_event[f"{route}_{expiry_case}"]
            result = case["result"]
            assert result["is_error"] is False
            assert result["value"]["count"] == 1
            assert case["fixture"]["link"] in _table(
                case, "after", "agent_links"
            )
            assert len(_table(case, "after", "messages")) == len(
                _table(case, "before", "messages")
            ) + 1
            assert len(_table(case, "after", "message_recipients")) == len(
                _table(case, "before", "message_recipients")
            ) + 1
            assert _git_commits(case, "after") > _git_commits(case, "before")
            assert case["after"]["archive"] != case["before"]["archive"]
            assert case["after"]["signals"] != case["before"]["signals"]

        control = by_event[f"{route}_pending_control"]
        expected_error_class = (
            "local_contact_required"
            if route == "local_send"
            else "explicit_recipient_unroutable_with_pending_link"
            if route == "cross_reply"
            else "external_approved_link_required"
        )
        assert control["result"] == {
            "is_error": True,
            "error_class": expected_error_class,
        }
        assert len(_table(control, "after", "messages")) == len(
            _table(control, "before", "messages")
        )
        assert len(_table(control, "after", "message_recipients")) == len(
            _table(control, "before", "message_recipients")
        )
        assert control["after"]["archive"] == control["before"]["archive"]
        assert control["after"]["signals"] == control["before"]["signals"]
        assert control["after"]["git"] == control["before"]["git"]


def _d2_projection(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project full evidence onto D2 only, without freezing D3-D6 behavior."""
    projected: list[dict[str, Any]] = []
    for case in cases:
        result = case["result"]
        item: dict[str, Any] = {
            "event": case["event"],
            "route": case["route"],
            "fixture": {
                "status": case["fixture"]["status"],
                "expiry_case": case["fixture"]["expiry_case"],
            },
            "result": {"is_error": result["is_error"]},
        }
        if result["is_error"]:
            item["result"]["error_class"] = result["error_class"]
        elif case["route"] == "respond_contact":
            item["result"].update(
                approved=result["value"]["approved"],
                updated=result["value"]["updated"],
                expiry_refresh_seconds=600,
            )
        else:
            item["result"]["count"] = result["value"]["count"]

        before_messages = len(_table(case, "before", "messages"))
        after_messages = len(_table(case, "after", "messages"))
        before_recipients = len(_table(case, "before", "message_recipients"))
        after_recipients = len(_table(case, "after", "message_recipients"))
        item["effects"] = {
            "message_delta": after_messages - before_messages,
            "recipient_delta": after_recipients - before_recipients,
            "archive_changed": case["after"]["archive"] != case["before"]["archive"],
            "signals_changed": case["after"]["signals"] != case["before"]["signals"],
            "git_commit_delta_positive": (
                _git_commits(case, "after") > _git_commits(case, "before")
            ),
        }
        projected.append(item)
    return projected


def test_d2_expiry_semantics_match_frozen_live_without_core_change(
    frozen_live_checkout_d2: Path,
    tmp_path: Path,
) -> None:
    caller_tokens = {
        agent_name: secrets.token_urlsafe(32)
        for agent_name in (_GREEN, _BLUE, _RED)
    }
    live_state_root = tmp_path / "live-state"
    core_state_root = tmp_path / "core-state"
    project_a = tmp_path / "shared-project-a"
    project_b = tmp_path / "shared-project-b"
    project_a.mkdir()
    project_b.mkdir()
    live = _run_worker(
        namespace=LIVE_NAMESPACE,
        root=live_state_root,
        project_a=project_a,
        project_b=project_b,
        frozen_live_checkout=frozen_live_checkout_d2,
        caller_tokens=caller_tokens,
    )
    core = _run_worker(
        namespace=CORE_NAMESPACE,
        root=core_state_root,
        project_a=project_a,
        project_b=project_b,
        frozen_live_checkout=frozen_live_checkout_d2,
        caller_tokens=caller_tokens,
    )

    _assert_d2_semantics(live)
    _assert_d2_semantics(core)
    projected_live = _d2_projection(live["cases"])
    projected_core = _d2_projection(core["cases"])
    difference = _first_difference(projected_live, projected_core)
    assert difference is None, difference
