"""Hermetic evidence for pending decision D4.

These probes freeze today's behavior as evidence for a pending decision, not as
an assertion that accepting a contact response without a pending request is the
correct product behavior. Rewrite them to encode the selected requirement after
the decision.

Frozen live and Core run in separate, secret-free subprocesses with private
SQLite, archive, and notification roots.  The probe records the public response,
the exact contact-link row, message/recipient counts, and archive/Git/signal state
on both sides of the no-pending ``respond_contact(accept=True)`` call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
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
TTL_SECONDS = 600


# Both namespaces execute this exact worker in fresh interpreters; live and Core
# modules are never imported into the same process.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def install_llm_stub(namespace: str) -> None:
    module_name = f"{namespace}.llm"
    stub = types.ModuleType(module_name)

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("D4 probe entered the disabled LLM seam")

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


def payload(result: Any) -> Any:
    value = result.structured_content
    if value is None:
        value = result.data
    value = jsonable(value)
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value


async def required_call(client: Any, name: str, arguments: dict[str, Any]) -> Any:
    result = await client.call_tool(name, arguments, raise_on_error=False)
    if result.is_error:
        raise AssertionError(f"tool {name} returned an error: {payload(result)!r}")
    return payload(result)


def tree_snapshot(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.name == ".archive.lock" or path.name.endswith(".lock"):
            continue
        files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def git_snapshot(root: Path) -> dict[str, Any]:
    def run(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    return {
        "head": run("rev-parse", "HEAD"),
        "commit_count": int(run("rev-list", "--count", "HEAD")),
        "status": run("status", "--porcelain=v1"),
    }


def database_snapshot(database: Path, project_key: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        project = connection.execute(
            "SELECT id, slug, human_key FROM projects WHERE human_key = ?",
            (project_key,),
        ).fetchone()
        if project is None:
            raise AssertionError("D4 project row is missing")
        agents = connection.execute(
            "SELECT id, name FROM agents WHERE project_id = ? ORDER BY id",
            (project["id"],),
        ).fetchall()
        links = connection.execute(
            "SELECT id, a_project_id, a_agent_id, b_project_id, b_agent_id, "
            "status, reason, created_ts, updated_ts, expires_ts "
            "FROM agent_links ORDER BY id"
        ).fetchall()
        return {
            "integrity_check": [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            ],
            "foreign_key_violations": [
                list(row) for row in connection.execute("PRAGMA foreign_key_check")
            ],
            "project": dict(project),
            "agents": [dict(row) for row in agents],
            "links": [dict(row) for row in links],
            "message_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE project_id = ?",
                    (project["id"],),
                ).fetchone()[0]
            ),
            "recipient_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM message_recipients"
                ).fetchone()[0]
            ),
        }
    finally:
        connection.close()


def durable_snapshot(
    database: Path,
    storage: Path,
    signals: Path,
    project_key: str,
) -> dict[str, Any]:
    return {
        "database": database_snapshot(database, project_key),
        "archive": tree_snapshot(storage),
        "git": git_snapshot(storage),
        "signals": tree_snapshot(signals),
    }


async def main() -> None:
    namespace = os.environ["D4_NAMESPACE"]
    source_root = Path(os.environ["D4_SOURCE_ROOT"]).resolve(strict=True)
    database = Path(os.environ["D4_DATABASE"])
    storage = Path(os.environ["D4_STORAGE"])
    signals = Path(os.environ["D4_SIGNALS"])
    project_key = os.environ["D4_PROJECT_KEY"]
    output = Path(os.environ["D4_OUTPUT"])
    ttl_seconds = int(os.environ["D4_TTL_SECONDS"])

    install_llm_stub(namespace)
    app = importlib.import_module(f"{namespace}.app")
    Path(app.__file__).resolve(strict=True).relative_to(source_root)

    from fastmcp import Client

    async with Client(app.build_mcp_server()) as client:
        await required_call(
            client,
            "ensure_project",
            {"human_key": project_key, "format": "json"},
        )
        for name, token in (
            ("GreenCastle", "d4-green-owner-token"),
            ("BlueLake", "d4-blue-owner-token"),
        ):
            await required_call(
                client,
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "pending-decision-d4-probe",
                    "model": "fixture-model",
                    "name": name,
                    "task_description": "D4 hermetic evidence",
                    "registration_token": token,
                    "format": "json",
                },
            )

        before = durable_snapshot(database, storage, signals, project_key)
        started_at = datetime.now(timezone.utc).isoformat()
        response = await required_call(
            client,
            "respond_contact",
            {
                "project_key": project_key,
                "to_agent": "BlueLake",
                "from_agent": "GreenCastle",
                "accept": True,
                "ttl_seconds": ttl_seconds,
                "format": "json",
            },
        )
        finished_at = datetime.now(timezone.utc).isoformat()
        after = durable_snapshot(database, storage, signals, project_key)

    result = {
        "namespace": namespace,
        "call_window": {"started_at": started_at, "finished_at": finished_at},
        "response": response,
        "before": before,
        "after": after,
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(result, destination, indent=2, sort_keys=True)
        destination.write("\n")


asyncio.run(main())
"""


