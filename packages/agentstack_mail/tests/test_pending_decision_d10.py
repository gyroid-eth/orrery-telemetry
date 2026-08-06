"""Hermetic evidence for pending decision D10.

These probes freeze today's behavior as evidence for a pending decision, not as
an assertion that the behavior is correct. Rewrite them to encode the chosen
requirement after the decision.

The production SQLite connection and ``PRAGMA busy_timeout`` are both 60 seconds.
Waiting that long in the default suite would obscure the semantic boundary rather
than strengthen it, so the lock probe first records the production value and then
uses a checkout-local 75 ms ``PRAGMA busy_timeout``.  The same external-writer,
commit, rollback, and recovery path is exercised without changing production code.

The race probe deliberately establishes only mutual exclusion for its finite
samples.  It cannot establish FIFO ordering, a named winner, statistical balance,
or starvation freedom; each of those is a claim over schedules not exhausted by a
finite test run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
TEST_BUSY_TIMEOUT_MS = 75
PRODUCTION_BUSY_TIMEOUT_MS = 60_000
RACE_TRIALS = 4

_FAIRNESS_NON_CLAIMS = (
    "fifo_order",
    "named_winner",
    "statistical_balance",
    "starvation_freedom",
)

# Kept in this exclusive test file so both namespaces execute the exact same probe
# in fresh interpreters.  No live and Core modules are ever imported together.
_WORKER_SOURCE = r"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys
import time
import types
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _install_llm_stub(namespace: str) -> None:
    module_name = f"{namespace}.llm"
    stub = types.ModuleType(module_name)

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("D10 probe entered the disabled LLM seam")

    stub.complete_system_user = fail_if_called
    sys.modules[module_name] = stub


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json", by_alias=True, exclude_none=True))
    return repr(value)


def _payload(result: Any) -> Any:
    value = result.structured_content
    if value is None:
        value = result.data
    value = _jsonable(value)
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value


async def _required_call(client: Any, name: str, arguments: dict[str, Any]) -> Any:
    result = await client.call_tool(name, arguments)
    if result.is_error:
        raise AssertionError(f"setup tool {name} returned an error: {_payload(result)!r}")
    return _payload(result)


async def _capture_call(client: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await client.call_tool(name, arguments)
    except BaseException as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return {
        "ok": not result.is_error,
        "error_type": None if not result.is_error else "tool_result",
        "error": None if not result.is_error else repr(_payload(result)),
        "payload": _payload(result),
    }


async def _setup(client: Any, project_key: str) -> None:
    await _required_call(client, "ensure_project", {"human_key": project_key})
    for name in ("GreenCastle", "BlueLake"):
        await _required_call(
            client,
            "register_agent",
            {
                "project_key": project_key,
                "program": "d10-probe",
                "model": "frozen-differential",
                "name": name,
                "task_description": "D10 hermetic evidence",
                "registration_token": f"d10-private-{name}",
            },
        )


def _reservation_count(database: Path) -> int:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM file_reservations").fetchone()[0])
    finally:
        connection.close()


def _reservation_artifact_count(storage: Path) -> int:
    if not storage.is_dir():
        return 0
    return sum(
        1
        for path in storage.rglob("*.json")
        if "file_reservations" in path.parts
    )


async def _lock_timeout(
    namespace: str,
    app: Any,
    database: Path,
    storage: Path,
    project_key: str,
    test_timeout_ms: int,
) -> dict[str, Any]:
    from fastmcp import Client
    from sqlalchemy import event

    db = importlib.import_module(f"{namespace}.db")
    server = app.build_mcp_server()
    async with Client(server) as client:
        await _setup(client, project_key)

        engine = db.get_engine()
        async with engine.connect() as connection:
            configured_timeout_ms = int(
                (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
            )

        # SQLAlchemy checkout hooks run for every pooled connection.  Registering
        # this after the production connect hook preserves all production setup
        # while scaling only SQLite's wait duration for this worker process.
        def set_test_busy_timeout(dbapi_connection: Any, *_args: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA busy_timeout={test_timeout_ms}")
            finally:
                cursor.close()

        event.listen(engine.sync_engine, "checkout", set_test_busy_timeout)
        async with engine.connect() as connection:
            effective_test_timeout_ms = int(
                (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
            )

        count_before = _reservation_count(database)
        artifacts_before = _reservation_artifact_count(storage)
        blocker = sqlite3.connect(database, isolation_level=None, timeout=0.0)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            # A harmless uncommitted write makes the external writer ownership
            # explicit.  It is rolled back after the timed-out public call.
            blocker.execute("UPDATE projects SET slug=slug")
            started = time.monotonic()
            blocked = await _capture_call(
                client,
                "file_reservation_paths",
                {
                    "project_key": project_key,
                    "agent_name": "GreenCastle",
                    "paths": ["src/d10-timeout.py"],
                    "ttl_seconds": 3600,
                    "exclusive": True,
                    "reason": "external writer timeout evidence",
                },
            )
            elapsed_ms = (time.monotonic() - started) * 1000.0
            count_while_locked = int(
                blocker.execute("SELECT COUNT(*) FROM file_reservations").fetchone()[0]
            )
            artifacts_while_locked = _reservation_artifact_count(storage)
        finally:
            blocker.rollback()
            blocker.close()

        recovered = await _capture_call(
            client,
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": "GreenCastle",
                "paths": ["src/d10-timeout.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": "post-lock recovery evidence",
            },
        )

    return {
        "configured_timeout_ms": configured_timeout_ms,
        "effective_test_timeout_ms": effective_test_timeout_ms,
        "blocked": blocked,
        "elapsed_ms": elapsed_ms,
        "count_before": count_before,
        "count_while_locked": count_while_locked,
        "count_after_recovery": _reservation_count(database),
        "artifacts_before": artifacts_before,
        "artifacts_while_locked": artifacts_while_locked,
        "artifacts_after_recovery": _reservation_artifact_count(storage),
        "recovered": recovered,
    }


def _granted_count(result: dict[str, Any]) -> int:
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return 0
    granted = payload.get("granted")
    return len(granted) if isinstance(granted, list) else 0


def _conflict_count(result: dict[str, Any]) -> int:
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return 0
    conflicts = payload.get("conflicts")
    return len(conflicts) if isinstance(conflicts, list) else 0


async def _fairness_samples(app: Any, project_key: str, trials: int) -> dict[str, Any]:
    from fastmcp import Client

    server = app.build_mcp_server()
    async with Client(server) as green_client, Client(server) as blue_client:
        await _setup(green_client, project_key)
        clients = {
            "GreenCastle": green_client,
            "BlueLake": blue_client,
        }
        samples: list[dict[str, Any]] = []
        for trial in range(trials):
            creation_order = (
                ("GreenCastle", "BlueLake")
                if trial % 2 == 0
                else ("BlueLake", "GreenCastle")
            )
            ready = 0
            ready_lock = asyncio.Lock()
            all_ready = asyncio.Event()
            start = asyncio.Event()

            async def contender(name: str) -> tuple[str, dict[str, Any]]:
                nonlocal ready
                async with ready_lock:
                    ready += 1
                    if ready == 2:
                        all_ready.set()
                await start.wait()
                result = await _capture_call(
                    clients[name],
                    "file_reservation_paths",
                    {
                        "project_key": project_key,
                        "agent_name": name,
                        "paths": [f"src/d10-race-{trial}.py"],
                        "ttl_seconds": 3600,
                        "exclusive": True,
                        "reason": f"bounded race sample {trial}",
                    },
                )
                return name, result

            tasks = [asyncio.create_task(contender(name)) for name in creation_order]
            await asyncio.wait_for(all_ready.wait(), timeout=5.0)
            start.set()
            observations = dict(await asyncio.gather(*tasks))
            winners = [
                name for name, result in observations.items() if _granted_count(result) == 1
            ]
            losers = [
                name for name, result in observations.items() if _conflict_count(result) == 1
            ]
            samples.append(
                {
                    "trial": trial,
                    "creation_order": list(creation_order),
                    "winners": winners,
                    "losers": losers,
                    "all_calls_ok": all(result["ok"] for result in observations.values()),
                }
            )

    return {
        "samples": samples,
        "established": ["one_grant_one_conflict_per_sample"],
        "not_established": [
            "fifo_order",
            "named_winner",
            "statistical_balance",
            "starvation_freedom",
        ],
        "limit": (
            "finite completed races cannot quantify every scheduler/process ordering "
            "or prove progress over an unbounded wait"
        ),
    }


async def main() -> None:
    namespace = os.environ["D10_NAMESPACE"]
    scenario = os.environ["D10_SCENARIO"]
    output = Path(os.environ["D10_OUTPUT"])
    database = Path(os.environ["D10_DATABASE"])
    storage = Path(os.environ["D10_STORAGE"])
    project_key = os.environ["D10_PROJECT_KEY"]
    source_root = Path(os.environ["D10_SOURCE_ROOT"]).resolve(strict=True)

    _install_llm_stub(namespace)
    app = importlib.import_module(f"{namespace}.app")
    Path(app.__file__).resolve(strict=True).relative_to(source_root)

    if scenario == "lock_timeout":
        evidence = await _lock_timeout(
            namespace,
            app,
            database,
            storage,
            project_key,
            int(os.environ["D10_TEST_BUSY_TIMEOUT_MS"]),
        )
    elif scenario == "fairness":
        evidence = await _fairness_samples(
            app,
            project_key,
            int(os.environ["D10_RACE_TRIALS"]),
        )
    else:
        raise AssertionError(f"unsupported D10 scenario: {scenario}")

    payload = {
        "namespace": namespace,
        "scenario": scenario,
        "evidence": evidence,
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(payload, destination, indent=2, sort_keys=True)
        destination.write("\n")


asyncio.run(main())
"""


