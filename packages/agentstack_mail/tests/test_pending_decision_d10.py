"""Selected upstream-parity requirements for product decision D10.

The production SQLite ``PRAGMA busy_timeout`` is 60 seconds and journal mode is
WAL.
Waiting that long in the default suite would obscure the semantic boundary rather
than strengthen it, so the lock probe first records the production value and then
uses a checkout-local 75 ms ``PRAGMA busy_timeout``.  The same external-writer,
commit, rollback, and recovery path is exercised without changing production code.
It is a scaled probe of the same SQLite mechanism, not an assertion about exact
production wall time.

The race probes use explicit rendezvous at the archive-lock acquisition seam or,
for split roots, after both conflict reads.  Every topology is repeated with launch
order reversed.  The selected requirement deliberately preserves frozen live's
storage-root-dependent behavior: one winner with a shared archive lock, but two
conflicting winners when one database is paired with distinct archive locks.

Finite samples cannot establish FIFO ordering, a named winner, statistical
balance, starvation freedom, or behavior over every unconstrained schedule.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
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
LOCK_TRIALS = 3
SAME_PROCESS_RACE_TRIALS = 4
PROCESS_RACE_TRIALS = 2

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
from contextlib import asynccontextmanager
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


def _reservation_count_for_path(database: Path, path_pattern: str) -> int:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM file_reservations "
            "WHERE path_pattern = ? AND released_ts IS NULL "
            "AND expires_ts > CURRENT_TIMESTAMP",
            (path_pattern,),
        ).fetchone()
        return int(row[0])
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


def _write_barrier_marker(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        destination.write("ready\n")


async def _wait_at_process_barrier() -> None:
    ready = Path(os.environ["D10_READY_MARKER"])
    release = Path(os.environ["D10_RELEASE_MARKER"])
    _write_barrier_marker(ready)
    deadline = time.monotonic() + 20.0
    while not release.is_file():
        if time.monotonic() >= deadline:
            raise AssertionError("D10 process barrier was not released")
        await asyncio.sleep(0.005)


async def _initialize_process_root(app: Any, project_key: str) -> dict[str, Any]:
    from fastmcp import Client

    server = app.build_mcp_server()
    async with Client(server) as client:
        await _setup(client, project_key)
    return {"initialized": True}


async def _process_contender(
    app: Any,
    project_key: str,
    agent_name: str,
    path: str,
    barrier_seam: str,
) -> dict[str, Any]:
    from fastmcp import Client

    if barrier_seam == "archive_lock_acquisition":
        original_archive_lock = app._archive_write_lock

        @asynccontextmanager
        async def gated_archive_lock(archive: Any, **kwargs: Any):
            await _wait_at_process_barrier()
            async with original_archive_lock(archive, **kwargs):
                yield

        app._archive_write_lock = gated_archive_lock
    elif barrier_seam == "after_conflict_read_before_insert":
        original_create = app._create_file_reservation

        async def gated_create(*args: Any, **kwargs: Any) -> Any:
            await _wait_at_process_barrier()
            return await original_create(*args, **kwargs)

        app._create_file_reservation = gated_create
    else:
        raise AssertionError(f"unsupported D10 process barrier seam: {barrier_seam}")

    server = app.build_mcp_server()
    async with Client(server) as client:
        result = await _capture_call(
            client,
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "paths": [path],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": f"two-process {barrier_seam} parity",
            },
        )
    return {
        "ok": result["ok"],
        "grants": _granted_count(result),
        "conflicts": _conflict_count(result),
    }


async def _lock_timeout(
    namespace: str,
    app: Any,
    database: Path,
    storage: Path,
    project_key: str,
    test_timeout_ms: int,
    trials: int,
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
            journal_mode = str(
                (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar_one()
            ).lower()

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

        samples: list[dict[str, Any]] = []
        for trial in range(trials):
            path = f"src/d10-timeout-{trial}.py"
            count_before = _reservation_count(database)
            artifacts_before = _reservation_artifact_count(storage)
            blocker = sqlite3.connect(database, isolation_level=None, timeout=0.0)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                # A harmless uncommitted write makes the external writer
                # ownership explicit. It is rolled back after the public call.
                blocker.execute("UPDATE projects SET slug=slug")
                blocked = await _capture_call(
                    client,
                    "file_reservation_paths",
                    {
                        "project_key": project_key,
                        "agent_name": "GreenCastle",
                        "paths": [path],
                        "ttl_seconds": 3600,
                        "exclusive": True,
                        "reason": f"external writer timeout sample {trial}",
                    },
                )
                count_while_locked = int(
                    blocker.execute(
                        "SELECT COUNT(*) FROM file_reservations"
                    ).fetchone()[0]
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
                    "paths": [path],
                    "ttl_seconds": 3600,
                    "exclusive": True,
                    "reason": f"post-lock recovery sample {trial}",
                },
            )
            samples.append(
                {
                    "trial": trial,
                    "blocked": blocked,
                    "count_delta_while_locked": count_while_locked - count_before,
                    "artifact_delta_while_locked": (
                        artifacts_while_locked - artifacts_before
                    ),
                    "recovered": recovered,
                    "count_delta_after_recovery": (
                        _reservation_count(database) - count_before
                    ),
                    "artifact_delta_after_recovery": (
                        _reservation_artifact_count(storage) - artifacts_before
                    ),
                }
            )

    return {
        "configured_timeout_ms": configured_timeout_ms,
        "effective_test_timeout_ms": effective_test_timeout_ms,
        "journal_mode": journal_mode,
        "external_lock_mode": "begin_immediate",
        "samples": samples,
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


async def _shared_process_samples(
    app: Any,
    database: Path,
    project_key: str,
    trials: int,
) -> dict[str, Any]:
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
            path = f"src/d10-same-process-{trial}.py"
            creation_order = (
                ("GreenCastle", "BlueLake")
                if trial % 2 == 0
                else ("BlueLake", "GreenCastle")
            )
            lock_attempts = 0
            ready_lock = asyncio.Lock()
            all_ready = asyncio.Event()
            start = asyncio.Event()

            original_archive_lock = app._archive_write_lock

            @asynccontextmanager
            async def gated_archive_lock(archive: Any, **kwargs: Any):
                nonlocal lock_attempts
                async with ready_lock:
                    lock_attempts += 1
                    if lock_attempts == 2:
                        all_ready.set()
                await start.wait()
                async with original_archive_lock(archive, **kwargs):
                    yield

            app._archive_write_lock = gated_archive_lock

            async def contender(name: str) -> tuple[str, dict[str, Any]]:
                result = await _capture_call(
                    clients[name],
                    "file_reservation_paths",
                    {
                        "project_key": project_key,
                        "agent_name": name,
                        "paths": [path],
                        "ttl_seconds": 3600,
                        "exclusive": True,
                        "reason": f"same-process lock race sample {trial}",
                    },
                )
                return name, result

            try:
                tasks = [
                    asyncio.create_task(contender(name)) for name in creation_order
                ]
                await asyncio.wait_for(all_ready.wait(), timeout=5.0)
                start.set()
                observations = dict(await asyncio.gather(*tasks))
            finally:
                app._archive_write_lock = original_archive_lock
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
                    "lock_attempts_before_release": lock_attempts,
                    "active_rows": _reservation_count_for_path(database, path),
                }
            )

    return {
        "conditions": {
            "processes": 1,
            "clients": 2,
            "database": "shared",
            "archive_lock": "shared",
            "barrier": "before_archive_lock_acquisition",
            "path_kind": "same_exact_exclusive",
        },
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
    db = importlib.import_module(f"{namespace}.db")
    storage_module = importlib.import_module(f"{namespace}.storage")
    for module in (app, db, storage_module):
        Path(module.__file__).resolve(strict=True).relative_to(source_root)

    if scenario == "lock_timeout":
        evidence = await _lock_timeout(
            namespace,
            app,
            database,
            storage,
            project_key,
            int(os.environ["D10_TEST_BUSY_TIMEOUT_MS"]),
            int(os.environ["D10_LOCK_TRIALS"]),
        )
    elif scenario == "same_process_shared_root":
        evidence = await _shared_process_samples(
            app,
            database,
            project_key,
            int(os.environ["D10_RACE_TRIALS"]),
        )
    elif scenario == "initialize":
        evidence = await _initialize_process_root(app, project_key)
    elif scenario == "process_contender":
        evidence = await _process_contender(
            app,
            project_key,
            os.environ["D10_AGENT_NAME"],
            os.environ["D10_PATH"],
            os.environ["D10_BARRIER_SEAM"],
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


def _captured_grant_count(result: dict[str, Any]) -> int:
    granted = result.get("payload", {}).get("granted", [])
    return len(granted) if isinstance(granted, list) else 0


def _captured_conflict_count(result: dict[str, Any]) -> int:
    conflicts = result.get("payload", {}).get("conflicts", [])
    return len(conflicts) if isinstance(conflicts, list) else 0


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
            "D10_LOCK_TRIALS": str(LOCK_TRIALS),
            "D10_RACE_TRIALS": str(SAME_PROCESS_RACE_TRIALS),
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


def _process_roots(
    root: Path,
    *,
    database: Path,
    storage: Path,
    source: Path,
) -> WorkerStateRoots:
    return WorkerStateRoots(
        home=root / "home",
        database=database,
        storage=storage,
        signals=root / "signals",
        temp=root / "tmp",
        cwd=root / "cwd",
        pythonpath=(source,),
    )


def _process_environment(
    *,
    namespace: str,
    roots: WorkerStateRoots,
    source: Path,
    scenario: str,
    output: Path,
    project_key: Path,
) -> dict[str, str]:
    environment = isolated_worker_env(os.environ, namespace, roots)
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
            "D10_LOCK_TRIALS": str(LOCK_TRIALS),
            "D10_RACE_TRIALS": str(SAME_PROCESS_RACE_TRIALS),
        }
    )
    return environment


def _run_initializer(
    *,
    namespace: str,
    source: Path,
    database: Path,
    storage: Path,
    project_key: Path,
    root: Path,
) -> None:
    roots = _process_roots(
        root,
        database=database,
        storage=storage,
        source=source,
    )
    output = root / "initialize-output.json"
    environment = _process_environment(
        namespace=namespace,
        roots=roots,
        source=source,
        scenario="initialize",
        output=output,
        project_key=project_key,
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
            f"{namespace} D10 initializer exited {completed.returncode}:\n{transcript}",
            pytrace=False,
        )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["evidence"] == {"initialized": True}


def _write_release_marker(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        destination.write("release\n")


def _target_row_projection(
    database: Path,
    project_key: Path,
    path_pattern: str,
) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT file_reservations.agent_id,
                   file_reservations.exclusive,
                   file_reservations.released_ts,
                   file_reservations.expires_ts > CURRENT_TIMESTAMP
            FROM file_reservations
            JOIN projects ON projects.id = file_reservations.project_id
            WHERE projects.human_key = ?
              AND file_reservations.path_pattern = ?
            ORDER BY file_reservations.agent_id
            """,
            (str(project_key), path_pattern),
        ).fetchall()
    finally:
        connection.close()
    return {
        "row_count": len(rows),
        "distinct_holders": len({row[0] for row in rows}),
        "all_exclusive": all(bool(row[1]) for row in rows),
        "all_active": all(row[2] is None and bool(row[3]) for row in rows),
    }


