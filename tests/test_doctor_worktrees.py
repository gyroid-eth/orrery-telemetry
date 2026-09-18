"""Focused coverage for doctor reporting of persistent worktree leftovers."""

from __future__ import annotations

import os
import pathlib
import sqlite3
import stat
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "doctor.sh"


def _extract_function(name: str) -> str:
    text = DOCTOR.read_text(encoding="utf-8")
    marker = f"\n{name}() {{"
    start = text.index(marker) + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def test_doctor_reports_only_unregistered_nonlive_worktrees_without_deleting(tmp_path):
    project_key = str(tmp_path / "project")
    database = tmp_path / "mail.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                retired_at TEXT
            );
            """
        )
        connection.execute("INSERT INTO projects VALUES (1, ?)", (project_key,))
        connection.executemany(
            "INSERT INTO agents VALUES (?, 1, ?, ?)",
            (
                (1, "RegisteredChild", None),
                (2, "RetiredChild", "2026-09-18T00:00:00Z"),
            ),
        )

    worktree_root = tmp_path / "worktrees"
    for name in ("LiveChild", "RegisteredChild", "RetiredChild", "OrphanChild"):
        (worktree_root / name).mkdir(parents=True)
        (worktree_root / name / "uncommitted.txt").write_text(
            "keep me\n", encoding="utf-8"
        )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text(
        "#!/bin/bash\n"
        '[[ "$1" == "has-session" && "$2" == "-t" && "$3" == "=LiveChild" ]]\n',
        encoding="utf-8",
    )
    tmux.chmod(tmux.stat().st_mode | stat.S_IEXEC)

    script = "\n".join(
        (
            "set -euo pipefail",
            f"PYTHON_BIN={str(pathlib.Path(sys.executable))!r}",
            f"MAIL_DB_PATH={str(database)!r}",
            f"PROJECT_KEY={project_key!r}",
            f"WORKTREE_ROOT={str(worktree_root)!r}",
            f"HOME={str(tmp_path / 'home')!r}",
            _extract_function("report_orphan_worktrees"),
            "report_orphan_worktrees",
        )
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "LiveChild" not in result.stderr
    assert "RegisteredChild" not in result.stderr
    assert str(worktree_root / "RetiredChild") in result.stderr
    assert str(worktree_root / "OrphanChild") in result.stderr
    assert "remove it manually" in result.stderr
    for name in ("LiveChild", "RegisteredChild", "RetiredChild", "OrphanChild"):
        assert (worktree_root / name / "uncommitted.txt").read_text(
            encoding="utf-8"
        ) == "keep me\n"
