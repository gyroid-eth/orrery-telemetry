"""Selected D8 messaging parity and pending D9 crash observations.

D8 Path A preserves the measured DB-first messaging behavior at three literal
``SIGKILL`` seams: after the first and second completed bundle copies, and after
all three copies are staged immediately before Git commit.  It also preserves
the measured ordinary ``write_message_bundle`` exception behavior.  The D8
projection binds only the committed message/recipient relationship, completed
archive-copy roles, and the selected Git boundary or stable tool-error origin.

D8 does not claim ordinary failures outside the messaging bundle seam,
registration/profile writes, death inside Git's native commit, retry,
recovery, reconciliation, power-loss durability, concurrency, receipt state,
or signal lifecycle.  D9 remains unselected evidence: its timestamp helper
commits ``read_ts`` and then kills the process before the separate ``ack_ts``
update.
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
_D8_BODY = "The child must die after staging this bundle."
_D8_SUBSET_SUBJECT = "D8 crash after bundle subset"
_D8_SUBSET_BODY = "Only the completed bundle subset may survive."
_D8_EXCEPTION_SUBJECT = "D8 message bundle exception"
_D8_EXCEPTION_BODY = "The database commit must survive the injected bundle error."
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
source_root = Path(os.environ["D8_SOURCE_ROOT"]).resolve(strict=True)
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
storage = importlib.import_module(f"{namespace}.storage")
assert Path(app.__file__).resolve(strict=True).is_relative_to(source_root), (
    "D8 precommit worker imported app outside the authenticated source"
)
assert Path(storage.__file__).resolve(strict=True).is_relative_to(source_root), (
    "D8 precommit worker imported storage outside the authenticated source"
)


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
source_root = Path(os.environ["D8_SOURCE_ROOT"]).resolve(strict=True)
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
assert Path(app.__file__).resolve(strict=True).is_relative_to(source_root), (
    "D8 subset worker imported app outside the authenticated source"
)
assert Path(storage.__file__).resolve(strict=True).is_relative_to(source_root), (
    "D8 subset worker imported storage outside the authenticated source"
)


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


_D8_EXCEPTION_WORKER = r"""
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

from differential_probe import _install_llm_stub


namespace, root_text = sys.argv[1:3]
root = Path(root_text)
source_root = Path(os.environ["D8_SOURCE_ROOT"]).resolve(strict=True)
project_key = str(root / "project")
output_path = root / "d8-message-bundle-exception.json"
Path(project_key).mkdir(parents=True, exist_ok=True)

_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")
storage = importlib.import_module(f"{namespace}.storage")
assert Path(app.__file__).resolve(strict=True).is_relative_to(source_root), (
    "D8 exception worker imported app outside the authenticated source"
)
assert Path(storage.__file__).resolve(strict=True).is_relative_to(source_root), (
    "D8 exception worker imported storage outside the authenticated source"
)


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
            ("GreenCastle", "d8-exception-green-owner-token"),
            ("BlueLake", "d8-exception-blue-owner-token"),
        ):
            await call(
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "pending-decision-crash-probe",
                    "model": "fixture-model",
                    "name": agent_name,
                    "task_description": "D8 message bundle exception probe",
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

        original_write_message_bundle = app.write_message_bundle

        async def fail_message_bundle(*_args, **_kwargs):
            raise RuntimeError("D8 injected write_message_bundle failure")

        app.write_message_bundle = fail_message_bundle
        try:
            result = await client.call_tool_mcp(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": "GreenCastle",
                    "sender_token": "d8-exception-green-owner-token",
                    "to": ["BlueLake"],
                    "subject": "D8 message bundle exception",
                    "body_md": (
                        "The database commit must survive the injected bundle error."
                    ),
                    "format": "json",
                },
            )
        finally:
            app.write_message_bundle = original_write_message_bundle

    raw_result = result.model_dump(mode="json", by_alias=True)
    serialized_result = json.dumps(raw_result, ensure_ascii=False, sort_keys=True)
    observation = {
        "tool_error": bool(result.isError),
        "injected_bundle_failure": (
            "D8 injected write_message_bundle failure" in serialized_result
        ),
    }
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(observation, output, sort_keys=True)
        output.write("\n")


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
    source = source.resolve(strict=True)
    roots = WorkerStateRoots.under(
        root,
        pythonpath=(TESTS_ROOT, source),
    )
    environment = isolated_worker_env(os.environ, namespace, roots)
    environment["D8_SOURCE_ROOT"] = str(source)
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