@pytest.fixture(scope="session")
def frozen_live_checkout_d10(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-d10-frozen-live"),
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
    scenario: str,
    root: Path,
    frozen_live_checkout: Path,
) -> dict[str, Any]:
    source = _source_for(namespace, frozen_live_checkout).resolve()
    roots = WorkerStateRoots.under(root, pythonpath=(source,))
    environment = isolated_worker_env(os.environ, namespace, roots)
    project_key = (root / "project").resolve()
    project_key.mkdir(parents=True)
    output = root / "d10-output.json"
    environment.update(
        {
            "D10_NAMESPACE": namespace,
            "D10_SCENARIO": scenario,
            "D10_OUTPUT": str(output),
            "D10_DATABASE": str(roots.database),
            "D10_STORAGE": str(roots.storage),
            "D10_PROJECT_KEY": str(project_key),
            "D10_SOURCE_ROOT": str(source),
            "D10_TEST_BUSY_TIMEOUT_MS": str(TEST_BUSY_TIMEOUT_MS),
            "D10_RACE_TRIALS": str(RACE_TRIALS),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER_SOURCE],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    if completed.returncode != 0:
        transcript = (completed.stdout + completed.stderr)[-6000:]
        pytest.fail(
            f"{namespace} D10 {scenario} worker exited "
            f"{completed.returncode}:\n{transcript}",
            pytrace=False,
        )
    assert output.is_file()
    assert output.stat().st_mode & 0o077 == 0
    return json.loads(output.read_text(encoding="utf-8"))


@pytest.mark.parametrize("namespace", [LIVE_NAMESPACE, CORE_NAMESPACE])
def test_external_writer_beyond_busy_timeout_fails_without_reservation_then_recovers(
    namespace: str,
    frozen_live_checkout_d10: Path,
    tmp_path: Path,
) -> None:
    """A writer held past SQLite's wait boundary fails; release permits retry.

    The 75 ms wait is behavior-equivalent to a writer held beyond the production
    60-second PRAGMA.  Both are SQLite ``busy_timeout`` on the same commit path.
    """

    output = _run_worker(
        namespace=namespace,
        scenario="lock_timeout",
        root=tmp_path / namespace,
        frozen_live_checkout=frozen_live_checkout_d10,
    )
    evidence = output["evidence"]

    assert evidence["configured_timeout_ms"] == PRODUCTION_BUSY_TIMEOUT_MS
    assert evidence["effective_test_timeout_ms"] == TEST_BUSY_TIMEOUT_MS
    assert evidence["blocked"]["ok"] is False
    assert evidence["blocked"]["error_type"] == "ToolError"
    # The public MCP boundary intentionally sanitizes SQLAlchemy's underlying
    # ``database is locked`` OperationalError to a generic recoverable error.
    assert "database error occurred" in evidence["blocked"]["error"].lower()
    # The public call can encounter the timeout in several DB/check-in steps, so
    # its wall time need not equal one PRAGMA interval. Returning before this
    # lower bound would not prove that a busy wait was exercised. The worker's
    # 45-second subprocess timeout is the runaway guard; a narrower upper wall
    # time would turn CI scheduling speed into a product requirement.
    assert evidence["elapsed_ms"] >= TEST_BUSY_TIMEOUT_MS * 0.5
    assert evidence["count_before"] == 0
    assert evidence["count_while_locked"] == 0
    assert evidence["artifacts_before"] == 0
    assert evidence["artifacts_while_locked"] == 0

    recovered = evidence["recovered"]
    assert recovered["ok"] is True
    assert len(recovered["payload"]["granted"]) == 1
    assert recovered["payload"]["conflicts"] == []
    assert evidence["count_after_recovery"] == 1
    # One path-hash artifact and one stable ``id-<id>.json`` projection are
    # written for a successfully recovered reservation.
    assert evidence["artifacts_after_recovery"] == 2


@pytest.mark.parametrize("namespace", [LIVE_NAMESPACE, CORE_NAMESPACE])
def test_finite_shared_root_races_establish_safety_but_not_fairness(
    namespace: str,
    frozen_live_checkout_d10: Path,
    tmp_path: Path,
) -> None:
    """Bounded samples prove one-winner safety, not scheduler fairness."""

    output = _run_worker(
        namespace=namespace,
        scenario="fairness",
        root=tmp_path / namespace,
        frozen_live_checkout=frozen_live_checkout_d10,
    )
    evidence = output["evidence"]

    assert evidence["established"] == ["one_grant_one_conflict_per_sample"]
    assert tuple(evidence["not_established"]) == _FAIRNESS_NON_CLAIMS
    assert "finite" in evidence["limit"]
    assert "unbounded wait" in evidence["limit"]
    assert len(evidence["samples"]) == RACE_TRIALS

    contenders = {"GreenCastle", "BlueLake"}
    for trial, sample in enumerate(evidence["samples"]):
        assert sample["trial"] == trial
        assert set(sample["creation_order"]) == contenders
        assert len(sample["winners"]) == 1
        assert len(sample["losers"]) == 1
        assert set(sample["winners"] + sample["losers"]) == contenders
        assert sample["all_calls_ok"] is True
        # Intentionally no assertion about which name wins, whether task-creation
        # order wins, or how often each name wins.  None is guaranteed by the
        # implementation, and finite samples cannot prove starvation freedom.
