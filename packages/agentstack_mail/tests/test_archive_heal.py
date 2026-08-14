"""Startup repair for the Git audit archive."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from git import Repo

from agentstack_mail import config, storage


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> config.Settings:
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    config.clear_settings_cache()
    return config.get_settings()


def _archive_with_dirty_files(tmp_path: Path) -> tuple[Repo, Path]:
    root = tmp_path / "archive"
    root.mkdir(parents=True)
    repo = Repo.init(str(root))
    attributes = root / ".gitattributes"
    tracked = root / "tracked.md"
    attributes.write_text("* text=auto\n", encoding="utf-8")
    tracked.write_text("before\n", encoding="utf-8")
    repo.index.add([".gitattributes", "tracked.md"])
    repo.index.commit("init")
    tracked.write_text("after\n", encoding="utf-8")
    (root / "untracked.md").write_text("left by a hard shutdown\n", encoding="utf-8")
    return repo, root


def test_startup_heal_commits_untracked_and_modified_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    repo, root = _archive_with_dirty_files(tmp_path)
    before = sum(1 for _ in repo.iter_commits())

    summary = asyncio.run(storage.heal_archive_locks(settings))

    assert summary["recovery_error"] is None
    assert summary["recovered_paths"] == ["tracked.md", "untracked.md"]
    assert summary["gc_due"] is True
    assert summary["gc_checked"] is True
    assert summary["gc_error"] is None
    assert sum(1 for _ in repo.iter_commits()) == before + 1
    assert repo.head.commit.message == "chore: recover uncommitted archive files"
    assert not repo.is_dirty(untracked_files=True)
    assert (root / ".git" / storage._ARCHIVE_GC_MARKER_NAME).is_file()
    repo.close()


def test_startup_gc_check_is_rate_limited_for_one_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    repo, root = _archive_with_dirty_files(tmp_path)
    first = asyncio.run(storage.heal_archive_locks(settings))
    assert first["gc_checked"] is True
    index_before = (root / ".git" / "index").read_bytes()

    called = False

    async def unexpected_gc(_root: Path) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(storage, "_run_archive_gc_auto", unexpected_gc)
    second = asyncio.run(storage.heal_archive_locks(settings))

    assert second["gc_due"] is False
    assert second["gc_checked"] is False
    assert called is False
    assert (root / ".git" / "index").read_bytes() == index_before
    repo.close()
