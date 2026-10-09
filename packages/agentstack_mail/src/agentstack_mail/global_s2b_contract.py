"""Single S2b wire/preimage contract, also usable by the stdlib client."""

import base64
import hashlib
from copy import deepcopy

from .global_server import GlobalError
from .schema_contract import (
    fixture,
    normalize_arguments,
    request_preimage,
    canonical,
    validate_schema,
)

FIXTURE = fixture("global-server-s2b.json")
TOOLS = FIXTURE["tool_contracts"]
DDL = FIXTURE["db_extensions"]["ddl"]


def validate(value, schema, reason="ARGUMENTS_UNSUPPORTED"):
    validate_schema(value, schema, reason, error_type=GlobalError)


def attachments(values):
    if len(values) > 8:
        raise GlobalError("PAYLOAD_TOO_LARGE")
    result = []
    total = 0
    for item in values:
        name = item["filename"]
        if not name or name in (".", "..") or any(c in name for c in "/\\\x00"):
            raise GlobalError("ATTACHMENT_INVALID")
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
        except (ValueError, UnicodeError):
            raise GlobalError("ATTACHMENT_INVALID") from None
        if base64.b64encode(content).decode("ascii") != item["content_base64"]:
            raise GlobalError("ATTACHMENT_INVALID")
        total += len(content)
        if len(content) > 262144 or total > 524288:
            raise GlobalError("PAYLOAD_TOO_LARGE")
        result.append(
            {
                **item,
                "content": content,
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
    return result


def arguments(tool, supplied):
    if tool == "send_message" and supplied.get("thread_id") is not None:
        raise GlobalError("LEGACY_THREAD_WRITE_NOT_SUPPORTED")
    args = normalize_arguments(
        supplied, TOOLS[tool]["input_schema"], error_type=GlobalError
    )
    for key in ("agent_id", "message_id", "reply_to"):
        if args.get(key) is not None and args[key] > 2**63 - 1:
            raise GlobalError("ARGUMENTS_UNSUPPORTED")
    for key in ("to_agent_ids", "cc_agent_ids", "bcc_agent_ids"):
        if any(aid > 2**63 - 1 for aid in (args.get(key) or [])):
            raise GlobalError("ARGUMENTS_UNSUPPORTED")
    if args.get("format") not in (None, "json"):
        raise GlobalError("FORMAT_NOT_SUPPORTED")
    if args.get("sender_token") not in (None, args["registration_token"]):
        raise GlobalError("CREDENTIAL_INPUT_CONFLICT")
    if tool in ("send_message", "reply_message"):
        if args.get("attachment_paths"):
            raise GlobalError("ATTACHMENT_PATHS_NOT_SUPPORTED")
        if args.get("convert_images"):
            raise GlobalError("IMAGE_CONVERSION_NOT_SUPPORTED")
        if args.get("broadcast"):
            raise GlobalError("BROADCAST_NOT_SUPPORTED")
        if args.get("auto_contact_if_blocked"):
            raise GlobalError("AUTO_CONTACT_NOT_SUPPORTED")
        if (
            len(args["body_md"].encode("utf-8")) > 65536
            or len(args.get("subject", "").encode("utf-8")) > 800
        ):
            raise GlobalError("PAYLOAD_TOO_LARGE")
        attachments(args["attachments"])
    if (
        tool == "search_messages"
        and args["attachment_sha256"] is not None
        and (args["limit"] != 1 or args["include_bodies"])
    ):
        raise GlobalError("ARGUMENTS_UNSUPPORTED")
    if tool == "summarize_thread":
        if args["llm_mode"] or args["llm_model"] is not None:
            raise GlobalError("LLM_SUMMARY_NOT_SUPPORTED")
        if (args["message_id"] is None) == (args["thread_id"] is None):
            raise GlobalError("ARGUMENTS_UNSUPPORTED")
    return args


def semantic_arguments(tool, args):
    result = deepcopy(args)
    if tool in ("send_message", "reply_message"):
        body = result.pop("body_md").encode("utf-8")
        result["body_utf8_sha256"] = hashlib.sha256(body).hexdigest()
        result["body_utf8_byte_size"] = len(body)
        result["attachments"] = [
            {k: v for k, v in item.items() if k not in ("content", "content_base64")}
            for item in attachments(args["attachments"])
        ]
    result.pop("sender_token", None)
    return result


def preimage(tool, args):
    return request_preimage(
        tool,
        semantic_arguments(tool, args),
        [*TOOLS[tool]["accepted_ignored_arguments"], "format", "sender_token"],
        error_type=GlobalError,
    )


def preimage_schema(tool):
    schema = deepcopy(TOOLS[tool]["input_schema"])
    props = schema["properties"]
    if tool in ("send_message", "reply_message"):
        props.pop("body_md")
        schema["required"].remove("body_md")
        props.update(
            body_utf8_sha256={"type": "string", "pattern": "^[0-9a-f]{64}$"},
            body_utf8_byte_size={"type": "integer", "minimum": 0, "maximum": 65536},
        )
        schema["required"] += ["body_utf8_sha256", "body_utf8_byte_size"]
        item = props["attachments"]["items"]
        item["properties"].pop("content_base64")
        item["required"].remove("content_base64")
        item["properties"].update(
            sha256={"type": "string", "pattern": "^[0-9a-f]{64}$"},
            byte_size={"type": "integer", "minimum": 0, "maximum": 262144},
        )
        item["required"] += ["sha256", "byte_size"]
    for key in [
        "registration_token",
        "request_id",
        "expected_server_instance_id",
        "candidate_generation",
        "authority_epoch",
        "agent_id",
        "expected_credential_generation",
        *TOOLS[tool]["accepted_ignored_arguments"],
        "sender_token",
        "format",
    ]:
        props.pop(key, None)
        if key in schema["required"]:
            schema["required"].remove(key)
    return schema


def wire_size(result):
    # ToolResult uses this exact compact text plus structuredContent. Reserve
    # framing/request-id space rather than budgeting only one representation.
    return (
        len(
            canonical(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "result": {
                        "content": [{"type": "text", "text": canonical(result)}],
                        "structuredContent": result,
                        "isError": False,
                    },
                }
            ).encode()
        )
        + 4096
    )


def ensure_budget(result):
    if wire_size(result) > 1048576:
        raise GlobalError("MESSAGE_RESPONSE_TOO_LARGE")
    return result


def make_intent(tool, supplied):
    """One token-free resend intent for the later client integration."""
    if TOOLS[tool]["operation"] != "write":
        raise GlobalError("ARGUMENTS_UNSUPPORTED")
    args = arguments(tool, supplied)
    header = {
        "kind": "orrery-global-message-intent-v1",
        "tool": tool,
        "request_id": args["request_id"],
        "server_instance_id": args["expected_server_instance_id"],
        **{k: args[k] for k in ("candidate_generation", "authority_epoch", "agent_id")},
        "credential_generation": args["expected_credential_generation"],
        "phase": "planned",
        "receipt": None,
    }
    excluded = {
        "registration_token",
        "sender_token",
        "request_id",
        "expected_server_instance_id",
        "candidate_generation",
        "authority_epoch",
        "agent_id",
        "expected_credential_generation",
        "format",
        *TOOLS[tool]["accepted_ignored_arguments"],
    }
    header["canonical_intent"] = {k: v for k, v in args.items() if k not in excluded}
    if len(canonical(header["canonical_intent"]).encode("utf-8")) > 1048576:
        raise GlobalError("PAYLOAD_TOO_LARGE")
    _, header["request_hash"] = preimage(tool, args)
    return header


def resume_intent(intent, owner):
    """Reconstruct exact bytes without caller arguments or recipient lookup."""
    keys = {
        "kind",
        "tool",
        "request_id",
        "server_instance_id",
        "candidate_generation",
        "authority_epoch",
        "agent_id",
        "credential_generation",
        "phase",
        "receipt",
        "canonical_intent",
        "request_hash",
    }
    if (
        set(intent) != keys
        or intent.get("kind") != "orrery-global-message-intent-v1"
        or intent.get("phase") not in ("planned", "committed")
    ):
        raise GlobalError("OUTPUT_SCHEMA_INVALID")
    bindings = {
        "server_instance_id": "expected_server_instance_id",
        "candidate_generation": "candidate_generation",
        "authority_epoch": "authority_epoch",
        "agent_id": "agent_id",
        "credential_generation": "expected_credential_generation",
    }
    if any(intent[k] != owner.get(v) for k, v in bindings.items()):
        raise GlobalError("MUTATION_PENDING_REQUIRES_RESOLUTION")
    args = arguments(
        intent["tool"],
        {**intent["canonical_intent"], **owner, "request_id": intent["request_id"]},
    )
    if (
        make_intent(intent["tool"], args)["canonical_intent"]
        != intent["canonical_intent"]
        or preimage(intent["tool"], args)[1] != intent["request_hash"]
    ):
        raise GlobalError("OUTPUT_SCHEMA_INVALID")
    return args


def complete_intent(intent, receipt, owner):
    resume_intent(intent, owner)
    validate(receipt, TOOLS[intent["tool"]]["output_schema"], "RESPONSE_INVALID")
    for key in (
        "server_instance_id",
        "candidate_generation",
        "authority_epoch",
        "agent_id",
        "credential_generation",
        "request_id",
    ):
        if receipt[key] != intent[key]:
            raise GlobalError("RESPONSE_INVALID")
    return {**intent, "phase": "committed", "receipt": receipt}
