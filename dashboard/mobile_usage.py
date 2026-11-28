"""Project dashboard quotas onto the phone's public, numeric-only usage view."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import re


PROVIDERS = ('claude', 'codex', 'antigravity', 'dsh')
FRESH_SECONDS = 300
_MAX_EPOCH = 253402300799  # Last second of 9999, within JavaScript Date range.
_USD = re.compile(r'-?[0-9]{1,12}(?:\.[0-9]{1,12})?')


def _epoch(value):
    return value if type(value) is int and 0 < value <= _MAX_EPOCH else None


def _reading(remaining, observed, reset, *, stale, now):
    return {
        'remaining_percent': remaining,
        'observed_at': observed,
        'resets_at': reset,
        'status': (
            'unavailable' if remaining is None or observed is None or observed > now
            else 'stale' if stale or now - observed >= FRESH_SECONDS or reset is not None and reset <= now
            else 'available'
        ),
    }


def _windows(provider, rows, observed, stale, now):
    if not isinstance(rows, list):
        return []
    by_duration = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        duration = row.get('window_seconds')
        duration = duration if type(duration) is int and 0 < duration <= 366 * 86400 else None
        remaining = row.get('remaining_percent')
        if (row.get('value_status') == 'unknown' or type(remaining) not in (int, float)
                or not 0 <= remaining <= 100):
            remaining = None
        observed_at = observed if row.get('observed_at') is None else _epoch(row['observed_at'])
        reading = _reading(remaining, observed_at, _epoch(row.get('resets_at')),
                           stale=stale or row.get('value_status') == 'previous', now=now)
        if reading['status'] == 'unavailable':
            reading.update(remaining_percent=None, observed_at=None)
        # Prefer the ordinary account window when a provider supplies one.
        # Otherwise report the tightest observed bucket for a given duration.
        # Provider-supplied IDs and labels never leave the relay.
        bucket_id = row.get('id')
        ordinary = (provider == 'claude' and bucket_id in ('five_hour', 'seven_day')
                    or provider == 'codex' and duration is not None
                    and bucket_id == f'codex-{duration // 60}m')
        rank = ({'available': 0, 'stale': 1, 'unavailable': 2}[reading['status']],
                not ordinary, reading['remaining_percent'] if reading['remaining_percent'] is not None else 101)
        if duration not in by_duration or rank < by_duration[duration][0]:
            by_duration[duration] = (rank, {'window_seconds': duration, **reading})
    durations = sorted(by_duration, key=lambda value: (value is None, value or 0))
    return [by_duration[duration][1] for duration in durations[:12]]


def _provider(provider, raw, now):
    reading = {'provider': provider, 'status': 'unavailable', 'windows': []}
    status = raw.get('status')
    if status not in ('ok', 'degraded', 'stale'):
        return reading
    observed = _epoch(raw.get('observed_at'))
    stale = status == 'stale'
    if provider == 'dsh':
        balance = raw.get('balance')
        if (observed is not None and observed <= now and isinstance(balance, dict)
                and balance.get('currency') == 'USD' and type(balance.get('is_available')) is bool):
            amount = balance.get('total_balance')
            if isinstance(amount, str) and _USD.fullmatch(amount):
                cents = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_FLOOR)
                reading.update(status='stale' if stale or now - observed >= FRESH_SECONDS else 'available',
                               balance_usd=str(abs(cents) if not cents else cents), observed_at=observed,
                               balance_available=balance['is_available'])
        return reading
    windows = _windows(provider, raw.get('buckets'), observed, stale, now)
    reading['windows'] = windows
    if any(window['status'] == 'available' for window in windows):
        reading['status'] = 'available'
    elif any(window['status'] == 'stale' for window in windows):
        reading['status'] = 'stale'
    return reading


def project(raw, *, now):
    """Allowlist provider keys, structural window durations and scalar values."""
    if not isinstance(raw.get('providers'), list):
        raise ValueError('invalid_usage')
    by_provider = {}
    for row in raw['providers']:
        if isinstance(row, dict) and row.get('provider') in PROVIDERS:
            key = row['provider']
            if key in by_provider:
                raise ValueError('invalid_usage')
            by_provider[key] = _provider(key, row, now)
    return {
        'server_time': now,
        'fresh_seconds': FRESH_SECONDS,
        'providers': [by_provider[key] for key in PROVIDERS if key in by_provider],
    }
