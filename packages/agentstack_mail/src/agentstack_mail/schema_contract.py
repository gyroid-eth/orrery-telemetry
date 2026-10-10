"""Stdlib validation for the bounded ORRERY wire schema subset."""

import hashlib
import json
import math
from pathlib import Path
import re


class ContractError(ValueError):
    """Bounded wire errors shared by server and stdlib clients."""


def _nonnegative_integer(value):
    return type(value) is int and value >= 0


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _pattern(value):
    if not isinstance(value, str):
        return False
    try:
        re.compile(value)
        return True
    except re.error:
        return False


SCHEMA_TYPES = {
    "object": dict,
    "array": list,
    "integer": int,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


# Admission and supported keyword names have one source. Child schemas are
# traversed below only after these shapes have been checked.
SCHEMA_VALUE_RULES = {
    "additionalProperties": lambda v: type(v) is bool,
    "anyOf": lambda v: isinstance(v, list) and bool(v),
    "const": lambda v: True,
    "default": lambda v: True,
    "enum": lambda v: isinstance(v, list),
    "format": lambda v: isinstance(v, str),
    "items": lambda v: isinstance(v, dict),
    "maxLength": _nonnegative_integer,
    "maximum": _number,
    "minLength": _nonnegative_integer,
    "minimum": _number,
    "pattern": _pattern,
    "properties": lambda v: isinstance(v, dict) and all(isinstance(k, str) for k in v),
    "required": lambda v: isinstance(v, list) and all(isinstance(k, str) for k in v),
    "type": lambda v: isinstance(v, str) and v in SCHEMA_TYPES,
    "title": lambda v: isinstance(v, str),
    "description": lambda v: isinstance(v, str),
    "minItems": _nonnegative_integer,
    "maxItems": _nonnegative_integer,
    "uniqueItems": lambda v: type(v) is bool,
    "x-fastmcp-wrap-result": lambda v: type(v) is bool,
}


def validate_schema_definition(schema, *, error_type=ValueError):
    """Inspect every schema branch once when a fixture is admitted."""
    if not isinstance(schema, dict) or any(
        key not in SCHEMA_VALUE_RULES or not SCHEMA_VALUE_RULES[key](value)
        for key, value in schema.items()
    ):
        raise error_type("CAPABILITY_INVALID")
    for child in schema.get("anyOf", []):
        validate_schema_definition(child, error_type=error_type)
    for child in schema.get("properties", {}).values():
        validate_schema_definition(child, error_type=error_type)
    if "items" in schema:
        validate_schema_definition(schema["items"], error_type=error_type)


def fixture(name="global-server-s2a.json"):
    here = Path(__file__).resolve().parent
    for path in (
        here / name,
        here / "fixtures" / name,
        here.parent.parent / "fixtures" / name,
    ):
        if path.is_file():
            value = json.loads(path.read_text())

            # Fixed named definitions are expanded before schema admission.
            # schema_ref is a fixture compiler marker, never a wire keyword.
            if name == "global-server-s2c.json":

                def expand(node):
                    if isinstance(node, dict):
                        if "schema_ref" in node:
                            if node != {"schema_ref": "lease"}:
                                raise ValueError("CAPABILITY_INVALID")
                            return expand(value["lease_schema"])
                        return {k: expand(v) for k, v in node.items()}
                    if isinstance(node, list):
                        return [expand(v) for v in node]
                    return node

                value = expand(value)
                value["client_uuid_contract"] = {
                    "definite_rejections": assemble_rejections(value)
                }

            def definitions(node):
                if isinstance(node, dict):
                    for key, child in node.items():
                        if key.endswith("_schema") and isinstance(child, dict):
                            validate_schema_definition(child)
                        else:
                            definitions(child)
                elif isinstance(node, list):
                    for child in node:
                        definitions(child)

            definitions(value)
            return value
    raise ValueError("CAPABILITY_INVALID")


def validate_schema(
    value, schema, reason="ARGUMENTS_UNSUPPORTED", *, error_type=ValueError
):
    """Validate the deliberately bounded schema subset used by this fixture.

    No coercion: bool is not int, unknown fields cannot disappear in FastMCP.
    Both the server and stdlib wrapper use this one canonical implementation.
    """
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_schema(value, option, reason, error_type=error_type)
                return
            except error_type:
                pass
        raise error_type(reason)
    if "const" in schema and (
        value != schema["const"]
        or (isinstance(value, bool) != isinstance(schema["const"], bool))
    ):
        raise error_type(reason)
    if "enum" in schema and value not in schema["enum"]:
        raise error_type(reason)
    kind = schema.get("type")
    if kind and (kind not in SCHEMA_TYPES or type(value) is not SCHEMA_TYPES[kind]):
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
        if (
            not schema.get("minItems", 0)
            <= len(value)
            <= schema.get("maxItems", len(value))
        ):
            raise error_type(reason)
        if schema.get("uniqueItems") and len({canonical(v) for v in value}) != len(
            value
        ):
            raise error_type(reason)
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
    return fixture()


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


def assemble_rejections(s2c):
    """Compile phase deltas once; runtime uses definitive_rejection only."""
    phases = fixture("global-server-s2b.json")["fixed_reasons"]
    preserve = set(phases["preserve_pending"])
    before = set(phases["pre_receipt"]) - preserve
    after = set(phases["new_intent_only"]) - preserve
    known = set().union(*map(set, phases.values()))
    inherited = fixture("global-server-s2a.json")["client_uuid_contract"][
        "definite_rejections"
    ]
    for target, source in (
        (before, "before_receipt_lookup"),
        (after, "after_receipt_lookup"),
    ):
        target.update(set(inherited[source]) - known)
    known.update(before | after)
    delta = s2c["rejection_delta"]["append"]
    before.update(set(delta["pre_receipt"]) - known)
    after.update(set(delta["new_intent_only"]) - known)
    return {
        "before_receipt_lookup": sorted(before),
        "after_receipt_lookup": sorted(after),
    }
