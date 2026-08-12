from __future__ import annotations

import asyncio
import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from agentstack_mail import app
from agentstack_mail.config import clear_settings_cache
from agentstack_mail.db import ensure_schema, get_session, reset_database_state
from agentstack_mail.models import Agent, FileReservation, Project


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _init_repo(tmp_path: Path, file_count: int = 57) -> tuple[Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Reservation Test")
    patterns: list[str] = []
    for index in range(file_count):
        name = f"probe-{index:03d}.txt"
        (repo / name).write_text(f"{index}\n", encoding="utf-8")
        patterns.append(name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed probe files")
    return repo, patterns


def _compute_serial(
    repo: Path,
    patterns: list[str],
) -> list[app._ReservationActivityResult]:
    recent_after = datetime.now(timezone.utc) - timedelta(days=1)
    return [
        app._compute_reservation_activity(
            repo,
            repo,
            pattern,
            recent_after=recent_after,
            total_deadline=time.monotonic() + 30.0,
        )
        for pattern in patterns
    ]


def test_57_probe_results_and_order_match_serial_reference(tmp_path: Path) -> None:
    repo, patterns = _init_repo(tmp_path)
    serial = _compute_serial(repo, patterns)
    concurrent = asyncio.run(
        app._probe_reservation_activities(
            repo,
            repo,
            patterns,
            recent_after=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )

    assert len(concurrent) == 57
    assert concurrent == serial
    assert all(result.probe_complete for result in concurrent)


class _TrackedProcess:
    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        lock: threading.Lock,
        state: dict[str, int],
    ) -> None:
        self._process = process
        self._lock = lock
        self._state = state
        self._counted = True

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def _finish_count(self) -> None:
        with self._lock:
            if self._counted:
                self._state["active"] -= 1
                self._counted = False

    def communicate(self, *args: Any, **kwargs: Any) -> tuple[str, str]:
        try:
            return self._process.communicate(*args, **kwargs)
        finally:
            if self._process.poll() is not None:
                self._finish_count()

    def kill(self) -> None:
        self._process.kill()


def test_two_collectors_share_one_process_global_git_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, patterns = _init_repo(tmp_path)
    patterns = patterns[:16]
    real_git = shutil.which("git")
    assert real_git is not None
    slow_git = tmp_path / "slow-git"
    slow_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "time.sleep(0.08)\n"
        f"os.execv({real_git!r}, ['git', *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    slow_git.chmod(0o755)

    real_popen = subprocess.Popen
    lock = threading.Lock()
    state = {"active": 0, "maximum": 0}

    def tracking_popen(*args: Any, **kwargs: Any) -> _TrackedProcess:
        process = real_popen(*args, **kwargs)
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        return _TrackedProcess(process, lock=lock, state=state)

    monkeypatch.setattr(app, "_git_executable", lambda: str(slow_git))
    monkeypatch.setattr(app.subprocess, "Popen", tracking_popen)
    monkeypatch.setattr(
        app,
        "Repo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reservation probes must not construct or share Repo")
        ),
    )

    async def run_both() -> list[list[app._ReservationActivityResult]]:
        recent_after = datetime.now(timezone.utc) - timedelta(days=1)
        return await asyncio.gather(
            app._probe_reservation_activities(
                repo, repo, patterns, recent_after=recent_after
            ),
            app._probe_reservation_activities(
                repo, repo, patterns, recent_after=recent_after
            ),
        )

    results = asyncio.run(run_both())

    assert state["maximum"] == app._RESERVATION_PROBE_CONCURRENCY == 8
    assert state["active"] == 0
    assert all(result.probe_complete for batch in results for result in batch)


