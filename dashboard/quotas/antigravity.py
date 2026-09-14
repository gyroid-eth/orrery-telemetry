"""Antigravity quota adapter using the read-only headless /usage command."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import QuotaBucket, QuotaSnapshot


_MIN_SAFE_VERSION = (1, 1, 24)
_OPT_IN_ENV = "AGENTSTACK_ANTIGRAVITY_QUOTA_ENABLED"
_OPT_IN_FILE_ENV = "AGENTSTACK_ANTIGRAVITY_QUOTA_OPT_IN_FILE"
_OPT_IN_FILENAME = "antigravity-quota.enabled"


class AntigravityQuotaProvider:
    provider_name = "antigravity"
    source_name = "agy-print-usage"
    ttl_seconds = 180

    def __init__(self, command: str | None = None, *, timeout: float = 15.0, enabled: bool | None = None, opt_in_file: str | os.PathLike[str] | None = None, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        configured = command or os.environ.get("AGENTSTACK_GEMINI_BIN", "").strip() or os.environ.get("AGENTSTACK_ANTIGRAVITY_BIN", "").strip()
        self.command = _resolve_command(configured or "agy")
        self.timeout = timeout
        self._enabled_override = enabled
        self.opt_in_file = Path(opt_in_file).expanduser() if opt_in_file is not None else _default_opt_in_file()
        self._runner = runner

    def read(self) -> QuotaSnapshot:
        observed_at = int(time.time())
        if not self._is_enabled():
            return QuotaSnapshot(provider=self.provider_name, source=self.source_name, observed_at=observed_at, status="unavailable", reason="telemetry_opt_in_required")
        version = _read_version(self.command, runner=self._runner)
        if version is None or version < _MIN_SAFE_VERSION:
            return QuotaSnapshot(provider=self.provider_name, source=self.source_name, observed_at=observed_at, status="unavailable", reason="agy_too_old_for_safe_print_usage")
        process = self._runner([self.command, "-p", "/usage", "--output-format", "json"], capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=self.timeout)
        if process.returncode != 0:
            raise RuntimeError(f"agy /usage failed with exit {process.returncode}")
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("agy /usage returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("agy /usage returned a non-object payload")
        return parse_antigravity_usage(payload, observed_at=observed_at)

    def _is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        if _env_bool(_OPT_IN_ENV, False):
            return True
        try:
            return self.opt_in_file.is_file()
        except OSError:
            return False


def parse_antigravity_usage(payload: Mapping[str, Any], *, observed_at: int) -> QuotaSnapshot:
    groups = _find_groups(payload)
    if groups is None:
        return QuotaSnapshot(provider="antigravity", source="agy-print-usage", observed_at=observed_at, status="unavailable", reason="usage_groups_missing")
    buckets: list[QuotaBucket] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        group_name = str(group.get("displayName") or group.get("display_name") or group.get("name") or "Antigravity")
        raw_buckets = group.get("buckets")
        if not isinstance(raw_buckets, Sequence) or isinstance(raw_buckets, (str, bytes)):
            continue
        for raw in raw_buckets:
            if not isinstance(raw, Mapping):
                continue
            fraction = _remaining_fraction(raw)
            if fraction is None or not 0.0 <= fraction <= 1.0 or raw.get("disabled") is True:
                continue
            remaining = fraction * 100.0
            bucket_id = str(raw.get("bucketId") or raw.get("bucket_id") or raw.get("id") or raw.get("window") or "quota")
            display_name = str(raw.get("displayName") or raw.get("display_name") or raw.get("label") or raw.get("window") or bucket_id)
            window_seconds = _window_seconds(str(raw.get("window") or ""))
            if window_seconds is None:
                window_seconds = _window_seconds(f"{bucket_id} {display_name}")
            window_label = _window_label(display_name, window_seconds)
            label = f"{group_name} · {window_label}" if group_name else window_label
            resets_at = _parse_reset(raw.get("resetTime") or raw.get("reset_time"))
            buckets.append(QuotaBucket.from_remaining(id=bucket_id, label=label, scope="account", remaining_percent=remaining, window_seconds=window_seconds, resets_at=resets_at, quality="exact"))
    if not buckets:
        return QuotaSnapshot(provider="antigravity", source="agy-print-usage", observed_at=observed_at, status="unavailable", reason="no_active_quota_buckets")
    return QuotaSnapshot(provider="antigravity", source="agy-print-usage", observed_at=observed_at, status="ok", buckets=tuple(buckets))


def _find_groups(payload: Mapping[str, Any]) -> Sequence[Any] | None:
    candidates: list[object] = [payload]
    for key in ("data", "response", "command", "result"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
            nested = value.get("data")
            if isinstance(nested, Mapping):
                candidates.append(nested)
            nested_response = value.get("response")
            if isinstance(nested_response, Mapping):
                candidates.append(nested_response)
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        groups = candidate.get("groups")
        if isinstance(groups, Sequence) and not isinstance(groups, (str, bytes)):
            return groups
    return None


def _remaining_fraction(bucket: Mapping[str, Any]) -> float | None:
    direct = bucket.get("remaining_fraction")
    if direct is None:
        direct = bucket.get("remainingFraction")
    if direct is not None:
        return _number(direct)
    remaining = bucket.get("remaining")
    if isinstance(remaining, Mapping):
        for key in ("remainingFraction", "remaining_fraction", "value"):
            if key in remaining:
                return _number(remaining.get(key))
    return None


def _resolve_command(configured: str) -> str:
    path = Path(configured).expanduser()
    if path.is_absolute() or "/" in configured:
        return str(path)
    resolved = shutil.which(configured)
    if resolved:
        return resolved
    if configured == "agy":
        for candidate in (Path("~/.local/bin/agy").expanduser(), Path("/opt/homebrew/bin/agy"), Path("/usr/local/bin/agy")):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return configured


def _default_opt_in_file() -> Path:
    explicit = os.environ.get(_OPT_IN_FILE_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime = os.environ.get("AGENTSTACK_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime).expanduser() / _OPT_IN_FILENAME
    return Path("~/.agentstack/runtime").expanduser() / _OPT_IN_FILENAME


def _read_version(command: str, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> tuple[int, int, int] | None:
    try:
        process = runner([command, "--version"], capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None
    match = re.fullmatch(r"(?:Antigravity CLI\s+)?v?(\d+)\.(\d+)\.(\d+)", process.stdout.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _parse_reset(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    number = _number(value)
    if number is not None:
        return int(number) if number >= 0 else None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        timestamp = int(parsed.timestamp())
        return timestamp if timestamp >= 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _window_seconds(value: str) -> int | None:
    text = value.strip().lower()
    if re.search(r"(?:^|[^a-z])week(?:ly)?(?:[^a-z]|$)", text):
        return 7 * 24 * 60 * 60
    match = re.search(r"(?:^|[^a-z0-9.])(\d+(?:\.\d+)?)\s*([mhdw])(?:$|[^a-z])", text)
    if not match:
        return None
    amount = float(match.group(1))
    scale = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    seconds = int(amount * scale)
    return seconds if seconds > 0 else None


def _window_label(raw: str, seconds: int | None) -> str:
    if seconds == 5 * 60 * 60:
        return "5h"
    if seconds == 7 * 24 * 60 * 60:
        return "7d"
    return raw.strip() or "quota"
