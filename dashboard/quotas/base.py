"""Shared quota telemetry schema for provider account limits."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


QuotaQuality = Literal["exact", "derived", "estimated"]
QuotaStatus = Literal["ok", "degraded", "stale", "unavailable"]
QuotaValueStatus = Literal["current", "previous", "unknown"]


def normalize_percent(value: object) -> float:
    """Return a finite percentage clamped to the provider-neutral 0..100 range."""

    if isinstance(value, bool):
        raise ValueError("boolean is not a percentage")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid percentage: {value!r}") from exc
    if number != number or number in {float("inf"), float("-inf")}:
        raise ValueError(f"invalid percentage: {value!r}")
    return round(max(0.0, min(100.0, number)), 6)


@dataclass(frozen=True, slots=True)
class QuotaBucket:
    id: str
    label: str
    scope: str
    used_percent: float
    remaining_percent: float
    window_seconds: int | None
    resets_at: int | None
    quality: QuotaQuality = "exact"
    # When this window itself was last reported, if that is older than the
    # provider's observation (a per-model window a later session did not
    # report). None means it is as fresh as the snapshot.
    observed_at: int | None = None
    # A routed provider can combine windows reported by different readers.
    # Keep both facts on the window instead of pretending the newest source
    # observed every value in the combined snapshot.
    source: str | None = None
    value_status: QuotaValueStatus = "current"

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.id, self.label, self.scope)):
            raise ValueError("quota bucket id, label and scope must be non-empty")
        if self.quality not in {"exact", "derived", "estimated"}:
            raise ValueError(f"invalid quota quality: {self.quality}")
        used = normalize_percent(self.used_percent)
        remaining = normalize_percent(self.remaining_percent)
        if self.window_seconds is not None and (type(self.window_seconds) is not int or self.window_seconds <= 0):
            raise ValueError("window_seconds must be positive")
        if self.resets_at is not None and (type(self.resets_at) is not int or self.resets_at < 0):
            raise ValueError("resets_at must be non-negative")
        if self.observed_at is not None and (type(self.observed_at) is not int or self.observed_at < 0):
            raise ValueError("observed_at must be non-negative")
        if self.source is not None and (not isinstance(self.source, str) or not self.source.strip()):
            raise ValueError("quota bucket source must be non-empty when present")
        if self.value_status not in {"current", "previous", "unknown"}:
            raise ValueError(f"invalid quota value status: {self.value_status}")
        if abs(used + remaining - 100.0) > 0.00001:
            raise ValueError("used and remaining percentages must add up to 100")
        object.__setattr__(self, "used_percent", used)
        object.__setattr__(self, "remaining_percent", remaining)

    @classmethod
    def from_used(cls, *, id: str, label: str, scope: str, used_percent: object, window_seconds: int | None, resets_at: int | None, quality: QuotaQuality = "exact", observed_at: int | None = None, source: str | None = None, value_status: QuotaValueStatus = "current") -> "QuotaBucket":
        used = normalize_percent(used_percent)
        return cls(id=id, label=label, scope=scope, used_percent=used, remaining_percent=normalize_percent(100.0 - used), window_seconds=window_seconds, resets_at=resets_at, quality=quality, observed_at=observed_at, source=source, value_status=value_status)

    @classmethod
    def from_remaining(cls, *, id: str, label: str, scope: str, remaining_percent: object, window_seconds: int | None, resets_at: int | None, quality: QuotaQuality = "exact", observed_at: int | None = None, source: str | None = None, value_status: QuotaValueStatus = "current") -> "QuotaBucket":
        remaining = normalize_percent(remaining_percent)
        return cls(id=id, label=label, scope=scope, used_percent=normalize_percent(100.0 - remaining), remaining_percent=remaining, window_seconds=window_seconds, resets_at=resets_at, quality=quality, observed_at=observed_at, source=source, value_status=value_status)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "window_seconds": self.window_seconds,
            "resets_at": self.resets_at,
            "quality": self.quality,
            "observed_at": self.observed_at,
            "source": self.source,
            "value_status": self.value_status,
        }
        # Unknown means unknown. Keeping the old numbers out of the wire shape
        # makes it impossible for a consumer to accidentally draw them as a
        # current balance after their retention deadline or reset has passed.
        if self.value_status != "unknown":
            payload["used_percent"] = round(self.used_percent, 3)
            payload["remaining_percent"] = round(self.remaining_percent, 3)
        return payload


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    provider: str
    source: str
    observed_at: int
    status: QuotaStatus
    buckets: tuple[QuotaBucket, ...] = ()
    reason: str = ""
    # Freshness (`status`) and acquisition/completeness are independent axes.
    # For example, a rate-limited account reader may combine current observer
    # windows with previous account-only windows: status ok, degraded true,
    # partial false.
    degraded: bool = False
    partial: bool = False

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.provider, self.source)):
            raise ValueError("quota snapshot provider and source must be non-empty")
        if self.status not in {"ok", "degraded", "stale", "unavailable"}:
            raise ValueError(f"invalid quota status: {self.status}")
        if type(self.observed_at) is not int or self.observed_at < 0:
            raise ValueError("observed_at must be non-negative")
        if not isinstance(self.buckets, tuple) or not all(isinstance(bucket, QuotaBucket) for bucket in self.buckets):
            raise ValueError("buckets must be a tuple of quota buckets")
        if type(self.degraded) is not bool or type(self.partial) is not bool:
            raise ValueError("degraded and partial must be booleans")

    def with_status(self, status: QuotaStatus, reason: str = "") -> "QuotaSnapshot":
        return replace(self, status=status, reason=reason)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"provider": self.provider, "status": self.status, "degraded": self.degraded, "partial": self.partial, "source": self.source, "observed_at": self.observed_at, "buckets": [bucket.to_dict() for bucket in self.buckets]}
        if self.reason:
            payload["reason"] = self.reason
        return payload
