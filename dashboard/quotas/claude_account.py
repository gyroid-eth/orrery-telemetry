"""Claude quota adapter reading the account's own usage endpoint.

The status-line observer only ever sees what Claude Code hands a status line,
and that payload carries the account windows without the per-model weekly ones
(measured: a Fable session reports no model window either). This adapter asks
the same endpoint the CLI asks, with the credentials the CLI already stored on
this machine, so every window the account has is present without asking the
operator to configure a status line.

What leaves the machine: one authenticated GET to Anthropic's usage endpoint,
no request body, no project or agent data. The token is read from the local
Claude credentials and is never logged or written to the snapshot.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .base import QuotaBucket, QuotaSnapshot


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = "~/.claude/.credentials.json"
DISABLE_ENV = "AGENTSTACK_CLAUDE_ACCOUNT_QUOTA"
# The endpoint rate-limits polite callers too. After a 429 stay quiet at least
# this long, whatever Retry-After said, so a dashboard left open cannot turn
# into a poller the account is throttled for.
BACKOFF_SECONDS = 300


class _AuthRequired(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class ClaudeAccountQuotaProvider:
    provider_name = "claude"
    source_name = "claude-account-usage"
    ttl_seconds = 120

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        clock: Callable[[], float] = time.time,
        fetch: Callable[[str, float], Mapping[str, Any]] | None = None,
        token_reader: Callable[[], str | None] | None = None,
    ) -> None:
        self.timeout = timeout
        self._clock = clock
        self._fetch = fetch or _fetch_usage
        self._token_reader = token_reader or read_access_token
        self._quiet_until = 0.0

    def read(self) -> QuotaSnapshot:
        """Answer without touching the network when it already cannot help."""
        now = int(self._clock())
        if os.environ.get(DISABLE_ENV, "").strip().lower() in {"0", "off", "false", "no"}:
            return self._unavailable(now, "account_usage_disabled")
        if self._clock() < self._quiet_until:
            return self._unavailable(now, "rate_limited")
        token = self._token_reader()
        if not token:
            return self._unavailable(now, "sign_in_required")
        try:
            payload = self._fetch(token, self.timeout)
        except _AuthRequired:
            return self._unavailable(now, "sign_in_required")
        except _RateLimited as exc:
            self._quiet_until = self._clock() + exc.retry_after
            return self._unavailable(now, "rate_limited")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            # The token must not reach a log line through an exception message.
            raise RuntimeError(f"claude usage probe failed: {type(exc).__name__}") from None
        return parse_account_usage(payload, observed_at=now)

    def _unavailable(self, observed_at: int, reason: str) -> QuotaSnapshot:
        return QuotaSnapshot(
            provider=self.provider_name,
            source=self.source_name,
            observed_at=observed_at,
            status="unavailable",
            reason=reason,
        )


def parse_account_usage(payload: Mapping[str, Any], *, observed_at: int) -> QuotaSnapshot:
    """Turn the usage body into buckets, account windows first.

    `five_hour` and `seven_day` are the account windows and carry `utilization`
    as a percentage. The per-model weekly windows (Fable, Opus, Sonnet …) live
    in `limits[]` as `weekly_scoped` entries, each naming its model.
    """
    buckets: list[QuotaBucket] = []
    for key, label, window_seconds in (
        ("five_hour", "5h", 5 * 60 * 60),
        ("seven_day", "7d", 7 * 24 * 60 * 60),
    ):
        entry = payload.get(key)
        if not isinstance(entry, Mapping):
            continue
        used = _percent_or_none(entry.get("utilization"))
        if used is None:
            continue
        buckets.append(
            QuotaBucket.from_used(
                id=key,
                label=label,
                scope="account",
                used_percent=used,
                window_seconds=window_seconds,
                resets_at=_epoch_or_none(entry.get("resets_at")),
                quality="exact",
            )
        )

    seen = {bucket.id for bucket in buckets}
    limits = payload.get("limits")
    for entry in limits if isinstance(limits, list) else []:
        if not isinstance(entry, Mapping) or entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, Mapping) else None
        name = model.get("display_name") if isinstance(model, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            continue
        used = _percent_or_none(entry.get("percent"))
        if used is None:
            continue
        # Two different models can slug to the same id ("A/B" and "A-B"), and
        # dropping one would silently hide a window — possibly the tighter of
        # the two. Prefer the id the API gives the model, and keep every
        # distinct window even when the names collide.
        identity = model.get("id") if isinstance(model, Mapping) else None
        key = identity.strip() if isinstance(identity, str) and identity.strip() else name.strip()
        slug = "".join(ch if ch.isalnum() else "-" for ch in key.lower()).strip("-")
        bucket_id = f"model-{slug or 'model'}"
        if bucket_id in seen:
            suffix = 2
            while f"{bucket_id}-{suffix}" in seen:
                suffix += 1
            bucket_id = f"{bucket_id}-{suffix}"
        seen.add(bucket_id)
        buckets.append(
            QuotaBucket.from_used(
                id=bucket_id,
                label=name.strip(),
                scope="account",
                used_percent=used,
                window_seconds=7 * 24 * 60 * 60,
                resets_at=_epoch_or_none(entry.get("resets_at")),
                quality="exact",
            )
        )

    if not buckets:
        return QuotaSnapshot(
            provider="claude",
            source="claude-account-usage",
            observed_at=observed_at,
            status="unavailable",
            reason="no_windows_returned",
        )
    return QuotaSnapshot(
        provider="claude",
        source="claude-account-usage",
        observed_at=observed_at,
        status="ok",
        buckets=tuple(buckets),
    )


def read_access_token() -> str | None:
    """The token Claude Code already stored for this machine, or None."""
    env = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    return env or _token_from_file() or _token_from_keychain()


def _token_from_file(path: str = CREDENTIALS_PATH) -> str | None:
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _token_from_credentials(data)


def _token_from_keychain() -> str | None:
    security = "/usr/bin/security"
    if not os.path.exists(security):
        return None
    try:
        done = subprocess.run(
            [security, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    return _token_from_credentials(data)


def _token_from_credentials(data: object) -> str | None:
    oauth = data.get("claudeAiOauth") if isinstance(data, Mapping) else None
    token = oauth.get("accessToken") if isinstance(oauth, Mapping) else None
    return token.strip() if isinstance(token, str) and token.strip() else None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect while carrying the account token.

    urllib copies request headers, Authorization included, onto the redirect
    target. The usage endpoint is a fixed HTTPS address; if it ever answered
    with a redirect, following it would hand the token to whatever host the
    response named.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _retry_after_seconds(value: object) -> int:
    """`Retry-After` is either delta-seconds or an HTTP-date."""
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip()
    try:
        return max(0, int(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return 0
    if when is None:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int(when.timestamp() - time.time()))


def _fetch_usage(token: str, timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "orrery-telemetry",
        },
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise _AuthRequired() from None
        if exc.code == 429:
            raise _RateLimited(max(_retry_after_seconds(exc.headers.get("Retry-After")),
                                   BACKOFF_SECONDS)) from None
        raise RuntimeError(f"http_{exc.code}") from None
    data = json.loads(body)
    if not isinstance(data, Mapping):
        raise RuntimeError("non_object_usage_body")
    return data


def _percent_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _epoch_or_none(value: object) -> int | None:
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
