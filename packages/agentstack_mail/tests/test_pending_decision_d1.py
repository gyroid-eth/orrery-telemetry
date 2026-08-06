"""D1 is the maintainer-adjudicated selected requirement for Core registration.

D1 fixes explicit-token re-registration semantics. The omitted-token regression
test records that D6/D7 compatibility stayed unchanged; it does not select their
future claim or authorization policy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
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
_INITIAL_PROGRAM = "pending-decision-d1-initial"
_INITIAL_MODEL = "fixture-model-initial"
_INITIAL_TASK = "D1 initial metadata"
_UPDATED_PROGRAM = "pending-decision-d1-updated"
_UPDATED_MODEL = "fixture-model-updated"
_UPDATED_ATTACHMENTS_POLICY = "inline"
_CONFLICTING_TASK = "D1 conflicting metadata must not persist"
_SAME_TOKEN_TASK = "D1 same-token metadata update"
_OMITTED_TOKEN_TASK = "D1 omitted-token compatibility update"
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
omitted_token_task = "D1 omitted-token compatibility update"
initial_program = "pending-decision-d1-initial"
initial_model = "fixture-model-initial"
updated_program = "pending-decision-d1-updated"
updated_model = "fixture-model-updated"
updated_attachments_policy = "inline"

if mode == "window_conflict":
    os.environ["AGENTSTACK_MAIL_WINDOW_ID"] = "11111111-2222-4333-8444-555555555555"

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


def copy_byte_tree(source, destination, excluded_top_level=()):
    for path in sorted(source.rglob("*")):
        if path.is_file():
            relative = path.relative_to(source)
            if relative.parts and relative.parts[0] in excluded_top_level:
                continue
            write_bytes(destination / relative, path.read_bytes())


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
    copy_byte_tree(
        repository,
        snapshot_root / "archive",
        excluded_top_level={".git"},
    )
    return commit_count(repository)


def read_agent():
    connection = sqlite3.connect(f"file:{root / 'mail.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT name, program, model, task_description, attachments_policy, "
            "registration_token "
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
        if mode == "concurrent_null":
            await required_call(
                client,
                "macro_start_session",
                {
                    "human_key": str(project_key),
                    "program": initial_program,
                    "model": initial_model,
                    "agent_name": agent_name,
                    "task_description": initial_task,
                    "format": "json",
                },
            )
        else:
            await required_call(
                client,
                "register_agent",
                {
                    "project_key": str(project_key),
                    "program": initial_program,
                    "model": initial_model,
                    "name": agent_name,
                    "task_description": initial_task,
                    "attachments_policy": "auto",
                    "registration_token": original_token,
                    "format": "json",
                },
            )

    before_commits = snapshot("before")

    if mode == "concurrent_null":
        original_claim = app._claim_null_registration_token
        claim_arrivals = 0
        claim_results = []
        both_arrived = asyncio.Event()

        async def synchronize_before_claim(*args, **kwargs):
            nonlocal claim_arrivals
            claim_arrivals += 1
            if claim_arrivals == 2:
                both_arrived.set()
            try:
                await asyncio.wait_for(both_arrived.wait(), timeout=0.5)
            except TimeoutError:
                pass
            claimed = await original_claim(*args, **kwargs)
            claim_results.append(claimed)
            return claimed

        app._claim_null_registration_token = synchronize_before_claim
        requests = (
            {
                "owner": "a",
                "program": f"{updated_program}-a",
                "model": f"{updated_model}-a",
                "task_description": f"{same_token_task} owner a",
                "registration_token": original_token,
            },
            {
                "owner": "b",
                "program": f"{updated_program}-b",
                "model": f"{updated_model}-b",
                "task_description": f"{same_token_task} owner b",
                "registration_token": conflicting_token,
            },
        )

        async def concurrent_registration(client, request):
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": str(project_key),
                    "program": request["program"],
                    "model": request["model"],
                    "name": agent_name,
                    "task_description": request["task_description"],
                    "attachments_policy": updated_attachments_policy,
                    "registration_token": request["registration_token"],
                    "format": "json",
                },
                raise_on_error=False,
            )
            return {
                "owner": request["owner"],
                "ok": not result.is_error,
                "diagnostic": result_diagnostic(result),
            }

        async with (
            Client(app.build_mcp_server()) as client_a,
            Client(app.build_mcp_server()) as client_b,
        ):
            outcomes = await asyncio.gather(
                concurrent_registration(client_a, requests[0]),
                concurrent_registration(client_b, requests[1]),
            )
        result_payload = {
            "outcomes": outcomes,
            "success_count": sum(outcome["ok"] for outcome in outcomes),
            "claim_arrivals": claim_arrivals,
            "claim_results": claim_results,
        }
    else:
        if mode in {"conflict", "window_conflict"}:
            requested_token = conflicting_token
            requested_task = conflicting_task
        elif mode == "same":
            requested_token = original_token
            requested_task = same_token_task
        elif mode == "omitted":
            requested_token = None
            requested_task = omitted_token_task
        else:
            raise AssertionError(f"unsupported mode: {mode!r}")

        arguments = {
            "project_key": str(project_key),
            "program": updated_program,
            "model": updated_model,
            "name": None if mode == "window_conflict" else agent_name,
            "task_description": requested_task,
            "attachments_policy": updated_attachments_policy,
            "format": "json",
        }
        if requested_token is not None:
            arguments["registration_token"] = requested_token
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "register_agent",
                arguments,
                raise_on_error=False,
            )
        result_payload = {
            "ok": not result.is_error,
            "diagnostic": result_diagnostic(result),
        }

    after_commits = snapshot("after")
    agent = read_agent()
    profile, _repository = profile_and_repository()
    token_owner = None
    if agent["registration_token"] == original_token:
        token_owner = "a"
    elif agent["registration_token"] == conflicting_token:
        token_owner = "b"
    output_path.write_text(
        json.dumps(
            {
                **result_payload,
                "before_commits": before_commits,
                "after_commits": after_commits,
                "agent": {
                    "name": agent["name"],
                    "program": agent["program"],
                    "model": agent["model"],
                    "task_description": agent["task_description"],
                    "attachments_policy": agent["attachments_policy"],
                    "token_is_original": (
                        agent["registration_token"] == original_token
                    ),
                    "token_owner": token_owner,
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


@pytest.mark.parametrize("mode", ("conflict", "window_conflict"))
def test_conflicting_explicit_token_is_rejected_without_durable_change(
    mode: str,
    tmp_path: Path,
) -> None:
    payload, root = _run_case(tmp_path, mode)

    assert payload["ok"] is False
    assert _SELECTED_AUTH_ERROR in payload["diagnostic"]
    assert payload["agent"]["token_is_original"] is True
    assert payload["agent"]["program"] == _INITIAL_PROGRAM
    assert payload["agent"]["model"] == _INITIAL_MODEL
    assert payload["agent"]["task_description"] == _INITIAL_TASK
    assert payload["agent"]["attachments_policy"] == "auto"
    _assert_exact_domain_snapshot(root, "database")
    _assert_exact_domain_snapshot(root, "profile")
    _assert_exact_domain_snapshot(root, "git")
    _assert_exact_domain_snapshot(root, "archive")
    assert payload["after_commits"] == payload["before_commits"]


def test_same_explicit_token_updates_metadata_with_exactly_one_git_commit(
    tmp_path: Path,
) -> None:
    payload, _root = _run_case(tmp_path, "same")

    assert payload["ok"] is True, payload["diagnostic"]
    assert payload["agent"]["token_is_original"] is True
    assert payload["agent"]["program"] == _UPDATED_PROGRAM
    assert payload["agent"]["model"] == _UPDATED_MODEL
    assert payload["agent"]["task_description"] == _SAME_TOKEN_TASK
    assert payload["agent"]["attachments_policy"] == _UPDATED_ATTACHMENTS_POLICY
    assert payload["profile"]["program"] == _UPDATED_PROGRAM
    assert payload["profile"]["model_raw"] == _UPDATED_MODEL
    assert payload["profile"]["task_description"] == _SAME_TOKEN_TASK
    assert payload["profile"]["attachments_policy"] == _UPDATED_ATTACHMENTS_POLICY
    assert payload["after_commits"] == payload["before_commits"] + 1


def test_omitted_token_preserves_existing_credential_and_update_semantics(
    tmp_path: Path,
) -> None:
    payload, _root = _run_case(tmp_path, "omitted")

    assert payload["ok"] is True, payload["diagnostic"]
    assert payload["agent"]["token_is_original"] is True
    assert payload["agent"]["program"] == _UPDATED_PROGRAM
    assert payload["agent"]["model"] == _UPDATED_MODEL
    assert payload["agent"]["task_description"] == _OMITTED_TOKEN_TASK
    assert payload["agent"]["attachments_policy"] == _UPDATED_ATTACHMENTS_POLICY
    assert payload["profile"]["program"] == _UPDATED_PROGRAM
    assert payload["profile"]["model_raw"] == _UPDATED_MODEL
    assert payload["profile"]["task_description"] == _OMITTED_TOKEN_TASK
    assert payload["profile"]["attachments_policy"] == "auto"
    assert payload["after_commits"] == payload["before_commits"] + 1


def test_concurrent_explicit_tokens_against_null_identity_are_first_winner(
    tmp_path: Path,
) -> None:
    payload, _root = _run_case(tmp_path, "concurrent_null")

    assert payload["claim_arrivals"] == 2
    assert sorted(payload["claim_results"]) == [False, True]
    assert payload["success_count"] == 1, payload["outcomes"]
    failed = [outcome for outcome in payload["outcomes"] if not outcome["ok"]]
    assert len(failed) == 1
    assert _SELECTED_AUTH_ERROR in failed[0]["diagnostic"]
    for outcome in payload["outcomes"]:
        assert _ORIGINAL_TOKEN not in outcome["diagnostic"]
        assert _CONFLICTING_TOKEN not in outcome["diagnostic"]
    winner = next(outcome["owner"] for outcome in payload["outcomes"] if outcome["ok"])
    assert payload["agent"]["token_owner"] == winner
    assert payload["agent"]["program"] == f"{_UPDATED_PROGRAM}-{winner}"
    assert payload["agent"]["model"] == f"{_UPDATED_MODEL}-{winner}"
    assert payload["agent"]["task_description"] == f"{_SAME_TOKEN_TASK} owner {winner}"
    assert payload["agent"]["attachments_policy"] == _UPDATED_ATTACHMENTS_POLICY
    assert payload["profile"]["program"] == f"{_UPDATED_PROGRAM}-{winner}"
    assert payload["profile"]["model_raw"] == f"{_UPDATED_MODEL}-{winner}"
    assert (
        payload["profile"]["task_description"] == f"{_SAME_TOKEN_TASK} owner {winner}"
    )
    assert payload["profile"]["attachments_policy"] == _UPDATED_ATTACHMENTS_POLICY
    assert "registration_token" not in payload["profile"]
    assert payload["after_commits"] == payload["before_commits"] + 1