def _stop_workers(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
    for process in processes:
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _run_two_process_topology(
    *,
    namespace: str,
    frozen_live_checkout: Path,
    root: Path,
    split_archive_roots: bool,
) -> list[dict[str, Any]]:
    source = _source_for(namespace, frozen_live_checkout).resolve()
    root.mkdir(parents=True)
    database = root / "shared-mail.sqlite3"
    project_key = (root / "project").resolve()
    project_key.mkdir()
    storages = (
        [root / "archive-GreenCastle", root / "archive-BlueLake"]
        if split_archive_roots
        else [root / "shared-archive", root / "shared-archive"]
    )

    # Archive creation has its own Git-init race. Preinitialize every distinct
    # archive root sequentially so this gate reaches the reservation lock seam.
    for index, storage in enumerate(dict.fromkeys(storages)):
        _run_initializer(
            namespace=namespace,
            source=source,
            database=database,
            storage=storage,
            project_key=project_key,
            root=root / f"initializer-{index}",
        )

    samples: list[dict[str, Any]] = []
    names = ("GreenCastle", "BlueLake")
    barrier_seam = (
        "after_conflict_read_before_insert"
        if split_archive_roots
        else "archive_lock_acquisition"
    )
    for trial in range(PROCESS_RACE_TRIALS):
        path_pattern = f"src/d10-two-process-{trial}.py"
        barrier_root = root / f"barrier-{trial}"
        barrier_root.mkdir()
        release = barrier_root / "release"
        launch_order = names if trial % 2 == 0 else tuple(reversed(names))
        processes: list[subprocess.Popen[str]] = []
        records: list[tuple[str, Path, Path, subprocess.Popen[str]]] = []
        for name in launch_order:
            index = names.index(name)
            process_root = root / f"trial-{trial}-{name}"
            roots = _process_roots(
                process_root,
                database=database,
                storage=storages[index],
                source=source,
            )
            output = process_root / "contender-output.json"
            ready = barrier_root / f"ready-{name}"
            environment = _process_environment(
                namespace=namespace,
                roots=roots,
                source=source,
                scenario="process_contender",
                output=output,
                project_key=project_key,
            )
            environment.update(
                {
                    "D10_AGENT_NAME": name,
                    "D10_PATH": path_pattern,
                    "D10_BARRIER_SEAM": barrier_seam,
                    "D10_READY_MARKER": str(ready),
                    "D10_RELEASE_MARKER": str(release),
                }
            )
            process = subprocess.Popen(
                [sys.executable, "-c", _WORKER_SOURCE],
                cwd=roots.cwd,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(process)
            records.append((name, ready, output, process))

        deadline = time.monotonic() + 20.0
        while not all(ready.is_file() for _, ready, _, _ in records):
            if any(process.poll() is not None for process in processes):
                _stop_workers(processes)
                pytest.fail(
                    f"{namespace} D10 {barrier_seam} contender exited before rendezvous",
                    pytrace=False,
                )
            if time.monotonic() >= deadline:
                _stop_workers(processes)
                pytest.fail(
                    f"{namespace} D10 {barrier_seam} rendezvous was not established",
                    pytrace=False,
                )
            time.sleep(0.01)
        _write_release_marker(release)

        outcomes: list[dict[str, Any]] = []
        for name, _, output, process in records:
            try:
                stdout, stderr = process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                _stop_workers(processes)
                pytest.fail(
                    f"{namespace} D10 {barrier_seam} contender timed out",
                    pytrace=False,
                )
            if process.returncode != 0:
                transcript = (stdout + stderr)[-6000:]
                _stop_workers(processes)
                pytest.fail(
                    f"{namespace} D10 {barrier_seam} contender {name} exited "
                    f"{process.returncode}:\n{transcript}",
                    pytrace=False,
                )
            assert output.is_file()
            assert output.stat().st_mode & 0o077 == 0
            evidence = json.loads(output.read_text(encoding="utf-8"))["evidence"]
            outcomes.append(evidence)

        samples.append(
            {
                "trial": trial,
                "launch_order": list(launch_order),
                "barrier_ready_count": sum(
                    ready.is_file() for _, ready, _, _ in records
                ),
                "outcomes": sorted(
                    (
                        {
                            "ok": outcome["ok"],
                            "grants": outcome["grants"],
                            "conflicts": outcome["conflicts"],
                        }
                        for outcome in outcomes
                    ),
                    key=lambda item: (item["grants"], item["conflicts"]),
                ),
                "rows": _target_row_projection(
                    database,
                    project_key,
                    path_pattern,
                ),
            }
        )
    return samples


def test_d10_selected_parity_scaled_sqlite_lock_timeout_and_recovery(
    frozen_live_checkout_d10: Path,
    tmp_path: Path,
) -> None:
    """Three scaled BEGIN IMMEDIATE trials match without wall-clock claims."""

    projections: dict[str, dict[str, Any]] = {}
    for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE):
        output = _run_worker(
            namespace=namespace,
            scenario="lock_timeout",
            root=tmp_path / namespace,
            frozen_live_checkout=frozen_live_checkout_d10,
        )
        evidence = output["evidence"]
        projections[namespace] = {
            "configured_timeout_ms": evidence["configured_timeout_ms"],
            "effective_test_timeout_ms": evidence["effective_test_timeout_ms"],
            "journal_mode": evidence["journal_mode"],
            "external_lock_mode": evidence["external_lock_mode"],
            "samples": [
                {
                    "trial": sample["trial"],
                    "blocked_ok": sample["blocked"]["ok"],
                    "blocked_error_type": sample["blocked"]["error_type"],
                    "sanitized_database_error": (
                        "database error occurred"
                        in sample["blocked"]["error"].lower()
                    ),
                    "count_delta_while_locked": sample[
                        "count_delta_while_locked"
                    ],
                    "artifact_delta_while_locked": sample[
                        "artifact_delta_while_locked"
                    ],
                    "recovered_ok": sample["recovered"]["ok"],
                    "recovered_grants": _captured_grant_count(
                        sample["recovered"]
                    ),
                    "recovered_conflicts": _captured_conflict_count(
                        sample["recovered"]
                    ),
                    "count_delta_after_recovery": sample[
                        "count_delta_after_recovery"
                    ],
                    "artifact_delta_after_recovery": sample[
                        "artifact_delta_after_recovery"
                    ],
                }
                for sample in evidence["samples"]
            ],
        }

    expected_samples = [
        {
            "trial": trial,
            "blocked_ok": False,
            "blocked_error_type": "ToolError",
            "sanitized_database_error": True,
            "count_delta_while_locked": 0,
            "artifact_delta_while_locked": 0,
            "recovered_ok": True,
            "recovered_grants": 1,
            "recovered_conflicts": 0,
            "count_delta_after_recovery": 1,
            "artifact_delta_after_recovery": 2,
        }
        for trial in range(LOCK_TRIALS)
    ]
    expected = {
        "configured_timeout_ms": PRODUCTION_BUSY_TIMEOUT_MS,
        "effective_test_timeout_ms": TEST_BUSY_TIMEOUT_MS,
        "journal_mode": "wal",
        "external_lock_mode": "begin_immediate",
        "samples": expected_samples,
    }
    assert projections[LIVE_NAMESPACE] == expected
    assert projections[CORE_NAMESPACE] == expected
    assert projections[CORE_NAMESPACE] == projections[LIVE_NAMESPACE]


def test_d10_selected_parity_same_process_shared_root_rendezvous(
    frozen_live_checkout_d10: Path,
    tmp_path: Path,
) -> None:
    """Two clients reach the shared lock seam in every bounded sample."""

    projections: dict[str, dict[str, Any]] = {}
    for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE):
        output = _run_worker(
            namespace=namespace,
            scenario="same_process_shared_root",
            root=tmp_path / namespace,
            frozen_live_checkout=frozen_live_checkout_d10,
        )
        evidence = output["evidence"]
        assert tuple(evidence["not_established"]) == _FAIRNESS_NON_CLAIMS
        projections[namespace] = {
            "conditions": evidence["conditions"],
            "samples": [
                {
                    "trial": sample["trial"],
                    "creation_order_has_both_contenders": (
                        set(sample["creation_order"])
                        == {"GreenCastle", "BlueLake"}
                    ),
                    "lock_attempts_before_release": sample[
                        "lock_attempts_before_release"
                    ],
                    "all_calls_ok": sample["all_calls_ok"],
                    "grants": len(sample["winners"]),
                    "conflicts": len(sample["losers"]),
                    "outcomes_cover_both_contenders": (
                        set(sample["winners"] + sample["losers"])
                        == {"GreenCastle", "BlueLake"}
                    ),
                    "active_rows": sample["active_rows"],
                }
                for sample in evidence["samples"]
            ],
        }

    expected = {
        "conditions": {
            "processes": 1,
            "clients": 2,
            "database": "shared",
            "archive_lock": "shared",
            "barrier": "before_archive_lock_acquisition",
            "path_kind": "same_exact_exclusive",
        },
        "samples": [
            {
                "trial": trial,
                "creation_order_has_both_contenders": True,
                "lock_attempts_before_release": 2,
                "all_calls_ok": True,
                "grants": 1,
                "conflicts": 1,
                "outcomes_cover_both_contenders": True,
                "active_rows": 1,
            }
            for trial in range(SAME_PROCESS_RACE_TRIALS)
        ],
    }
    assert projections[LIVE_NAMESPACE] == expected
    assert projections[CORE_NAMESPACE] == expected
    assert projections[CORE_NAMESPACE] == projections[LIVE_NAMESPACE]


