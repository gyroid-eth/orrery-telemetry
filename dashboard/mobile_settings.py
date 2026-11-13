#!/usr/bin/env python3
"""Phone access settings and the launchd job that runs the mobile reader.

The cockpit's Settings panel calls this through ``agentstack-mobile``; a
person can call it the same way. It owns three things and nothing else:

* ``~/.agentstack/mobile.json`` (mode 0600): the two switches, the owner
  login and public origin the reader admits, and when the person said they
  reviewed who can reach the phone port;
* the launchd job ``org.agentstack.mobile-relay`` that runs
  ``mobile_relay.py --config`` with that file;
* a read-only look at Tailscale (signed-in name, Serve, Funnel).

It never runs ``tailscale serve`` or changes an access policy, and the
policy confirmation stays the person's word: nothing here verifies it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

try:
    from dashboard import mobile_relay
except ModuleNotFoundError:
    import mobile_relay

LABEL = 'org.agentstack.mobile-relay'
MAX_SETTINGS_BYTES = 4096
DEFAULTS = {
    'access': False,
    'send': True,
    'owner_login': '',
    'public_origin': '',
    'policy_reviewed_at': None,
    'listen_port': 8781,
    'upstream_port': 8770,
    'cockpit_port': 8791,
}
REVIEWED_AT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
SERVE_PORT = 9443
TAILSCALE_APP = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
COMMAND_TIMEOUT = 5
START_WAIT = 5.0
MIN_PYTHON = (3, 11)


class SettingsError(ValueError):
    """A fixed code the cockpit can show; never carries a path or a value."""


def home() -> Path:
    return Path(os.environ.get('AGENTSTACK_HOME') or Path.home() / '.agentstack')


def settings_path() -> Path:
    return home() / 'mobile.json'


def plist_path() -> Path:
    return Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')


def relay_script() -> Path:
    return Path(mobile_relay.__file__).resolve()


def _check(settings: dict) -> dict:
    if not isinstance(settings, dict) or set(settings) - set(DEFAULTS):
        raise SettingsError('settings_invalid')
    merged = {**DEFAULTS, **settings}
    for key in ('access', 'send'):
        if type(merged[key]) is not bool:
            raise SettingsError('settings_invalid')
    for key in ('owner_login', 'public_origin'):
        if type(merged[key]) is not str or len(merged[key]) > 254:
            raise SettingsError('settings_invalid')
    for key in ('listen_port', 'upstream_port', 'cockpit_port'):
        if type(merged[key]) is not int:
            raise SettingsError('settings_invalid')
    at = merged['policy_reviewed_at']
    if at is not None and (type(at) is not str or not REVIEWED_AT.fullmatch(at)):
        raise SettingsError('settings_invalid')
    return merged


def load(path: Path | None = None) -> dict:
    """Read the settings file strictly; a missing file means everything off."""
    path = path or settings_path()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return dict(DEFAULTS)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > MAX_SETTINGS_BYTES):
        raise SettingsError('settings_unsafe')
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise SettingsError('settings_invalid') from None
    return _check(raw)


def save(settings: dict, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(_check(settings), indent=2, sort_keys=True) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.mobile.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def relay_config(settings: dict) -> mobile_relay.Config:
    """Phone access reads everything (history and previews included) once the
    person has reviewed the policy; sending also needs that review."""
    reviewed = settings['policy_reviewed_at'] is not None
    return mobile_relay.Config(
        settings['owner_login'], settings['public_origin'],
        settings['listen_port'], settings['upstream_port'],
        input_enabled=settings['send'] and reviewed,
        phone_policy_reviewed=reviewed,
        cockpit_port=settings['cockpit_port'],
        history_enabled=reviewed,
        push_enabled=reviewed and push_python() is not None)


def push_python():
    try:return mobile_relay.mobile_push.runtime_python(home())
    except ValueError:raise SettingsError('mobile_push_runtime_invalid') from None


# --- outside world ------------------------------------------------------------

def _run(argv, *, timeout=COMMAND_TIMEOUT):
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done


def launchctl() -> str:
    return os.environ.get('AGENTSTACK_LAUNCHCTL') or '/bin/launchctl'


def domain() -> str:
    return f'gui/{os.getuid()}'


def job_loaded() -> bool | None:
    done = _run([launchctl(), 'print', f'{domain()}/{LABEL}'])
    if done is None:
        return None
    return done.returncode == 0


def listening(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


def tailscale_bin() -> str | None:
    override = os.environ.get('AGENTSTACK_TAILSCALE')
    if override is not None:
        return override if override and os.access(override, os.X_OK) else None
    found = shutil.which('tailscale')
    if found:
        return found
    return TAILSCALE_APP if os.access(TAILSCALE_APP, os.X_OK) else None


def _json(argv):
    done = _run(argv)
    if done is None or done.returncode != 0:
        return None
    try:
        value = json.loads(done.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def tailscale_state(listen_port: int) -> dict:
    """Read-only: who is signed in, and whether Serve points 9443 at the reader."""
    state = {'found': False, 'signed_in': False, 'dns_name': '', 'login': '',
             'serve': 'unknown', 'funnel': None}
    binary = tailscale_bin()
    if not binary:
        return state
    state['found'] = True
    status = _json([binary, 'status', '--json'])
    if status:
        me = status.get('Self') if isinstance(status.get('Self'), dict) else {}
        users = status.get('User') if isinstance(status.get('User'), dict) else {}
        user = users.get(str(me.get('UserID'))) if me else None
        dns = me.get('DNSName') if isinstance(me.get('DNSName'), str) else ''
        login = user.get('LoginName') if isinstance(user, dict) and isinstance(user.get('LoginName'), str) else ''
        state.update(signed_in=status.get('BackendState') == 'Running',
                     dns_name=dns.rstrip('.'), login=login)
    serve = _json([binary, 'serve', 'status', '--json'])
    if serve is not None:
        state['serve'], state['funnel'] = serve_state(serve, state['dns_name'], listen_port)
    return state


def serve_state(serve: dict, dns_name: str, listen_port: int):
    """('ok'|'missing'|'other', funnel) for HTTPS 9443 -> 127.0.0.1:<listen_port>."""
    tcp = serve.get('TCP') if isinstance(serve.get('TCP'), dict) else {}
    entry = tcp.get(str(SERVE_PORT))
    funnel_map = serve.get('AllowFunnel') if isinstance(serve.get('AllowFunnel'), dict) else {}
    host = f'{dns_name}:{SERVE_PORT}'
    funnel = bool(funnel_map.get(host)) if dns_name else any(bool(v) for v in funnel_map.values())
    if not isinstance(entry, dict):
        return 'missing', funnel
    web = serve.get('Web') if isinstance(serve.get('Web'), dict) else {}
    site = web.get(host) if dns_name else None
    handlers = site.get('Handlers') if isinstance(site, dict) and isinstance(site.get('Handlers'), dict) else {}
    root = handlers.get('/') if isinstance(handlers.get('/'), dict) else {}
    proxy = root.get('Proxy') if isinstance(root.get('Proxy'), str) else ''
    wanted = {f'http://127.0.0.1:{listen_port}', f'http://localhost:{listen_port}', f'127.0.0.1:{listen_port}'}
    return ('ok' if entry.get('HTTPS') and proxy.rstrip('/') in wanted else 'other'), funnel


# --- commands -----------------------------------------------------------------

def job_python_missing() -> bool:
    """True when the job's plist names a Python that is no longer there (a
    Homebrew or venv interpreter removed after Phone access was turned on)."""
    try:
        job = plistlib.loads(plist_path().read_bytes())
        python = job['ProgramArguments'][0]
    except (OSError, ValueError, KeyError, IndexError, TypeError, plistlib.InvalidFileException):
        return False
    return not (isinstance(python, str) and os.access(python, os.X_OK))


def status() -> dict:
    try:
        settings = load()
        problem = None
    except SettingsError as exc:
        settings, problem = dict(DEFAULTS), str(exc)
    reviewed = settings['policy_reviewed_at'] is not None
    tailscale = tailscale_state(settings['listen_port'])
    suggest = {'owner_login': tailscale['login'],
               'public_origin': f'https://{tailscale["dns_name"]}:{SERVE_PORT}' if tailscale['dns_name'] else ''}
    loaded = job_loaded()
    heard = listening(settings['listen_port'])
    return {
        'ok': problem is None, 'error': problem,
        # saved: Settings (or this command) has written the file at least once
        'saved': settings_path().is_file(),
        'settings': {k: settings[k] for k in ('access', 'send', 'owner_login', 'public_origin', 'policy_reviewed_at')},
        'effective': {'read': settings['access'], 'history': settings['access'] and reviewed,
                      'send': settings['access'] and settings['send'] and reviewed},
        # manual: something answers on the reader's port but this job is not
        # loaded, i.e. a reader started by hand. Settings neither sees nor
        # controls what it serves.
        'relay': {'job': {True: 'loaded', False: 'absent', None: 'unknown'}[loaded],
                  'listening': heard, 'manual': loaded is False and heard,
                  'python_missing': job_python_missing(), 'port': settings['listen_port']},
        'tailscale': tailscale, 'suggest': suggest,
    }


def _plist(python: str) -> bytes:
    python=push_python() or python
    logs = home() / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    return plistlib.dumps({
        'Label': LABEL,
        'ProgramArguments': [python, str(relay_script()), '--config', str(settings_path())],
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 10,
        'StandardOutPath': str(logs / 'mobile-relay.log'),
        'StandardErrorPath': str(logs / 'mobile-relay.log'),
    })


def _bootout():
    _run([launchctl(), 'bootout', f'{domain()}/{LABEL}'])
    for _ in range(50):
        if not job_loaded():
            return
        time.sleep(0.1)
    raise SettingsError('job_stuck')


def apply(change: dict, *, now=None) -> dict:
    """Merge a change from Settings, validate it, save it, and (re)start or stop
    the job. Refuses before writing anything when the change is invalid or a
    reader that launchd does not own holds the port (on or off: Settings does
    not control that reader). A job that does not start leaves access off."""
    if not isinstance(change, dict) or set(change) - {'access', 'send', 'owner_login', 'public_origin', 'policy_reviewed'}:
        raise SettingsError('change_invalid')
    settings = load()
    identity = (settings['owner_login'], settings['public_origin'])
    for key in ('access', 'send', 'owner_login', 'public_origin'):
        if key in change:
            settings[key] = change[key]
    if 'policy_reviewed' in change:
        if type(change['policy_reviewed']) is not bool:
            raise SettingsError('change_invalid')
        stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')
        settings['policy_reviewed_at'] = stamp if change['policy_reviewed'] else None
    elif (settings['owner_login'], settings['public_origin']) != identity:
        # The review was about who can reach this owner's address; a new
        # owner or address needs it again.
        settings['policy_reviewed_at'] = None
    settings = _check(settings)
    if settings['access']:
        try:
            relay_config(settings)
        except ValueError:
            raise SettingsError('identity_invalid') from None
        if sys.version_info < MIN_PYTHON:
            # The job runs the reader with this interpreter.
            raise SettingsError('python_too_old')
    loaded = job_loaded()
    if 'access' in change and loaded is False and listening(settings['listen_port']):
        raise SettingsError('port_in_use')
    if settings['access']:
        if loaded is None:
            raise SettingsError('launchd_unavailable')
        if not loaded and listening(settings['listen_port']):
            raise SettingsError('port_in_use')
        if loaded:
            _bootout()  # job_stuck here leaves the file as it was
        save(settings)
        target = plist_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_plist(sys.executable))
        _run([launchctl(), 'enable', f'{domain()}/{LABEL}'])
        done = _run([launchctl(), 'bootstrap', domain(), str(target)])
        if done is None or done.returncode != 0:
            # Leave nothing that launchd would start at the next login, even
            # when the half-started job does not stop.
            save({**settings, 'access': False})
            try:
                _remove_job()
            except SettingsError:
                plist_path().unlink(missing_ok=True)
            raise SettingsError('launchd_failed')
        deadline = time.monotonic() + START_WAIT
        while not listening(settings['listen_port']) and time.monotonic() < deadline:
            time.sleep(0.1)
    else:
        if loaded:
            _bootout()
        save(settings)
        _remove_job()
    return status()


def _remove_job():
    if job_loaded():
        _bootout()
    try:
        plist_path().unlink()
    except FileNotFoundError:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='agentstack-mobile', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='print the settings, the job and the read-only Tailscale state as JSON')
    sub.add_parser('apply', help='read a JSON change on stdin, save it and (re)start or stop the reader')
    sub.add_parser('off', help='stop the reader and turn phone access off')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            result = status()
        elif args.command == 'off':
            result = apply({'access': False})
        else:
            raw = sys.stdin.buffer.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise SettingsError('change_invalid')
            try:
                change = json.loads(raw)
            except ValueError:
                raise SettingsError('change_invalid') from None
            result = apply(change)
    except SettingsError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 2
    print(json.dumps(result))
    return 0 if result.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
