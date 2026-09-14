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
_ACCOUNT_SOURCE = "claude-account-usage"
_OBSERVER_SOURCE = "claude-statusline"


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
        self._window_registry: dict[str, QuotaBucket] = {}
        self._account_ids_by_label: dict[str, set[str]] = {}
        self._account_floor: int | None = None
        self._has_account_catalog = False

    def read(self) -> QuotaSnapshot:
        now = self._clock()
        account = self._read_account()

        # Check identity before touching the observer. Its file is account-
        # scoped too, and may still contain the prior account's values.
        if account.reason in {"sign_in_required", "account_identity_changed"}:
            self._clear_identity()
            return replace(account, degraded=True, partial=True)

        observed = self._read_statusline()

        if account.reason == "account_usage_disabled":
            # With the account source off, nothing reads the credentials, so a
            # sign-out or an account switch cannot be noticed — and the
            # observer's file carries no account identity of its own. A window
            # held across reads would then outlive the account it belongs to,
            # so each read answers with what it just observed.
            self._clear_identity()
        new_complete_account = account.status == "ok" or (
            account.reason == "no_windows_returned"
            and account.observed_at != self._account_floor
        )
        if new_complete_account:
            # The complete endpoint is the catalog authority. A new answer,
            # including an authenticated empty answer, replaces all older
            # window names and establishes the observer freshness floor.
            self._window_registry.clear()
            self._account_ids_by_label.clear()
            self._account_floor = account.observed_at
            self._has_account_catalog = True
        elif account.buckets:
            # A retained account snapshot could be the first read of a route
            # assembled around an already-running provider.
            self._has_account_catalog = True
            if self._account_floor is None:
                self._account_floor = account.observed_at

        self._age_registry(now)
        account_windows = self._windows(account, now)
        observed_windows = self._windows(observed, now)
        self._index_account_windows(account_windows)
        if self._account_floor is not None:
            observed_windows = tuple(
                bucket
                for bucket in observed_windows
                if self._window_time(bucket) > self._account_floor
            )

        for bucket in account_windows:
            self._remember(bucket)
        for bucket in observed_windows:
            self._remember(bucket)
        self._expire_registry(now)
        buckets = tuple(self._window_registry.values())

        account_failed = account.status == "unavailable" or (
            account.status == "stale" and account.reason != "account_refresh_scheduled"
        )
        observer_failed = observed.reason == "statusline_observation_failed"
        observer_degrades = observer_failed and account.status != "ok"
        degraded = account_failed or observer_degrades
        partial = account_failed and not self._has_account_catalog

        if not buckets:
            chosen = account if account.status != "ok" else observed
            return replace(chosen, degraded=degraded, partial=partial)

        current = any(bucket.value_status == "current" for bucket in buckets)
        status = "ok" if current else "stale"
        reason = ""
        if account_failed:
            reason = f"fallback_{account.reason or 'account_unavailable'}"
        elif observer_degrades:
            reason = "fallback_statusline_observation_failed"
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
            degraded=degraded,
            partial=partial,
        )

    def _age_registry(self, now: float) -> None:
        self._window_registry = {
            bucket_id: self._aged(bucket, now, previous=True)
            for bucket_id, bucket in self._window_registry.items()
        }

    def _expire_registry(self, now: float) -> None:
        self._window_registry = {
            bucket_id: self._aged(bucket, now, previous=False)
            for bucket_id, bucket in self._window_registry.items()
        }

    def _aged(self, bucket: QuotaBucket, now: float, *, previous: bool) -> QuotaBucket:
        value_status = bucket.value_status
        if previous and value_status == "current":
            value_status = "previous"
        observed_at = self._window_time(bucket)
        expires_at = observed_at + self.retention_seconds
        if bucket.resets_at is not None:
            expires_at = min(expires_at, bucket.resets_at)
        if now >= expires_at:
            value_status = "unknown"
        return replace(bucket, value_status=value_status)

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
            routed = replace(
                bucket,
                observed_at=observed_at,
                source=bucket.source or snapshot.source,
                value_status=value_status,
            )
            windows.append(self._aged(routed, now, previous=False))
        return tuple(windows)

    def _remember(self, bucket: QuotaBucket) -> None:
        existing = self._window_registry.get(bucket.id)
        existing_key = bucket.id
        if existing is None:
            account_ids = self._account_ids_by_label.get(
                self._normalized_label(bucket.label), set()
            )
            # Display-name fallback is safe only if the account catalog has a
            # single model with that exact normalized name. Keep the canonical
            # account id even when a newer observer value currently supplies
            # the bucket's source and number.
            if bucket.source == _OBSERVER_SOURCE and len(account_ids) == 1:
                existing_key = next(iter(account_ids))
                existing = self._window_registry.get(existing_key)
            elif bucket.source == _ACCOUNT_SOURCE and account_ids == {bucket.id}:
                matches = [
                    (candidate_id, candidate)
                    for candidate_id, candidate in self._window_registry.items()
                    if candidate.source == _OBSERVER_SOURCE
                    and self._normalized_label(candidate.label)
                    == self._normalized_label(bucket.label)
                ]
                if len(matches) == 1:
                    existing_key, existing = matches[0]
        if existing is None:
            self._window_registry[bucket.id] = bucket
            return

        canonical_id = self._account_id(existing, bucket)
        winner = bucket if self._newer(bucket, existing) else existing
        winner = replace(winner, id=canonical_id)
        if existing_key != canonical_id:
            del self._window_registry[existing_key]
        self._window_registry[canonical_id] = winner

    def _index_account_windows(self, buckets: tuple[QuotaBucket, ...]) -> None:
        for bucket in buckets:
            if bucket.source != _ACCOUNT_SOURCE or not bucket.id.startswith("model-"):
                continue
            label = self._normalized_label(bucket.label)
            self._account_ids_by_label.setdefault(label, set()).add(bucket.id)

    @staticmethod
    def _account_id(left: QuotaBucket, right: QuotaBucket) -> str:
        if left.source == _ACCOUNT_SOURCE:
            return left.id
        if right.source == _ACCOUNT_SOURCE:
            return right.id
        return left.id

    @classmethod
    def _newer(cls, left: QuotaBucket, right: QuotaBucket) -> bool:
        left_key = (cls._window_time(left), cls._status_rank(left.value_status))
        right_key = (cls._window_time(right), cls._status_rank(right.value_status))
        return left_key > right_key

    @staticmethod
    def _status_rank(status: str) -> int:
        return {"unknown": 0, "previous": 1, "current": 2}.get(status, 0)

    @staticmethod
    def _normalized_label(label: str) -> str:
        return " ".join(label.split()).casefold()

    @staticmethod
    def _window_time(bucket: QuotaBucket) -> int:
        return bucket.observed_at or 0

    def _clear_identity(self) -> None:
        self._window_registry.clear()
        self._account_ids_by_label.clear()
        self._account_floor = None
        self._has_account_catalog = False

    def _read_account(self) -> QuotaSnapshot:
        try:
            return self.account.read()
        except Exception as exc:  # noqa: BLE001 - observer still gets its chance
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

    def _read_statusline(self) -> QuotaSnapshot:
        try:
            return self.statusline.read()
        except Exception as exc:  # noqa: BLE001 - account answer remains usable
            LOGGER.info("claude status-line observation unavailable (%s)", type(exc).__name__)
            return QuotaSnapshot(
                provider=self.provider_name,
                source=self.statusline.source_name,
                observed_at=int(self._clock()),
                status="unavailable",
                reason="statusline_observation_failed",
            )
