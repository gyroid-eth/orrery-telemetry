"""Preview and explicitly change the dedicated WSL Phone access Serve route."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

try:
    from dashboard import mobile_settings as mobile, mobile_systemd
except ModuleNotFoundError:
    import mobile_settings as mobile
    import mobile_systemd

PORT = mobile.SERVE_PORT
LISTEN_PORT = mobile.DEFAULTS['listen_port']
TARGET = f'http://127.0.0.1:{LISTEN_PORT}'
MARKER = 'mobile-serve.json'
MAP_FIELDS = {'TCP', 'Web', 'AllowFunnel', 'Foreground', 'Services'}


def _read_marker(path: Path) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 4096):
        raise mobile.SettingsError('serve_receipt_invalid')
    try:
        marker = json.loads(path.read_bytes())
        if not isinstance(marker, dict) or set(marker) != {'node', 'host', 'backend'}:
            raise ValueError
        if not all(isinstance(value, str) and value for value in marker.values()):
            raise ValueError
    except ValueError:
        raise mobile.SettingsError('serve_receipt_invalid') from None
    return marker


def _write_marker(path: Path, owner: dict):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.mobile-serve.', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(owner, stream)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _map(source: dict, key: str) -> dict:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise mobile.SettingsError('serve_status_unavailable')
    return value


def _dedicated_port(config: dict) -> dict:
    """Keep every 9443 entry, including Funnel and other hosts on this port."""
    if set(config) - MAP_FIELDS:
        raise mobile.SettingsError('serve_status_unavailable')
    result = {}
    for key in ('TCP', 'Web', 'AllowFunnel'):
        matches = {
            name: value for name, value in _map(config, key).items()
            if name == str(PORT) or name.endswith(f':{PORT}')
        }
        if matches:
            result[key] = matches
    foreground = _map(config, 'Foreground').values()
    if any(not isinstance(value, dict) for value in foreground):
        raise mobile.SettingsError('serve_status_unavailable')
    if any(_dedicated_port(value) for value in foreground):
        result['Foreground'] = True
    return result


def _wanted(host: str) -> dict:
    return {
        'TCP': {str(PORT): {'HTTPS': True}},
        'Web': {f'{host}:{PORT}': {'Handlers': {'/': {'Proxy': TARGET}}}},
    }


def _read_serve(binary: str) -> dict:
    config = mobile._json([binary, 'serve', 'status', '--json'], empty_null=True)
    if config is None:
        raise mobile.SettingsError('serve_status_unavailable')
    return _dedicated_port(config)


def _node(binary: str) -> dict:
    status = mobile._json([binary, 'status', '--json'])
    if status is None:
        raise mobile.SettingsError('tailscale_status_unavailable')
    me = _map(status, 'Self')
    node, dns = me.get('ID'), me.get('DNSName')
    if status.get('BackendState') != 'Running' or not isinstance(node, str) or not node:
        raise mobile.SettingsError('tailscale_sign_in')
    host = dns.rstrip('.') if isinstance(dns, str) else ''
    try:
        scheme, hostname, port = mobile.mobile_relay.admission.origin(f'https://{host}:{PORT}')
    except ValueError:
        raise mobile.SettingsError('tailscale_dns_unavailable') from None
    if scheme != 'https' or port != PORT or not hostname.endswith('.ts.net'):
        raise mobile.SettingsError('tailscale_dns_unavailable')
    return {'node': node, 'host': hostname,
            'backend': 'windows' if binary.lower().endswith('.exe') else 'linux'}


def _plan():
    binary = mobile.tailscale_bin()
    if not binary:
        raise mobile.SettingsError('tailscale_missing')
    owner = _node(binary)
    current = _read_serve(binary)
    marker = _read_marker(mobile.home() / MARKER)
    if current:
        if current != _wanted(owner['host']) or marker != owner:
            raise mobile.SettingsError('serve_port_in_use')
        action = 'remove'
    else:
        settings = mobile.load()
        if (not settings['access'] or settings['listen_port'] != LISTEN_PORT
                or mobile.job_loaded() is not True or not mobile.listening(LISTEN_PORT)):
            raise mobile.SettingsError('phone_relay_required')
        if settings['public_origin'] != f'https://{owner["host"]}:{PORT}':
            raise mobile.SettingsError('phone_address_mismatch')
        action = 'add'
    revision = hashlib.sha256(json.dumps([binary, owner, current, action], sort_keys=True).encode()).hexdigest()
    return {'ok': True, 'action': action, 'revision': revision, 'backend': owner['backend'],
            'port': PORT, 'target': TARGET}, binary, owner


def preview() -> dict:
    try:
        plan, _, _ = _plan()
        return plan
    except (mobile.SettingsError, OSError) as exc:
        return {'ok': False, 'error': str(exc) if isinstance(exc, mobile.SettingsError) else 'serve_settings_unavailable'}


def confirm(body) -> dict:
    if not isinstance(body, dict) or set(body) != {'action', 'revision'}:
        return {'ok': False, 'error': 'serve_confirmation_invalid'}
    try:
        with mobile_systemd.locked(mobile.home()):
            plan, binary, owner = _plan()
            if (body['action'], body['revision']) != (plan['action'], plan['revision']):
                raise mobile.SettingsError('serve_preview_changed')
            marker = mobile.home() / MARKER
            argv = [binary, 'serve', '--bg', f'--https={PORT}']
            if plan['action'] == 'add':
                argv.append(TARGET)
                expected = _wanted(owner['host'])
            else:
                argv.append('off')
                expected = {}
            done = mobile.tailscale_run(argv, timeout=20)
            if done is None or done.returncode:
                raise mobile.SettingsError('serve_command_failed')
            if _node(binary) != owner or _read_serve(binary) != expected:
                raise mobile.SettingsError('serve_change_unverified')
            if plan['action'] == 'add':
                _write_marker(marker, owner)
            else:
                marker.unlink(missing_ok=True)
            return {'ok': True, 'action': plan['action']}
    except (mobile.SettingsError, mobile_systemd.UnitError) as exc:
        return {'ok': False, 'error': str(exc)}
    except OSError:
        return {'ok': False, 'error': 'serve_settings_unavailable'}
