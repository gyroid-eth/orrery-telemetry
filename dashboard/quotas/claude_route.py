"""Combine Claude's account endpoint with its local status-line observation.

The account reader is the complete but rate-limited source. The observer is a
cheap local source that can update account windows between outbound reads. A
route read therefore always checks both, keeps each window's own source and
observation time, and never makes the route TTL the network request interval.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace

from .base import QuotaBucket, QuotaSnapshot
from .claude import ClaudeQuotaProvider
from .claude_account import ClaudeAccountQuotaProvider


LOGGER = logging.getLogger("agentstack.dashboard.quotas")
ACCOUNT_WINDOW_RETENTION_SECONDS = 30 * 60


class ClaudeQuotaRoute:
    provider_name = "claude"
    source_name = "claude-combined"
    # This TTL is deliberately only the local observer cadence. The account
    # provider owns its independent 600 s + jitter outbound budget.
    ttl_seconds = 30
    # QuotaService's generic 600 s bound would discard retained account window
    # names. This route applies the stricter per-window deadline itself.
    manages_bucket_freshness = True
    # The route/account provider also owns credential-bound fallback. Reusing
    # QuotaService's older provider snapshot could cross a sign-out or account
    # change after the route deliberately discarded it.
    manages_fallback = True

    def __init__(
        self,
        account: ClaudeAccountQuotaProvider | None = None,
        statusline: ClaudeQuotaProvider | None = None,
        *,
        clock: Callable[[], float] = time.time,
        retention_seconds: int = ACCOUNT_WINDOW_RETENTION_SECONDS,
    ) -> None:
        self.account = account if account is not None else ClaudeAccountQuotaProvider()
        self.statusline = statusline if statusline is not None else ClaudeQuotaProvider()
        self._clock = clock
        self.retention_seconds = max(1, int(retention_seconds))

    def read(self) -> QuotaSnapshot:
        now = self._clock()
        account = self._read_account()
        observed = self.statusline.read()

        if account.reason in {"sign_in_required", "account_identity_changed"}:
            # The observer file is account-scoped too. Once identity becomes
            # uncertain, even locally observed values may belong to the prior
            # account and cannot be used as a fallback.
            return replace(account, degraded=True, partial=True)

        if account.reason == "no_windows_returned":
            # Authentication succeeded and the complete endpoint answered,
            # but it did not name a window. An older observer snapshot is not
            # evidence for this now-confirmed account.
            observed = replace(
                observed,
                buckets=tuple(
                    bucket
                    for bucket in observed.buckets
                    if (bucket.observed_at or observed.observed_at) > account.observed_at
                ),
            )

        if not account.buckets and not observed.buckets:
            # Neither answered: report the account's reason, so "signed out"
            # does not read as "no status line observed yet".
            return replace(
                account,
                degraded=account.status != "ok",
                partial=account.status != "ok",
            )

        account_windows = self._windows(account, now)
        observed_windows = self._windows(observed, now)
        if account.buckets:
            # A complete account observation establishes an identity/freshness
            # floor. Observer-only model windows at or before that instant can
            # belong to an earlier login and must not be appended.
            observed_windows = tuple(
                bucket
                for bucket in observed_windows
                if self._window_time(bucket) > account.observed_at
            )
        combined: dict[str, QuotaBucket] = {bucket.id: bucket for bucket in account_windows}
        for bucket in observed_windows:
            previous = combined.get(bucket.id)
            if previous is None or self._window_time(bucket) > self._window_time(previous):
                combined[bucket.id] = bucket

        buckets = tuple(combined.values())
        current = any(bucket.value_status == "current" for bucket in buckets)
        status = "ok" if current else "stale"
        account_failed = account.status == "unavailable" or (
            account.status == "stale" and account.reason != "account_refresh_scheduled"
        )
        partial = account_failed and not account.buckets
        reason = ""
        if account_failed:
            reason = f"fallback_{account.reason or 'account_unavailable'}"
        elif status == "stale":
            reason = account.reason or observed.reason or "previous_observation"

        sources = {bucket.source for bucket in buckets if bucket.source}
        source = next(iter(sources)) if len(sources) == 1 else self.source_name
        return QuotaSnapshot(
            provider=self.provider_name,
            source=source,
            observed_at=max((self._window_time(bucket) for bucket in buckets), default=int(now)),
            status=status,
            buckets=buckets,
            reason=reason,
            degraded=account_failed,
            partial=partial,
        )

    def _windows(self, snapshot: QuotaSnapshot, now: float) -> tuple[QuotaBucket, ...]:
        windows: list[QuotaBucket] = []
        for bucket in snapshot.buckets:
            observed_at = bucket.observed_at or snapshot.observed_at
            value_status = bucket.value_status
            if value_status != "unknown" and (
                snapshot.status in {"stale", "unavailable"}
                or observed_at < snapshot.observed_at
            ):
                value_status = "previous"
            expires_at = observed_at + self.retention_seconds
            if bucket.resets_at is not None:
                expires_at = min(expires_at, bucket.resets_at)
            if now >= expires_at:
                value_status = "unknown"
            windows.append(
                replace(
                    bucket,
                    observed_at=observed_at,
                    source=bucket.source or snapshot.source,
                    value_status=value_status,
                )
            )
        return tuple(windows)

    @staticmethod
    def _window_time(bucket: QuotaBucket) -> int:
        return bucket.observed_at or 0

    def _read_account(self) -> QuotaSnapshot:
        """Turn account transport failures into a routable snapshot.

        The account provider retains successful windows itself. An exception
        here therefore means it has never had a safe value for this credential;
        the observer still gets its independent chance to answer.
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
                degraded=True,
                partial=True,
            )
