"""Crash observations for unresolved AgentStack Mail decisions D8 and D9.

These probes freeze today's behavior as evidence for a pending decision, not as
an assertion that the behavior is correct. Rewrite them to encode the chosen
requirement after the decision.

These tests record current frozen-live and Core durability; passing is not an
approval of the behavior.  Each operation runs in a worker-private subprocess
and is terminated by a literal ``SIGKILL`` at a test-installed seam.

D8 probes two deterministic Python seams.  A test-only ``_write_text`` wrapper
kills immediately after the first or second successful canonical/outbox/inbox
write, fixing the observable partial-bundle subsets.  The existing later seam
kills after all three files are written and GitPython stages them, but before
``IndexFile.commit`` runs.  Only an instruction-level crash inside Git's native
commit remains without a direct test seam.

D9 wraps the existing timestamp helper, lets its ``read_ts`` transaction
commit, then kills the process before control returns to
``acknowledge_message`` for the separate ``ack_ts`` update.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
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
TESTS_ROOT = Path(__file__).resolve().parent
CORE_SOURCE = PACKAGE_ROOT / "src"
_NAMESPACES = (LIVE_NAMESPACE, CORE_NAMESPACE)
_D8_SUBJECT = "D8 crash before Git commit"
_D8_SUBSET_SUBJECT = "D8 crash after bundle subset"
_D9_SUBJECT = "D9 crash between read and ack"


_D8_WORKER = r"""
import asyncio
import importlib
import json
import os
import signal
import sys
from pathlib import Path

from differential_probe import _install_llm_stub


namespace, root_text = sys.argv[1:3]
root = Path(root_text)
project_key = str(root / "project")
marker = root / "d8-staged-before-commit.json"
Path(project_key).mkdir(parents=True, exist_ok=True)


def write_marker(payload):
    with marker.open("w", encoding="utf-8") as output:
        json.dump(payload, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")


async def main():
    from fastmcp import Client
    from git.index.base import IndexFile

    async with Client(app.build_mcp_server()) as client:
        async def call(name, arguments):
            result = await client.call_tool(name, arguments, raise_on_error=False)
            if result.is_error:
                raise AssertionError(f"setup tool {name} failed: {result.data!r}")
            return result.data

        await call("ensure_project", {"human_key": project_key, "format": "json"})
        for agent_name, token in (
            ("GreenCastle", "d8-green-owner-token"),
            ("BlueLake", "d8-blue-owner-token"),
        ):
            await call(
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "pending-decision-crash-probe",
                    "model": "fixture-model",
                    "name": agent_name,
                    "task_description": "D8 literal crash probe",
                    "registration_token": token,
                    "format": "json",
                },
            )
        await call(
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "open",
                "format": "json",
            },
        )

        original_commit = IndexFile.commit

        def kill_after_stage(index, message, *args, **kwargs):
            write_marker(
                {
                    "seam": "bundle_files_written_and_index_staged_before_commit",
                    "head_before": index.repo.head.commit.hexsha,
                    "commit_message": str(message),
                }
            )
            os.kill(os.getpid(), signal.SIGKILL)
            raise AssertionError("SIGKILL unexpectedly returned")

        IndexFile.commit = kill_after_stage
        try:
            await call(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": "GreenCastle",
                    "sender_token": "d8-green-owner-token",
                    "to": ["BlueLake"],
                    "subject": "D8 crash before Git commit",
                    "body_md": "The child must die after staging this bundle.",
                    "format": "json",
                },
            )
        finally:
            IndexFile.commit = original_commit


asyncio.run(main())
"""


_D8_SUBSET_WORKER = r"""
import asyncio
import importlib
import json
import os
import signal
import sys
from pathlib import Path

from differential_probe import _install_llm_stub


namespace, root_text, kill_after_text = sys.argv[1:4]
kill_after = int(kill_after_text)
root = Path(root_text)
project_key = str(root / "project")
marker = root / f"d8-subset-after-{kill_after}.json"
Path(project_key).mkdir(parents=True, exist_ok=True)