def _run_to_completion(
    *,
    namespace: str,
    source: Path,
    root: Path,
    program: str,
) -> WorkerStateRoots:
    source = source.resolve(strict=True)
    roots = WorkerStateRoots.under(
        root,
        pythonpath=(TESTS_ROOT, source),
    )
    environment = isolated_worker_env(os.environ, namespace, roots)
    environment["D8_SOURCE_ROOT"] = str(source)
    completed = subprocess.run(
        [sys.executable, "-c", program, namespace, str(root)],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    diagnostic = (completed.stdout + completed.stderr)[-4000:]
    assert completed.returncode == 0, diagnostic
    return roots


def _read_marker(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"crash marker missing: {path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _read_d8_delivery_records(
    database_path: Path,
    subject: str,
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                senders.name AS sender_name,
                messages.subject AS subject,
                messages.body_md AS body_md,
                recipients.name AS recipient_name,
                message_recipients.kind AS recipient_kind
            FROM messages
            JOIN agents AS senders
              ON senders.id = messages.sender_id
            JOIN message_recipients
              ON message_recipients.message_id = messages.id
            JOIN agents AS recipients
              ON recipients.id = message_recipients.agent_id
            WHERE messages.subject = ?
            ORDER BY messages.id, recipients.id
            """,
            (subject,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _read_d9_recipient_state(
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


def _git_output(root: Path, *arguments: str) -> str:
    completed = _git(root, *arguments)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _d8_archive_role(relative_path: str) -> str:
    parts = Path(relative_path).parts
    for index, part in enumerate(parts):
        if part == "messages" and parts[index + 1 : index + 2] != ("threads",):
            return "canonical"
        if parts[index : index + 3] == ("agents", "GreenCastle", "outbox"):
            return "sender_outbox"
        if parts[index : index + 3] == ("agents", "BlueLake", "inbox"):
            return "recipient_inbox"
    return "unexpected"


def _d8_bundle_snapshot(
    storage: Path,
    *,
    subject: str,
    body: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    roles_by_path: dict[str, str] = {}
    contents_match = True
    for path in sorted(storage.rglob("*.md")):
        content = path.read_text(encoding="utf-8")
        if subject not in content:
            continue
        relative = path.relative_to(storage).as_posix()
        roles_by_path[relative] = _d8_archive_role(relative)
        contents_match = contents_match and body in content
    return (
        {
            "completed_archive_roles": sorted(roles_by_path.values()),
            "archive_contents_match_message": contents_match,
        },
        roles_by_path,
    )


def _d8_common_projection(
    roots: WorkerStateRoots,
    *,
    subject: str,
    body: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    bundle, roles_by_path = _d8_bundle_snapshot(
        roots.storage,
        subject=subject,
        body=body,
    )
    return (
        {
            "database_delivery_records": _read_d8_delivery_records(
                roots.database,
                subject,
            ),
            **bundle,
        },
        roles_by_path,
    )


def _observe_d8_precommit(
    *,
    namespace: str,
    source: Path,
    root: Path,
) -> dict[str, Any]:
    roots, _completed = _run_until_sigkill(
        namespace=namespace,
        source=source,
        root=root,
        program=_D8_WORKER,
    )
    marker = _read_marker(root / "d8-staged-before-commit.json")
    common, roles_by_path = _d8_common_projection(
        roots,
        subject=_D8_SUBJECT,
        body=_D8_BODY,
    )
    staged_paths = set(
        _git_output(roots.storage, "diff", "--cached", "--name-only").splitlines()
    )
    committed = _git_output(
        roots.storage,
        "log",
        "--all",
        "--fixed-strings",
        "--grep",
        _D8_SUBJECT,
        "--format=%H",
    )
    return {
        "seam": marker.get("seam"),
        **common,
        "git_head_unchanged": (
            _git_output(roots.storage, "rev-parse", "HEAD")
            == marker.get("head_before")
        ),
        "staged_archive_roles": sorted(
            roles_by_path.get(path, "unexpected") for path in staged_paths
        ),
        "only_completed_bundle_staged": staged_paths == set(roles_by_path),
        "message_commit_absent": committed == "",
    }


def _observe_d8_subset(
    *,
    namespace: str,
    source: Path,
    root: Path,
    kill_after: int,
) -> dict[str, Any]:
    roots, _completed = _run_until_sigkill(
        namespace=namespace,
        source=source,
        root=root,
        program=_D8_SUBSET_WORKER,
        extra_arguments=(str(kill_after),),
    )
    marker = _read_marker(root / f"d8-subset-after-{kill_after}.json")
    common, roles_by_path = _d8_common_projection(
        roots,
        subject=_D8_SUBSET_SUBJECT,
        body=_D8_SUBSET_BODY,
    )
    successful_paths = marker.get("successful_paths") or []
    staged_paths = set(
        _git_output(roots.storage, "diff", "--cached", "--name-only").splitlines()
    )
    committed = _git_output(
        roots.storage,
        "log",
        "--all",
        "--fixed-strings",
        "--grep",
        _D8_SUBSET_SUBJECT,
        "--format=%H",
    )
    return {
        "seam": marker.get("seam"),
        "successful_write_count": marker.get("successful_write_count"),
        **common,
        "successful_write_roles": [
            roles_by_path.get(path, "unexpected") for path in successful_paths
        ],
        "archive_paths_match_successful_writes": (
            set(successful_paths) == set(roles_by_path)
        ),
        "git_head_unchanged": (
            _git_output(roots.storage, "rev-parse", "HEAD")
            == marker.get("head_before")
        ),
        "staged_paths_empty": staged_paths == set(),
        "message_commit_absent": committed == "",
    }


def _observe_d8_exception(
    *,
    namespace: str,
    source: Path,
    root: Path,
) -> dict[str, Any]:
    roots = _run_to_completion(
        namespace=namespace,
        source=source,
        root=root,
        program=_D8_EXCEPTION_WORKER,
    )
    tool_result = _read_marker(root / "d8-message-bundle-exception.json")
    common, _roles_by_path = _d8_common_projection(
        roots,
        subject=_D8_EXCEPTION_SUBJECT,
        body=_D8_EXCEPTION_BODY,
    )
    common.pop("archive_contents_match_message")
    return {**tool_result, **common}


def _expected_d8_database_records(subject: str, body: str) -> list[dict[str, str]]:
    return [
        {
            "sender_name": "GreenCastle",
            "subject": subject,
            "body_md": body,
            "recipient_name": "BlueLake",
            "recipient_kind": "to",
        }
    ]


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="literal SIGKILL durability probes require a POSIX signal",
)
def test_d8_selected_parity_database_and_staged_bundle_after_precommit_sigkill(
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    """Require selected DB-first parity at the staged precommit crash seam."""

    observations = {
        namespace: _observe_d8_precommit(
            namespace=namespace,
            source=_source_for(namespace, frozen_live_checkout),
            root=tmp_path / f"precommit-{namespace}",
        )
        for namespace in _NAMESPACES
    }
    expected = {
        "seam": "bundle_files_written_and_index_staged_before_commit",
        "database_delivery_records": _expected_d8_database_records(
            _D8_SUBJECT,
            _D8_BODY,
        ),
        "completed_archive_roles": [
            "canonical",
            "recipient_inbox",
            "sender_outbox",
        ],
        "archive_contents_match_message": True,
        "git_head_unchanged": True,
        "staged_archive_roles": [
            "canonical",
            "recipient_inbox",
            "sender_outbox",
        ],
        "only_completed_bundle_staged": True,
        "message_commit_absent": True,
    }

    assert observations[LIVE_NAMESPACE] == expected
    assert observations[CORE_NAMESPACE] == expected
    assert observations[CORE_NAMESPACE] == observations[LIVE_NAMESPACE]


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="literal SIGKILL durability probes require a POSIX signal",
)
def test_d8_selected_parity_completed_bundle_subset_after_write_sigkill(
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    """Require selected DB-first parity for the first two completed copies."""

    observations = {
        namespace: {
            kill_after: _observe_d8_subset(
                namespace=namespace,
                source=_source_for(namespace, frozen_live_checkout),
                root=tmp_path / f"subset-{namespace}-{kill_after}",
                kill_after=kill_after,
            )
            for kill_after in (1, 2)
        }
        for namespace in _NAMESPACES
    }
    database_records = _expected_d8_database_records(
        _D8_SUBSET_SUBJECT,
        _D8_SUBSET_BODY,
    )
    expected = {
        1: {
            "seam": "successful_bundle_write_before_next_copy",
            "successful_write_count": 1,
            "database_delivery_records": database_records,
            "completed_archive_roles": ["canonical"],
            "archive_contents_match_message": True,
            "successful_write_roles": ["canonical"],
            "archive_paths_match_successful_writes": True,
            "git_head_unchanged": True,
            "staged_paths_empty": True,
            "message_commit_absent": True,
        },
        2: {
            "seam": "successful_bundle_write_before_next_copy",
            "successful_write_count": 2,
            "database_delivery_records": database_records,
            "completed_archive_roles": ["canonical", "sender_outbox"],
            "archive_contents_match_message": True,
            "successful_write_roles": ["canonical", "sender_outbox"],
            "archive_paths_match_successful_writes": True,
            "git_head_unchanged": True,
            "staged_paths_empty": True,
            "message_commit_absent": True,
        },
    }

    assert observations[LIVE_NAMESPACE] == expected
    assert observations[CORE_NAMESPACE] == expected
    assert observations[CORE_NAMESPACE] == observations[LIVE_NAMESPACE]


def test_d8_selected_parity_message_bundle_exception_leaves_committed_database_without_archive(
    frozen_live_checkout: Path,
    tmp_path: Path,
) -> None:
    """Require only the stable messaging-bundle exception projection."""

    observations = {
        namespace: _observe_d8_exception(
            namespace=namespace,
            source=_source_for(namespace, frozen_live_checkout),
            root=tmp_path / f"exception-{namespace}",
        )
        for namespace in _NAMESPACES
    }
    expected = {
        "tool_error": True,
        "injected_bundle_failure": True,
        "database_delivery_records": _expected_d8_database_records(
            _D8_EXCEPTION_SUBJECT,
            _D8_EXCEPTION_BODY,
        ),
        "completed_archive_roles": [],
    }

    assert observations[LIVE_NAMESPACE] == expected
    assert observations[CORE_NAMESPACE] == expected
    assert observations[CORE_NAMESPACE] == observations[LIVE_NAMESPACE]


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

    recipient = _read_d9_recipient_state(roots.database, _D9_SUBJECT)
    assert recipient["message_id"] == marker["message_id"]
    assert recipient["recipient_name"] == "BlueLake"
    assert recipient["recipient_kind"] == "to"
    assert recipient["read_ts"] is not None
    assert recipient["ack_ts"] is None
