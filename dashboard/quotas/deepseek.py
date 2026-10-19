"""明示opt-inされたDeepSeekの残高。鍵は別processだけが読む。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .balance import AccountBalance
from .base import QuotaSnapshot


class DeepSeekBalanceProvider:
    provider_name = 'dsh'
    source_name = 'deepseek-account-balance'
    ttl_seconds = 240
    max_age_seconds = 300
    manages_fallback = True
    measurement_kind = 'balance'

    def __init__(self, enabled, *, runner=subprocess.run):
        self.is_enabled = enabled
        self.runner = runner

    def read(self):
        def unknown(reason):
            return QuotaSnapshot(provider=self.provider_name, source=self.source_name,
                                 observed_at=int(time.time()), status='unavailable',
                                 measurement_kind='balance', reason=reason)
        if not self.is_enabled():
            return unknown('telemetry_opt_in_required')
        try:
            result = self.runner(
                [sys.executable, str(Path(__file__).with_name('deepseek_worker.py'))],
                env={'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~')},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
            payload = json.loads(result.stdout)
            if result.returncode != 0 or payload.get('ok') is not True:
                allowed = {'balance_key_unavailable', 'balance_unauthorized',
                           'balance_http_error', 'balance_unreachable', 'balance_invalid_response'}
                reason = payload.get('reason')
                return unknown(reason if reason in allowed else 'balance_read_failed')
            balance = AccountBalance.from_payload(payload.get('balance'))
            observed = payload.get('observed_at')
            if type(observed) is not int:
                return unknown('balance_invalid_response')
            return QuotaSnapshot(provider=self.provider_name, source=self.source_name,
                                 observed_at=observed, status='ok', measurement_kind='balance',
                                 balance=balance)
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
            return unknown('balance_read_failed')
