"""WSL Phone access and Serve with disposable command-line fakes."""
from __future__ import annotations

from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from dashboard import mobile_serve as serve, mobile_settings as mobile, mobile_systemd as systemd, server

HOST = 'computer.example.ts.net'
ENABLE = {'access': True, 'owner_login': 'owner@example.test',
          'public_origin': f'https://{HOST}:9443'}
WINDOWS_STATUS = {
    'BackendState': 'Running',
    'Self': {'ID': 'nWindows', 'DNSName': HOST + '.', 'UserID': 7},
    'User': {'7': {'LoginName': ENABLE['owner_login']}},
}
OTHER_ROUTES = {
    'TCP': {'443': {'HTTPS': True}, '8443': {'HTTPS': True}},
    'Web': {f'{HOST}:443': {'Handlers': {'/': {'Text': 'Existing site'}}},
            f'{HOST}:8443': {'Handlers': {'/service': {'Proxy': 'http://127.0.0.1:1234'}}}},
    'AllowFunnel': {f'{HOST}:8443': True},
}

FAKE_SYSTEMCTL = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args.pop(0) == '--user'
with open(os.environ['FAKE_SYSTEMCTL_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\n')
unit = Path(os.environ['FAKE_UNIT'])
active = Path(os.environ['FAKE_ACTIVE'])
if args[0] == 'show':
    if os.environ.get('FAKE_SYSTEMD_UNAVAILABLE'):
        sys.exit(1)
    fragment = os.environ.get('FAKE_SYSTEMD_FRAGMENT') or (str(unit.resolve()) if unit.exists() else '')
    print('LoadState=' + ('loaded' if fragment else 'not-found'))
    print('ActiveState=' + ('active' if active.exists() else 'inactive'))
    print('FragmentPath=' + fragment)
    print('NeedDaemonReload=no')
    print('DropInPaths=' + os.environ.get('FAKE_SYSTEMD_DROPIN', ''))
elif args[0] == 'enable':
    assert args[1:] == ['--now', '--', unit.name] and unit.is_file()
    if os.environ.get('FAKE_SYSTEMD_START_FAILURE'):
        sys.exit(1)
    active.touch()
elif args[0] in {'stop', 'disable'}:
    assert args[-2:] == ['--', unit.name]
    active.unlink(missing_ok=True)
else:
    assert args == ['daemon-reload']
'''

FAKE_TAILSCALE = r'''
import json, os, sys
from pathlib import Path
path = Path(os.environ['FAKE_SERVE'])
args = sys.argv[1:]
with open(os.environ['FAKE_SERVE_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\n')
status = json.loads(Path(os.environ['FAKE_TAILSCALE_STATUS']).read_text())
config = json.loads(path.read_text())
if args == ['status', '--json']:
    print(json.dumps(status))
elif args == ['serve', 'status', '--json']:
    print(json.dumps(config))
else:
    assert args[:3] == ['serve', '--bg', '--https=9443'] and len(args) == 4
    if os.environ.get('FAKE_SERVE_FAILURE'):
        sys.exit(1)
    config = config or {}
    target = args[3]
    host = status['Self']['DNSName'].rstrip('.') + ':9443'
    if target == 'off':
        del config['TCP']['9443']
        del config['Web'][host]
    else:
        assert target == 'http://127.0.0.1:8781'
        config.setdefault('TCP', {})['9443'] = {'HTTPS': True}
        config.setdefault('Web', {})[host] = {'Handlers': {'/': {'Proxy': target}}}
    path.write_text(json.dumps(config))
'''


def executable(path, source):
    path.write_text(f'#!{sys.executable}\n' + source)
    path.chmod(0o755)
    return path


def calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def wsl(tmp_path, monkeypatch):
    home = tmp_path / 'test user % \"home'
    home.mkdir()
    local_bin = tmp_path / 'bin'
    local_bin.mkdir()
    windows = tmp_path / 'Program Files' / 'Tailscale'
    windows.mkdir(parents=True)
    exe = executable(windows / 'tailscale.exe', FAKE_TAILSCALE)
    executable(local_bin / 'systemctl', FAKE_SYSTEMCTL)
    monkeypatch.setenv('PATH', str(local_bin))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('AGENTSTACK_HOME', raising=False)
    monkeypatch.delenv('AGENTSTACK_TAILSCALE', raising=False)
    monkeypatch.setattr(systemd, 'is_wsl', lambda: True)
    monkeypatch.setattr(mobile, 'WINDOWS_TAILSCALE', str(exe))
    monkeypatch.setattr(mobile, 'TAILSCALE_APP', str(tmp_path / 'missing-mac'))
    monkeypatch.setattr(mobile, 'push_python', lambda: None)
    monkeypatch.setattr(mobile, 'relay_script', lambda: mobile.home() / 'dashboard/mobile_relay.py')
    monkeypatch.setattr(mobile, 'START_WAIT', 0.5)
    # Keep fake .exe calls independent of real WSL interop sockets on all hosts.
    monkeypatch.setattr(mobile.wsl_anchor, 'interop_env', lambda: None)
    env = SimpleNamespace(home=home, unit=systemd.unit_path(), marker=mobile.home() / serve.MARKER,
                          active=tmp_path / 'active', system_calls=tmp_path / 'systemctl-calls',
                          ts_config=tmp_path / 'serve.json', ts_status=tmp_path / 'tailscale.json',
                          ts_calls=tmp_path / 'tailscale-calls', local_bin=local_bin, exe=exe)
    for key, value in {'FAKE_UNIT': env.unit, 'FAKE_ACTIVE': env.active,
                       'FAKE_SYSTEMCTL_CALLS': env.system_calls, 'FAKE_SERVE': env.ts_config,
                       'FAKE_TAILSCALE_STATUS': env.ts_status, 'FAKE_SERVE_CALLS': env.ts_calls}.items():
        monkeypatch.setenv(key, str(value))
    env.ts_config.write_text(json.dumps(OTHER_ROUTES))
    env.ts_status.write_text(json.dumps(WINDOWS_STATUS))
    monkeypatch.setattr(mobile, 'listening', lambda port: env.active.exists())
    return env


def test_windows_fallback_and_linux_preference(wsl, monkeypatch):
    assert mobile.tailscale_bin() == str(wsl.exe)
    if_path = executable(wsl.local_bin / 'tailscale.exe', FAKE_TAILSCALE)
    assert mobile.tailscale_bin() == str(if_path)
    linux = executable(wsl.local_bin / 'tailscale', FAKE_TAILSCALE)
    assert mobile.tailscale_bin() == str(linux)
    linux.unlink()
    monkeypatch.setattr(systemd, 'is_wsl', lambda: False)
    assert mobile.tailscale_bin() is None


def test_windows_status_is_read_only(wsl):
    view = mobile.status()
    assert view['tailscale']['signed_in'] and view['tailscale']['serve'] == 'missing'
    assert view['suggest'] == {'owner_login': ENABLE['owner_login'], 'public_origin': ENABLE['public_origin']}
    assert view['relay']['job'] == 'absent'
    assert calls(wsl.ts_calls) == [['status', '--json'], ['serve', 'status', '--json']]


def test_phone_access_start_change_and_off_through_cli(wsl, monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(ENABLE).encode())))
    assert mobile.main(['apply']) == 0
    assert json.loads(capsys.readouterr().out)['relay']['job'] == 'loaded'
    unit = wsl.unit.read_text()
    assert f'ExecStart=":{sys.executable}"' in unit
    assert '"--config"' in unit and '%% \\"home/.agentstack/mobile.json"' in unit
    assert 'WantedBy=default.target' in unit
    assert mobile.apply({'send': False})['settings']['send'] is False
    assert mobile.main(['off']) == 0
    assert json.loads(capsys.readouterr().out)['settings']['access'] is False
    assert not wsl.unit.exists() and not wsl.active.exists()
    assert [args[0] for args in calls(wsl.system_calls) if args[0] != 'show'] == [
        'daemon-reload', 'enable', 'stop', 'daemon-reload', 'enable', 'disable', 'daemon-reload',
    ]
    assert all(args in (['status', '--json'], ['serve', 'status', '--json']) for args in calls(wsl.ts_calls))


@pytest.mark.parametrize('failure', ['FAKE_SYSTEMD_START_FAILURE', 'no-listener'])
def test_failed_start_turns_settings_off_and_removes_unit(wsl, monkeypatch, failure):
    if failure == 'no-listener':
        monkeypatch.setattr(mobile, 'listening', lambda port: False)
    else:
        monkeypatch.setenv(failure, '1')
    with pytest.raises(mobile.SettingsError, match='systemd_failed'):
        mobile.apply(ENABLE)
    assert not mobile.load()['access']
    assert not wsl.unit.exists() and not wsl.active.exists()


def test_manual_port_and_unavailable_systemd_leave_settings_untouched(wsl, monkeypatch):
    monkeypatch.setattr(mobile, 'listening', lambda port: True)
    with pytest.raises(mobile.SettingsError, match='port_in_use'):
        mobile.apply(ENABLE)
    monkeypatch.setenv('FAKE_SYSTEMD_UNAVAILABLE', '1')
    with pytest.raises(systemd.UnitError, match='systemd_unavailable'):
        mobile.apply(ENABLE)
    assert not mobile.settings_path().exists() and not wsl.unit.exists()


@pytest.mark.parametrize('conflict', ['file', 'symlink', 'foreign-fragment', 'dropin'])
def test_foreign_unit_is_never_started_or_stopped(wsl, monkeypatch, conflict):
    wsl.unit.parent.mkdir(parents=True)
    if conflict == 'file':
        wsl.unit.write_text('[Service]\nExecStart=/usr/bin/false\n')
    elif conflict == 'symlink':
        other = wsl.unit.parent / 'other.service'
        other.write_text('[Service]\nExecStart=/usr/bin/false\n')
        wsl.unit.symlink_to(other)
    elif conflict == 'foreign-fragment':
        monkeypatch.setenv('FAKE_SYSTEMD_FRAGMENT', '/usr/lib/systemd/user/other.service')
    else:
        wsl.unit.write_text(systemd._contents(sys.executable, mobile.settings_path().resolve(), mobile.relay_script()))
        monkeypatch.setenv('FAKE_SYSTEMD_DROPIN', '/etc/systemd/user/override.conf')
    with pytest.raises(systemd.UnitError, match='systemd_unit_conflict'):
        mobile.apply(ENABLE)
    assert all(args[0] == 'show' for args in calls(wsl.system_calls))
    assert not mobile.settings_path().exists()


def test_uninstall_is_scoped_and_dry_run_keeps_own_unit(wsl, tmp_path, capsys):
    mobile.apply(ENABLE)
    systemd.uninstall(tmp_path / 'unrelated', dry_run=False)
    systemd.uninstall(mobile.home(), dry_run=True)
    assert 'would disable and remove' in capsys.readouterr().out
    assert wsl.unit.is_file() and wsl.active.exists()
    systemd.uninstall(mobile.home(), dry_run=False)
    assert not wsl.unit.exists() and not wsl.active.exists()


def confirm(plan):
    return serve.confirm({key: plan[key] for key in ('action', 'revision')})


def mutations(wsl):
    return [args for args in calls(wsl.ts_calls) if '--bg' in args]


def test_preview_confirm_add_off_remove_preserves_other_ports(wsl):
    assert serve.preview()['error'] == 'phone_relay_required'
    mobile.apply(ENABLE)
    preview = serve.preview()
    assert (preview['action'], preview['backend'], preview['port'], preview['target']) == (
        'add', 'windows', 9443, 'http://127.0.0.1:8781')
    assert mutations(wsl) == []
    assert confirm(preview) == {'ok': True, 'action': 'add'}
    assert wsl.marker.stat().st_mode & 0o777 == 0o600
    assert mobile.status()['tailscale']['serve'] == 'ok'
    assert confirm(preview)['error'] == 'serve_preview_changed'
    mobile.apply({'access': False})
    remove = serve.preview()
    assert remove['action'] == 'remove'
    assert len(mutations(wsl)) == 1
    assert confirm(remove) == {'ok': True, 'action': 'remove'}
    assert json.loads(wsl.ts_config.read_text()) == OTHER_ROUTES
    assert not wsl.marker.exists()
    assert mutations(wsl) == [
        ['serve', '--bg', '--https=9443', 'http://127.0.0.1:8781'],
        ['serve', '--bg', '--https=9443', 'off'],
    ]


@pytest.mark.parametrize('config', [
    {'TCP': {'9443': {'TCPForward': '127.0.0.1:1234'}}},
    {'Web': {f'{HOST}:9443': {'Handlers': {'/user': {'Text': 'mine'}}}}},
    {'AllowFunnel': {f'{HOST}:9443': True}},
    {'Foreground': {'session': {'TCP': {'9443': {'HTTPS': True}}}}},
    serve._wanted(HOST),  # Even an identical manually created route is foreign.
])
def test_any_existing_9443_configuration_blocks_change(wsl, config):
    mobile.apply(ENABLE)
    plan = serve.preview()
    wsl.ts_config.write_text(json.dumps(config))
    assert serve.preview()['error'] == 'serve_port_in_use'
    assert confirm(plan)['error'] == 'serve_port_in_use'
    assert mutations(wsl) == []


def test_remove_refuses_changed_route(wsl):
    mobile.apply(ENABLE)
    assert confirm(serve.preview())['ok']
    remove = serve.preview()
    config = json.loads(wsl.ts_config.read_text())
    config['Web'][f'{HOST}:9443']['Handlers']['/extra'] = {'Text': 'my page'}
    wsl.ts_config.write_text(json.dumps(config))
    assert confirm(remove)['error'] == 'serve_port_in_use'
    assert len(mutations(wsl)) == 1


def test_null_serve_config_and_failed_command_are_safe(wsl, monkeypatch):
    mobile.apply(ENABLE)
    wsl.ts_config.write_text('null')
    assert mobile.status()['tailscale']['serve'] == 'missing'
    preview = serve.preview()
    assert preview['action'] == 'add'
    monkeypatch.setenv('FAKE_SERVE_FAILURE', '1')
    assert confirm(preview)['error'] == 'serve_command_failed'
    assert not wsl.marker.exists() and wsl.ts_config.read_text() == 'null'


def test_route_admits_local_post_with_preview_and_rejects_cross_origin(wsl, monkeypatch):
    mobile.apply(ENABLE)
    monkeypatch.setattr(server, '_is_wsl', lambda: True)
    monkeypatch.setattr(server, 'GLOBAL_MODE', False)
    dashboard = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    port = dashboard.server_port
    origin = f'http://127.0.0.1:{port}'
    thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    thread.start()

    def request(method, *, body=None, **headers):
        with closing(HTTPConnection('127.0.0.1', port, timeout=8)) as connection:
            connection.request(method, '/api/mobile-serve', body, headers)
            response = connection.getresponse()
            return response.status, response.read()

    try:
        status, raw = request('GET')
        assert status == 200
        plan = json.loads(raw)
        body = json.dumps({key: plan[key] for key in ('action', 'revision')})
        headers = {'Content-Type': 'application/json'}
        assert request('POST', body=body, **headers)[0] == 403
        assert request('POST', body=body, Origin='https://example.test', **headers)[0] == 403
        assert request('POST', body='{}', Origin=origin, **headers)[0] == 409
        assert request('POST', body=body, Origin=origin, **{'Content-Type': 'text/plain'})[0] == 400
        assert mutations(wsl) == []
        assert request('POST', body=body, Origin=origin, **headers)[0] == 200
        assert len(mutations(wsl)) == 1
    finally:
        dashboard.shutdown()
        dashboard.server_close()
        thread.join()
