#!/usr/bin/env python3
"""親が承認後に使う残高読取runner。既定1GET、--pairは前後2GETのみ。"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard.quotas.balance import AccountBalance

LIMITATION = '表示された小数点以下の桁より小さい課金は、この差分では見えません。'


def read_once(offline_endpoint=None):
    args = [sys.executable, str(ROOT / 'dashboard/quotas/deepseek_worker.py')]
    env = {'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~')}
    if offline_endpoint:
        args += ['--offline-endpoint', offline_endpoint]
        env['DEEPSEEK_API_KEY'] = 'orrery-offline-dummy'
    try:
        result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15)
        value = json.loads(result.stdout)
        if result.returncode != 0 or value.get('ok') is not True:
            return {'ok': False, 'reason': 'balance_read_failed'}
        balance = AccountBalance.from_payload(value.get('balance'))
        observed = value.get('observed_at')
        if type(observed) is not int:
            return {'ok': False, 'reason': 'balance_invalid_response'}
        return {'ok': True, 'observed_at': observed, 'balance': balance.to_dict()}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
        return {'ok': False, 'reason': 'balance_read_failed'}


def compare(before, after):
    if not before.get('ok') or not after.get('ok'):
        return {'status': 'unknown', 'changes': []}
    left = {v.currency: v for v in AccountBalance.from_payload(before['balance']).balance_infos}
    right = {v.currency: v for v in AccountBalance.from_payload(after['balance']).balance_infos}
    if left.keys() != right.keys():
        return {'status': 'unknown', 'changes': [], 'reason': 'currency_set_changed'}
    return {'status': 'observed_difference', 'changes': [dict(currency=currency, **{
        name: str(Decimal(getattr(right[currency], name)) - Decimal(getattr(left[currency], name)))
        for name in ('total_balance', 'granted_balance', 'topped_up_balance')
    }) for currency in sorted(left)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true', help='承認済みの実確認。鍵はworkerだけがKeychainから読む')
    mode.add_argument('--offline-endpoint', help='dummy専用のloopback fixture')
    parser.add_argument('--pair', action='store_true', help='3秒あけて前後2GET。ほかのDeepSeek要求はしない')
    parser.add_argument('--output', type=Path, help='金額だけの結果JSONを保存')
    args = parser.parse_args()
    # runnerは鍵を取得しない。取得workerに親のenvを継承させない。
    home = os.path.expanduser('~')
    os.environ.clear()
    os.environ.update({'PATH': '/usr/bin:/bin', 'HOME': home})
    before = read_once(args.offline_endpoint)
    result = {'reads': [before], 'limitation': LIMITATION,
              'conclusion': '観測値だけを記録します。課金が無いことの証明ではありません。'}
    if args.pair and before['ok']:
        time.sleep(3)
        after = read_once(args.offline_endpoint)
        result['reads'].append(after)
        result['comparison'] = compare(before, after)
    elif args.pair:
        result['comparison'] = {'status': 'unknown', 'changes': []}
    output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    print(output, end='')
    return 0 if all(item['ok'] for item in result['reads']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