def test_slow_git_is_killed_by_total_deadline_without_lingering_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, patterns = _init_repo(tmp_path)
    pid_file = tmp_path / "slow-git.pids"
    slow_git = tmp_path / "hanging-git"
    slow_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        f"with open({str(pid_file)!r}, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(f'{os.getpid()}\\n')\n"
        "    stream.flush()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    slow_git.chmod(0o755)
    monkeypatch.setattr(app, "_git_executable", lambda: str(slow_git))

    started = time.monotonic()
    results = asyncio.run(
        app._probe_reservation_activities(
            repo,
            repo,
            patterns,
            recent_after=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    elapsed = time.monotonic() - started

    assert elapsed <= app._RESERVATION_PROBE_TOTAL_TIMEOUT_SECONDS + 0.5
    assert all(not result.probe_complete for result in results)
    time.sleep(0.2)
    pids = [int(line) for line in pid_file.read_text(encoding="utf-8").splitlines()]
    assert pids
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_no_commit_is_complete_but_git_error_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _patterns = _init_repo(tmp_path, file_count=1)
    untracked = repo / "untracked.txt"
    untracked.write_text("not committed\n", encoding="utf-8")
    complete = app._compute_reservation_activity(
        repo,
        repo,
        untracked.name,
        recent_after=datetime.now(timezone.utc) - timedelta(days=1),
        total_deadline=time.monotonic() + 3.0,
    )
    assert complete.probe_complete is True
    assert complete.git_activity is None

    failing_git = tmp_path / "failing-git"
    failing_git.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    failing_git.chmod(0o755)
    monkeypatch.setattr(app, "_git_executable", lambda: str(failing_git))
    failed = app._compute_reservation_activity(
        repo,
        repo,
        untracked.name,
        recent_after=datetime.now(timezone.utc) - timedelta(days=1),
        total_deadline=time.monotonic() + 3.0,
    )
    assert failed.probe_complete is False
    assert failed.git_activity is None


def test_glob_scan_that_crosses_deadline_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _patterns = _init_repo(tmp_path, file_count=1)

    def slow_paths(_base: Path, _normalized: str) -> Any:
        time.sleep(0.08)
        yield repo / "probe-000.txt"

    monkeypatch.setattr(app, "_iter_matching_paths", slow_paths)
    result = app._compute_reservation_activity(
        repo,
        repo,
        "*.txt",
        recent_after=None,
        total_deadline=time.monotonic() + 0.04,
    )
    assert result.probe_complete is False
    assert result.git_activity is None


@pytest.fixture
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "mail.sqlite3"
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{database}",
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR",
        str(tmp_path / "signals"),
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_FILE_RESERVATION_INACTIVITY_SECONDS", "600")
    monkeypatch.setenv("AGENTSTACK_MAIL_FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "300")
    monkeypatch.setenv("AGENTSTACK_MAIL_GIT_AUTHOR_NAME", "Reservation Test")
    monkeypatch.setenv("AGENTSTACK_MAIL_GIT_AUTHOR_EMAIL", "test@example.com")
    clear_settings_cache()
    reset_database_state()
    yield
    reset_database_state()
    clear_settings_cache()


async def _seed_reservations(
    workspace: Path,
    *,
    expired_ttl: bool,
    path_pattern: str = "probe.txt",
) -> tuple[int, int, int | None]:
    await ensure_schema()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as session:
        project = Project(slug="reservation-test", human_key=str(workspace))
        session.add(project)
        await session.flush()
        assert project.id is not None
        agent = Agent(
            project_id=project.id,
            name="BlueLake",
            program="test",
            model="test",
            last_active_ts=now - timedelta(minutes=30),
        )
        session.add(agent)
        await session.flush()
        assert agent.id is not None
        active = FileReservation(
            project_id=project.id,
            agent_id=agent.id,
            path_pattern=path_pattern,
            expires_ts=now + timedelta(hours=1),
        )
        session.add(active)
        expired: FileReservation | None = None
        if expired_ttl:
            expired = FileReservation(
                project_id=project.id,
                agent_id=agent.id,
                path_pattern="expired.txt",
                expires_ts=now - timedelta(seconds=1),
            )
            session.add(expired)
        await session.commit()
        await session.refresh(active)
        if expired is not None:
            await session.refresh(expired)
        assert active.id is not None
        return project.id, active.id, expired.id if expired is not None else None


def test_unknown_probe_never_auto_releases_but_ttl_expiry_still_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "probe.txt").write_text("active\n", encoding="utf-8")
    (workspace / "expired.txt").write_text("expired\n", encoding="utf-8")

    async def unknown_probes(
        _workspace: Path | None,
        _repo_root: Path | None,
        patterns: list[str],
        *,
        recent_after: datetime | None,
    ) -> list[app._ReservationActivityResult]:
        del recent_after
        return [
            app._ReservationActivityResult(probe_complete=False)
            for _pattern in patterns
        ]

    async def no_archive_write(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(app, "_probe_reservation_activities", unknown_probes)
    monkeypatch.setattr(app, "_write_file_reservation_records", no_archive_write)

    async def exercise() -> tuple[Any, Any, list[app.FileReservationStatus]]:
        project_id, active_id, expired_id = await _seed_reservations(
            workspace,
            expired_ttl=True,
        )
        assert expired_id is not None
        sweep = await app._expire_stale_file_reservations(project_id)
        async with get_session() as session:
            active = await session.get(FileReservation, active_id)
            expired = await session.get(FileReservation, expired_id)
        return active, expired, sweep.auto_released

    active, expired, released = asyncio.run(exercise())
    assert active is not None and active.released_ts is None
    assert expired is not None and expired.released_ts is not None
    assert released == []


def test_recent_git_alone_prevents_stale_release(
    tmp_path: Path,
    isolated_runtime: None,
) -> None:
    workspace, _patterns = _init_repo(tmp_path, file_count=1)
    tracked = workspace / "probe-000.txt"
    now = datetime.now(timezone.utc)
    commit_time = (now - timedelta(minutes=1)).isoformat()
    commit_env = os.environ.copy()
    commit_env["GIT_AUTHOR_DATE"] = commit_time
    commit_env["GIT_COMMITTER_DATE"] = commit_time
    _git(workspace, "commit", "--amend", "--no-edit", env=commit_env)
    old_mtime = (now - timedelta(minutes=15)).timestamp()
    os.utime(tracked, (old_mtime, old_mtime))

    async def exercise() -> app.FileReservationStatus:
        project_id, _active_id, _expired_id = await _seed_reservations(
            workspace,
            expired_ttl=False,
            path_pattern="probe-000.txt",
        )
        async with get_session() as session:
            project = await session.get(Project, project_id)
        assert project is not None
        statuses = await app._collect_file_reservation_statuses(project, now=now)
        assert len(statuses) == 1
        return statuses[0]

    status = asyncio.run(exercise())
    assert status.probe_complete is True
    assert status.activity_unknown is False
    assert status.last_fs_activity is not None
    assert now - status.last_fs_activity >= timedelta(minutes=14)
    assert status.last_git_activity is not None
    assert now - status.last_git_activity <= timedelta(minutes=5)
    assert status.stale is False
    assert "git_activity_recent" in status.stale_reasons


def test_resource_uses_sweep_statuses_without_a_second_collect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project(id=1, slug="single-pass", human_key="/tmp/single-pass")
    marker = object()
    calls = 0

    async def one_sweep(
        project_id: int,
        **kwargs: Any,
    ) -> app._FileReservationSweepResult:
        nonlocal calls
        calls += 1
        assert project_id == 1
        assert kwargs["include_released_statuses"] is True
        return app._FileReservationSweepResult(
            auto_released=[],
            statuses=[marker],  # type: ignore[list-item]
        )

    monkeypatch.setattr(app, "_expire_stale_file_reservations", one_sweep)
    statuses = asyncio.run(
        app._file_reservation_resource_statuses(project, active_only=False)
    )

    assert calls == 1
    assert statuses == [marker]


def test_performance_gate_runs_the_57_path_contract(tmp_path: Path) -> None:
    repo, _patterns = _init_repo(tmp_path)
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "reservation_performance_gate.py"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(repo),
            "--threshold-seconds",
            "10",
            "--repetitions",
            "3",
            "--skip-live-snapshot",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    payloads = [json.loads(line) for line in completed.stdout.splitlines()]
    runs = [
        payload for payload in payloads if payload.get("set") == "57-concrete"
    ]
    summary = next(
        payload for payload in payloads if payload.get("set") == "57-concrete-summary"
    )
    assert len(runs) == 3
    assert all(payload["count"] == 57 for payload in runs)
    assert all(payload["matched"] == 57 for payload in runs)
    assert all(payload["probe_complete"] == 57 for payload in runs)
    assert len({payload["input_sha256"] for payload in runs}) == 1
    assert all("result_sha256" not in payload for payload in runs)
    assert summary["complete_runs"] == 3
    assert summary["required_complete_runs"] == 2
    assert summary["median_wall_seconds"] <= summary["max_wall_seconds"]
    assert payloads[-1]["passed"] is True


def test_performance_gate_uses_median_and_complete_run_majority() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "reservation_performance_gate.py"
    )
    namespace = runpy.run_path(str(script))
    summarize = namespace["summarize_concrete_runs"]
    passes = namespace["passes_gate"]

    def result(seconds: float, complete: bool = True) -> dict[str, Any]:
        count = 57 if complete else 40
        return {
            "wall_seconds": seconds,
            "matched": count,
            "probe_complete": count,
            "input_sha256": "stable-input",
        }

    one_loaded_outlier = summarize(
        [
            result(1.5),
            result(1.6),
            result(4.02, complete=False),
            result(1.7),
            result(2.0),
        ],
        expected_count=57,
    )
    serial_regression = summarize(
        [result(9.3), result(9.5), result(9.7), result(9.4), result(9.6)],
        expected_count=57,
    )
    incomplete_majority = summarize(
        [
            result(1.5),
            result(1.6),
            result(4.0, complete=False),
            result(4.0, complete=False),
            result(4.0, complete=False),
        ],
        expected_count=57,
    )

    assert passes(one_loaded_outlier, threshold_seconds=6.0) is True
    assert passes(serial_regression, threshold_seconds=6.0) is False
    assert passes(incomplete_majority, threshold_seconds=6.0) is False
