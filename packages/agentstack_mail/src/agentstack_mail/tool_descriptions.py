"""Compact published descriptions for the compatibility tools.

The upstream-derived docstrings in :mod:`agentstack_mail.app` run 500–3,000
bytes each; published verbatim, tools/list weighed 38 KB (~9,500 tokens) and
every agent session paid that before its first message. The docstrings stay in
the source as developer documentation; what MCP clients see is this table —
each entry keeps the caveats an agent actually needs at call time and nothing
else.

Two read tools (``search_messages``, ``summarize_thread``) are absent on
purpose: their descriptions are frozen by ``live-tools-list.json`` via
``test_actual_tool_schemas_match_the_frozen_live_contract`` and must publish
the fixture text unchanged.
"""

from __future__ import annotations

from types import MappingProxyType

COMPACT_TOOL_DESCRIPTIONS = MappingProxyType(
    {
        "acknowledge_message": (
            "Acknowledge a message that was sent with ack_required=true. "
            "Args: project_key, agent_name, message_id."
        ),
        "ensure_project": (
            "Create or fetch the project workspace for a human_key (an absolute "
            "project path). Idempotent; call once before registering agents. "
            "Do not invent variants of the key — cooperating agents must pass "
            "the identical string."
        ),
        "fetch_inbox": (
            "List recent inbox messages for an agent without changing "
            "read/ack state. Filters: urgent_only, since_ts, topic, limit "
            "(default 20), include_bodies (default false — bodies cost "
            "tokens; fetch them only when the notification did not already "
            "carry the text)."
        ),
        "fetch_summary": (
            "Compact inbox digest for an agent: unread/urgent counts plus "
            "short previews. Cheaper than fetch_inbox when you only need to "
            "know whether anything demands attention."
        ),
        "fetch_topic": (
            "List recent messages carrying a topic tag within a project. "
            "Args: project_key, topic; optional limit/include_bodies."
        ),
        "file_reservation_paths": (
            "Reserve project-relative paths/globs before editing so other "
            "agents see your intent (advisory lease). ttl_seconds >= 600 "
            "recommended; exclusive=true for edits. Returns granted leases "
            "and conflicts — do not edit a path another agent holds "
            "exclusively; coordinate or wait for expiry."
        ),
        "health_check": (
            "Return service readiness (status, endpoint, database). No "
            "arguments; safe to call anytime."
        ),
        "list_contacts": (
            "List an agent's contacts and their policies. Args: project_key, "
            "agent_name."
        ),
        "macro_contact_handshake": (
            "One-call contact setup between two agents (request + auto-accept "
            "where policy allows). Args: project_key, requester, target."
        ),
        "macro_file_reservation_cycle": (
            "Reserve paths and report conflicts in one call — the preferred "
            "way to take file reservations before editing. Args: project_key, "
            "agent_name, paths, ttl_seconds (>=600 recommended)."
        ),
        "macro_start_session": (
            "One-call session bootstrap: ensure_project + register_agent + "
            "inbox summary. Equivalent to calling the three tools separately."
        ),
        "mark_message_read": (
            "Mark a message as read for an agent. Args: project_key, "
            "agent_name, message_id."
        ),
        "register_agent": (
            "Register this session as an agent, or refresh an existing "
            "agent's program/model/task_description and last_active. If "
            "AGENT_NAME is set in your environment, pass it as name verbatim "
            "— any name is accepted (passthrough mode; do not second-guess "
            "naming-format warnings). Omit name to have one generated. Pass "
            "registration_token when you hold one; the server returns the "
            "token to keep for retire_agent."
        ),
        "release_file_reservations": (
            "Release file reservations you hold, by paths or all. Args: "
            "project_key, agent_name; optional paths."
        ),
        "renew_file_reservations": (
            "Extend the TTL of reservations you hold. Args: project_key, "
            "agent_name; optional paths, ttl_seconds."
        ),
        "reply_message": (
            "Reply within an existing thread; recipients default to the "
            "original sender. Args: project_key, message_id, sender_name, "
            "body_md. Keep bodies short — send paths plus a summary, never "
            "whole file contents."
        ),
        "request_contact": (
            "Ask another agent for contact permission (required when their "
            "policy is contacts_only). Args: project_key, requester, target."
        ),
        "respond_contact": (
            "Accept or decline a pending contact request. Args: project_key, "
            "agent_name, requester, accept."
        ),
        "retire_agent": (
            "Retire an agent when its work is done so the roster stays "
            "truthful. Args: project_key, agent_name; pass registration_token "
            "if you hold one. Message history is preserved."
        ),
        "send_message": (
            "Send a Markdown message to named agents (to/cc/bcc). Args: "
            "project_key, sender_name, to, subject, body_md; optional "
            "importance (low/normal/high/urgent), ack_required, thread_id, "
            "topic, attachment_paths. Keep bodies short — send file paths "
            "plus a change summary, never whole file contents; recipients "
            "read files themselves."
        ),
        "set_contact_policy": (
            "Set an agent's inbound contact policy: open, contacts_only, or "
            "block_all. Args: project_key, agent_name, policy."
        ),
        "unretire_agent": (
            "Restore a retired agent to active status so it accepts new "
            "messages again. Args: project_key, agent_name, "
            "registration_token (optional; pass it if you hold it). Use when "
            "an agent is alive but the roster says retired — activity "
            "recorded after retired_at is the signature."
        ),
        "whois": (
            "Look up one agent's profile (program, model, task, last_active) "
            "by exact name. A 'not found' error is the normal signal that a "
            "name is free."
        ),
    }
)
