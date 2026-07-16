"""Allowlisted agent-mail proxy boundary for Codex App identities."""


class AgentMailClient:
    """Token-bearing server-side client; no generic tool passthrough."""

    def fetch_inbox(self, external_id: str):
        raise NotImplementedError("agent-mail proxy is not implemented in this scaffold")