def test_d10_selected_parity_two_process_shared_archive_lock(
    frozen_live_checkout_d10: Path,
    tmp_path: Path,
) -> None:
    """A preinitialized shared archive lock yields one durable winner."""

    observed: dict[str, list[dict[str, Any]]] = {}
    for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE):
        observed[namespace] = _run_two_process_topology(
            namespace=namespace,
            frozen_live_checkout=frozen_live_checkout_d10,
            root=tmp_path / namespace,
            split_archive_roots=False,
        )

    expected = [
        {
            "trial": trial,
            "launch_order": (
                ["GreenCastle", "BlueLake"]
                if trial % 2 == 0
                else ["BlueLake", "GreenCastle"]
            ),
            "barrier_ready_count": 2,
            "outcomes": [
                {"ok": True, "grants": 0, "conflicts": 1},
                {"ok": True, "grants": 1, "conflicts": 0},
            ],
            "rows": {
                "row_count": 1,
                "distinct_holders": 1,
                "all_exclusive": True,
                "all_active": True,
            },
        }
        for trial in range(PROCESS_RACE_TRIALS)
    ]
    assert observed[LIVE_NAMESPACE] == expected
    assert observed[CORE_NAMESPACE] == expected
    assert observed[CORE_NAMESPACE] == observed[LIVE_NAMESPACE]


