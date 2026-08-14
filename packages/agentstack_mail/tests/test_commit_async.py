"""Async archive commits (AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC).

Measured on 2026-08-13: every archive-writing tool call (send_message,
register_agent, file_reservation_paths, ...) blocked on a synchronous git
commit into a 53k-file audit repo — a 1.7s floor per call in isolation and
3.7-7s in production. The archive files are written to the working tree
before the commit, so deferring only the commit keeps the audit trail while
taking git out of the request path.

Commits are deferred by default; setting the environment variable to false is
the kill switch that restores synchronous commits.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from git import Repo

from agentstack_mail import config, storage


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> None:
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.clear_settings_cache()


def _make_repo(tmp_path: Path) -> tuple[Repo, Path]:
    root = tmp_path / "archive"
    root.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(str(root))
    seed = root / ".gitattributes"
    seed.write_text("* text=auto\n")
    repo.index.add([".gitattributes"])
    repo.index.commit("init")
    return repo, root


def _write_probe(root: Path, name: str) -> str:
    path = root / name
    path.write_text(f"probe {name}\n")
    return path.relative_to(root).as_posix()


def _commit_count(repo: Repo) -> int:
    return sum(1 for _ in repo.iter_commits())


def test_commit_async_defaults_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure(monkeypatch, tmp_path)
    settings = config.get_settings()
    assert settings.storage.commit_async is True


def test_commit_async_false_is_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC="false")
    settings = config.get_settings()
    assert settings.storage.commit_async is False


def test_sync_commit_lands_before_return(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC="false")
    settings = config.get_settings()
    repo, root = _make_repo(tmp_path)
    rel = _write_probe(root, "sync.md")

    async def run() -> None:
        await storage._commit(repo, settings, "probe: sync", [rel], use_queue=False)
        assert _commit_count(repo) == 2

    asyncio.run(run())


def test_async_commit_returns_first_then_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    settings = config.get_settings()
    assert settings.storage.commit_async is True
    repo, root = _make_repo(tmp_path)
    rel = _write_probe(root, "async.md")

    async def run() -> None:
        before = _commit_count(repo)
        await storage._commit(repo, settings, "probe: async", [rel], use_queue=False)
        # The commit runs in a background task; poll for it to land.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _commit_count(repo) == before + 1:
                break
            await asyncio.sleep(0.05)
        assert _commit_count(repo) == before + 1

    asyncio.run(run())


def test_async_commit_via_queue_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC="true")
    settings = config.get_settings()
    repo, root = _make_repo(tmp_path)
    rel = _write_probe(root, "queued.md")

    async def run() -> None:
        queue = storage._CommitQueue()
        await queue.start()
        try:
            before = _commit_count(repo)
            await queue.enqueue(root, settings, "probe: queued async", [rel], wait=False)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if _commit_count(repo) == before + 1:
                    break
                await asyncio.sleep(0.05)
            assert _commit_count(repo) == before + 1
        finally:
            await queue.stop()

    asyncio.run(run())


def test_graceful_shutdown_drains_queue_and_preserves_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    settings = config.get_settings()
    repo, root = _make_repo(tmp_path)
    rel = _write_probe(root, "shutdown.md")
    cwd_before = Path.cwd()

    async def run() -> None:
        before = _commit_count(repo)
        await storage._commit(repo, settings, "probe: graceful shutdown", [rel])
        await storage.shutdown_commit_queue()
        assert _commit_count(repo) == before + 1

    asyncio.run(run())
    assert Path.cwd() == cwd_before


def test_async_commit_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC="true")
    settings = config.get_settings()
    repo, root = _make_repo(tmp_path)

    async def run() -> None:
        # Nonexistent path: the background commit fails, but the caller's
        # await returns without raising.
        await storage._commit(
            repo, settings, "probe: bad path", ["does-not-exist.md"], use_queue=False
        )
        # Give the background task a moment to fail and be consumed.
        for _ in range(100):
            if not storage._BACKGROUND_COMMIT_TASKS:
                break
            await asyncio.sleep(0.05)
        assert not storage._BACKGROUND_COMMIT_TASKS

    asyncio.run(run())
