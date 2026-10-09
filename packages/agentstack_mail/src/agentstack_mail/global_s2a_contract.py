"""Shared normative S2a schemas and canonical request bytes (no runtime state)."""

from __future__ import annotations

import hashlib
import json

from .global_server import GlobalError
from .schema_contract import (
    validate_schema,
    s2a_fixture,
    canonical,
    normalize_arguments,
    request_preimage,
)

FIXTURE = s2a_fixture()
# Editable checkouts keep fixtures outside src; installed wheels include them.
TOOLS = FIXTURE["tool_contracts"]
DDL = FIXTURE["candidate_validation_contract"]["extension_ddl"]


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
    return validate_schema(value, schema, reason, error_type=GlobalError)


def arguments(tool, supplied):
    return normalize_arguments(
        supplied, TOOLS[tool]["input_schema"], error_type=GlobalError
    )


def preimage(tool, args):
    return request_preimage(
        tool, args, TOOLS[tool]["accepted_ignored_arguments"], error_type=GlobalError
    )
