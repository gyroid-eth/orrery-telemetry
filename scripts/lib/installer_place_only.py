"""Admission for fresh stage payload placement. No filesystem writes or probes."""
import os
from pathlib import Path
import pwd
import stat
import sys


class StageAdmissionError(ValueError):
    pass


def stage_path_text(raw, reason="PLACE_ONLY_STAGE_PATH_INVALID"):
    if any(c in str(raw) for c in ('\n', '\r', '\\', '"')):
        raise StageAdmissionError(reason)


def admit(raw, home, previous_install):
    stage_path_text(raw)
    target = Path(raw)
    if not target.is_absolute():
        raise StageAdmissionError('PLACE_ONLY_REQUIRES_STAGE_DIR')
    # A fresh destination avoids overwriting state or following an installed
    # payload's symlinks. Permission/IO errors are never treated as absence.
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise StageAdmissionError('PLACE_ONLY_REQUIRES_FRESH_DIR')
    target = target.resolve(strict=False)
    stage_path_text(target)
    parent = target.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or not parent.st_mode & stat.S_IXUSR:
        raise StageAdmissionError('PLACE_ONLY_STAGE_PARENT_INVALID')
    private_parent = parent.st_uid == os.getuid() and not parent.st_mode & 0o022
    sticky_parent = parent.st_uid == 0 and parent.st_mode & stat.S_ISVTX
    if not (private_parent or sticky_parent):
        raise StageAdmissionError('PLACE_ONLY_STAGE_PARENT_INVALID')
    homes = {Path(home).resolve(), Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()}
    protected = {h / '.agentstack' for h in homes}
    previous = Path(previous_install).resolve()
    if previous != target:
        protected.add(previous)
    for h in homes:
        if target == h or target in h.parents:
            raise StageAdmissionError('PLACE_ONLY_LIVE_TARGET_REJECTED')
        protected.update(h / name for name in ('.claude', '.codex', '.gemini', 'Library', '.config'))
    if any(target == root or root in target.parents for root in protected):
        raise StageAdmissionError('PLACE_ONLY_LIVE_TARGET_REJECTED')
    # These destinations are persisted into the generated env/manifest. Refuse
    # outside roots, rather than retaining live overrides in a stage install.
    for key in ('AGENTSTACK_MAIL_STATE_ROOT', 'AGENTSTACK_MAIL_SERVICE_ROOT',
                'AGENTSTACK_MAIL_SERVICE_VENV', 'AGENTSTACK_MAIL_SERVICE_ENV',
                'AGENTSTACK_MAIL_DB', 'AGENTSTACK_MAIL_ENV', 'AGENTSTACK_MAIL_HOME',
                'AGENTSTACK_SIGNALS_DIR', 'AGENTSTACK_DASHBOARD_LOG',
                'AGENTSTACK_WORKTREE_ROOT', 'AGENTSTACK_MANAGED_AGENTS_FILE'):
        raw_value = os.environ.get(key)
        if raw_value:
            value = Path(raw_value)
            if not value.is_absolute():
                raise StageAdmissionError('PLACE_ONLY_EXTERNAL_STATE_REJECTED')
            value = value.resolve(strict=False)
            if value != target and target not in value.parents:
                raise StageAdmissionError('PLACE_ONLY_EXTERNAL_STATE_REJECTED')
    return str(target)


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['--values'] and len(sys.argv) > 2:
            for value in sys.argv[2:]:
                stage_path_text(value, 'PLACE_ONLY_VALUE_INVALID')
        elif sys.argv[1:2] == ['--admit'] and len(sys.argv) == 5:
            print(admit(*sys.argv[2:]))
        else:
            raise SystemExit(64)
    except StageAdmissionError as error:
        print('PLACE_ONLY_STAGE_ADMISSION_REJECTED ' + str(error), file=sys.stderr)
        raise SystemExit(2)
    except (OSError, ValueError, KeyError, RuntimeError):
        # Fixed failure, no raw path/env output. No fallback or directory creation.
        print('PLACE_ONLY_STAGE_ADMISSION_REJECTED', file=sys.stderr)
        raise SystemExit(2)