def write_marker(payload):
    with marker.open("w", encoding="utf-8") as output:
        json.dump(payload, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")
storage = importlib.import_module(f"{namespace}.storage")


async def main():
    from fastmcp import Client
    from git import Repo

    async with Client(app.build_mcp_server()) as client:
        async def call(name, arguments):
            result = await client.call_tool(name, arguments, raise_on_error=False)
            if result.is_error:
                raise AssertionError(f"setup tool {name} failed: {result.data!r}")
            return result.data

        await call("ensure_project", {"human_key": project_key, "format": "json"})
        for agent_name, token in (
            ("GreenCastle", "d8-subset-green-owner-token"),
            ("BlueLake", "d8-subset-blue-owner-token"),
        ):
            await call(
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "pending-decision-crash-probe",
                    "model": "fixture-model",
                    "name": agent_name,
                    "task_description": "D8 subset-write crash probe",
                    "registration_token": token,
                    "format": "json",
                },
            )
        await call(
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "open",
                "format": "json",
            },
        )

        repo = Repo(str(root / "archive"))
        try:
            head_before = repo.head.commit.hexsha
        finally:
            repo.close()

        original_write_text = storage._write_text
        successful_paths = []

        async def kill_after_successful_subset(path, content):
            await original_write_text(path, content)
            if "__d8-crash-after-bundle-subset__" not in path.name:
                return
            successful_paths.append(path.relative_to(root / "archive").as_posix())
            if len(successful_paths) == kill_after:
                write_marker(
                    {
                        "seam": "successful_bundle_write_before_next_copy",
                        "successful_write_count": len(successful_paths),
                        "successful_paths": successful_paths,
                        "head_before": head_before,
                    }
                )
                os.kill(os.getpid(), signal.SIGKILL)
                raise AssertionError("SIGKILL unexpectedly returned")

        storage._write_text = kill_after_successful_subset
        try:
            await call(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": "GreenCastle",
                    "sender_token": "d8-subset-green-owner-token",
                    "to": ["BlueLake"],
                    "subject": "D8 crash after bundle subset",
                    "body_md": "Only the completed bundle subset may survive.",
                    "format": "json",
                },
            )
        finally:
            storage._write_text = original_write_text


asyncio.run(main())
"""


_D9_WORKER = r"""
import asyncio
import importlib
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

from differential_probe import _install_llm_stub


namespace, root_text = sys.argv[1:3]
root = Path(root_text)
project_key = str(root / "project")
database_path = root / "mail.sqlite3"
marker = root / "d9-read-committed-before-ack.json"
Path(project_key).mkdir(parents=True, exist_ok=True)


def write_marker(payload):
    with marker.open("w", encoding="utf-8") as output:
        json.dump(payload, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")


async def main():
    from fastmcp import Client

    async with Client(app.build_mcp_server()) as client:
        async def call(name, arguments):
            result = await client.call_tool(name, arguments, raise_on_error=False)
            if result.is_error:
                raise AssertionError(f"setup tool {name} failed: {result.data!r}")
            return result.data

        await call("ensure_project", {"human_key": project_key, "format": "json"})
        for agent_name, token in (
            ("GreenCastle", "d9-green-owner-token"),
            ("BlueLake", "d9-blue-owner-token"),
        ):
            await call(
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "pending-decision-crash-probe",
                    "model": "fixture-model",
                    "name": agent_name,
                    "task_description": "D9 literal crash probe",
                    "registration_token": token,
                    "format": "json",
                },
            )
        await call(
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": "BlueLake",
                "policy": "open",
                "format": "json",
            },
        )
        await call(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": "GreenCastle",
                "sender_token": "d9-green-owner-token",
                "to": ["BlueLake"],
                "subject": "D9 crash between read and ack",
                "body_md": "The child must die after read_ts commits.",
                "ack_required": True,
                "format": "json",
            },
        )

        connection = sqlite3.connect(database_path)
        try:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE subject = ?",
                    ("D9 crash between read and ack",),
                ).fetchone()[0]
            )
        finally:
            connection.close()

        original_update = app._update_recipient_timestamp

        async def kill_after_read(agent, candidate_message_id, field):
            timestamp = await original_update(agent, candidate_message_id, field)
            if field == "read_ts":
                write_marker(
                    {
                        "seam": "read_ts_committed_before_ack_ts_call",
                        "message_id": candidate_message_id,
                        "read_timestamp_observed": timestamp is not None,
                    }
                )
                os.kill(os.getpid(), signal.SIGKILL)
                raise AssertionError("SIGKILL unexpectedly returned")
            return timestamp

        app._update_recipient_timestamp = kill_after_read
        try:
            await call(
                "acknowledge_message",
                {
                    "project_key": project_key,
                    "agent_name": "BlueLake",
                    "message_id": message_id,
                    "format": "json",
                },
            )
        finally:
            app._update_recipient_timestamp = original_update


