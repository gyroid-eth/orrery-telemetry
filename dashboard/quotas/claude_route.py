"""Choose where Claude's quota comes from on this machine.

Two sources answer for the same account and neither is always present. The
account usage endpoint returns every window, including the per-model weekly
ones, but needs credentials Claude Code stored locally and can be rate
limited. The status-line observer needs no network at all but only ever sees
the account windows, and only after the operator wires it up.

Ask the account first, and fall back to the observer whenever the account
cannot answer, so a rate limit, an outage or a signed-out machine degrades to
fewer windows instead of to nothing. A fallback answer is reported as
`degraded` carrying why the account could not answer: the observer's windows
are a subset, and nothing else in the payload would say so.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .base import QuotaSnapshot
from .claude import ClaudeQuotaProvider
from .claude_account import ClaudeAccountQuotaProvider


LOGGER = logging.getLogger("agentstack.dashboard.quotas")


class ClaudeQuotaRoute:
    provider_name = "claude"
    source_name = "claude-account-usage"
    ttl_seconds = 120

    def __init__(
        self,
        account: ClaudeAccountQuotaProvider | None = None,
        statusline: ClaudeQuotaProvider | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.account = account if account is not None else ClaudeAccountQuotaProvider()
        self.statusline = statusline if statusline is not None else ClaudeQuotaProvider()
        self._clock = clock

    def read(self) -> QuotaSnapshot:
        account_snapshot = self._read_account()
        if account_snapshot.status == "ok":
            return account_snapshot

        observed = self.statusline.read()
        if observed.status == "ok":
            # The observer answers with the account windows only, so say that
            # this is the lesser answer and why the fuller one was missed.
            return observed.with_status(
                "degraded", f"fallback_{account_snapshot.reason or 'account_unavailable'}")
        # Neither answered: report the account's reason, so "signed out" does
        # not read as "no status line observed yet".
        return account_snapshot

    def _read_account(self) -> QuotaSnapshot:
        """The account's answer, as a snapshot even when it fails.

        A provider raises on a transport failure (timeout, 5xx, unreadable
        body) because that is how QuotaService keeps one provider's failure
        from reaching the others. Here it must not end the read: the whole
        point of the route is that a failed account read still leaves the
        observer to ask, and a timeout is the commonest way it fails.

        The provider answers "signed out" and "turned off" without touching the
        network, so this asks it directly rather than pre-checking.
        """
        try:
            return self.account.read()
        except Exception as exc:  # noqa: BLE001 - reported, then the observer answers
            LOGGER.info("claude account usage unavailable (%s)", type(exc).__name__)
            return QuotaSnapshot(
                provider=self.provider_name,
                source=self.account.source_name,
                observed_at=int(self._clock()),
                status="unavailable",
                reason="account_usage_failed",
            )
