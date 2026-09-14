"""Choose where Claude's quota comes from on this machine.

Two sources answer for the same account and neither is always present. The
account usage endpoint returns every window, including the per-model weekly
ones, but needs credentials Claude Code stored locally and can be rate
limited. The status-line observer needs no network at all but only ever sees
the account windows, and only after the operator wires it up.

Ask the account first, and fall back to the observer whenever the account
cannot answer, so a rate limit or a signed-out machine degrades to fewer
windows instead of to nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .base import QuotaSnapshot
from .claude import ClaudeQuotaProvider
from .claude_account import ClaudeAccountQuotaProvider


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
        account_snapshot: QuotaSnapshot | None = None
        if self.account.available():
            account_snapshot = self.account.read()
            if account_snapshot.status == "ok":
                return account_snapshot

        observed = self.statusline.read()
        if observed.status == "ok":
            return observed
        # Neither answered: report the account's reason when it was the source
        # that was actually tried, so "signed out" does not read as "no status
        # line observed yet".
        return account_snapshot or observed
