"""Subprocess worker for frozen-live versus AgentStack Mail comparisons.

The worker imports exactly one server namespace, runs one ordered scenario via
an in-memory FastMCP client, and writes JSON to an explicit output path.  Live
and Core workers are never imported into the same interpreter.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import types
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_DOMAIN_TABLES = (
    "projects",
    "agents",
    "agent_links",
    "messages",
    "message_recipients",
    "file_reservations",
)
_TOKEN_FIELD_NAMES = frozenset(
    {"agent_token", "registration_token", "sender_token"}
)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json", by_alias=True, exclude_none=True))
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return repr(value)


def _contains_secret(value: Any, secrets: Mapping[str, str]) -> bool:
    secret_values = tuple(secret for secret in secrets.values() if secret)
    if isinstance(value, str):
        return any(secret in value for secret in secret_values)
    if isinstance(value, (bytes, bytearray)):
        return any(secret.encode("utf-8") in value for secret in secret_values)
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secrets) or _contains_secret(item, secrets)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret(item, secrets) for item in value)
    if dataclasses.is_dataclass(value):
        return _contains_secret(dataclasses.asdict(value), secrets)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _contains_secret(model_dump(mode="python", by_alias=True), secrets)
    if hasattr(value, "__dict__"):
        return _contains_secret(vars(value), secrets)
    return False


def _contains_credential_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _TOKEN_FIELD_NAMES and item not in (None, ""):
                return True
            if _contains_credential_field(item):
                return True
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return any(_contains_credential_field(item) for item in value)
    return False


def _safe_database_value(
    column: str,
    value: Any,
    secrets: Mapping[str, str],
) -> Any:
    if column == "registration_token" and value is not None:
        for agent_name, secret in secrets.items():
            if value == secret:
                return f"<EXPECTED_TOKEN:{agent_name}>"
        return "<SERVER_GENERATED_TOKEN>"
    if _contains_secret(value, secrets):
        return "<UNEXPECTED_TOKEN_VALUE>"
    return _jsonable(value)


def _snapshot_database(path: Path, secrets: Mapping[str, str]) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "tables": {}}

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        available = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_key_violations = [
            list(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        schema = [
            list(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
        schema_sha256 = hashlib.sha256(
            json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()
        tables: dict[str, Any] = {}
        for table in _DOMAIN_TABLES:
            if table not in available:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            rows = []
            for row in connection.execute(f"SELECT * FROM {quoted}").fetchall():
                rows.append(
                    {
                        key: _safe_database_value(key, row[key], secrets)
                        # sqlite3.Row iteration yields values, unlike Mapping.
                        for key in row.keys()  # noqa: SIM118
                    }
                )
            rows.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
            tables[table] = rows
        return {
            "exists": True,
            "integrity_check": integrity,
            "foreign_key_violations": foreign_key_violations,
            "schema_sha256": schema_sha256,
            "tables": tables,
        }
    finally:
        connection.close()


def _snapshot_tree(root: Path, secrets: Mapping[str, str]) -> dict[str, Any]:
    if not root.is_dir():
        return {"exists": False, "files": {}}

    root = root.resolve()
    files: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise AssertionError(f"state tree contains a symbolic link: {relative}")
        if not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, ValueError) as exc:
            raise AssertionError(
                f"state file escapes its worker root: {relative}"
            ) from exc
        if path.name == ".archive.lock" or path.name.endswith(".lock"):
            continue
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            continue
        if _contains_secret(payload, secrets):
            raise AssertionError(f"state file disclosed a caller token: {relative}")
        try:
            files[relative.as_posix()] = {"text": payload.decode("utf-8")}
        except UnicodeDecodeError:
            files[relative.as_posix()] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
    return {"exists": True, "files": files}


def _git_diagnostics(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        return {"exists": False}

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            text=True,
            capture_output=True,
        )

    status = run("status", "--porcelain=v1")
    fsck = run("fsck", "--full")
    log = run(
        "log",
        "--reverse",
        "--pretty=format:--COMMIT--%n%an <%ae>%n%cn <%ce>%n%s%n%b",
        "--name-only",
    )
    return {
        "exists": True,
        "status_returncode": status.returncode,
        "status": status.stdout,
        "fsck_returncode": fsck.returncode,
        "fsck_stderr": fsck.stderr,
        "log_returncode": log.returncode,
        "log": log.stdout,
    }


def _snapshot(state: Mapping[str, Any], secrets: Mapping[str, str]) -> dict[str, Any]:
    database_path = Path(str(state["database_path"]))
    archive_root = Path(str(state["archive_root"]))
    signals_root = Path(str(state["signals_root"]))
    return {
        "database": _snapshot_database(database_path, secrets),
        "archive": _snapshot_tree(archive_root, secrets),
        "signals": _snapshot_tree(signals_root, secrets),
        "git": _git_diagnostics(archive_root),
    }


def _validated_state_paths(state: Mapping[str, Any]) -> None:
    state_root = Path(str(state["state_root"])).resolve(strict=True)
    candidates = {
        "database": Path(str(state["database_path"])).resolve(),
        "archive": Path(str(state["archive_root"])).resolve(strict=True),
        "signals": Path(str(state["signals_root"])).resolve(strict=True),
    }
    if len(set(candidates.values())) != len(candidates):
        raise AssertionError("worker database, archive, and signal roots must be disjoint")
    for label, candidate in candidates.items():
        try:
            candidate.relative_to(state_root)
        except ValueError as exc:
            raise AssertionError(
                f"worker {label} path is outside its state root"
            ) from exc


def _assert_module_origin(module: Any, expected_root: str, label: str) -> None:
    origin_value = getattr(module, "__file__", None)
    if not origin_value:
        raise AssertionError(f"{label} module has no filesystem origin")
    origin = Path(origin_value).resolve(strict=True)
    root = Path(expected_root).resolve(strict=True)
    try:
        origin.relative_to(root)
    except ValueError as exc:
        raise AssertionError(
            f"{label} module resolved outside its authenticated source root"
        ) from exc


def _assert_public_content_matches(result: Any, structured: Any) -> None:
    text_parts = [
        text
        for item in result.content
        if isinstance((text := getattr(item, "text", None)), str)
    ]
    expected = structured
    if isinstance(expected, Mapping) and set(expected) == {"result"}:
        expected = expected["result"]
    if not text_parts and not result.content and expected == []:
        return
    if len(text_parts) != 1:
        raise AssertionError("tool result must expose exactly one public text projection")
    try:
        text_payload = json.loads(text_parts[0])
    except json.JSONDecodeError as exc:
        raise AssertionError("tool public text projection is not JSON") from exc
    if _jsonable(text_payload) != _jsonable(expected):
        raise AssertionError(
            "tool structured_content and public text projections disagree"
        )


def _install_llm_stub(namespace: str) -> None:
    module_name = f"{namespace}.llm"
    stub = types.ModuleType(module_name)

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("differential scenario entered the disabled LLM seam")

    stub.complete_system_user = fail_if_called  # type: ignore[attr-defined]
    sys.modules[module_name] = stub


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if stat.S_IMODE(args.input.stat().st_mode) & 0o077:
        raise AssertionError("differential input must not be group/world accessible")
    input_payload = json.loads(args.input.read_text(encoding="utf-8"))
    state = input_payload["state"]
    secrets = input_payload["secrets"]
    if not isinstance(state, Mapping) or not isinstance(secrets, Mapping):
        raise TypeError("input state and secrets must be objects")
    _validated_state_paths(state)

    _install_llm_stub(args.namespace)
    app = importlib.import_module(f"{args.namespace}.app")
    scenario = importlib.import_module(f"differential_{args.scenario}")
    _assert_module_origin(app, str(state["source_root"]), "mail app")
    _assert_module_origin(scenario, str(state["scenario_root"]), "scenario")
    build_mcp_server = app.build_mcp_server
    scenario_run = scenario.run
    scenario_tools = frozenset(scenario.SCENARIO_TOOLS)

    from fastmcp import Client

    server = build_mcp_server()
    available_tools = await server.get_tools()
    available_resources = await server.get_resources()
    available_resource_templates = await server.get_resource_templates()
    available_prompts = await server.get_prompts()
    missing_tools = scenario_tools - set(available_tools)
    if missing_tools:
        raise AssertionError(f"scenario tools missing from server: {sorted(missing_tools)}")

    checkpoints: list[dict[str, Any]] = []
    tools_used: list[str] = []
    last_call_window: dict[str, str] | None = None

    async with Client(server) as client:

        async def call(tool_name: str, arguments: Mapping[str, Any]) -> Any:
            nonlocal last_call_window
            if last_call_window is not None:
                raise AssertionError("scenario called another tool before checkpointing")
            tools_used.append(tool_name)
            started_at = datetime.now(timezone.utc)
            result = await client.call_tool(tool_name, dict(arguments))
            finished_at = datetime.now(timezone.utc)
            last_call_window = {
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
            }
            # Compare the public MCP serialization.  FastMCP's convenience
            # ``data`` projection can turn list-return wrapper models into
            # empty dictionaries, silently discarding message fields.
            data = result.structured_content
            if data is None:
                data = result.data
            if _contains_secret(data, secrets) or _contains_secret(
                result.content, secrets
            ):
                raise AssertionError(f"tool {tool_name} disclosed a caller token")
            payload = _jsonable(data)
            if result.is_error:
                raise AssertionError(
                    f"tool {tool_name} returned an error: {json.dumps(payload, ensure_ascii=False)}"
                )
            if _contains_credential_field(payload):
                raise AssertionError(f"tool {tool_name} disclosed a credential field")
            _assert_public_content_matches(result, data)
            return payload

        async def checkpoint(event_name: str, result: Any) -> None:
            nonlocal last_call_window
            if last_call_window is None:
                raise AssertionError("scenario checkpointed without a preceding tool call")
            payload = {
                "event": event_name,
                "result": _jsonable(result),
                "call_window": last_call_window,
                "durable": _snapshot(state, secrets),
            }
            last_call_window = None
            if _contains_secret(payload, secrets):
                raise AssertionError(f"checkpoint {event_name} disclosed a caller token")
            checkpoints.append(payload)

        await scenario_run(
            call,
            checkpoint,
            str(state["project_key"]),
            secrets,
        )

    used = frozenset(tools_used)
    if used != scenario_tools:
        raise AssertionError(
            "scenario tool coverage mismatch: "
            f"missing={sorted(scenario_tools - used)}, extra={sorted(used - scenario_tools)}"
        )
    if last_call_window is not None:
        raise AssertionError("scenario ended without checkpointing its final tool call")

    output = {
        "version": 1,
        "namespace": args.namespace,
        "scenario": args.scenario,
        "server": {
            "tool_count": len(available_tools),
            "tool_names": sorted(available_tools),
            "resource_count": len(available_resources),
            "resource_names": sorted(available_resources),
            "resource_template_count": len(available_resource_templates),
            "resource_template_uris": sorted(available_resource_templates),
            "prompt_count": len(available_prompts),
            "prompt_names": sorted(available_prompts),
        },
        "tool_trace": tools_used,
        "tools_used": sorted(used),
        "checkpoints": checkpoints,
    }
    if _contains_secret(output, secrets):
        raise AssertionError("differential output disclosed a caller token")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument(
        "--scenario",
        choices=("identity", "lifecycle", "reservation_signal"),
        required=True,
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = asyncio.run(_run(args))
    payload = json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor = os.open(
        args.output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        destination.write(payload)


if __name__ == "__main__":
    main()
