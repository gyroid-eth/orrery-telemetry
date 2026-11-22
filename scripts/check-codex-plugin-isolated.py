#!/usr/bin/env python3
"""Check local plugin placement and scoped trust, without model/thread/login calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

from codex_plugin_trust import apply_trust, atomic_json, capture_plan, payload_digest

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-binary', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout-seconds', type=int, default=120)
    parser.add_argument('--network-isolated', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.network_isolated:
        if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists():
            profile = '(version 1) (allow default) (deny network*)'
            try:
                allowed = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, '/usr/bin/true'],
                                         capture_output=True, timeout=5).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                allowed = False
            if allowed:
                return subprocess.call(['/usr/bin/sandbox-exec', '-p', profile, sys.executable,
                                        str(Path(__file__).resolve()), *sys.argv[1:], '--network-isolated'])
        unshare = shutil.which('unshare')
        if unshare:
            try:
                allowed = subprocess.run([unshare, '-Urn', 'true'], capture_output=True, timeout=5).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                allowed = False
            if allowed:
                return subprocess.call([unshare, '-Urn', sys.executable, str(Path(__file__).resolve()),
                                        *sys.argv[1:], '--network-isolated'])
    output = Path(args.output).resolve()
    binary = str(Path(args.codex_binary).expanduser().resolve())
    fixture = Path(tempfile.mkdtemp(prefix='orrery-plugin-', dir='/tmp'))
    home, codex_home, cwd = fixture/'home', fixture/'codex', fixture/'project'
    for path in (home, codex_home, cwd): path.mkdir()
    environment = {'HOME': str(home), 'CODEX_HOME': str(codex_home),
                   'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
                   'TMPDIR': str(fixture), 'RUST_LOG': 'warn',
                   'XDG_CONFIG_HOME': str(home/'.config'), 'XDG_CACHE_HOME': str(home/'.cache'),
                   'XDG_DATA_HOME': str(home/'.local/share')}
    os.environ.clear(); os.environ.update(environment)
    deadline = time.monotonic()+args.timeout_seconds
    report = {'schema_version': 1, 'fixture': str(fixture), 'binary': binary,
              'network_blocked': args.network_isolated,
              'network_note': 'network namespace/profile' if args.network_isolated else
                              'network not blocked; only local plugin commands and config/hook RPC; no model/thread/login',
              'phase': 'version', 'model_thread_login_requested': False,
              'hook_trust': 'unknown', 'history': 'unobserved'}

    def alarm(signum, frame): raise TimeoutError('overall timeout')
    signal.signal(signal.SIGALRM, alarm); signal.alarm(args.timeout_seconds)

    def run(argv):
        p = subprocess.Popen(argv, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True, text=True)
        try:
            stdout, stderr = p.communicate(timeout=min(20, max(.1, deadline-time.monotonic())))
        except BaseException:
            if p.poll() is None: os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise
        (fixture/f'command-{report["phase"]}.log').write_text(stdout+stderr)
        if p.returncode: raise RuntimeError(f'{report["phase"]} command exit {p.returncode}')
        return stdout

    status = 1
    try:
        report['version'] = run([binary, '--version']).strip()
        report['phase'] = 'snapshot'
        market = fixture/'marketplace'
        run([sys.executable, str(ROOT/'scripts/build-codex-app-marketplace.py'),
             str(ROOT/'integrations/codex_app'), str(market), '--marketplace-name', 'agentstack-local'])
        report['phase'] = 'marketplace'
        run([binary, 'plugin', 'marketplace', 'add', str(market), '--json'])
        report['phase'] = 'plugin'
        installed = json.loads(run([binary, 'plugin', 'add', 'agentstack-codex-app@agentstack-local', '--json']))
        selected = Path(installed['installedPath']).resolve()
        if not selected.is_relative_to(codex_home.resolve()): raise RuntimeError('selected cache escaped isolated home')
        expected, observed = payload_digest(market/'plugins/agentstack-codex-app'), payload_digest(selected)
        report.update(plugin_id=installed['pluginId'], selected_path=str(selected),
                      expected_digest=expected, observed_digest=observed, placement_verified=expected == observed)
        if expected != observed: raise RuntimeError('selected cache payload mismatch')
        config = codex_home/'config.toml'
        config.write_text(config.read_text()+'\n[hooks.state."unrelated-fixture"]\nenabled = false\ntrusted_hash = "sha256:sentinel"\n')
        report['phase'] = 'hook_plan'
        plan = capture_plan(binary, codex_home, cwd, selected, installed['pluginId'])
        atomic_json(fixture/'review-plan.json', plan)
        # This consent is for this synthetic fixture only. Production callers
        # must obtain their existing human approval before marking a plan.
        plan['approved'] = True
        atomic_json(fixture/'approved-plan.json', plan)
        report['phase'] = 'hook_trust'
        result = apply_trust(plan)
        atomic_json(fixture/'trust-result.json', result)
        report.update(result)
        state = tomllib.loads(config.read_text())['hooks']['state']
        report['unrelated_state_preserved'] = state['unrelated-fixture'] == {'enabled': False, 'trusted_hash': 'sha256:sentinel'}
        report['auth_present'] = (codex_home/'auth.json').exists()
        report['effective_timeouts'] = sorted({h['timeoutSec'] for h in plan['hooks']})
        if not report['unrelated_state_preserved'] or report['auth_present']: raise RuntimeError('fixture preservation failed')
        report['phase'] = 'complete'; status = 0
    except Exception as exc:
        report['error'] = str(exc); report['hook_trust'] = 'needs_review'
        report['next_action'] = 'review ORRERY hooks in /hooks; Resume remains unobserved'
    finally:
        signal.alarm(0)
        report['exit_code'] = status
        atomic_json(output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
