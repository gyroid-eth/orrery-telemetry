"""D1 is the maintainer-adjudicated selected requirement for Core registration.

D1 fixes explicit-token re-registration semantics. D6 omitted-token behavior is
out of scope and is deliberately not exercised or changed by these tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from differential_source import (
    CORE_NAMESPACE,
    WorkerStateRoots,
    isolated_worker_env,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
CORE_SOURCE = PACKAGE_ROOT / "src"

_AGENT_NAME = "GreenCastle"
_ORIGINAL_TOKEN = "d1-original-owner-token"
_CONFLICTING_TOKEN = "d1-conflicting-owner-token"
_INITIAL_TASK = "D1 initial metadata"
_CONFLICTING_TASK = "D1 conflicting metadata must not persist"
_SAME_TOKEN_TASK = "D1 same-token metadata update"
_SELECTED_AUTH_ERROR = (
    "registration_token does not match the existing token for this agent"
)


_WORKER = r"""
import asyncio
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from differential_probe import _install_llm_stub


namespace, root_text, mode = sys.argv[1:4]
root = Path(root_text)
project_key = root / "project"
project_key.mkdir(parents=True, exist_ok=True)
output_path = root / "result.json"

agent_name = "GreenCastle"
original_token = "d1-original-owner-token"
conflicting_token = "d1-conflicting-owner-token"
initial_task = "D1 initial metadata"
conflicting_task = "D1 conflicting metadata must not persist"
same_token_task = "D1 same-token metadata update"

_install_llm_stub(namespace)
app = importlib.import_module(f"{namespace}.app")


def jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return jsonable(model_dump(mode="json", by_alias=True, exclude_none=True))
    return repr(value)


def public_payload(result):
    value = result.structured_content
    if value is None:
        value = result.data
    return jsonable(value)


def result_diagnostic(result):
    return json.dumps(
        {
            "payload": public_payload(result),
            "content": jsonable(result.content),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def write_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def copy_byte_tree(source, destination):
    for path in sorted(source.rglob("*")):
        if path.is_file():
            write_bytes(destination / path.relative_to(source), path.read_bytes())


def profile_and_repository():
    profiles = list(
        (root / "archive" / "projects").glob(
            f"*/agents/{agent_name}/profile.json"
        )
    )
    if len(profiles) != 1:
        raise AssertionError(f"expected one profile, found {profiles!r}")
    profile = profiles[0]
    return profile, root / "archive"


def commit_count(repository):
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(completed.stdout.strip())


def snapshot(label):
    snapshot_root = root / "snapshots" / label
    database = root / "mail.sqlite3"
    for path in sorted(database.parent.glob(f"{database.name}*")):
        # The SHM file is process-local coordination state. The database and
        # WAL/journal are the byte-exact durable SQLite representation.
        if path.is_file() and path.name != f"{database.name}-shm":
            write_bytes(snapshot_root / "database" / path.name, path.read_bytes())

    profile, repository = profile_and_repository()
    write_bytes(snapshot_root / "profile" / "profile.json", profile.read_bytes())
    copy_byte_tree(repository / ".git", snapshot_root / "git")
    return commit_count(repository)


def read_agent():
    connection = sqlite3.connect(f"file:{root / 'mail.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT name, program, model, task_description, registration_token "
            "FROM agents WHERE name = ?",
            (agent_name,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError("registered agent row is missing")
    return dict(row)


async def required_call(client, name, arguments):
    result = await client.call_tool(name, arguments, raise_on_error=False)
    if result.is_error:
        raise AssertionError(
            f"setup tool {name} failed: {result_diagnostic(result)}"
        )
    return result


async def main():
    from fastmcp import Client

    async with Client(app.build_mcp_server()) as client:
        await required_call(
            client,
            "ensure_project",
            {"human_key": str(project_key), "format": "json"},
        )
        await required_call(
            client,
            "register_agent",
            {
                "project_key": str(project_key),
                "program": "pending-decision-d1",
                "model": "fixture-model",
                "name": agent_name,
                "task_description": initial_task,
                "registration_token": original_token,
                "format": "json",
            },
        )

        before_commits = snapshot("before")
        if mode == "conflict":
            requested_token = conflicting_token
            requested_task = conflicting_task
        elif mode == "same":
            requested_token = original_token
            requested_task = same_token_task
        else:
            raise AssertionError(f"unsupported mode: {mode!r}")

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": str(project_key),
                "program": "pending-decision-d1",
                "model": "fixture-model",
                "name": agent_name,
                "task_description": requested_task,
                "registration_token": requested_token,
                "format": "json",
            },
            raise_on_error=False,
        )
        after_commits = snapshot("after")
        agent = read_agent()
        profile, _repository = profile_and_repository()
        output_path.write_text(
            json.dumps(
                {
                    "ok": not result.is_error,
                    "diagnostic": result_diagnostic(result),
                    "before_commits": before_commits,
                    "after_commits": after_commits,
                    "agent": {
                        "name": agent["name"],
                        "program": agent["program"],
                        "model": agent["model"],
                        "task_description": agent["task_description"],
                        "token_is_original": (
                            agent["registration_token"] == original_token
                        ),
                    },
                    "profile": json.loads(profile.read_text(encoding="utf-8")),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


asyncio.run(main())
"""


def _snapshot_bytes(path: Path) -> dict[str, bytes]:
    assert path.is_dir(), f"snapshot directory missing: {path}"
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _run_case(tmp_path: Path, mode: str) -> tuple[dict[str, Any], Path]:
    root = tmp_path / mode
    roots = WorkerStateRoots.under(
        root,
        pythonpath=(TESTS_ROOT, CORE_SOURCE),
    )
    environment = isolated_worker_env(os.environ, CORE_NAMESPACE, roots)
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER, CORE_NAMESPACE, str(root), mode],
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    diagnostic = (completed.stdout + completed.stderr)[-8000:]
    assert completed.returncode == 0, diagnostic
    result_path = root / "result.json"
    assert result_path.is_file(), diagnostic
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload, root


def _assert_exact_domain_snapshot(root: Path, domain: str) -> None:
    before = _snapshot_bytes(root / "snapshots" / "before" / domain)
    after = _snapshot_bytes(root / "snapshots" / "after" / domain)
    assert after == before, f"{domain} durable bytes changed after rejected token"


def test_conflicting_explicit_token_is_rejected_without_durable_change(
    tmp_path: Path,
) -> None:
    payload, root = _run_case(tmp_path, "conflict")

    assert payload["ok"] is False
    assert _SELECTED_AUTH_ERROR in payload["diagnostic"]
    assert payload["agent"]["token_is_original"] is True
    _assert_exact_domain_snapshot(root, "database")
    _assert_exact_domain_snapshot(root, "profile")
    _assert_exact_domain_snapshot(root, "git")
    assert payload["after_commits"] == payload["before_commits"]


def test_same_explicit_token_updates_metadata_with_exactly_one_git_commit(
    tmp_path: Path,
) -> None:
    payload, _root = _run_case(tmp_path, "same")

    assert payload["ok"] is True, payload["diagnostic"]
    assert payload["agent"]["token_is_original"] is True
    assert payload["agent"]["task_description"] == _SAME_TOKEN_TASK
    assert payload["profile"]["task_description"] == _SAME_TOKEN_TASK
    assert payload["after_commits"] == payload["before_commits"] + 1
