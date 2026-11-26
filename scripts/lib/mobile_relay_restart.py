"""Reload the installed mobile relay code without enabling Phone access."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dashboard import mobile_settings, mobile_systemd  # noqa: E402


def _owned_plist(plist: Path, script: Path, config: Path) -> bool:
    try:
        info = plist.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 16384):
            return False
        job = plistlib.loads(plist.read_bytes())
    except (FileNotFoundError, ValueError, plistlib.InvalidFileException):
        return False
    argv = job.get('ProgramArguments') if isinstance(job, dict) else None
    return (isinstance(argv, list) and len(argv) == 4
            and all(isinstance(arg, str) for arg in argv)
            and job.get('Label') == mobile_settings.LABEL and argv[2] == '--config'
            and all(Path(arg).is_absolute() for arg in (argv[0], argv[1], argv[3]))
            and Path(argv[1]).resolve() == script and Path(argv[3]).resolve() == config)


def restart(install: Path, *, dry_run: bool) -> None:
    install = install.resolve()
    config = install / 'mobile.json'
    script = (install / 'dashboard/mobile_relay.py').resolve()
    if not mobile_settings.load(config)['access']:
        return

    if mobile_systemd.is_wsl():
        unit = mobile_systemd.RelayUnit(config, script)
        if unit.interpreter() is None:
            return
        if dry_run:
            print(f'DRY-RUN would restart the mobile relay if active ({mobile_systemd.UNIT})')
            return
        with mobile_systemd.locked(install):
            if mobile_settings.load(config)['access'] and unit.restart_if_active():
                print(f'Restarted the mobile relay ({mobile_systemd.UNIT})')
    elif sys.platform == 'darwin':
        plist = mobile_settings.plist_path()
        if not _owned_plist(plist, script, config):
            return
        target = f'{mobile_settings.domain()}/{mobile_settings.LABEL}'
        if dry_run:
            print(f'DRY-RUN would restart the mobile relay if running ({target})')
            return
        ctl = mobile_settings.launchctl()
        job = mobile_settings._run([ctl, 'print', target])
        if job is None:
            raise mobile_settings.SettingsError('launchd_unavailable')
        if job.returncode:
            return
        path = re.search(rb'^[ \t]+path = (.+)$', job.stdout, re.MULTILINE)
        if not path or Path(os.fsdecode(path[1])).resolve() != plist.resolve():
            raise mobile_settings.SettingsError('launchd_job_conflict')
        if not re.search(rb'^[ \t]+state = running[ \t]*$', job.stdout, re.MULTILINE):
            return
        result = mobile_settings._run([ctl, 'kickstart', '-k', target])
        if result is None or result.returncode:
            raise mobile_settings.SettingsError('launchd_restart_failed')
        print(f'Restarted the mobile relay ({target})')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        restart(args.install_dir, dry_run=args.dry_run)
    except (mobile_settings.SettingsError, mobile_systemd.UnitError) as exc:
        parser.exit(1, f'mobile relay: {exc}\n')
    except (OSError, RuntimeError):
        parser.exit(1, 'mobile relay: could not inspect the installed service\n')


if __name__ == '__main__':
    main()
