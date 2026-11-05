"""Phone access from Settings: the settings file, the launchd job, and the
default that phone access reads conversation history and previews.

launchd and Tailscale are fakes on a temporary HOME; nothing here touches the
real launchd domain, the live reader port or a real Tailscale.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import socket
import stat
import subprocess
import sys
import threading

import pytest

from dashboard import mobile_relay as relay
from dashboard import mobile_settings as ms

REPO = Path(__file__).resolve().parents[1]
OWNER = 'owner@example.test'
ORIGIN = 'https://computer.example.ts.net:9443'

FAKE_LAUNCHCTL = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ['FAKE_LAUNCHD_STATE']
log = os.environ['FAKE_LAUNCHD_LOG']
with open(log, 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
loaded = os.path.exists(state)
cmd = sys.argv[1]
if cmd == 'print':
    sys.exit(0 if loaded else 113)
if cmd == 'bootstrap':
    if os.environ.get('FAKE_LAUNCHD_FAIL_BOOTSTRAP'):
        if os.environ['FAKE_LAUNCHD_FAIL_BOOTSTRAP'] == 'loaded':
            open(state, 'w').write(sys.argv[3])
        sys.exit(5)
    if loaded:
        sys.exit(37)
    open(state, 'w').write(sys.argv[3])
    sys.exit(0)
if cmd == 'bootout':
    if os.environ.get('FAKE_LAUNCHD_STUCK'):
        sys.exit(0)
    if loaded:
        os.unlink(state)
    sys.exit(0 if loaded else 3)
sys.exit(0)
'''

FAKE_TAILSCALE = r'''#!/usr/bin/env python3
import json, os, sys
data = json.load(open(os.environ['FAKE_TAILSCALE_DATA']))
if sys.argv[1:] == ['status', '--json']:
    print(json.dumps(data['status']))
elif sys.argv[1:] == ['serve', 'status', '--json']:
    print(json.dumps(data['serve']))
else:
    sys.exit(9)
'''


def write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def holding(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', port))
    s.listen(8)
    stop = threading.Event()

    def accept():
        s.settimeout(0.1)
        while not stop.is_set():
            try:
                c, _ = s.accept()
                c.close()
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()
        s.close()


SERVE_OK = lambda port: {
    'TCP': {'9443': {'HTTPS': True}},
    'Web': {'computer.example.ts.net:9443': {'Handlers': {'/': {'Proxy': f'http://127.0.0.1:{port}'}}}},
}
STATUS = {'BackendState': 'Running',
          'Self': {'DNSName': 'computer.example.ts.net.', 'UserID': 42},
          'User': {'42': {'LoginName': OWNER}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.agentstack').mkdir(parents=True)
    tools = tmp_path / 'tools'
    tools.mkdir()
    state = tmp_path / 'launchd-state'
    log = tmp_path / 'launchd.log'
    log.write_text('')
    data = tmp_path / 'tailscale.json'
    port = free_port()
    data.write_text(json.dumps({'status': STATUS, 'serve': SERVE_OK(port)}))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('AGENTSTACK_HOME', str(home / '.agentstack'))
    monkeypatch.setenv('AGENTSTACK_LAUNCHCTL', str(write_exec(tools / 'launchctl', FAKE_LAUNCHCTL)))
    monkeypatch.setenv('AGENTSTACK_TAILSCALE', str(write_exec(tools / 'tailscale', FAKE_TAILSCALE)))
    monkeypatch.setenv('FAKE_LAUNCHD_STATE', str(state))
    monkeypatch.setenv('FAKE_LAUNCHD_LOG', str(log))
    monkeypatch.setenv('FAKE_TAILSCALE_DATA', str(data))
    monkeypatch.setattr(ms, 'START_WAIT', 0.0)
    ms.save({**ms.DEFAULTS, 'listen_port': port})

    class Env:
        pass
    e = Env()
    e.home, e.state, e.port, e.data, e.tools = home, state, port, data, tools
    e.calls = lambda: [json.loads(line)[0] for line in log.read_text().splitlines()]
    return e


def turn_on(**extra):
    return ms.apply({'access': True, 'owner_login': OWNER, 'public_origin': ORIGIN, **extra})


# --- the default: phone access reads history and previews -------------------

BASE = ['--owner-login', OWNER, '--public-origin', ORIGIN]


def config_from(argv, monkeypatch):
    seen = {}
    monkeypatch.setattr(relay, 'serve', lambda config: seen.setdefault('config', config) and 0)
    assert relay.main(argv) == 0
    return seen['config']


def test_reviewed_reader_has_history_on_without_a_separate_flag(monkeypatch):
    config = config_from(['--enable', '--phone-policy-reviewed', *BASE], monkeypatch)
    assert config.history_enabled is True and config.input_enabled is False


def test_disable_history_keeps_history_and_previews_off(monkeypatch):
    config = config_from(['--enable', '--phone-policy-reviewed', '--disable-history', *BASE], monkeypatch)
    assert config.history_enabled is False
    with pytest.raises(SystemExit):
        relay.main(['--enable', '--phone-policy-reviewed', '--enable-history', '--disable-history', *BASE])


def test_reader_without_policy_review_still_starts_and_says_why_history_is_off(monkeypatch, capsys):
    config = config_from(['--enable', *BASE], monkeypatch)
    assert config.history_enabled is False
    server = relay.make_server(config, store=object(), test_port=0)
    try:
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        import http.client
        con = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        con.request('GET', '/view/history', headers={
            'Host': 'computer.example.ts.net:9443', 'Tailscale-User-Login': OWNER})
        body = json.loads(con.getresponse().read())
        con.close()
    finally:
        server.shutdown()
        server.server_close()
    assert body['enabled'] is False and body['reason'] == 'policy_not_reviewed'
    assert relay.main(['--check', *BASE]) == 0
    assert 'off until --phone-policy-reviewed' in capsys.readouterr().out


def test_hand_started_reader_says_at_start_that_history_is_now_on(monkeypatch, capsys):
    # Before this change, --phone-policy-reviewed without --enable-history
    # kept history off; a restart after updating turns it on, and says so.
    config_from(['--enable', '--phone-policy-reviewed', *BASE], monkeypatch)
    out = capsys.readouterr().out
    assert 'history and file/image previews are on' in out and '--disable-history to keep' in out
    for flags in (['--enable-history'], ['--disable-history']):
        config_from(['--enable', '--phone-policy-reviewed', *flags, *BASE], monkeypatch)
        assert '--disable-history to keep' not in capsys.readouterr().out


def test_check_reports_history_on(capsys):
    assert relay.main(['--check', '--phone-policy-reviewed', *BASE]) == 0
    assert 'conversation history and previews are on' in capsys.readouterr().out


# --- relay --config ------------------------------------------------------------

def test_config_file_starts_the_reader_with_both_switches(env, monkeypatch):
    ms.save({**ms.load(), 'access': True, 'owner_login': OWNER, 'public_origin': ORIGIN,
             'policy_reviewed_at': '2026-11-05T01:02:03Z'})
    config = config_from(['--config', str(ms.settings_path())], monkeypatch)
    assert (config.owner_login, config.public_origin, config.listen_port) == (OWNER, ORIGIN, env.port)
    assert config.history_enabled is True and config.input_enabled is True and config.phone_policy_reviewed is True


def test_config_file_without_review_reads_but_neither_shows_history_nor_sends(env, monkeypatch):
    ms.save({**ms.load(), 'access': True, 'owner_login': OWNER, 'public_origin': ORIGIN})
    config = config_from(['--config', str(ms.settings_path())], monkeypatch)
    assert config.history_enabled is False and config.input_enabled is False


def test_config_file_with_access_off_starts_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(relay, 'serve', lambda config: pytest.fail('must not listen'))
    assert relay.main(['--config', str(ms.settings_path())]) == 0
    assert 'Phone access is off' in capsys.readouterr().out


@pytest.mark.parametrize('extra', [['--enable'], ['--listen-port', '9999'], ['--enable-terminal']])
def test_config_takes_no_other_option(env, extra):
    with pytest.raises(SystemExit):
        relay.main(['--config', str(ms.settings_path()), *extra])


def test_unsafe_or_unknown_settings_file_is_refused(env, monkeypatch):
    path = ms.settings_path()
    monkeypatch.setattr(relay, 'serve', lambda config: pytest.fail('must not listen'))
    path.chmod(0o644)
    with pytest.raises(SystemExit):
        relay.main(['--config', str(path)])
    path.chmod(0o600)
    path.write_text(json.dumps({**ms.DEFAULTS, 'access': True, 'owner_login': OWNER, 'public_origin': ORIGIN,
                                'history': False}))
    with pytest.raises(ms.SettingsError, match='settings_invalid'):
        ms.load(path)
    with pytest.raises(SystemExit):
        relay.main(['--config', str(path)])
    path.write_text(json.dumps({**ms.DEFAULTS, 'access': 1}))
    with pytest.raises(ms.SettingsError):
        ms.load(path)


# --- apply: settings file and launchd job -----------------------------------

def test_turning_on_writes_a_private_file_and_a_job_that_runs_the_reader_with_it(env):
    result = turn_on()
    path = ms.settings_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved['access'] is True and saved['owner_login'] == OWNER and saved['send'] is True
    job = plistlib.loads(ms.plist_path().read_bytes())
    assert job['Label'] == 'org.agentstack.mobile-relay'
    assert job['ProgramArguments'][1:] == [str(ms.relay_script()), '--config', str(path)]
    assert job['KeepAlive'] == {'SuccessfulExit': False}
    assert 'bootstrap' in env.calls() and env.state.exists()
    assert result['relay']['job'] == 'loaded'


def test_changing_a_setting_restarts_the_job(env):
    turn_on()
    before = len(env.calls())
    ms.apply({'send': False})
    calls = env.calls()[before:]
    assert calls.index('bootout') < calls.index('bootstrap')
    assert json.loads(ms.settings_path().read_text())['send'] is False


def test_turning_off_stops_the_job_and_removes_its_plist(env):
    turn_on()
    result = ms.apply({'access': False})
    assert not env.state.exists() and not ms.plist_path().exists()
    assert json.loads(ms.settings_path().read_text())['access'] is False
    assert result['relay']['job'] == 'absent'


def test_another_reader_on_the_port_is_refused_before_anything_is_written(env):
    before = ms.settings_path().read_bytes()
    with holding(env.port):
        with pytest.raises(ms.SettingsError, match='port_in_use'):
            turn_on()
    assert ms.settings_path().read_bytes() == before
    assert not ms.plist_path().exists() and 'bootstrap' not in env.calls()


def test_hand_started_reader_is_shown_and_settings_change_nothing_until_it_stops(env):
    # A reader started by hand (not by this job) holds the port: status says
    # so, and neither turning access on nor off writes or touches launchd.
    before = ms.settings_path().read_bytes()
    with holding(env.port):
        relay_state = ms.status()['relay']
        assert relay_state['manual'] is True and relay_state['listening'] is True
        for change in ({'access': False}, {'access': True, 'owner_login': OWNER, 'public_origin': ORIGIN}):
            with pytest.raises(ms.SettingsError, match='port_in_use'):
                ms.apply(change)
    assert ms.settings_path().read_bytes() == before
    assert not ms.plist_path().exists() and not {'bootstrap', 'bootout'} & set(env.calls())
    assert ms.status()['relay']['manual'] is False


def test_own_running_job_on_the_port_is_not_mistaken_for_another_reader(env):
    turn_on()
    with holding(env.port):
        ms.apply({'send': False})
    assert env.calls().count('bootstrap') == 2


@pytest.mark.parametrize('change', [
    {'access': True, 'owner_login': '', 'public_origin': ORIGIN},
    {'access': True, 'owner_login': OWNER, 'public_origin': 'https://example.com'},
    {'access': True, 'owner_login': OWNER, 'public_origin': 'http://computer.example.ts.net:9443'},
])
def test_invalid_identity_is_refused_before_anything_is_written(env, change):
    before = ms.settings_path().read_bytes()
    with pytest.raises(ms.SettingsError, match='identity_invalid'):
        ms.apply(change)
    assert ms.settings_path().read_bytes() == before and env.calls() == []


def test_too_old_python_is_refused_before_anything_is_written(env, monkeypatch):
    before = ms.settings_path().read_bytes()
    monkeypatch.setattr(ms, 'MIN_PYTHON', (99, 0))
    with pytest.raises(ms.SettingsError, match='python_too_old'):
        turn_on()
    assert ms.settings_path().read_bytes() == before and env.calls() == []


@pytest.mark.parametrize('change', [{'history': False}, {'access': 'yes'}, {'policy_reviewed': 1},
                                    {'listen_port': 8781}, []])
def test_unknown_or_mistyped_change_is_refused(env, change):
    with pytest.raises(ms.SettingsError):
        ms.apply(change)


def test_policy_review_is_a_dated_statement_and_gates_history_and_sending(env):
    result = turn_on()
    assert result['effective'] == {'read': True, 'history': False, 'send': False}
    when = dt.datetime(2026, 11, 5, 1, 2, 3, tzinfo=dt.timezone.utc)
    result = ms.apply({'policy_reviewed': True}, now=when)
    assert result['settings']['policy_reviewed_at'] == '2026-11-05T01:02:03Z'
    assert result['effective'] == {'read': True, 'history': True, 'send': True}
    result = ms.apply({'send': False})
    assert result['effective']['send'] is False and result['effective']['history'] is True


def test_changing_owner_or_address_asks_for_the_review_again(env):
    turn_on(policy_reviewed=True)
    result = ms.apply({'owner_login': 'other@example.test'})
    assert result['settings']['policy_reviewed_at'] is None
    assert result['effective'] == {'read': True, 'history': False, 'send': False}
    ms.apply({'policy_reviewed': True})
    result = ms.apply({'public_origin': 'https://other.example.ts.net:9443'})
    assert result['settings']['policy_reviewed_at'] is None
    ms.apply({'policy_reviewed': True})
    result = ms.apply({'owner_login': 'other@example.test', 'send': False})
    assert result['settings']['policy_reviewed_at'] is not None


def test_failed_launchd_start_leaves_phone_access_off_with_no_job(env, monkeypatch):
    monkeypatch.setenv('FAKE_LAUNCHD_FAIL_BOOTSTRAP', '1')
    with pytest.raises(ms.SettingsError, match='launchd_failed'):
        turn_on(policy_reviewed=True)
    saved = ms.load()
    assert saved['access'] is False and not ms.plist_path().exists()
    # the rest of what was asked is kept, so trying again needs no retyping
    assert saved['owner_login'] == OWNER and saved['policy_reviewed_at'] is not None


def test_failed_start_whose_job_will_not_stop_still_leaves_access_off(env, monkeypatch):
    # bootstrap fails but leaves the job loaded, and bootout does not take:
    # nothing should be left that launchd would start at the next login.
    monkeypatch.setenv('FAKE_LAUNCHD_FAIL_BOOTSTRAP', 'loaded')
    monkeypatch.setenv('FAKE_LAUNCHD_STUCK', '1')
    monkeypatch.setattr(ms.time, 'sleep', lambda s: None)
    with pytest.raises(ms.SettingsError, match='launchd_failed'):
        turn_on(policy_reviewed=True)
    assert ms.load()['access'] is False and not ms.plist_path().exists()


def test_old_job_that_does_not_stop_changes_nothing(env, monkeypatch):
    turn_on()
    before = ms.settings_path().read_bytes()
    monkeypatch.setenv('FAKE_LAUNCHD_STUCK', '1')
    monkeypatch.setattr(ms.time, 'sleep', lambda s: None)
    for change in ({'send': False}, {'access': False}):
        with pytest.raises(ms.SettingsError, match='job_stuck'):
            ms.apply(change)
        assert ms.settings_path().read_bytes() == before and ms.plist_path().exists()


def test_status_says_when_the_jobs_python_is_gone(env):
    turn_on()
    assert ms.status()['relay']['python_missing'] is False
    job = plistlib.loads(ms.plist_path().read_bytes())
    job['ProgramArguments'][0] = str(env.home / 'gone' / 'python3')
    ms.plist_path().write_bytes(plistlib.dumps(job))
    assert ms.status()['relay']['python_missing'] is True
    turn_on()
    assert ms.status()['relay']['python_missing'] is False


# --- status: read-only Tailscale ----------------------------------------------

def test_status_fills_owner_and_address_from_tailscale(env):
    result = ms.status()
    assert result['suggest'] == {'owner_login': OWNER, 'public_origin': ORIGIN}
    assert result['tailscale']['serve'] == 'ok' and result['tailscale']['funnel'] is False
    assert result['tailscale']['signed_in'] is True


def test_status_reports_missing_serve_and_funnel(env):
    env.data.write_text(json.dumps({'status': STATUS, 'serve': {}}))
    assert ms.status()['tailscale']['serve'] == 'missing'
    serve = SERVE_OK(env.port)
    serve['AllowFunnel'] = {'computer.example.ts.net:9443': True}
    env.data.write_text(json.dumps({'status': STATUS, 'serve': serve}))
    tailscale = ms.status()['tailscale']
    assert tailscale['serve'] == 'ok' and tailscale['funnel'] is True
    other = SERVE_OK(env.port + 1)
    env.data.write_text(json.dumps({'status': STATUS, 'serve': other}))
    assert ms.status()['tailscale']['serve'] == 'other'


def test_status_never_changes_tailscale(env):
    log = env.tools / 'tailscale.log'
    write_exec(env.tools / 'tailscale', FAKE_TAILSCALE.replace(
        "data = json.load", f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\ndata = json.load"))
    ms.status()
    turn_on()
    assert set(log.read_text().splitlines()) == {'status --json', 'serve status --json'}


def test_status_says_whether_settings_were_ever_saved(env):
    assert ms.status()['saved'] is True
    ms.settings_path().unlink()
    assert ms.status()['saved'] is False and ms.status()['settings']['access'] is False


def test_status_without_tailscale_says_so(env, monkeypatch):
    monkeypatch.setenv('AGENTSTACK_TAILSCALE', '')
    result = ms.status()
    assert result['tailscale']['found'] is False and result['suggest'] == {'owner_login': '', 'public_origin': ''}


# --- the installed command ----------------------------------------------------

def test_command_prints_json_and_fixed_error_codes(env):
    run = lambda *a, stdin='': subprocess.run([sys.executable, str(REPO / 'bin/agentstack-mobile'), *a],
                                              input=stdin, capture_output=True, text=True, env=os.environ, timeout=30)
    done = run('status')
    assert done.returncode == 0 and json.loads(done.stdout)['settings']['access'] is False
    done = run('apply', stdin='{"access": true, "owner_login": "", "public_origin": "x"}')
    assert done.returncode == 2 and json.loads(done.stdout) == {'ok': False, 'error': 'identity_invalid'}
    done = run('apply', stdin='not json')
    assert json.loads(done.stdout) == {'ok': False, 'error': 'change_invalid'}


def test_installer_places_the_command_but_starts_no_job():
    install = (REPO / 'scripts/install.sh').read_text()
    assert 'cp "$REPO_ROOT/bin/agentstack-mobile" "$BIN_DIR/agentstack-mobile"' in install
    assert 'org.agentstack.mobile-relay' not in install
    assert 'org.agentstack.mobile-relay' in (REPO / 'scripts/uninstall.sh').read_text()
