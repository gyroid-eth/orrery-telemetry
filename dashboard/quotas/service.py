"""Quota provider orchestration with TTL caching and failure isolation."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Protocol

from .base import QuotaSnapshot


LOGGER = logging.getLogger("agentstack.dashboard.quotas")


class QuotaProvider(Protocol):
    provider_name: str
    source_name: str
    ttl_seconds: int

    def read(self) -> QuotaSnapshot: ...


@dataclass(slots=True)
class _CacheEntry:
    fetched_at: float
    snapshot: QuotaSnapshot


class QuotaService:
    """Collect provider quota without coupling it to the hot /api/agents path."""

    def __init__(
        self,
        providers: list[QuotaProvider],
        *,
        default_ttl_seconds: int = 60,
        stale_seconds: int = 600,
        clock=time.time,
    ) -> None:
        self.providers = list(providers)
        names = [provider.provider_name for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("quota provider names must be unique")
        self.default_ttl_seconds = max(1, int(default_ttl_seconds))
        self.stale_seconds = max(1, int(stale_seconds))
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._last_success: dict[str, QuotaSnapshot] = {}
        self._provider_locks = {name: threading.Lock() for name in names}

    def read_all(self) -> dict[str, object]:
        if len(self.providers) <= 1:
            snapshots = [self._read_provider(provider) for provider in self.providers]
        else:
            with ThreadPoolExecutor(
                max_workers=len(self.providers),
                thread_name_prefix="quota-refresh",
            ) as pool:
                futures = [pool.submit(self._read_provider, provider) for provider in self.providers]
                snapshots = [future.result() for future in futures]
        now = self._clock()
        snapshots = [
            self._bounded_snapshot(provider, snapshot, now)
            for provider, snapshot in zip(self.providers, snapshots)
        ]
        return {
            "ts": int(now),
            "degraded": any(
                snapshot.status in {"degraded", "unavailable"} or snapshot.degraded
                for snapshot in snapshots
            ),
            "providers": [self._snapshot_payload(snapshot, now) for snapshot in snapshots],
        }

    def _snapshot_payload(self, snapshot: QuotaSnapshot, now: float) -> dict[str, object]:
        payload = snapshot.to_dict()
        previous = self._last_success.get(snapshot.provider)
        has_observation = bool(snapshot.buckets) or snapshot.reason in {
            "observation_stale", "observation_expired",
        }
        observed = snapshot.observed_at if has_observation else (
            previous.observed_at if previous is not None else None
        )
        payload["last_observed_at"] = observed if observed is not None and observed <= now else None
        cached = self._cache.get(snapshot.provider)
        payload["checked_at"] = int(cached.fetched_at) if cached is not None else None
        return payload

    def _read_provider(self, provider: QuotaProvider) -> QuotaSnapshot:
        name = provider.provider_name
        lock = self._provider_locks[name]
        # A route that owns credential-bound fallback must see the refresh that
        # is already in flight. Returning its generic prior success here could
        # resurrect a value while that refresh discovers a sign-out.
        managed_fallback = bool(getattr(provider, "manages_fallback", False))
        if not lock.acquire(blocking=managed_fallback):
            return self._stale_or_unavailable(provider, self._clock(), "refresh_in_progress")
        try:
            now = self._clock()
            ttl = max(1, int(getattr(provider, "ttl_seconds", self.default_ttl_seconds)))
            cached = self._cache.get(name)
            if cached is not None and 0 <= now - cached.fetched_at < ttl:
                return self._bounded_snapshot(provider, cached.snapshot, now)

            try:
                snapshot = provider.read()
                if snapshot.provider != name:
                    raise ValueError(
                        f"provider returned snapshot for {snapshot.provider!r}, expected {name!r}"
                    )
                snapshot = self._bounded_snapshot(provider, snapshot, self._clock())
            except Exception as exc:
                LOGGER.warning(
                    "quota provider %s failed (%s)",
                    name,
                    type(exc).__name__,
                )
                snapshot = self._failure_snapshot(provider, now)
            else:
                if snapshot.status in {"ok", "degraded"} and snapshot.buckets:
                    self._last_success[name] = snapshot
                elif snapshot.status == "unavailable" and not getattr(
                    provider, "manages_fallback", False
                ):
                    snapshot = self._stale_or_unavailable(
                        provider,
                        now,
                        snapshot.reason or "provider_unavailable",
                        fallback=snapshot,
                    )
                elif snapshot.status == "unavailable":
                    self._last_success.pop(name, None)

            now = self._clock()
            snapshot = self._bounded_snapshot(provider, snapshot, now)
            self._cache[name] = _CacheEntry(fetched_at=now, snapshot=snapshot)
            return snapshot
        finally:
            lock.release()

    def _bounded_snapshot(
        self, provider: QuotaProvider, snapshot: QuotaSnapshot, now: float
    ) -> QuotaSnapshot:
        # Composite routes can retain each window independently, including an
        # expired name with no wire-visible value. Applying the generic 600 s
        # snapshot bound here would erase exactly that per-window state.
        if getattr(provider, "manages_bucket_freshness", False):
            return snapshot
        max_age = min(self.stale_seconds, getattr(provider, "max_age_seconds", self.stale_seconds))
        age = now - snapshot.observed_at
        if snapshot.buckets and (age < 0 or age > max_age):
            return QuotaSnapshot(
                provider=provider.provider_name,
                source=provider.source_name,
                observed_at=snapshot.observed_at,
                status="unavailable",
                reason="observation_expired" if age >= 0 else "observation_in_future",
            )
        if snapshot.buckets and any(
            bucket.resets_at is not None and bucket.resets_at <= now
            for bucket in snapshot.buckets
        ):
            return snapshot.with_status("stale", "window_reset_pending")
        return snapshot

    def _failure_snapshot(
        self,
        provider: QuotaProvider,
        now: float,
    ) -> QuotaSnapshot:
        if getattr(provider, "manages_fallback", False):
            # The provider did not return a credential-checked snapshot. Its
            # own previous value is deliberately unavailable to this layer.
            self._last_success.pop(provider.provider_name, None)
            return QuotaSnapshot(
                provider=provider.provider_name,
                source=provider.source_name,
                observed_at=int(now),
                status="unavailable",
                reason="provider_read_failed",
                degraded=True,
                partial=True,
            )
        return self._stale_or_unavailable(provider, now, "provider_read_failed")

    def _stale_or_unavailable(
        self,
        provider: QuotaProvider,
        now: float,
        reason: str,
        *,
        fallback: QuotaSnapshot | None = None,
    ) -> QuotaSnapshot:
        previous = self._last_success.get(provider.provider_name)
        if fallback is not None and fallback.reason in {"observation_stale", "observation_expired"}:
            if previous is None or fallback.observed_at > previous.observed_at:
                return fallback
        if previous is not None:
            retained = replace(
                previous.with_status("stale", reason),
                degraded=True,
            )
            return self._bounded_snapshot(provider, retained, now)
        return QuotaSnapshot(
            provider=provider.provider_name,
            source=provider.source_name,
            observed_at=int(now),
            status="unavailable",
            reason=reason,
        )