asyncio.run(main())
"""


@pytest.fixture(scope="module")
def frozen_live_checkout(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    return reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("agentstack-mail-d8-d9-frozen-live"),
    )


def _source_for(namespace: str, frozen_live_checkout: Path) -> Path:
    if namespace == LIVE_NAMESPACE:
        return frozen_live_checkout / "src"
    if namespace == CORE_NAMESPACE:
        return CORE_SOURCE
    raise AssertionError(f"unexpected namespace {namespace!r}")


def _run_until_sigkill(
    *,
    namespace: str,
    source: Path,
    root: Path,
    program: str,
    extra_arguments: tuple[str, ...] = (),
) -> tuple[WorkerStateRoots, subprocess.CompletedProcess[str]]:
    roots = WorkerStateRoots.under(
        root,
        pythonpath=(TESTS_ROOT, source),
    )
    environment = isolated_worker_env(os.environ, namespace, roots)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            namespace,
            str(root),
            *extra_arguments,
        ],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    diagnostic = (completed.stdout + completed.stderr)[-4000:]
    assert completed.returncode == -signal.SIGKILL, diagnostic
    return roots, completed


def _read_marker(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"crash marker missing: {path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _read_recipient_state(
    database_path: Path,
    subject: str,
) -> sqlite3.Row:
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT
                messages.id AS message_id,
                messages.subject AS subject,
                agents.name AS recipient_name,
                message_recipients.kind AS recipient_kind,
                message_recipients.read_ts AS read_ts,
                message_recipients.ack_ts AS ack_ts
            FROM messages
            JOIN message_recipients
              ON message_recipients.message_id = messages.id
            JOIN agents
              ON agents.id = message_recipients.agent_id
            WHERE messages.subject = ?
            """,
            (subject,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, f"message row missing after child death: {subject}"
    return row


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
        if (value := os.environ.get(name))
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="literal SIGKILL durability probes require a POSIX signal",
)
@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_d8_observes_database_and_staged_bundle_after_precommit_sigkill(
    namespace: str,
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    """Observe D8's crash boundary without accepting it as product behavior."""

    root = tmp_path / namespace
    roots, _completed = _run_until_sigkill(
        namespace=namespace,
        source=_source_for(namespace, frozen_live_checkout),
        root=root,
        program=_D8_WORKER,
    )

    marker = _read_marker(root / "d8-staged-before-commit.json")
    assert marker["seam"] == "bundle_files_written_and_index_staged_before_commit"
    assert _D8_SUBJECT in marker["commit_message"]

    recipient = _read_recipient_state(roots.database, _D8_SUBJECT)
    assert recipient["recipient_name"] == "BlueLake"
    assert recipient["recipient_kind"] == "to"
    assert recipient["read_ts"] is None
    assert recipient["ack_ts"] is None

    bundle_paths = sorted(
        path
        for path in roots.storage.rglob("*__d8-crash-before-git-commit__*.md")
        if path.is_file()
    )
    assert len(bundle_paths) == 3
    relative_bundle_paths = {
        path.relative_to(roots.storage).as_posix() for path in bundle_paths
    }
    assert any("/messages/" in f"/{path}" for path in relative_bundle_paths)
    assert any("/GreenCastle/outbox/" in f"/{path}" for path in relative_bundle_paths)
    assert any("/BlueLake/inbox/" in f"/{path}" for path in relative_bundle_paths)
    for path in bundle_paths:
        content = path.read_text(encoding="utf-8")
        assert _D8_SUBJECT in content
        assert "The child must die after staging this bundle." in content

    head = _git(roots.storage, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    assert head.stdout.strip() == marker["head_before"]

    staged = _git(roots.storage, "diff", "--cached", "--name-only")
    assert staged.returncode == 0, staged.stderr
    staged_paths = set(staged.stdout.splitlines())
    assert relative_bundle_paths <= staged_paths

    committed = _git(
        roots.storage,
        "log",
        "--all",
        "--fixed-strings",
        "--grep",
        _D8_SUBJECT,
        "--format=%H",
    )
    assert committed.returncode == 0, committed.stderr
    assert committed.stdout.strip() == ""
    assert not any(path.is_file() for path in roots.signals.rglob("*"))


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="literal SIGKILL durability probes require a POSIX signal",
)
@pytest.mark.parametrize("kill_after", (1, 2))
@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_d8_observes_only_completed_bundle_subset_after_write_sigkill(
    namespace: str,
    kill_after: int,
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    """Observe the exact D8 subset that survives a post-write SIGKILL."""

    root = tmp_path / f"{namespace}-{kill_after}"
    roots, _completed = _run_until_sigkill(
        namespace=namespace,
        source=_source_for(namespace, frozen_live_checkout),
        root=root,
        program=_D8_SUBSET_WORKER,
        extra_arguments=(str(kill_after),),
    )

    marker = _read_marker(root / f"d8-subset-after-{kill_after}.json")
    assert marker["seam"] == "successful_bundle_write_before_next_copy"
    assert marker["successful_write_count"] == kill_after

    recipient = _read_recipient_state(roots.database, _D8_SUBSET_SUBJECT)
    assert recipient["recipient_name"] == "BlueLake"
    assert recipient["recipient_kind"] == "to"
    assert recipient["read_ts"] is None
    assert recipient["ack_ts"] is None

    bundle_paths = sorted(
        path
        for path in roots.storage.rglob("*__d8-crash-after-bundle-subset__*.md")
        if path.is_file()
    )
    relative_bundle_paths = [
        path.relative_to(roots.storage).as_posix() for path in bundle_paths
    ]
    successful_paths = marker["successful_paths"]
    assert set(relative_bundle_paths) == set(successful_paths)
    assert len(relative_bundle_paths) == kill_after
    assert "/messages/" in f"/{successful_paths[0]}"
    if kill_after == 1:
        assert not any(
            "/GreenCastle/outbox/" in f"/{path}" for path in relative_bundle_paths
        )
    else:
        assert "/GreenCastle/outbox/" in f"/{successful_paths[1]}"
    assert not any("/BlueLake/inbox/" in f"/{path}" for path in relative_bundle_paths)
    for path in bundle_paths:
        content = path.read_text(encoding="utf-8")
        assert _D8_SUBSET_SUBJECT in content
        assert "Only the completed bundle subset may survive." in content

    head = _git(roots.storage, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    assert head.stdout.strip() == marker["head_before"]

    staged = _git(roots.storage, "diff", "--cached", "--name-only")
    assert staged.returncode == 0, staged.stderr
    assert staged.stdout.strip() == ""

    committed = _git(
        roots.storage,
        "log",
        "--all",
        "--fixed-strings",
        "--grep",
        _D8_SUBSET_SUBJECT,
        "--format=%H",
    )
    assert committed.returncode == 0, committed.stderr
    assert committed.stdout.strip() == ""
    assert not any(path.is_file() for path in roots.signals.rglob("*"))


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="literal SIGKILL durability probes require a POSIX signal",
)
@pytest.mark.parametrize("namespace", _NAMESPACES)
def test_d9_observes_read_without_ack_after_between_commit_sigkill(
    namespace: str,
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    """Observe D9's two-transaction crash window without approving it."""

    root = tmp_path / namespace
    roots, _completed = _run_until_sigkill(
        namespace=namespace,
        source=_source_for(namespace, frozen_live_checkout),
        root=root,
        program=_D9_WORKER,
    )

    marker = _read_marker(root / "d9-read-committed-before-ack.json")
    assert marker["seam"] == "read_ts_committed_before_ack_ts_call"
    assert marker["read_timestamp_observed"] is True

    recipient = _read_recipient_state(roots.database, _D9_SUBJECT)
    assert recipient["message_id"] == marker["message_id"]
    assert recipient["recipient_name"] == "BlueLake"
    assert recipient["recipient_kind"] == "to"
    assert recipient["read_ts"] is not None
    assert recipient["ack_ts"] is None
