"""Native Windows regression coverage for deep Mail archive paths."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from git import Repo

from agentstack_mail import config, storage


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows path semantics",
)


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> config.Settings:
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    config.clear_settings_cache()
    return config.get_settings()


def test_message_archive_preserves_identity_beyond_max_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    project_slug = "project-" + ("x" * 140)
    sender = "BlueLake"
    recipient = "GreenCastle"
    subject = "windows-long-path-" + ("s" * 80)
    created = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    filename = (
        "2026-09-04T12-00-00Z__"
        + storage._SUBJECT_SLUG_RE.sub("-", subject).strip("-_").lower()[:80]
        + "__1.md"
    )
    logical_root = Path(settings.storage.root).expanduser().resolve()
    logical_message = (
        logical_root
        / "projects"
        / project_slug
        / "agents"
        / recipient
        / "inbox"
        / "2026"
        / "09"
        / filename
    )
    assert len(str(logical_message)) > 260

    async def exercise_archive() -> None:
        try:
            archive = await storage.ensure_archive(settings, project_slug)
            assert archive.slug == project_slug
            assert archive.root.name == project_slug
            assert str(archive.root).startswith("\\\\?\\")

            await storage.write_agent_profile(
                archive,
                {"name": sender, "project": project_slug},
            )
            await storage.write_message_bundle(
                archive,
                {
                    "id": 1,
                    "created": created.isoformat(),
                    "subject": subject,
                    "project": project_slug,
                },
                "Native Windows long-path regression.",
                sender,
                [recipient],
            )
        finally:
            await storage.shutdown_commit_queue()

    try:
        asyncio.run(exercise_archive())

        extended_message = storage._windows_extended_path(logical_message)
        assert extended_message.is_file()

        pending_message = logical_message.with_name(
            "pending-startup-recovery-" + ("p" * 96) + ".md"
        )
        extended_pending = storage._windows_extended_path(pending_message)
        extended_pending.write_text("recover me\n", encoding="utf-8")
        expected_pending = pending_message.relative_to(logical_root).as_posix()
        summary = asyncio.run(storage.heal_archive_locks(settings))
        assert summary["recovered_paths"] == [expected_pending]

        repo = Repo(str(logical_root))
        try:
            assert repo.git.config("--local", "--get", "core.longpaths") == "true"
            tracked = set(repo.git.ls_files().splitlines())
            expected = logical_message.relative_to(logical_root).as_posix()
            assert expected in tracked
            assert expected_pending in tracked
            assert not repo.is_dirty(untracked_files=True)
        finally:
            repo.close()
    finally:
        storage.clear_repo_cache()
        extended_root = storage._windows_extended_path(logical_root)
        if extended_root.exists():
            def remove_readonly(function: object, path: str, _error: object) -> None:
                os.chmod(path, stat.S_IWRITE)
                function(path)  # type: ignore[operator]

            shutil.rmtree(extended_root, onerror=remove_readonly)
