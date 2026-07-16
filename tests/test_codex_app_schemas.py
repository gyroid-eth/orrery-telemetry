from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
INTEGRATION_SCHEMAS = ROOT / "integrations" / "codex_app" / "schemas"


def _load(relative: str):
    return json.loads((SCHEMAS / relative).read_text(encoding="utf-8"))


def _is_type(value, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return checks[expected](value)


def _validate(value, schema: dict, path: str = "$") -> None:
    """Validate the schema features used by these versioned fixtures.

    The integration package declares ``jsonschema`` for consumers. This tiny
    checker keeps the repository's dependency-free test suite able to pin the
    same required fields, constants, enums, types, and formats.
    """

    if "const" in schema and value != schema["const"]:
        raise AssertionError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError(f"{path}: not in enum")
    expected = schema.get("type")
    if expected:
        accepted = [expected] if isinstance(expected, str) else expected
        if not any(_is_type(value, item) for item in accepted):
            raise AssertionError(f"{path}: wrong type")
        if value is None:
            return
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise AssertionError(f"{path}: missing {sorted(missing)}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise AssertionError(f"{path}: extra {sorted(extra)}")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise AssertionError(f"{path}: too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise AssertionError(f"{path}: pattern mismatch")
        if schema.get("format") == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    ("fixture", "schema"),
    [
        ("fixtures/runtime-event-root.json", "runtime-event-v1.json"),
        ("fixtures/runtime-event-subagent.json", "runtime-event-v1.json"),
        ("fixtures/binding-record.json", "binding-record-v1.json"),
    ],
)
def test_schema_fixtures_validate(fixture: str, schema: str):
    _validate(_load(fixture), _load(schema))


def test_runtime_event_schema_rejects_unknown_or_missing_fields():
    event = _load("fixtures/runtime-event-root.json")
    event["secret"] = "must-not-cross-boundary"
    with pytest.raises(AssertionError, match="extra"):
        _validate(event, _load("runtime-event-v1.json"))

    del event["secret"]
    del event["session_id"]
    with pytest.raises(AssertionError, match="missing"):
        _validate(event, _load("runtime-event-v1.json"))


@pytest.mark.parametrize(
    "relative",
    [
        "runtime-event-v1.json",
        "binding-record-v1.json",
        "migrations/001_delivery_state.sql",
    ],
)
def test_exportable_schema_mirror_matches_root_canonical(relative: str):
    assert (INTEGRATION_SCHEMAS / relative).read_bytes() == (
        SCHEMAS / relative
    ).read_bytes()


def test_delivery_migration_is_idempotent_and_enforces_delivery_semantics():
    migration = (SCHEMAS / "migrations" / "001_delivery_state.sql").read_text(
        encoding="utf-8"
    )
    connection = sqlite3.connect(":memory:")
    connection.executescript(migration)
    connection.executescript(migration)

    key = ("/workspace/example-project", "Calm-Noether", 101)
    connection.execute(
        "INSERT INTO codex_app_delivery_state "
        "(project_key, agent_name, message_id) VALUES (?, ?, ?)",
        key,
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO codex_app_delivery_state "
            "(project_key, agent_name, message_id) VALUES (?, ?, ?)",
            key,
        )

    connection.execute(
        "UPDATE codex_app_delivery_state "
        "SET status='leased', lease_owner='worker-1', "
        "lease_expires_at='2026-01-01T00:01:00Z', attempt_count=attempt_count+1 "
        "WHERE project_key=? AND agent_name=? AND message_id=?",
        key,
    )
    connection.execute(
        "UPDATE codex_app_delivery_state "
        "SET status='delivered', lease_owner=NULL, lease_expires_at=NULL, "
        "delivered_at='2026-01-01T00:00:30Z' "
        "WHERE project_key=? AND agent_name=? AND message_id=?",
        key,
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid .* transition"):
        connection.execute(
            "UPDATE codex_app_delivery_state "
            "SET status='leased', lease_owner='worker-2', "
            "lease_expires_at='2026-01-01T00:02:00Z' "
            "WHERE project_key=? AND agent_name=? AND message_id=?",
            key,
        )
