"""Claude quota adapter reading the account's own usage endpoint.

The status-line observer only ever sees what Claude Code hands a status line.
That always includes the account windows and newer versions can include model
windows seen by that session, but it is not a complete account-wide list. This
adapter asks the same endpoint the CLI asks, with the credentials the CLI
already stored on this machine, so every window the account has is present.

What leaves the machine: one authenticated GET to Anthropic's usage endpoint,
no request body, no project or agent data. The token is read from the local
Claude credentials and is never logged or written to the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .base import QuotaBucket, QuotaSnapshot


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = "~/.claude/.credentials.json"
DISABLE_ENV = "AGENTSTACK_CLAUDE_ACCOUNT_QUOTA"
# The dashboard is intended to be this machine's one reader of the account
# endpoint. A route is checked much more often so local status-line updates are
# noticed promptly; this interval budgets only outbound account requests.
FETCH_INTERVAL_SECONDS = 600
JITTER_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
# Kept as the public name used by the first version and its callers.
BACKOFF_SECONDS = FETCH_INTERVAL_SECONDS


class _AuthRequired(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class ClaudeAccountQuotaProvider:
    provider_name = "claude"
    source_name = "claude-account-usage"
    # An outer service may inspect retained state on the local cadence; the
    # provider's own `_next_fetch_at` remains the authority for network I/O.
    ttl_seconds = 30

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        clock: Callable[[], float] = time.time,
        fetch: Callable[[str, float], Mapping[str, Any]] | None = None,
        token_reader: Callable[[], str | None] | None = None,
        jitter: Callable[[float], float] | None = None,
        fetch_interval_seconds: int = FETCH_INTERVAL_SECONDS,
    ) -> None:
        self.timeout = timeout
        self._clock = clock
        self._fetch = fetch or _fetch_usage
        self._token_reader = token_reader or read_access_token
        self._jitter = jitter or (lambda maximum: random.uniform(0.0, maximum))
        self.fetch_interval_seconds = max(1, int(fetch_interval_seconds))
        self._next_fetch_at = 0.0
        # A successful authenticated response is an identity/freshness floor
        # even when it contains no windows. Keep that bucketless response too.
        self._last_authenticated: QuotaSnapshot | None = None
        self._last_reason = ""
        self._rate_limit_failures = 0
        self._token_fingerprint: bytes | None = None
        self._identity_unconfirmed = False

    def read(self) -> QuotaSnapshot:
        """Read at most once per outbound budget while retaining each window."""
        now_float = self._clock()
        now = int(now_float)
        if os.environ.get(DISABLE_ENV, "").strip().lower() in {"0", "off", "false", "no"}:
            self._forget_account(identity_unconfirmed=False)
            return self._unavailable(now, "account_usage_disabled")
        token = self._token_reader()
        if not token:
            self._forget_account(identity_unconfirmed=True)
            return self._unavailable(now, "sign_in_required")
        fingerprint = hashlib.sha256(token.encode("utf-8")).digest()
        if self._token_fingerprint is not None and fingerprint != self._token_fingerprint:
            # A different credential may name a different account. Never carry
            # the previous account's balances across that boundary.
            self._forget_account(identity_unconfirmed=True)
        self._token_fingerprint = fingerprint
        if self._identity_unconfirmed:
            self._last_reason = "account_identity_changed"
        if now_float < self._next_fetch_at:
            return self._retained_or_unavailable(
                now,
                self._last_reason or "account_refresh_scheduled",
            )
        try:
            payload = self._fetch(token, self.timeout)
        except _AuthRequired:
            # The same rejected fingerprint is still "sign in required", not
            # evidence that a different account appeared. Retain only its
            # digest so the budget-cached answer keeps the same reason.
            self._forget_account(
                identity_unconfirmed=False,
                preserve_fingerprint=True,
            )
            self._last_reason = "sign_in_required"
            self._schedule(self.fetch_interval_seconds)
            return self._unavailable(now, "sign_in_required")
        except _RateLimited as exc:
            exponential = min(
                self.fetch_interval_seconds * (2 ** self._rate_limit_failures),
                BACKOFF_MAX_SECONDS,
            )
            self._rate_limit_failures += 1
            self._last_reason = (
                "account_identity_changed" if self._identity_unconfirmed else "rate_limited"
            )
            self._schedule(max(exc.retry_after, exponential))
            return self._retained_or_unavailable(now, self._last_reason)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            self._last_reason = (
                "account_identity_changed"
                if self._identity_unconfirmed
                else "account_usage_failed"
            )
            self._schedule(self.fetch_interval_seconds)
            if self._last_authenticated is not None:
                return self._retained_or_unavailable(now, self._last_reason)
            if self._identity_unconfirmed:
                # A failed first request for a changed credential cannot prove
                # which account the local observer belongs to. Preserve that
                # identity signal so the route clears its old registry.
                return self._unavailable(now, self._last_reason)
            # The token must not reach a log line through an exception message.
            raise RuntimeError(f"claude usage probe failed: {type(exc).__name__}") from None
        snapshot = parse_account_usage(payload, observed_at=now)
        self._identity_unconfirmed = False
        self._rate_limit_failures = 0
        self._last_reason = "account_refresh_scheduled"
        self._schedule(self.fetch_interval_seconds)
        self._last_authenticated = snapshot
        return snapshot

    def _schedule(self, seconds: int | float) -> None:
        jitter = max(0.0, float(self._jitter(JITTER_SECONDS)))
        self._next_fetch_at = self._clock() + max(0.0, float(seconds)) + jitter

    def _retained_or_unavailable(self, observed_at: int, reason: str) -> QuotaSnapshot:
        if self._last_authenticated is not None:
            if self._last_authenticated.buckets:
                return self._last_authenticated.with_status("stale", reason)
            # Preserve the authenticated empty response's original observation
            # time and reason as the account freshness floor.
            if reason == "account_refresh_scheduled":
                return self._last_authenticated
            return replace(self._last_authenticated, reason=reason)
        return self._unavailable(observed_at, reason)

    def _forget_account(
        self,
        *,
        identity_unconfirmed: bool,
        preserve_fingerprint: bool = False,
    ) -> None:
        self._last_authenticated = None
        self._last_reason = ""
        # Identity changes invalidate values, not the machine-wide outbound
        # budget or its 429 streak. Only an authenticated success resets the
        # exponent, and token rotation must not buy an earlier request.
        if not preserve_fingerprint:
            self._token_fingerprint = None
        self._identity_unconfirmed = identity_unconfirmed

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
    repeated: set[tuple] = set()
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
        # distinct window even when the names collide. A window the payload
        # simply repeats is the one case to drop: it would draw twice.
        identity = model.get("id") if isinstance(model, Mapping) else None
        key = identity.strip() if isinstance(identity, str) and identity.strip() else name.strip()
        resets_at = _epoch_or_none(entry.get("resets_at"))
        fingerprint = (key, name.strip(), used, resets_at)
        if fingerprint in repeated:
            continue
        repeated.add(fingerprint)
        bucket_id = _model_bucket_id(key)
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
                resets_at=resets_at,
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
            raise _RateLimited(_retry_after_seconds(exc.headers.get("Retry-After"))) from None
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


def _model_bucket_id(identity: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in identity.lower()).strip("-")
    return slug if slug.startswith("model-") else f"model-{slug or 'model'}"


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
