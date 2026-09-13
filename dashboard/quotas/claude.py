"""Claude quota adapter for status-line observations.

Claude exposes subscription rate limits to status-line commands after the first
API response in a session. ORRERY does not overwrite a user's statusLine
configuration; it reads a snapshot only when one has been observed.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .base import QuotaBucket, QuotaSnapshot


class ClaudeQuotaProvider:
    provider_name = "claude"
    source_name = "claude-statusline"
    ttl_seconds = 30

    def __init__(
        self,
        snapshot_path: str | os.PathLike[str] | None = None,
        *,
        max_age_seconds: int = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.snapshot_path = (
            Path(snapshot_path).expanduser()
            if snapshot_path is not None
            else _default_snapshot_path()
        )
        self.max_age_seconds = max(1, int(max_age_seconds))
        self._clock = clock

    def read(self) -> QuotaSnapshot:
        now = int(self._clock())
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            modified = int(self.snapshot_path.stat().st_mtime)
        except FileNotFoundError:
            return QuotaSnapshot(
                provider=self.provider_name,
                source=self.source_name,
                observed_at=now,
                status="unavailable",
                reason="not_observed",
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid Claude quota observation: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("Claude quota observation must be a JSON object")
        observed_at = _int_or_none(payload.get("observed_at")) or modified
        if observed_at > now:
            return QuotaSnapshot(
                provider=self.provider_name,
                source=self.source_name,
                observed_at=now,
                status="unavailable",
                reason="observation_in_future",
            )
        snapshot = parse_claude_statusline(payload, observed_at=observed_at)
        if snapshot.buckets and now - observed_at > self.max_age_seconds:
            return QuotaSnapshot(
                provider=self.provider_name,
                source=self.source_name,
                observed_at=observed_at,
                status="unavailable",
                reason="observation_stale",
            )
        return snapshot


def parse_claude_statusline(
    payload: Mapping[str, Any],
    *,
    observed_at: int,
) -> QuotaSnapshot:
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, Mapping):
        return QuotaSnapshot(
            provider="claude",
            source="claude-statusline",
            observed_at=observed_at,
            status="unavailable",
            reason="rate_limits_not_observed",
        )

    definitions = (
        ("five_hour", "5h", 5 * 60 * 60),
        ("seven_day", "7d", 7 * 24 * 60 * 60),
    )
    buckets: list[QuotaBucket] = []
    for key, label, window_seconds in definitions:
        value = rate_limits.get(key)
        if not isinstance(value, Mapping):
            continue
        used = value.get("used_percentage")
        if used is None:
            continue
        buckets.append(
            QuotaBucket.from_used(
                id=key,
                label=label,
                scope="account",
                used_percent=used,
                window_seconds=window_seconds,
                resets_at=_int_or_none(value.get("resets_at")),
                quality="exact",
            )
        )

    if not buckets:
        return QuotaSnapshot(
            provider="claude",
            source="claude-statusline",
            observed_at=observed_at,
            status="unavailable",
            reason="rate_limit_windows_empty",
        )
    return QuotaSnapshot(
        provider="claude",
        source="claude-statusline",
        observed_at=observed_at,
        status="ok",
        buckets=tuple(buckets),
    )


def _default_snapshot_path() -> Path:
    explicit = os.environ.get("AGENTSTACK_CLAUDE_QUOTA_SNAPSHOT", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime = os.environ.get("AGENTSTACK_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime).expanduser() / "claude-quota.json"
    return Path("~/.agentstack/runtime/claude-quota.json").expanduser()


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None
