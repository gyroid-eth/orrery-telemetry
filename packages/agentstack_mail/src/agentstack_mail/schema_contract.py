"""Stdlib validation for the bounded ORRERY wire schema subset."""

import hashlib
import json
from pathlib import Path
import re


def validate_schema(
    value, schema, reason="ARGUMENTS_UNSUPPORTED", *, error_type=ValueError
):
    """Validate the deliberately bounded schema subset used by this fixture.

    No coercion: bool is not int, unknown fields cannot disappear in FastMCP.
    Both the server and stdlib wrapper use this one canonical implementation.
    """
    allowed = {
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "enum",
        "format",
        "items",
        "maxLength",
        "maximum",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "type",
        "title",
        "description",
    }
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise error_type(reason)
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_schema(value, option, reason, error_type=error_type)
                return
            except error_type:
                pass
        raise error_type(reason)
    if "const" in schema and value != schema["const"]:
        raise error_type(reason)
    if "enum" in schema and value not in schema["enum"]:
        raise error_type(reason)
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "integer": int,
        "string": str,
        "boolean": bool,
        "null": type(None),
    }
    if kind and (kind not in types or type(value) is not types[kind]):
        raise error_type(reason)
    if kind == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys() or (
            schema.get("additionalProperties") is False and value.keys() - props.keys()
        ):
            raise error_type(reason)
        for key, val in value.items():
            if key in props:
                validate_schema(val, props[key], reason, error_type=error_type)
    if kind == "array":
        for val in value:
            validate_schema(val, schema["items"], reason, error_type=error_type)
    if kind == "integer" and (
        value < schema.get("minimum", value) or value > schema.get("maximum", value)
    ):
        raise error_type(reason)
    if kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", len(value)
        ):
            raise error_type(reason)
        if schema.get("pattern") and re.fullmatch(schema["pattern"], value) is None:
            raise error_type(reason)
        if schema.get("format") == "date-time":
            from datetime import datetime

            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise error_type(reason) from None


def s2a_fixture():
    """One source fixture, also copied beside the installed stdlib helper."""
    here = Path(__file__).resolve().parent
    for path in (
        here / "global-server-s2a.json",
        here / "fixtures/global-server-s2a.json",
        here.parent.parent / "fixtures/global-server-s2a.json",
    ):
        if path.is_file():
            return json.loads(path.read_text())
    raise ValueError("CAPABILITY_INVALID")


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def normalize_arguments(supplied, schema, *, error_type=ValueError):
    if "request_id" in schema["properties"] and not (
        isinstance(supplied.get("request_id"), str)
        and re.fullmatch(
            schema["properties"]["request_id"]["pattern"], supplied["request_id"]
        )
    ):
        raise error_type("REQUEST_ID_INVALID")
    if "policy" in supplied and supplied["policy"] not in (
        "open",
        "auto",
        "contacts_only",
        "block_all",
    ):
        raise error_type("CONTACT_POLICY_INVALID")
    if (
        "task_description" in supplied
        and supplied["task_description"] is not None
        and (
            not isinstance(supplied["task_description"], str)
            or len(supplied["task_description"]) > 2048
        )
    ):
        raise error_type("TASK_DESCRIPTION_INVALID")
    if "ttl_seconds" in supplied and (
        type(supplied["ttl_seconds"]) is not int or supplied["ttl_seconds"] < 0
    ):
        raise error_type("CONTACT_TTL_INVALID")
    validate_schema(supplied, schema, error_type=error_type)
    result = {
        k: v["default"] for k, v in schema["properties"].items() if "default" in v
    }
    result.update(supplied)
    if "ttl_seconds" in result:
        result["ttl_seconds"] = max(60, result["ttl_seconds"])
    return result


def request_preimage(tool, args, ignored, *, error_type=ValueError):
    excluded = {
        "expected_server_instance_id",
        "candidate_generation",
        "authority_epoch",
        "agent_id",
        "expected_credential_generation",
        "registration_token",
        "request_id",
        *ignored,
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
        raise error_type("ARGUMENTS_UNSUPPORTED") from None
    if len(encoded) > 16384:
        raise error_type("REQUEST_INPUT_TOO_LARGE")
    return raw, hashlib.sha256(encoded).hexdigest()


def definitive_rejection(reason, *, existing_pending, contract):
    rules = contract["client_uuid_contract"]["definite_rejections"]
    return reason in rules["after_receipt_lookup"] or (
        not existing_pending and reason in rules["before_receipt_lookup"]
    )
