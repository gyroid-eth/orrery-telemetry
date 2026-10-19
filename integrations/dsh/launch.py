"""Offline TUI spike runner. No identity registration or credential lookup.

Ordinary inherited configuration can touch live files or leak credentials;
use an explicit profile, private state and only the dummy key in this phase.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from launch_plan import build_launch


def write_json(file, value):
    file.write_text(json.dumps(value, indent=2) + '\n')


def prepare(args):
    state = Path(args.state).resolve()
    if state.exists():
        raise SystemExit('State already exists; preserve prior attempt')
    runtime = Path(args.runtime).resolve()
    for package, expected in (('@deepseek-ai/dsh', '0.2.1-rc.2'),
                              ('@deepseek-harness-tui/dsh-tui', '0.16.1')):
        actual = json.loads((runtime / 'node_modules' / package / 'package.json').read_text())['version']
        if actual != expected:
            raise SystemExit('Pinned runtime package mismatch: ' + package)
    integration = Path(__file__).resolve().parent
    node = Path(args.node).resolve()
    version = subprocess.check_output([str(node), '--version'], text=True).strip()
    attempt_id = str(uuid.uuid4())
    plan = build_launch(node=str(node), node_version=version, runtime=str(runtime), state=str(state),
                        worktree=str(Path(args.worktree).resolve()), integration=str(integration),
                        backend_file=str(Path(args.backend_file).resolve()), backend_id=args.backend,
                        attempt_id=attempt_id, home=os.environ['HOME'],
                        patch_receipt=str(Path(args.patch_receipt).resolve()))
    backend = json.loads(Path(args.backend_file).read_text())['backends'][args.backend]
    if not backend['base_url'] or not backend['model']:
        raise SystemExit('Explicit synthetic backend configuration required')
    if not Path(plan['cwd']).is_dir():
        raise SystemExit('Existing private worktree required')
    state.mkdir(mode=0o700)
    attempt = {'attempt_id': attempt_id, 'phase': 'spike', 'agent_name': 'OfflineFixture',
               'started_at_ns': time.time_ns(), 'backend_id': args.backend,
               'node': {'path': str(node), 'version': version, 'sha256': hashlib.sha256(node.read_bytes()).hexdigest()},
               'dsh_version': '0.2.1-rc.2', 'tui_version': '0.16.1'}
    task = {'task_id': str(uuid.uuid4()), 'attempt_id': attempt_id, 'worktree': plan['cwd'],
            'allowed_paths': ['**'], 'reservation_source': 'offline-fixture',
            'reservation_receipts': [{'id': 1, 'owner': attempt['agent_name'], 'path_pattern': '**',
                                      'expires_ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()+3600))}],
            'expected': [], 'report_to': 'OfflineParent'}
    if args.guard_probe:
        (Path(plan['cwd']) / 'probe').mkdir(exist_ok=True)
        task.update(spike_probe=True, allowed_paths=['probe/**'], reservation_receipts=[
            {**task['reservation_receipts'][0], 'path_pattern': 'probe/ok.txt'},
            {'id': 2, 'owner': attempt['agent_name'], 'path_pattern': 'probe/expired.txt',
             'expires_ts': '2000-01-01T00:00:00Z'},
        ])
    write_json(state / 'attempt.json', attempt)
    write_json(state / 'task.json', task)
    profile = state / 'dsh/profiles/orrery-managed'
    profile.mkdir(parents=True)
    write_json(profile / 'package.json', {'name': 'orrery-managed-spike', 'private': True,
               'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-harness-tui/dsh-tui']}}})
    (profile / 'node_modules').symlink_to(runtime / 'node_modules', target_is_directory=True)
    (profile / 'cordis.patch.yml').write_text('[]\n')
    patch = [
        {'id': 'llm-deepseek', 'config': {'apiKeyEnv': 'DEEPSEEK_API_KEY', 'baseURL': backend['base_url'],
          'reasoningEffort': backend['effort'], 'maxTokens': backend['max_output_tokens'],
          'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
          **({'models': backend['model_definition']} if backend.get('model_definition') else {})}},
        {'id': 'agent-default-model', 'config': {'provider': 'deepseek-official', 'model': backend['model'],
                                               'reasoningEffort': backend['effort']}},
        {'id': 'dsh-tui-auth', 'disabled': True},
        {'id': 'dsh-tui-webserver', 'disabled': True},
        {'id': 'dsh-tui-cordis-host-runner', 'disabled': True},
        {'id': 'dsh-tui-code-runtime', 'disabled': True},
        {'id': 'working-activity', 'disabled': True},
        {'insert': [
            {'id': 'orrery-managed', 'name': str(integration / 'managed_plugin.mjs')},
            {'id': 'preset-orrery-managed', 'name': '@deepseek-ai/dsh-agent-preset',
             'config': {'id': 'orrery-managed', 'plugins': []}}]},
        {'id': 'dsh-tui', 'config': {'provider': 'deepseek-official', 'model': backend['model'],
          'cwd': plan['cwd'], 'preset': 'orrery-managed', 'effort': backend['effort'], 'launchpad': False,
          'fullscreen': True, 'terminalImages': False, 'activity': False, 'recapOnOpen': False}},
    ]
    write_json(state / 'selected.patch.json', patch)
    (state / 'tui').mkdir()
    plan['env']['DEEPSEEK_API_KEY'] = 'offline-dummy'
    return state, plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('node', 'runtime', 'state', 'worktree', 'backend-file', 'patch-receipt'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--backend', choices=['deepseek', 'strata'], default='strata')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--guard-probe', action='store_true', help='invoke actual read/write registry in the offline fixture')
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    state, plan = prepare(args)
    if args.prepare_only:
        return
    child = subprocess.Popen(plan['argv'], cwd=plan['cwd'], env=plan['env'], start_new_session=True)
    write_json(state / 'runtime-process.json', {'pid': child.pid, 'pgid': child.pid,
               'attempt_id': plan['env']['ORRERY_DSH_ATTEMPT_ID']})
    stopped_by = 'failure'
    try:
        child.wait(timeout=args.seconds)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        stopped_by = 'limit'
    finally:
        def group_absent():
            try:
                os.killpg(child.pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return None
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while group_absent() is False and time.monotonic() < deadline:
            child.poll()
            time.sleep(0.1)
        if group_absent() is False:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        absent = group_absent()
        write_json(state / 'runtime-exit.json', {'exit_code': child.returncode,
                   'signal': -child.returncode if child.returncode < 0 else None,
                   'group_absent': absent is True,
                   'group_check': 'unknown' if absent is None else 'observed', 'stopped_by': stopped_by})
    mounted = state / 'ui-mounted.json'
    events = [json.loads(line) for line in (state / 'events.jsonl').read_text().splitlines()] if (state / 'events.jsonl').is_file() else []
    audit = [json.loads(line) for line in (state / 'audit.jsonl').read_text().splitlines()]
    ready = any(e.get('phase') == 'plugin-ready' and e.get('tools') == ['read', 'write'] for e in events)
    probed = not args.guard_probe or any(e.get('phase') == 'spike-guard-probe' and e.get('passed') is True for e in events)
    bad = any(e.get('phase', '').startswith('blocked-') or e.get('phase') in ('runtime-fatal', 'limit-reached') for e in audit)
    exited = child.returncode == 0 or (stopped_by == 'limit' and child.returncode in (-signal.SIGTERM, -signal.SIGKILL))
    if not mounted.exists() or not json.loads(mounted.read_text()).get('mounted') or absent is not True or not ready or not probed or bad or not exited:
        raise SystemExit(86)


if __name__ == '__main__':
    main()
