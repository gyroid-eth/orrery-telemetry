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
from datetime import datetime, timezone
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

    # Per-model weekly windows (Claude Code 2.1.268+, `rate_limits.model_scoped`).
    # The status line hands these through unscaled: `utilization` is a 0..1
    # fraction and `resets_at` an ISO-8601 string, unlike the top-level windows.
    model_scoped = rate_limits.get("model_scoped")
    if isinstance(model_scoped, list):
        for entry in model_scoped:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("display_name")
            utilization = entry.get("utilization")
            if not isinstance(name, str) or not name.strip():
                continue
            if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
                continue
            stable_id = entry.get("model_id") or entry.get("id")
            identity = stable_id.strip() if isinstance(stable_id, str) and stable_id.strip() else name
            slug = "".join(ch if ch.isalnum() else "-" for ch in identity.strip().lower()).strip("-")
            bucket_id = slug if slug.startswith("model-") else f"model-{slug or 'model'}"
            # Claude Code reports only the running model's window, so the
            # observer carries the others until they reset. Such a window keeps
            # the time it was last seen; a window seen just now keeps None.
            seen = _int_or_none(entry.get("observed_at"))
            buckets.append(
                QuotaBucket.from_used(
                    id=bucket_id,
                    label=name.strip(),
                    scope="account",
                    used_percent=float(utilization) * 100.0,
                    window_seconds=7 * 24 * 60 * 60,
                    resets_at=_epoch_or_none(entry.get("resets_at")),
                    quality="exact",
                    observed_at=seen if seen is not None and seen < observed_at else None,
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


def _epoch_or_none(value: object) -> int | None:
    """Accept an epoch integer or an ISO-8601 string; anything else is unknown."""
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        epoch = int(parsed.timestamp())
        return epoch if epoch >= 0 else None
    return None
