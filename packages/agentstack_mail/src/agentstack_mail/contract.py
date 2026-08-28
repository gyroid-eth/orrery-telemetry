"""Versioned identity and isolation contract for the extracted service."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

CONTRACT_VERSION = 1

RUNTIME_REQUIRED_TOOLS = frozenset(
    {
        "acknowledge_message",
        "ensure_project",
        "fetch_inbox",
        "file_reservation_paths",
        "health_check",
        "register_agent",
        "release_file_reservations",
        "renew_file_reservations",
        "retire_agent",
        "send_message",
        "set_contact_policy",
        "whois",
    }
)

MODEL_COMPATIBILITY_TOOLS = frozenset(
    {
        "acknowledge_message",
        "ensure_project",
        "fetch_inbox",
        "fetch_summary",
        "fetch_topic",
        "file_reservation_paths",
        "health_check",
        "list_contacts",
        "macro_contact_handshake",
        "macro_file_reservation_cycle",
        "macro_start_session",
        "mark_message_read",
        "register_agent",
        "release_file_reservations",
        "renew_file_reservations",
        "reply_message",
        "request_contact",
        "respond_contact",
        "search_messages",
        "send_message",
        "set_contact_policy",
        "summarize_thread",
        "whois",
    }
)

# Published after the cutover, deliberately kept out of the two sets above.
#
# Those sets record what the frozen predecessor exposed; growing either one
# would claim this tool was part of that surface, which it was not. This set
# says the true thing instead: the surface gained a tool, on a date, for a
# reason, and the ledger of intentional differences carries the rest.
#
# unretire_agent is here because retirement had become one-way in practice. A
# session ending outside tmux could resolve an unrelated live agent's name and
# retire it (two agents lost that way on 2026-08-27), and nothing on the
# published surface could undo it: register_agent does not clear retired_at,
# and the tool that does was excluded as "not in the predecessor". Recovery
# meant editing the database by hand, which is the worst tool to reach for
# during an incident.
POST_CUTOVER_PUBLISHED_TOOLS = frozenset({"unretire_agent"})

COMPATIBILITY_TOOLS = (
    RUNTIME_REQUIRED_TOOLS | MODEL_COMPATIBILITY_TOOLS | POST_CUTOVER_PUBLISHED_TOOLS
)

LOCAL_ONLY_TOOLS = frozenset(
    {
        "bootstrap",
        "reserve_files",
        "renew_reservations",
        "release_reservations",
        "runtime_status",
    }
)

NON_COMPATIBILITY_UPSTREAM_TOOLS = frozenset(
    {
        "archive_project",
        "create_agent_identity",
        "deregister_agent",
        "expire_window",
        "force_release_file_reservation",
        "hard_delete_agent",
        "hard_delete_project",
        "install_precommit_guard",
        "list_window_identities",
        "macro_prepare_thread",
        "purge_old_messages",
        "rename_window",
        "summarize_recent",
        "unarchive_project",
        "uninstall_precommit_guard",
    }
)

POLICY_EXCLUDED_UPSTREAM_TOOLS = frozenset(
    {
        "create_agent_identity",
        "hard_delete_agent",
        "hard_delete_project",
        "purge_old_messages",
    }
)

SERVICE_IDENTITY = MappingProxyType(
    {
        # What humans see (MCP serverInfo.name, banners). The machine
        # identifiers below stay "agentstack-mail": they are file names, wheel
        # names, and config keys whose rename would be churn with no visible
        # payoff, while this one string is the whole "which server am I
        # talking to" experience.
        "display_name": "ORRERY Mail",
        "distribution": "agentstack-mail",
        "python_package": "agentstack_mail",
        "cli": "agentstack-mail",
        "mcp_provider_identity": "agentstack-mail",
        "claude_client_key": "mcp-agent-mail",
        "codex_client_key": "agent-mail",
        "client_key_policy": "preserve_existing",
        "launchd_label": "org.orrery.mail",
        "systemd_unit": "agentstack-mail.service",
        "environment_prefix": "AGENTSTACK_MAIL_",
    }
)


@dataclass(frozen=True, slots=True)
class IsolationDefaults:
    """Defaults that cannot collide with a stock or existing AgentMail service."""

    host: str = "127.0.0.1"
    port: int = 18765
    mcp_path: str = "/mcp"
    api_path: str = "/api/"
    home: str = "~/.agentstack/mail"
    database: str = "~/.agentstack/mail/storage.sqlite3"
    archive: str = "~/.agentstack/mail/archive"
    signals: str = "~/.agentstack/mail/signals"

    @property
    def mcp_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.mcp_path}"

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.api_path}"


ISOLATION_DEFAULTS = IsolationDefaults()

# The legacy values are test data only. Production code must not fall back to
# them when an AgentStack-specific value is absent.
LEGACY_COLLISION_VALUES = MappingProxyType(
    {
        "python_package": "mcp_agent_mail",
        "mcp_provider_identity": "mcp-agent-mail",
        "port": 8765,
        "database": "./storage.sqlite3",
        "archive": "~/.mcp_agent_mail_git_mailbox_repo",
        "signals": "~/.mcp_agent_mail/signals",
    }
)
