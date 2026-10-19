"""残高のoffline経路・取得process境界・既定不変・cacheを確かめる。"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
import urllib.request

import pytest
import dashboard.server as server
from dashboard.quotas import build_default_service
from dashboard.quotas.balance import AccountBalance
from dashboard.quotas.base import QuotaSnapshot
from dashboard.quotas.deepseek import DeepSeekBalanceProvider
from dashboard.quotas import deepseek_worker as worker
from dashboard.quotas.service import QuotaService

ROOT = Path(__file__).resolve().parents[1]
USD_USAGE = {'currency': 'USD', 'total_balance': '12.3400', 'is_available': True}
PAYLOAD = {'is_available': True, 'balance_infos': [
    {'currency': 'USD', 'total_balance': '12.3400', 'granted_balance': '2.0000', 'topped_up_balance': '10.3400'},
    {'currency': 'CNY', 'total_balance': '23.45', 'granted_balance': '3.00', 'topped_up_balance': '20.45'},
]}


@contextmanager
def balance_server():
    state = SimpleNamespace(payload=PAYLOAD, status=200, delay=0, reads=0, authorized=0)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/user/balance'
            state.reads += 1
            state.authorized += self.headers.get('Authorization') == 'Bearer ' + worker.DUMMY_KEY
            time.sleep(state.delay)
            self.send_response(state.status)
            self.end_headers()
            try:
                self.wfile.write(json.dumps(state.payload).encode())
            except BrokenPipeError:
                pass
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{http.server_port}/user/balance'
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_real_worker_offline_only_returns_validated_amounts():
    with balance_server() as (state, endpoint):
        args = [sys.executable, str(ROOT / 'dashboard/quotas/deepseek_worker.py'), '--offline-endpoint', endpoint]
        result = subprocess.run(args, env={'PATH': '/usr/bin:/bin', 'DEEPSEEK_API_KEY': worker.DUMMY_KEY},
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value['balance'] == PAYLOAD
        assert worker.DUMMY_KEY not in result.stdout + result.stderr
        assert state.reads == state.authorized == 1
        # endpointを与えても実鍵らしい値では一切送らない。
        rejected = subprocess.run(args, env={'PATH': '/usr/bin:/bin', 'DEEPSEEK_API_KEY': 'not-the-dummy'},
                                  capture_output=True, text=True, timeout=10)
        assert rejected.returncode == 2
        assert state.reads == 1


@pytest.mark.parametrize('status,payload,reason', [
    (401, {'error': worker.DUMMY_KEY}, 'balance_unauthorized'),
    (503, {'error': worker.DUMMY_KEY}, 'balance_http_error'),
    (200, {'is_available': True, 'balance_infos': []}, 'balance_invalid_response'),
    (200, {'is_available': 'yes', 'balance_infos': PAYLOAD['balance_infos']}, 'balance_invalid_response'),
])
def test_failure_is_unknown_and_does_not_reflect_response(status, payload, reason):
    with balance_server() as (state, endpoint):
        state.status, state.payload = status, payload
        result = worker.fetch_balance(worker.DUMMY_KEY, endpoint)
        assert result == {'ok': False, 'reason': reason}
        assert worker.DUMMY_KEY not in json.dumps(result)


def test_timeout_does_not_become_zero():
    with balance_server() as (state, endpoint):
        state.delay = 0.1
        result = worker.fetch_balance(worker.DUMMY_KEY, endpoint, timeout=0.01)
        assert result == {'ok': False, 'reason': 'balance_unreachable'}


def test_worker_owns_key_and_keychain_helper_does_not_inherit_it(monkeypatch, capsys):
    calls = []
    def fake_security(args, **kwargs):
        calls.append((args, kwargs))
        assert 'DEEPSEEK_API_KEY' not in kwargs['env']
        return SimpleNamespace(returncode=0, stdout=worker.DUMMY_KEY.encode())
    monkeypatch.setattr(worker.subprocess, 'run', fake_security)
    monkeypatch.setattr(sys, 'argv', ['deepseek_worker.py'])
    def fake_fetch(key, endpoint):
        assert key == os.environ['DEEPSEEK_API_KEY'] == worker.DUMMY_KEY
        assert endpoint == worker.ENDPOINT
        assert 'HTTP_PROXY' not in os.environ
        return {'ok': True, 'balance': PAYLOAD, 'observed_at': 1000}
    monkeypatch.setattr(worker, 'fetch_balance', fake_fetch)
    with patch.dict(os.environ, {'HTTP_PROXY': 'http://invalid', 'DEEPSEEK_API_KEY': 'ambient-canary'}):
        assert worker.main() == 0
        assert 'DEEPSEEK_API_KEY' not in os.environ
    assert calls[0][0][:2] == ['/usr/bin/security', 'find-generic-password']
    assert worker.DUMMY_KEY not in capsys.readouterr().out


def test_keychain_missing_or_locked_stops_before_http(monkeypatch, capsys):
    monkeypatch.setattr(worker, 'keychain_key', lambda: None)
    monkeypatch.setattr(worker, 'fetch_balance', lambda *a: pytest.fail('HTTP must not run'))
    monkeypatch.setattr(sys, 'argv', ['deepseek_worker.py'])
    with patch.dict(os.environ):
        assert worker.main() == 1
    assert json.loads(capsys.readouterr().out) == {'ok': False, 'reason': 'balance_key_unavailable'}


def test_adapter_cache_unknown_after_failure_and_disabled_no_process(monkeypatch):
    clock, enabled, calls = [1000.0], [True], []
    def runner(args, **kwargs):
        calls.append(args)
        assert set(kwargs['env']) == {'HOME', 'PATH'}
        assert not any('ambient-canary' in str(v) for v in (args, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'observed_at': 1000, 'balance': PAYLOAD}))
        return SimpleNamespace(returncode=1, stdout='{"ok":false,"reason":"balance_key_unavailable"}')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'ambient-canary')
    provider = DeepSeekBalanceProvider(lambda: enabled[0], runner=runner)
    service = QuotaService([provider], clock=lambda: clock[0])
    first = service.read_all()['providers'][0]
    assert first['balance'] == USD_USAGE and first['buckets'] == []
    assert first['last_observed_at'] == 1000
    clock[0] = 1239
    assert service.read_all()['providers'][0]['balance'] == USD_USAGE
    assert len(calls) == 1
    clock[0] = 1240
    failed = service.read_all()['providers'][0]
    assert failed['status'] == 'unavailable' and failed['balance'] is None
    assert failed['last_observed_at'] is None
    assert len(calls) == 2
    enabled[0] = False
    assert service.read_all()['providers'] == []
    assert provider.read().balance is None
    assert len(calls) == 2


def test_same_http_usage_consumer_off_matches_master_then_on(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    monkeypatch.setattr(server, 'GLOBAL_MODE', False)
    service = build_default_service(dsh_enabled=server._dsh_enabled)
    service._clock = lambda: 1000
    for provider in service.providers[:3]:
        snap = QuotaSnapshot(provider.provider_name, provider.source_name, 1000, 'unavailable', reason='fixture')
        monkeypatch.setattr(provider, 'read', lambda snap=snap: snap)
    dsh = service.providers[-1]
    calls = []
    def read(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'observed_at': 1000, 'balance': PAYLOAD}))
    dsh.runner = read
    monkeypatch.setattr(server, 'QUOTA_SERVICE', service)
    master = QuotaService(service.providers[:3], clock=lambda: 1000).read_all()
    http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        def get():
            with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/quotas', timeout=3) as response:
                return json.load(response)
        assert get() == master and calls == []
        (tmp_path / 'dsh-provider.json').write_text('{"enabled":true}')
        enabled = get()
        assert enabled['providers'][:3] == master['providers']
        assert enabled['providers'][3]['balance'] == USD_USAGE
        assert len(calls) == 1
        (tmp_path / 'dsh-provider.json').write_text('{"enabled":false}')
        assert get() == master and len(calls) == 1
    finally:
        http.shutdown(); http.server_close(); thread.join(timeout=2)


def test_balance_schema_rejects_partial_or_non_monetary_data():
    assert AccountBalance.from_payload(PAYLOAD).to_dict() == PAYLOAD
    assert AccountBalance.from_payload(PAYLOAD).to_usage_dict() == USD_USAGE
    assert AccountBalance.from_payload(dict(PAYLOAD, balance_infos=PAYLOAD['balance_infos'][1:])).to_usage_dict() == {
        'currency': 'USD', 'total_balance': None, 'is_available': True}
    for invalid in (dict(PAYLOAD, balance_infos=[PAYLOAD['balance_infos'][0], None]),
                    dict(PAYLOAD, balance_infos=[dict(PAYLOAD['balance_infos'][0], total_balance='NaN')]),
                    dict(PAYLOAD, balance_infos=PAYLOAD['balance_infos'][:1]*2)):
        with pytest.raises(ValueError):
            AccountBalance.from_payload(invalid)


@pytest.mark.parametrize('now,reason', [(999, 'observation_in_future'), (1301, 'observation_expired')])
def test_balance_uses_common_observation_bound_without_reusing_amount(now, reason):
    result = SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'observed_at': 1000, 'balance': PAYLOAD}))
    provider = DeepSeekBalanceProvider(lambda: True, runner=lambda *a, **k: result)
    payload = QuotaService([provider], clock=lambda: now).read_all()['providers'][0]
    assert payload['measurement_kind'] == 'balance'
    assert payload['status'] == 'unavailable' and payload['balance'] is None
    assert payload['reason'] == reason


@pytest.mark.parametrize('pair,expected_reads', [(False, 1), (True, 2)])
def test_runner_single_or_explicit_pair_with_decimal_difference_only(tmp_path, pair, expected_reads):
    with balance_server() as (state, endpoint):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/deepseek-balance-check.py'),
                                 '--offline-endpoint', endpoint, '--output', str(tmp_path/'check.json'),
                                 *(['--pair'] if pair else [])],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output == json.loads((tmp_path/'check.json').read_text())
        assert state.reads == state.authorized == expected_reads
        assert len(output['reads']) == expected_reads
        if pair:
            assert output['reads'][1]['observed_at'] - output['reads'][0]['observed_at'] >= 3
            assert {v['currency'] for v in output['comparison']['changes']} == {'CNY', 'USD'}
            assert all(float(v['total_balance']) == 0 for v in output['comparison']['changes'])
        else:
            assert 'comparison' not in output
        assert '小さい課金' in output['limitation']
        assert worker.DUMMY_KEY not in result.stdout + result.stderr


def test_runner_keeps_small_nonzero_delta_and_does_not_invent_missing_currency():
    spec = importlib.util.spec_from_file_location('balance_check', ROOT / 'scripts/deepseek-balance-check.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = {'ok': True, 'balance': PAYLOAD}
    after_payload = json.loads(json.dumps(PAYLOAD))
    after_payload['balance_infos'][0]['total_balance'] = '12.3399'
    after_payload['balance_infos'][0]['topped_up_balance'] = '10.3399'
    delta = module.compare(before, {'ok': True, 'balance': after_payload})
    usd = next(v for v in delta['changes'] if v['currency'] == 'USD')
    assert usd['total_balance'] == usd['topped_up_balance'] == '-0.0001'
    assert float(usd['granted_balance']) == 0
    after_payload['balance_infos'].pop()
    assert module.compare(before, {'ok': True, 'balance': after_payload})['status'] == 'unknown'
