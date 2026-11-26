"""Reload the Bridge code for an existing supervised service after core install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dashboard import mobile_systemd  # noqa: E402
from scripts import codex_plugin_ownership as ownership  # noqa: E402


def _owned_file(path: Path, max_bytes: int) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or info.st_size > max_bytes):
        raise ownership.OwnershipUnknown(f'unsafe service file: {path.name}')
    return True


def _restart_systemd(install: Path, label: str, *, dry_run: bool) -> None:
    # WSL has no automatic Bridge service. An already enabled user unit can
    # opt in by running this integration's runner.
    unit = f'{label}.service'
    path = Path.home() / '.config/systemd/user' / unit
    if not _owned_file(path, 16384):
        return
    runner = (install / 'bin/run-bridge').resolve()
    commands = re.findall(r'^[ \t]*ExecStart[ \t]*=[ \t]*(.+)$', path.read_text(), re.M)
    if len(commands) != 1:
        raise ownership.OwnershipUnknown('systemd Bridge service has no single ExecStart')
    args = shlex.split(commands[0])
    if args:
        args[0] = args[0].removeprefix(':')
    direct = [str(runner)]
    if args != direct and not ownership.owned_arguments(args, install):
        raise ownership.OwnershipUnknown('systemd Bridge service points to another executable')
    if dry_run:
        print(f'DRY-RUN would restart the Codex App Bridge if enabled and active ({unit})')
        return
    result = mobile_systemd._systemctl(
        'show', '--all',
        '--property=LoadState,ActiveState,UnitFileState,FragmentPath,DropInPaths,NeedDaemonReload',
        '--', unit)
    if result is None or result.returncode:
        raise ownership.OwnershipUnknown('systemd Bridge service inspection is unavailable')
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if fields.get('UnitFileState') not in {'enabled', 'enabled-runtime'} or fields.get('ActiveState') != 'active':
        return
    if (fields.get('LoadState') != 'loaded' or fields.get('FragmentPath') != str(path.resolve())
            or fields.get('DropInPaths') != '' or fields.get('NeedDaemonReload') != 'no'):
        raise ownership.OwnershipUnknown('loaded systemd Bridge service differs from its file')
    mobile_systemd._require('try-restart', '--', unit)
    print(f'Restarted the Codex App Bridge ({unit})')


def restart(install: Path, *, dry_run: bool) -> None:
    install = install.resolve()
    manifest = install / 'install-state.json'
    if not _owned_file(manifest, 65536):
        return
    data = json.loads(manifest.read_bytes())
    if (not isinstance(data, dict) or data.get('tool') != 'agentstack-codex-app'
            or not isinstance(data.get('install_dir'), str)
            or Path(data['install_dir']).resolve() != install):
        raise ownership.OwnershipUnknown('Bridge install receipt does not match the updated payload')
    launchd = data.get('launchd', {})
    if not isinstance(launchd, dict):
        raise ownership.OwnershipUnknown('invalid Bridge launchd receipt')
    label = launchd.get('label', 'org.agentstack.codex-app-bridge')
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', label):
        raise ownership.OwnershipUnknown('invalid Bridge service label')
    if mobile_systemd.is_wsl():
        _restart_systemd(install, label, dry_run=dry_run)
    elif sys.platform == 'darwin':
        service = data.get('service', {})
        if not isinstance(service, dict):
            raise ownership.OwnershipUnknown('invalid Bridge service receipt')
        if launchd.get('enabled') is not True or service.get('kind', 'launchd') != 'launchd':
            return
        plist = Path.home() / 'Library/LaunchAgents' / f'{label}.plist'
        saved_path = launchd.get('path')
        if (not isinstance(saved_path, str) or not Path(saved_path).is_absolute()
                or Path(saved_path).resolve() != plist.resolve()):
            raise ownership.OwnershipUnknown('Bridge plist is outside this HOME')
        if not _owned_file(plist, 16384):
            return
        target = f'gui/{os.getuid()}/{label}'
        if dry_run:
            print(f'DRY-RUN would restart the Codex App Bridge if running ({target})')
        elif ownership.check_launchd({'label': label, 'path': plist}, install, running_only=True):
            subprocess.run(['launchctl', 'kickstart', '-k', target], capture_output=True, check=True, timeout=5)
            print(f'Restarted the Codex App Bridge ({target})')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        restart(args.install_dir, dry_run=args.dry_run)
    except (ownership.OwnershipUnknown, mobile_systemd.UnitError) as exc:
        parser.exit(1, f'Codex App Bridge: {exc}\n')
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        parser.exit(1, 'Codex App Bridge: could not inspect or restart the installed service\n')


if __name__ == '__main__':
    main()
