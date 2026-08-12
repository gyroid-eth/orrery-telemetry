from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from agentstack_mail import migration


def _candidate_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "candidate"
    script_source = (
        Path(__file__).parents[1] / "scripts" / "build_rehearsal_seed.py"
    )
    files = {
        Path("packages/agentstack_mail/scripts/build_rehearsal_seed.py"): script_source,
        Path(
            "packages/agentstack_mail/src/agentstack_mail/migration.py"
        ): Path(migration.__file__),
    }
    for relative, source in files.items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Candidate Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "candidate@example.test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "candidate"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, commit


def _generator_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "build_rehearsal_seed.py"
    spec = importlib.util.spec_from_file_location("rehearsal_seed_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_rehearsal_seed_is_candidate_bound_and_production_shaped(
    tmp_path: Path,
) -> None:
    repository, candidate_commit = _candidate_repository(tmp_path)
    hostile = tmp_path / "hostile-git-environment"
    hostile.mkdir()
    subprocess.run(["git", "init", "-q", str(hostile)], check=True)
    output_root = tmp_path / "rehearsal-seed"
    script = Path(__file__).parents[1] / "scripts" / "build_rehearsal_seed.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output-root",
            str(output_root),
            "--production-source-db",
            "/production/mcp-agent-mail/storage.sqlite3",
            "--candidate-repo",
            str(repository),
            "--candidate-commit",
            candidate_commit,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "GIT_DIR": str(hostile / ".git"),
            "GIT_WORK_TREE": str(hostile),
            "GIT_INDEX_FILE": str(hostile / ".git" / "index"),
        },
    )
    result = json.loads(completed.stdout)
    receipt = json.loads(
        (output_root / "generator-receipt.json").read_text(encoding="utf-8")
    )
    provenance = json.loads(
        (output_root / "seed-provenance.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "generated"
    assert result["candidate_commit"] == candidate_commit
    assert result["generator_receipt"] == str(
        output_root / "generator-receipt.json"
    )
    assert result["generator_receipt_sha256"] == hashlib.sha256(
        (output_root / "generator-receipt.json").read_bytes()
    ).hexdigest()
    assert result["seed_database_size"] >= 50 * 1024 * 1024
    assert result["major_table_rows"] == {
        "agents": 800,
        "file_reservations": 2_000,
        "message_recipients": 8_200,
        "messages": 8_200,
        "projects": 1,
    }
    assert receipt["candidate_commit"] == candidate_commit
    assert receipt["production_source_opened"] is False
    assert provenance["kind"] == "production-shaped-synthetic"
    assert provenance["seed_database"] == str(
        output_root / "legacy" / "storage.sqlite3"
    )
    assert provenance["production_source_database"] == (
        "/production/mcp-agent-mail/storage.sqlite3"
    )
    assert receipt["seed_archive"] == {
        "path": str(output_root / "legacy" / "archive"),
        "snapshot": migration.snapshot_tree(
            output_root / "legacy" / "archive",
            required=True,
            excluded_root_names=migration.ARCHIVE_EXCLUDED_ROOT_NAMES,
        ),
    }
    assert receipt["seed_signals"] == {
        "path": str(output_root / "legacy" / "signals"),
        "snapshot": migration.snapshot_tree(
            output_root / "legacy" / "signals", required=False
        ),
    }

    database = sqlite3.connect(output_root / "legacy" / "storage.sqlite3")
    try:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, expected in result["major_table_rows"].items():
            assert database.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone() == (expected,)
    finally:
        database.close()
    assert not (output_root / "legacy" / "storage.sqlite3-wal").exists()
    assert not (output_root / "legacy" / "storage.sqlite3-shm").exists()


@pytest.mark.parametrize("failure", ["tree-fsync", "atomic-rename"])
def test_seed_generator_prepublication_failure_leaves_no_canonical_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    generator = _generator_module()
    repository, candidate_commit = _candidate_repository(tmp_path)
    output_root = tmp_path / "failed-seed"
    if failure == "tree-fsync":
        monkeypatch.setattr(
            generator,
            "_fsync_tree",
            lambda _root: (_ for _ in ()).throw(
                OSError(errno.EIO, "injected seed-tree fsync failure")
            ),
        )
    else:
        original_replace = generator.os.replace

        def fail_publish(source: Path, destination: Path) -> None:
            if Path(destination) == output_root:
                raise OSError(errno.EIO, "injected seed publish rename failure")
            original_replace(source, destination)

        monkeypatch.setattr(generator.os, "replace", fail_publish)

    with pytest.raises(OSError, match="injected seed"):
        generator.build_rehearsal_seed(
            output_root,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            repository,
            candidate_commit=candidate_commit,
        )
    assert not output_root.exists()
    assert not list(tmp_path.glob(".failed-seed.seed-*"))


def test_seed_generator_parent_fsync_failure_quarantines_published_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _generator_module()
    repository, candidate_commit = _candidate_repository(tmp_path)
    output_root = tmp_path / "unconfirmed-seed"
    original_fsync = generator.os.fsync
    injected = False

    def fail_published_parent(descriptor: int) -> None:
        nonlocal injected
        descriptor_info = os.fstat(descriptor)
        parent_info = output_root.parent.stat()
        if (
            output_root.exists()
            and not injected
            and descriptor_info.st_dev == parent_info.st_dev
            and descriptor_info.st_ino == parent_info.st_ino
        ):
            injected = True
            raise OSError(errno.EIO, "injected seed parent fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(generator.os, "fsync", fail_published_parent)
    with pytest.raises(OSError, match="seed parent fsync failure"):
        generator.build_rehearsal_seed(
            output_root,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            repository,
            candidate_commit=candidate_commit,
        )
    assert injected is True
    assert not output_root.exists()
    unconfirmed = list(tmp_path.glob(".unconfirmed-seed.seed-*.unconfirmed"))
    assert len(unconfirmed) == 1
    receipt = json.loads(
        (unconfirmed[0] / "generator-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["kind"] == "production-shaped-synthetic-seed-generation"


def test_seed_generator_postpublish_result_failure_quarantines_published_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _generator_module()
    repository, candidate_commit = _candidate_repository(tmp_path)
    output_root = tmp_path / "postpublish-failure-seed"
    original_sha256 = generator._sha256
    injected = False

    def fail_final_receipt_read(path: Path) -> str:
        nonlocal injected
        if (
            path == output_root / "generator-receipt.json"
            and output_root.exists()
            and not injected
        ):
            injected = True
            raise OSError(errno.EIO, "injected postpublish receipt read failure")
        return original_sha256(path)

    monkeypatch.setattr(generator, "_sha256", fail_final_receipt_read)
    with pytest.raises(OSError, match="postpublish receipt read failure"):
        generator.build_rehearsal_seed(
            output_root,
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            repository,
            candidate_commit=candidate_commit,
        )
    assert injected is True
    assert not output_root.exists()
    unconfirmed = list(
        tmp_path.glob(".postpublish-failure-seed.seed-*.unconfirmed")
    )
    assert len(unconfirmed) == 1
    receipt = json.loads(
        (unconfirmed[0] / "generator-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["kind"] == "production-shaped-synthetic-seed-generation"


def test_seed_generator_rejects_dirty_or_unknown_candidate_before_staging(
    tmp_path: Path,
) -> None:
    generator = _generator_module()
    repository, candidate_commit = _candidate_repository(tmp_path)
    dirty = repository / "dirty.txt"
    dirty.write_text("untracked\n", encoding="utf-8")
    with pytest.raises(generator.SeedBuildError, match=r"must be .*clean"):
        generator.build_rehearsal_seed(
            tmp_path / "dirty-seed",
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            repository,
            candidate_commit=candidate_commit,
        )
    dirty.unlink()
    with pytest.raises(generator.SeedBuildError, match="exact checkout HEAD"):
        generator.build_rehearsal_seed(
            tmp_path / "unknown-seed",
            Path("/production/mcp-agent-mail/storage.sqlite3"),
            repository,
            candidate_commit="1" * 40,
        )
    assert not list(tmp_path.glob(".*-seed.seed-*"))
