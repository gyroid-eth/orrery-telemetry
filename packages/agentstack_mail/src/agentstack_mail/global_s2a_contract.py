"""Shared normative S2a schemas and canonical request bytes (no runtime state)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .global_server import GlobalError

_fixture = Path(__file__).parent / "fixtures/global-server-s2a.json"
if not _fixture.is_file():
    _fixture = Path(__file__).parents[2] / "fixtures/global-server-s2a.json"
FIXTURE = json.loads(_fixture.read_text())
# Editable checkouts keep fixtures outside src; installed wheels include them.
TOOLS = FIXTURE["tool_contracts"]
DDL = FIXTURE["candidate_validation_contract"]["extension_ddl"]


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def unique_document(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise GlobalError("DOCUMENT_INVALID")
            result[k] = v
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError):
        raise GlobalError("DOCUMENT_INVALID") from None


def validate(value, schema, reason="ARGUMENTS_UNSUPPORTED"):
    """Validate the deliberately bounded schema subset used by this fixture.

    No coercion: bool is not int, unknown fields cannot disappear in FastMCP.
    The schemas are package-owned, never caller supplied.
    """
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate(value, option, reason)
                return
            except GlobalError:
                pass
        raise GlobalError(reason)
    if "const" in schema and value != schema["const"]:
        raise GlobalError(reason)
    if "enum" in schema and value not in schema["enum"]:
        raise GlobalError(reason)
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "integer": int,
        "string": str,
        "boolean": bool,
        "null": type(None),
    }
    if kind and type(value) is not types[kind]:
        raise GlobalError(reason)
    if kind == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys() or (
            schema.get("additionalProperties") is False and value.keys() - props.keys()
        ):
            raise GlobalError(reason)
        for key, val in value.items():
            if key in props:
                validate(val, props[key], reason)
    if kind == "array":
        for val in value:
            validate(val, schema["items"], reason)
    if kind == "integer" and (
        value < schema.get("minimum", value) or value > schema.get("maximum", value)
    ):
        raise GlobalError(reason)
    if kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", len(value)
        ):
            raise GlobalError(reason)
        if schema.get("pattern") and re.fullmatch(schema["pattern"], value) is None:
            raise GlobalError(reason)
        if schema.get("format") == "date-time":
            from datetime import datetime

            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise GlobalError(reason) from None


def arguments(tool, supplied):
    schema = TOOLS[tool]["input_schema"]
    # Fixed domain reasons before generic schema refusal.
    if "request_id" in schema["properties"] and not (
        isinstance(supplied.get("request_id"), str)
        and re.fullmatch(
            schema["properties"]["request_id"]["pattern"], supplied["request_id"]
        )
    ):
        raise GlobalError("REQUEST_ID_INVALID")
    if "ttl_seconds" in supplied and (
        type(supplied["ttl_seconds"]) is not int or supplied["ttl_seconds"] < 0
    ):
        raise GlobalError("CONTACT_TTL_INVALID")
    if "policy" in supplied and supplied["policy"] not in (
        "open",
        "auto",
        "contacts_only",
        "block_all",
    ):
        raise GlobalError("CONTACT_POLICY_INVALID")
    if (
        "task_description" in supplied
        and supplied["task_description"] is not None
        and (
            not isinstance(supplied["task_description"], str)
            or len(supplied["task_description"]) > 2048
        )
    ):
        raise GlobalError("TASK_DESCRIPTION_INVALID")
    validate(supplied, schema)
    result = {
        k: v["default"] for k, v in schema["properties"].items() if "default" in v
    }
    result.update(supplied)
    if "ttl_seconds" in result:
        result["ttl_seconds"] = max(60, result["ttl_seconds"])
    return result


def preimage(tool, args):
    excluded = {
        "expected_server_instance_id",
        "candidate_generation",
        "authority_epoch",
        "agent_id",
        "expected_credential_generation",
        "registration_token",
        "request_id",
        *TOOLS[tool]["accepted_ignored_arguments"],
    }
    value = {
        "version": "orrery-global-operation-request-v1",
        "tool": tool,
        "server_instance_id": args["expected_server_instance_id"],
        **{
            k: args[k]
            for k in (
                "candidate_generation",
                "authority_epoch",
                "agent_id",
                "expected_credential_generation",
            )
        },
        "arguments": {k: v for k, v in args.items() if k not in excluded},
    }
    try:
        raw = canonical(value)
        encoded = raw.encode("utf-8")
    except (UnicodeError, ValueError):
        raise GlobalError("ARGUMENTS_UNSUPPORTED") from None
    if len(encoded) > 16384:
        raise GlobalError("REQUEST_INPUT_TOO_LARGE")
    return raw, hashlib.sha256(encoded).hexdigest()
