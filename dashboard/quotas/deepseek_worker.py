"""鍵を読む短命worker。stdoutは検査した残高か固定の理由codeだけ。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from .balance import AccountBalance
else:
    from balance import AccountBalance

ENDPOINT = 'https://api.deepseek.com/user/balance'
DUMMY_KEY = 'orrery-offline-dummy'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # 認証headerを別の送信先へ引き継がない。通常のendpointでredirectは不要。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_balance(key, endpoint=ENDPOINT, *, timeout=8.0):
    request = urllib.request.Request(endpoint, headers={
        'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read(262144))
        balance = AccountBalance.from_payload(value)
        return {'ok': True, 'observed_at': int(time.time()), 'balance': balance.to_dict()}
    except urllib.error.HTTPError as error:
        return {'ok': False, 'reason': 'balance_unauthorized' if error.code in (401, 403) else 'balance_http_error'}
    except (OSError, urllib.error.URLError, TimeoutError):
        return {'ok': False, 'reason': 'balance_unreachable'}
    except (ValueError, TypeError, KeyError):
        return {'ok': False, 'reason': 'balance_invalid_response'}


def keychain_key():
    try:
        result = subprocess.run(
            ['/usr/bin/security', 'find-generic-password', '-s', 'deepseek-api', '-w'],
            env={'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~')},
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode().strip() or None
    except UnicodeError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline-endpoint', help='dummy専用・loopback /user/balance')
    args = parser.parse_args()
    dummy = os.environ.get('DEEPSEEK_API_KEY')
    home = os.path.expanduser('~')
    # 親のproxy/SSL設定や別の秘密を取得processへ引き継がない。
    os.environ.clear()
    os.environ.update({'PATH': '/usr/bin:/bin', 'HOME': home})
    endpoint = ENDPOINT
    if args.offline_endpoint:
        url = urllib.parse.urlsplit(args.offline_endpoint)
        if dummy != DUMMY_KEY or url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path != '/user/balance' or url.username or url.password:
            print(json.dumps({'ok': False, 'reason': 'offline_fixture_required'}))
            return 2
        endpoint, key = args.offline_endpoint, dummy
    else:
        key = keychain_key()
    if not key:
        print(json.dumps({'ok': False, 'reason': 'balance_key_unavailable'}))
        return 1
    os.environ['DEEPSEEK_API_KEY'] = key
    try:
        result = fetch_balance(key, endpoint)
    finally:
        os.environ.pop('DEEPSEEK_API_KEY', None)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
