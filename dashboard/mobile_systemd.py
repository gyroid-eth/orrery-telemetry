"""Manage the opt-in mobile relay's systemd user unit under WSL."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

UNIT = 'org.agentstack.mobile-relay.service'
INTERPRETER = '# ORRERY relay interpreter: '


class UnitError(ValueError):
    pass


def is_wsl() -> bool:
    if sys.platform != 'linux':
        return False
    try:
        return 'microsoft' in Path('/proc/version').read_text(errors='replace').lower()
    except OSError:
        return False


def unit_path() -> Path:
    return Path.home() / '.config/systemd/user' / UNIT


def _systemctl(*args):
    try:
        return subprocess.run(['systemctl', '--user', *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, errors='replace', timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None


def _require(*args):
    done = _systemctl(*args)
    if done is None or done.returncode:
        raise UnitError('systemd_failed')


def _quote(value: Path | str) -> str:
    raw = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise UnitError('systemd_path_invalid')
    return '"' + raw.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _contents(python: str, config: Path, script: Path) -> str:
    return '\n'.join((
        INTERPRETER + json.dumps(python),
        '[Unit]',
        'Description=ORRERY Phone Access relay',
        'After=network.target',
        '',
        '[Service]',
        'Type=simple',
        f'Environment={_quote("AGENTSTACK_HOME=" + str(config.parent))}',
        'ExecStart=' + ' '.join(_quote(arg) for arg in (':' + python, script, '--config', config)),
        'Restart=on-failure',
        'RestartSec=5',
        '',
        '[Install]',
        'WantedBy=default.target',
        '',
    ))


@contextmanager
def locked(home: Path):
    """Serialize the cockpit CLI, dashboard Serve confirmation, and uninstall."""
    home.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(home / 'mobile.lock', os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    except OSError:
        raise UnitError('mobile_lock_unavailable') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UnitError('mobile_lock_unavailable')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UnitError('mobile_settings_busy') from None
        yield
    finally:
        os.close(fd)


class RelayUnit:
    def __init__(self, config: Path, script: Path):
        self.path = unit_path()
        self.config = config.resolve()
        self.script = script.resolve()

    def interpreter(self) -> str | None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        try:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > 16384):
                raise ValueError
            content = self.path.read_text()
            line = content.split('\n', 1)[0]
            if not line.startswith(INTERPRETER):
                raise ValueError
            python = json.loads(line[len(INTERPRETER):])
            if not isinstance(python, str) or not os.path.isabs(python):
                raise ValueError
            if content == _contents(python, self.config, self.script):
                return python
        except (OSError, ValueError):
            pass
        raise UnitError('systemd_unit_conflict')

    def active(self) -> bool:
        owned = self.interpreter() is not None
        done = _systemctl('show', '--all',
                          '--property=LoadState,ActiveState,FragmentPath,DropInPaths,NeedDaemonReload',
                          '--', UNIT)
        fields = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line) if done else {}
        if (fields.get('LoadState') == 'not-found' and not fields.get('FragmentPath')
                and not fields.get('DropInPaths') and fields.get('ActiveState') == 'inactive'):
            return False
        if done is None or done.returncode or not fields.get('LoadState'):
            raise UnitError('systemd_unavailable')
        if (fields['LoadState'] != 'loaded' or not owned
                or fields.get('FragmentPath') != str(self.path.resolve())
                or fields.get('DropInPaths') != '' or fields.get('NeedDaemonReload') != 'no'):
            raise UnitError('systemd_unit_conflict')
        return fields.get('ActiveState') in {'active', 'activating', 'reloading'}

    def start(self, python: str):
        content = _contents(python, self.config, self.script)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                         prefix='.mobile-relay.', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(content)
                stream.close()
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        _require('daemon-reload')
        _require('enable', '--now', '--', UNIT)

    def stop(self):
        _require('stop', '--', UNIT)

    def remove(self):
        if self.interpreter() is None:
            return
        _require('disable', '--now', '--', UNIT)
        self.path.unlink()
        _require('daemon-reload')


def uninstall(install_dir: Path, *, dry_run: bool):
    """Stop only the unit generated by the installation being removed."""
    unit = RelayUnit(install_dir / 'mobile.json', install_dir / 'dashboard/mobile_relay.py')
    try:
        if unit.interpreter() is None:
            return
    except UnitError:
        # Another installation, or a file managed directly by the operator.
        return
    if dry_run:
        print(f'DRY-RUN would disable and remove: {unit.path}')
        return
    with locked(install_dir):
        unit.active()
        unit.remove()
