"""The schema5 wire catalog and canonical mutation preimages."""

from copy import deepcopy
import hashlib
from pathlib import PurePosixPath

try:
    from .schema_contract import (
        ContractError as GlobalError,
        fixture,
        normalize_arguments,
        request_preimage,
    )
    from .global_s2b_contract import check_subject, validate as validate
except ImportError:
    from schema_contract import (
        ContractError as GlobalError,
        fixture,
        normalize_arguments,
        request_preimage,
    )
    from global_s2b_contract import check_subject, validate as validate

FIXTURE = fixture("global-server-s2c.json")
TOOLS = FIXTURE["tool_contracts"] | FIXTURE["existing_tool_changes"]


def arguments(tool, supplied):
    for key in ("ttl_seconds", "extend_seconds", "file_reservation_ttl_seconds"):
        if (
            key in supplied
            and tool != "macro_contact_handshake"
            and (type(supplied[key]) is not int or not 60 <= supplied[key] <= 86400)
        ):
            raise GlobalError("TTL_OUT_OF_RANGE")
    if tool in ("renew_file_reservations", "release_file_reservations"):
        paths, ids = supplied.get("paths"), supplied.get("file_reservation_ids")
        if paths is not None and ids is not None:
            raise GlobalError("LEASE_SELECTOR_CONFLICT")
        if paths == [] or ids == []:
            raise GlobalError("LEASE_SELECTOR_EMPTY")
    args = normalize_arguments(
        supplied, TOOLS[tool]["input_schema"], error_type=GlobalError
    )
    if args.get("format") not in (None, "json"):
        raise GlobalError("FORMAT_NOT_SUPPORTED")
    for key, value in args.items():
        if key.endswith("agent_id") and value is not None and value > 2**63 - 1:
            raise GlobalError("ARGUMENTS_UNSUPPORTED")
    paths, ids = args.get("paths"), args.get("file_reservation_ids")
    if ids and any(i > 2**63 - 1 for i in ids):
        raise GlobalError("ARGUMENTS_UNSUPPORTED")
    for key in ("paths", "file_reservation_paths"):
        for path in args.get(key) or []:
            if path.startswith(("tool://", "resource://", "service://")):
                if path in ("tool://", "resource://", "service://") or "\x00" in path:
                    raise GlobalError("ARGUMENTS_UNSUPPORTED")
                continue
            if not PurePosixPath(path).is_absolute():
                raise GlobalError("ABSOLUTE_PATH_REQUIRED")
            parts = PurePosixPath(path).parts
            if "\x00" in path or "\\" in path or len(parts) > 128:
                raise GlobalError("ARGUMENTS_UNSUPPORTED")
            glob = next(
                (i for i, p in enumerate(parts) if any(c in p for c in "*?[")),
                len(parts),
            )
            if ".." in parts[glob:]:
                raise GlobalError("ARGUMENTS_UNSUPPORTED")
    for key in ("ttl_seconds", "extend_seconds", "file_reservation_ttl_seconds"):
        if (
            key in args
            and tool != "macro_contact_handshake"
            and not 60 <= args[key] <= 86400
        ):
            raise GlobalError("TTL_OUT_OF_RANGE")
    if tool == "macro_contact_handshake":
        subject, body = args["welcome_subject"], args["welcome_body"]
        if (subject is None) != (body is None):
            raise GlobalError("WELCOME_INPUT_INCOMPLETE")
        if subject is not None:
            check_subject(subject)
            if len(body.encode("utf-8")) > 65536:
                raise GlobalError("PAYLOAD_TOO_LARGE")
    return args


def semantic_arguments(tool, args):
    result = dict(args)
    if tool == "macro_contact_handshake":
        body = result.pop("welcome_body")
        result["welcome_body_sha256"] = (
            None if body is None else hashlib.sha256(body.encode()).hexdigest()
        )
        result["welcome_body_byte_size"] = None if body is None else len(body.encode())
    return result


def preimage(tool, args):
    return request_preimage(
        tool,
        semantic_arguments(tool, args),
        ignored=TOOLS[tool].get("accepted_ignored_arguments", ()),
        error_type=GlobalError,
    )


def preimage_schema(tool):
    schema = deepcopy(TOOLS[tool]["input_schema"])
    ignored = set(TOOLS[tool].get("accepted_ignored_arguments", ())) | {
        "request_id",
        "registration_token",
        "expected_server_instance_id",
        "candidate_generation",
        "authority_epoch",
        "agent_id",
        "expected_credential_generation",
    }
    for key in ignored:
        schema["properties"].pop(key, None)
    if tool == "macro_contact_handshake":
        schema["properties"].pop("welcome_body")
        schema["properties"].update(
            welcome_body_sha256={
                "anyOf": [
                    {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    {"type": "null"},
                ]
            },
            welcome_body_byte_size={
                "anyOf": [
                    {"type": "integer", "minimum": 0, "maximum": 65536},
                    {"type": "null"},
                ]
            },
        )
    schema["required"] = sorted(schema["properties"])
    return schema
