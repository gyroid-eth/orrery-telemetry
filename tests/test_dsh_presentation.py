"""同じinstaller/catalog/HTTP consumerでdsh opt-inのoff/onを対照する。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import dashboard.server as server
from test_gemini_core_install_boundary import _function, _run_install_payload


ROOT = Path(__file__).resolve().parents[1]


def _choice(installed, choice, dry_run=False):
    function = _function((ROOT / 'scripts/install.sh').read_text(), 'install_dsh_presentation')
    body = '\n'.join([
        'set -euo pipefail',
        'plan() { printf "%s\\n" "$*"; }',
        'REPO_ROOT="$1"; DASHBOARD_DIR="$2/dashboard"; DSH_OPT_IN="$3"; DRY_RUN="$4"',
        function, 'install_dsh_presentation',
    ])
    return subprocess.run(
        ['/bin/bash', '-c', body, 'dsh-presentation', str(ROOT), str(installed),
         choice, 'true' if dry_run else 'false'],
        env={'PATH': '/usr/bin:/bin'}, text=True, capture_output=True, check=False)


def _install(installed):
    with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}, clear=True):
        result = _run_install_payload(installed)
    assert result.returncode == 0, result.stderr


def _catalog_fixture(monkeypatch, installed):
    monkeypatch.setattr(server, 'GLOBAL_MODE', False)
    monkeypatch.setattr(server, 'HERE', str(installed / 'dashboard'))
    monkeypatch.setattr(server, '_spawn_scientist_statuses', lambda *args: {})
    monkeypatch.setattr(server, '_portrait_file', lambda *args: None)
    monkeypatch.setattr(server, 'SPAWN_SCIENTISTS_SCRIPT', str(ROOT / 'bin/lib/agentstack-scientists.sh'))
    monkeypatch.setattr(server, '_claude_catalog', lambda: SimpleNamespace(
        models=['claude-sonnet-4-6'], source='fixture', error='', overflow=[]))
    monkeypatch.setattr(server.codex_models, 'provider_catalog', lambda: {
        'id': 'codex', 'label': 'Codex', 'models': ['gpt-5.4'], 'default_model': 'gpt-5.4'})
    assets = dict(server.THEME_ASSETS)
    assets['/assets/deepseek.svg'] = (str(installed / 'dashboard/assets/deepseek.svg'), 'image/svg+xml')
    monkeypatch.setattr(server, 'THEME_ASSETS', assets)


def _get(origin, path):
    try:
        with urllib.request.urlopen(origin + path, timeout=3) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_same_http_catalog_and_asset_off_on_update_off(tmp_path, monkeypatch):
    installed = tmp_path / 'installed'
    _install(installed)
    _catalog_fixture(monkeypatch, installed)
    http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{http.server_port}'

    def inspect(enabled):
        status, body = _get(origin, '/api/spawn-names')
        assert status == 200, body
        catalog = json.loads(body)
        providers = {p['id']: p for p in catalog['providers']}
        assert ('dsh' in providers) is enabled
        assert providers['claude']['models'] == ['claude-sonnet-4-6']
        assert providers['codex']['models'] == ['gpt-5.4']
        assert server._provider_of('deepseek-flash') == ('deepseek' if enabled else '')
        assert server._provider_of('deepseek-official/deepseek-flash') == ('deepseek' if enabled else '')
        assert server.is_public_static('/assets/deepseek.svg') is enabled
        status, asset = _get(origin, '/assets/deepseek.svg')
        assert status == (200 if enabled else 404)
        if enabled:
            assert providers['dsh']['efforts'] == ['off', 'low', 'high', 'max']
            assert providers['dsh']['capabilities']['runtime'] is False
            assert asset == (ROOT / 'integrations/dsh/assets/deepseek.svg').read_bytes()

    try:
        inspect(False)
        assert _choice(installed, 'enable').returncode == 0
        inspect(True)
        # flagなしの実payload更新でも選択とassetを保つ。
        _install(installed)
        assert _choice(installed, '').returncode == 0
        inspect(True)
        assert _choice(installed, 'disable').returncode == 0
        assert (installed / 'dashboard/assets/deepseek.svg').exists()
        inspect(False)  # 古いassetが残っていてもfallback配信しない。
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)


def test_opt_in_dry_run_does_not_copy_asset_or_activate(tmp_path):
    installed = tmp_path / 'installed'
    result = _choice(installed, 'enable', dry_run=True)
    assert result.returncode == 0, result.stderr
    assert not installed.exists()
    assert 'runtime' in result.stdout


def test_installer_accepts_explicit_flags_before_help():
    for flag in ('--enable-dsh', '--disable-dsh'):
        result = subprocess.run(
            ['/bin/bash', str(ROOT / 'scripts/install.sh'), flag, '--help'],
            text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        assert '--enable-dsh' in result.stdout
        assert '--disable-dsh' in result.stdout


def test_unconnected_runtime_is_not_a_successful_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    (tmp_path / 'dsh-provider.json').write_text('{"enabled":true}')
    monkeypatch.setattr(server, 'GLOBAL_MODE', False)
    monkeypatch.setattr(server, '_mcp_bearer', lambda: (_ for _ in ()).throw(AssertionError('credential読取')))
    result = server.do_spawn({'provider': 'dsh', 'task': 'fixture', 'dir': str(tmp_path)})
    assert result['ok'] is False
    assert 'error' in result