def test_d10_selected_parity_two_process_split_roots_preserve_double_grant(
    frozen_live_checkout_d10: Path,
    tmp_path: Path,
) -> None:
    """Distinct archive locks preserve upstream's forced-overlap double grant."""

    observed: dict[str, list[dict[str, Any]]] = {}
    for namespace in (LIVE_NAMESPACE, CORE_NAMESPACE):
        observed[namespace] = _run_two_process_topology(
            namespace=namespace,
            frozen_live_checkout=frozen_live_checkout_d10,
            root=tmp_path / namespace,
            split_archive_roots=True,
        )

    expected = [
        {
            "trial": trial,
            "launch_order": (
                ["GreenCastle", "BlueLake"]
                if trial % 2 == 0
                else ["BlueLake", "GreenCastle"]
            ),
            "barrier_ready_count": 2,
            "outcomes": [
                {"ok": True, "grants": 1, "conflicts": 0},
                {"ok": True, "grants": 1, "conflicts": 0},
            ],
            "rows": {
                "row_count": 2,
                "distinct_holders": 2,
                "all_exclusive": True,
                "all_active": True,
            },
        }
        for trial in range(PROCESS_RACE_TRIALS)
    ]
    assert observed[LIVE_NAMESPACE] == expected
    assert observed[CORE_NAMESPACE] == expected
    assert observed[CORE_NAMESPACE] == observed[LIVE_NAMESPACE]