@pytest.fixture(scope="module")
def frozen_live_checkout_d4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-d4-frozen-live"),
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
    source: Path,
    root: Path,
) -> dict[str, Any]:
    roots = WorkerStateRoots.under(root, pythonpath=(source,))
    environment = isolated_worker_env(os.environ, namespace, roots)
    project_key = (root / "project").resolve()
    project_key.mkdir(parents=True)
    output = root / "d4-output.json"
    environment.update(
        {
            "D4_NAMESPACE": namespace,
            "D4_SOURCE_ROOT": str(source.resolve()),
            "D4_DATABASE": str(roots.database),
            "D4_STORAGE": str(roots.storage),
            "D4_SIGNALS": str(roots.signals),
            "D4_PROJECT_KEY": str(project_key),
            "D4_OUTPUT": str(output),
            "D4_TTL_SECONDS": str(TTL_SECONDS),
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
    if completed.returncode != 0:
        transcript = (completed.stdout + completed.stderr)[-6000:]
        pytest.fail(
            f"{namespace} D4 worker exited {completed.returncode}:\n{transcript}",
            pytrace=False,
        )
    assert output.is_file()
    assert output.stat().st_mode & 0o077 == 0
    return json.loads(output.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def d4_observations(
    frozen_live_checkout_d4: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, Any]]:
    observations = {}
    for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE):
        observations[namespace] = _run_worker(
            namespace=namespace,
            source=_source_for(namespace, frozen_live_checkout_d4),
            root=tmp_path_factory.mktemp(f"d4-{namespace}"),
        )
    return observations


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_observation(observation: dict[str, Any]) -> None:
    before = observation["before"]
    after = observation["after"]
    response = observation["response"]

    assert before["database"]["integrity_check"] == ["ok"]
    assert before["database"]["foreign_key_violations"] == []
    assert before["database"]["links"] == []
    assert before["database"]["message_count"] == 0
    assert before["database"]["recipient_count"] == 0

    assert after["database"]["integrity_check"] == ["ok"]
    assert after["database"]["foreign_key_violations"] == []
    assert after["database"]["message_count"] == 0
    assert after["database"]["recipient_count"] == 0
    assert len(after["database"]["links"]) == 1

    agents = {row["name"]: row["id"] for row in after["database"]["agents"]}
    project_id = after["database"]["project"]["id"]
    link = after["database"]["links"][0]
    assert link["id"] == 1
    assert link["a_project_id"] == project_id
    assert link["a_agent_id"] == agents["GreenCastle"]
    assert link["b_project_id"] == project_id
    assert link["b_agent_id"] == agents["BlueLake"]
    assert link["status"] == "approved"
    assert link["reason"] == ""
    assert link["created_ts"] == link["updated_ts"]
    assert (
        _parse_time(link["expires_ts"]) - _parse_time(link["updated_ts"])
    ).total_seconds() == TTL_SECONDS

    assert response == {
        "from": "GreenCastle",
        "to": "BlueLake",
        "approved": True,
        "expires_ts": response["expires_ts"],
        "updated": 1,
    }
    assert _parse_time(response["expires_ts"]) == _parse_time(link["expires_ts"])
    started_at = _parse_time(observation["call_window"]["started_at"])
    finished_at = _parse_time(observation["call_window"]["finished_at"])
    assert started_at <= _parse_time(link["created_ts"]) <= finished_at

    # The single agent_links insert is the only durable side effect of the call.
    assert after["database"]["project"] == before["database"]["project"]
    assert after["database"]["agents"] == before["database"]["agents"]
    assert after["archive"] == before["archive"]
    assert after["git"] == before["git"]
    assert after["signals"] == before["signals"] == {}


def _normalized(observation: dict[str, Any]) -> dict[str, Any]:
    """Keep measured semantics while removing only independent wall clocks."""

    response = dict(observation["response"])
    response["expires_ts"] = "<CALL_EXPIRY>"
    link = dict(observation["after"]["database"]["links"][0])
    created = _parse_time(link.pop("created_ts"))
    updated = _parse_time(link.pop("updated_ts"))
    expires = _parse_time(link.pop("expires_ts"))
    link.update(
        {
            "created_equals_updated": created == updated,
            "ttl_seconds": (expires - updated).total_seconds(),
        }
    )
    return {
        "response": response,
        "link": link,
        "before_link_count": len(observation["before"]["database"]["links"]),
        "after_link_count": len(observation["after"]["database"]["links"]),
        "message_count": observation["after"]["database"]["message_count"],
        "recipient_count": observation["after"]["database"]["recipient_count"],
        "archive_unchanged": (
            observation["after"]["archive"] == observation["before"]["archive"]
        ),
        "git_unchanged": observation["after"]["git"] == observation["before"]["git"],
        "signals_unchanged": (
            observation["after"]["signals"] == observation["before"]["signals"]
        ),
    }


def test_d4_accept_without_pending_creates_one_approved_link_in_live_and_core(
    d4_observations: dict[str, dict[str, Any]],
) -> None:
    """Record matching current behavior without selecting it as a requirement."""

    live = d4_observations[LIVE_NAMESPACE]
    core = d4_observations[CORE_NAMESPACE]
    _assert_observation(live)
    _assert_observation(core)
    assert _normalized(core) == _normalized(live)
