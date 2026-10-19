"""Operator-only diagnostic scan: credential bytes stay inside this process."""
from pathlib import Path
import shlex
import subprocess

from mail_lifecycle import read, save, stamp
from runner import base_env


def scan(state, run=subprocess.run):
    state = Path(state)
    config = read(state / 'run-config.json')
    profile = Path(config['profile_home'])
    route = read(profile / 'config.yaml')['providers'][config['provider']]
    result = run(shlex.split(route['key_cmd']), env=base_env(config),
                 capture_output=True, timeout=15)
    secret = result.stdout.rstrip(b'\r\n') if result.returncode == 0 else b''
    report = {'attempt_id': read(state / 'attempt.json')['attempt_id'], 'matches': None,
              'files_scanned': 0, 'errors': 0, 'checked_at': stamp(), 'source': 'operator-private-prefix-scan'}
    if len(secret) >= 8:
        prefix = secret[:8]
        report['matches'] = 0
        for root in (state, profile):
            for path in root.rglob('*'):
                if (not path.is_file() or path.is_symlink() or path.name.startswith('token-')
                        or path.name == 'owner.pending'):
                    continue  # Owner-token material is handled only by product helpers.
                try:
                    report['matches'] += path.read_bytes().count(prefix)
                    report['files_scanned'] += 1
                except OSError:
                    report['errors'] += 1
        if report['errors']:
            report['matches'] = None
    save(state / 'diagnostic-scan.json', report)
    return report  # No values, prefixes, matching paths, contents, or stderr.
