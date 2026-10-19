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
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from launch_plan import build_launch


def write_json(file, value):
    file.write_text(json.dumps(value, indent=2) + '\n')


def group_absent(pgid):
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return None


def stop_owned_group(child, grace=5):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.poll()
        return group_absent(child.pid)
    except PermissionError:
        return None
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        child.poll()
        observed = group_absent(child.pid)
        if observed is True or (observed is None and child.returncode is not None):
            return observed
        time.sleep(0.05)
    # A still-live owned leader may be killed even when signal-0 observation
    # is unavailable. That never turns an unknown group into a confirmed one.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return None
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return None
    return group_absent(child.pid)


class StopRequested(Exception):
    pass


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
          **({'models': [backend['model_definition']]} if backend.get('model_definition') else {})}},
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


def run_config(state):
    return json.loads((Path(state) / 'run-config.json').read_text())


def init_run(config_file, state):
    if os.getloadavg()[0] >= 15:
        raise ValueError('Load is at least 15; postpone new runtime work')
    config = json.loads(Path(config_file).read_text())
    keys = {'node', 'runtime', 'worktree', 'backend_file', 'backend_id', 'patch_receipt',
            'home', 'product_root', 'project_key', 'proxy_python', 'parent', 'connection', 'task'}
    if set(config) != keys or config['home'] != os.environ['HOME']:
        raise ValueError('Explicit non-secret run configuration and original HOME required')
    for key in ('node', 'runtime', 'worktree', 'backend_file', 'patch_receipt', 'home',
                'product_root', 'project_key', 'proxy_python'):
        if not Path(config[key]).is_absolute():
            raise ValueError('Absolute configured paths required')
    work = Path(config['worktree']).resolve()
    if work.is_relative_to(Path(config['project_key']).resolve()):
        raise ValueError('Use a dedicated worktree outside the live project')
    task = config['task']
    if set(task) != {'prompt', 'allowed_paths', 'expected'} or not isinstance(task['prompt'], str) or not task['prompt'].strip():
        raise ValueError('One explicit task with expected artifacts required')
    if not task['allowed_paths'] or not task['expected']:
        raise ValueError('Narrow file scope and expected artifact required')
    for relative in task['allowed_paths']:
        if not isinstance(relative, str) or Path(relative).is_absolute() or any(c in relative for c in '*?[]') or any(part in ('..', '.git') for part in Path(relative).parts):
            raise ValueError('First run accepts literal relative file paths only')
    for expected in task['expected']:
        if expected.get('path') not in task['allowed_paths'] or not ('contains' in expected or 'sha256' in expected):
            raise ValueError('Expected artifact must be in the reserved scope')
    args = argparse.Namespace(node=config['node'], runtime=config['runtime'], state=str(state),
        worktree=config['worktree'], backend_file=config['backend_file'], backend=config['backend_id'],
        patch_receipt=config['patch_receipt'], guard_probe=False)
    state, _ = prepare(args)
    backend = json.loads(Path(config['backend_file']).read_text())['backends'][config['backend_id']]
    config['model'] = backend['model']
    write_json(state / 'run-config.json', config)
    attempt = json.loads((state / 'attempt.json').read_text())
    attempt.update(phase='run', agent_name=None)
    write_json(state / 'attempt.json', attempt)
    manifest = json.loads((state / 'task.json').read_text())
    manifest.update(**task, report_to=config['parent'], reservation_source='product-bound-proxy', reservation_receipts=[])
    write_json(state / 'task.json', manifest)


def bind_run(state, require_reservations=True):
    state = Path(state)
    config = run_config(state)
    attempt = json.loads((state / 'attempt.json').read_text())
    identity = json.loads((state / 'mail-identity.json').read_text())
    if identity['parent'] != config['parent'] or identity['project_key'] != config['project_key'] or identity['program'] != 'dsh':
        raise ValueError('Registered identity does not match the run configuration')
    if attempt['phase'] != 'run' or attempt['agent_name'] not in (None, identity['agent_name']):
        raise ValueError('Attempt identity already differs')
    attempt['agent_name'] = identity['agent_name']
    write_json(state / 'attempt.json', attempt)
    task = json.loads((state / 'task.json').read_text())
    if require_reservations:
        reservation = json.loads((state / 'reservations.json').read_text())
        receipts = reservation['receipts']
        if reservation.get('complete') is not True or reservation['agent_name'] != identity['agent_name'] or reservation['attempt_id'] != attempt['attempt_id'] or not receipts:
            raise ValueError('Real reservation binding missing')
        task['reservation_receipts'] = receipts
        write_json(state / 'task.json', task)
    return config, attempt, task


def plan_run(state, *, offline):
    state = Path(state).resolve()
    config, attempt, task = bind_run(state)
    version = subprocess.check_output([config['node'], '--version'], text=True).strip()
    return build_launch(node=config['node'], node_version=version, runtime=config['runtime'],
        state=str(state), worktree=config['worktree'], integration=str(Path(__file__).resolve().parent),
        backend_file=config['backend_file'], backend_id=config['backend_id'],
        attempt_id=attempt['attempt_id'], home=config['home'], patch_receipt=config['patch_receipt'], offline=offline)


def credential(config):
    # Ordinary unlock prompts/timeouts must not trigger automatic retries.
    if sys.platform != 'darwin':
        raise ValueError('Credential adapter is not implemented for this OS')
    backend = json.loads(Path(config['backend_file']).read_text())['backends'][config['backend_id']]
    ref = backend['keychain']
    if not ref['service'] or not ref['account']:
        raise ValueError('Explicit credential reference required')
    result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-w',
                             '-s', ref['service'], '-a', ref['account']],
        env={'HOME': config['home'], 'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise ValueError('Credential lookup failed; do not retry automatically')
    return result.stdout.decode().rstrip('\n')


def start_pane(state):
    from preflight import verify
    state = Path(state).resolve()
    verify(state)
    config = run_config(state)
    if 'DEEPSEEK_API_KEY' in os.environ:
        raise ValueError('Do not inherit a model key into the launcher/tmux')
    session = 'dsh-' + json.loads((state / 'attempt.json').read_text())['attempt_id'][:8]
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), 'run', '--state', str(state)])
    clean = {'HOME': config['home'], 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8'}
    # Default socket, one new session; do not alter/kill the shared server.
    result = subprocess.run(['/usr/bin/env', 'tmux', 'new-session', '-d', '-P', '-F', '#{pane_id}',
                             '-s', session, '-c', config['worktree'], command],
        env={**clean, 'PATH': os.defpath + ':/opt/homebrew/bin:/usr/local/bin'},
        capture_output=True, text=True, check=True, timeout=15)
    write_json(state / 'owned-pane.json', {'session': session, 'pane_id': result.stdout.strip(),
               'attempt_id': json.loads((state / 'attempt.json').read_text())['attempt_id']})


def verify_owned_pane(state):
    state = Path(state)
    pane = json.loads((state / 'owned-pane.json').read_text())
    attempt = json.loads((state / 'attempt.json').read_text())
    if not os.environ.get('TMUX_PANE') or pane.get('pane_id') != os.environ['TMUX_PANE'] or pane.get('attempt_id') != attempt['attempt_id']:
        raise ValueError('Credential lookup requires this attempt\'s owned pane')


def execute_run(state, *, assemble=False):
    from preflight import verify, run_probe
    state = Path(state).resolve()
    if os.getloadavg()[0] >= 15:
        raise ValueError('Load is at least 15; do not start a runtime')
    plan = plan_run(state, offline=assemble)
    if assemble:
        plan['env']['DEEPSEEK_API_KEY'] = 'offline-dummy'
    else:
        if 'DEEPSEEK_API_KEY' in os.environ:
            raise ValueError('Inherited model key is not the managed credential route')
        verify(state)
        run_probe(state)
        verify(state)
        verify_owned_pane(state)
        # Exclusive marker is made before lookup: even a failed lookup is one attempt.
        with (state / 'run-started.json').open('x') as stream:
            json.dump({'at_ns': time.time_ns()}, stream)
        try:
            plan['env']['DEEPSEEK_API_KEY'] = credential(run_config(state))
        except (OSError, ValueError, subprocess.SubprocessError):
            write_json(state / 'runtime-exit.json', {'exit_code': None, 'signal': None,
                'group_absent': True, 'group_check': 'observed', 'runtime_not_started': True,
                'stopped_by': 'failure'})
            raise
    try:
        supervisor_started = subprocess.check_output(['/bin/ps', '-p', str(os.getpid()), '-o', 'lstart='], text=True).strip()
    except (OSError, subprocess.SubprocessError):
        supervisor_started = None
    try:
        child = subprocess.Popen(plan['argv'], cwd=plan['cwd'], env=plan['env'], start_new_session=True)
    finally:
        plan['env'].pop('DEEPSEEK_API_KEY', None)
    def stop(*_):
        raise StopRequested()
    original_int = signal.getsignal(signal.SIGINT)
    originals = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)}
    reason, seen_turns = 'failure', 0
    deadline = time.monotonic() + (45 if assemble else 600)
    try:
        write_json(state / 'runtime-process.json', {'pid': child.pid, 'pgid': child.pid,
                   'supervisor_pid': os.getpid(), 'supervisor_started': supervisor_started,
                   'attempt_id': plan['env']['ORRERY_DSH_ATTEMPT_ID']})
        while child.poll() is None:
            if time.monotonic() >= deadline:
                reason = 'limit'
                break
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            events = state / 'events.jsonl'
            turns = events.read_text().count('"phase":"turn-end"') if events.exists() else 0
            if not assemble and turns > seen_turns:
                seen_turns = turns
                subprocess.run([sys.executable, str(Path(__file__).with_name('completion.py')), str(state)],
                               env={'HOME': run_config(state)['home'], 'PATH': os.defpath},
                               capture_output=True, timeout=10)
    except (KeyboardInterrupt, StopRequested):
        reason = 'parent'
    finally:
        for sig in (*originals, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        absent = stop_owned_group(child)
        write_json(state / 'runtime-exit.json', {'exit_code': child.returncode,
            'signal': -child.returncode if child.returncode is not None and child.returncode < 0 else None,
            'group_absent': absent is True, 'group_check': 'unknown' if absent is None else 'observed',
            'stopped_by': reason})
        for sig, handler in originals.items():
            signal.signal(sig, handler)
        signal.signal(signal.SIGINT, original_int)
    return child.returncode


def stop_run(state):
    state = Path(state)
    process = json.loads((state / 'runtime-process.json').read_text())
    attempt = json.loads((state / 'attempt.json').read_text())
    if process['attempt_id'] != attempt['attempt_id']:
        raise ValueError('Process attempt mismatch')
    if (state / 'runtime-exit.json').exists():
        receipt = json.loads((state / 'runtime-exit.json').read_text())
        if receipt.get('group_absent') is True and group_absent(process['pgid']) is True:
            return
    # Signal the owned supervisor, which owns TERM/KILL/reaping of its runtime.
    # If its identity cannot be verified, preserve state for an external check.
    observed = subprocess.run(['/bin/ps', '-p', str(process['supervisor_pid']), '-o', 'lstart='],
                              capture_output=True, text=True, timeout=5)
    if observed.returncode or observed.stdout.strip() != process.get('supervisor_started'):
        raise ValueError('Supervisor ownership unavailable; inspect outside before cleanup')
    os.kill(process['supervisor_pid'], signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        receipt_file = state / 'runtime-exit.json'
        if receipt_file.exists():
            receipt = json.loads(receipt_file.read_text())
            if receipt.get('group_absent') is True and group_absent(process['pgid']) is True:
                return
        time.sleep(0.1)
    raise ValueError('Owned runtime disappearance not confirmed')


def clean_run(state):
    state = Path(state)
    attempt = json.loads((state / 'attempt.json').read_text())
    exit_receipt = json.loads((state / 'runtime-exit.json').read_text())
    if exit_receipt.get('group_absent') is not True or exit_receipt.get('group_check') != 'observed':
        raise ValueError('Owned group absence must be confirmed')
    if not exit_receipt.get('runtime_not_started'):
        process = json.loads((state / 'runtime-process.json').read_text())
        if process['attempt_id'] != attempt['attempt_id'] or group_absent(process['pgid']) is not True:
            raise ValueError('Owned group absence changed or is unknown')
    for name, source in [('release-receipt.json', 'product-bound-proxy'), ('retire-receipt.json', 'parent-bound-whois')]:
        if name == 'release-receipt.json' and not (state / name).exists() and exit_receipt.get('runtime_not_started') is True and not (state / 'reservation-intent.json').exists():
            continue
        receipt = json.loads((state / name).read_text())
        if receipt.get('attempt_id') != attempt['attempt_id'] or receipt.get('agent_name') != attempt['agent_name'] or receipt.get('confirmed') is not True or receipt.get('source') != source:
            raise ValueError('Remote release/retire proof missing; retain local state')
        if name == 'retire-receipt.json' and not receipt.get('retired_at'):
            raise ValueError('Canonical whois retirement timestamp required')
    identity = json.loads((state / 'mail-identity.json').read_text())
    token = Path(identity['token_file'])
    if token != state / ('token-' + attempt['agent_name']):
        raise ValueError('Unexpected owner handoff path')
    # Remove only this attempt's generated references and private runtime state.
    for name in ('dsh', 'tui', 'node-cache'):
        folder = state / name
        if folder.exists():
            shutil.rmtree(folder)
    for file in (token, state / 'mcp.json', state / 'mail.patch.json'):
        file.unlink(missing_ok=True)
    write_json(state / 'cleaned.json', {'attempt_id': attempt['attempt_id'], 'at_ns': time.time_ns()})


def abandon_run(state):
    """Mark an unstarted attempt for normal product release/retire, never success."""
    state = Path(state)
    if (state / 'runtime-process.json').exists():
        stop_run(state)
        return
    if (state / 'run-started.json').exists():
        receipt = json.loads((state / 'runtime-exit.json').read_text())
        if receipt.get('runtime_not_started') is not True:
            raise ValueError('Started attempt without ownership evidence; preserve state')
    write_json(state / 'runtime-exit.json', {'exit_code': None, 'signal': None,
        'group_absent': True, 'group_check': 'observed', 'runtime_not_started': True, 'stopped_by': 'failure'})


def run_cli():
    parser = argparse.ArgumentParser(description='Managed run connection; live steps are operator-only')
    parser.add_argument('action', choices=['init', 'bind', 'assemble', 'start', 'run', 'release', 'stop', 'attach', 'state', 'clean', 'abandon'])
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if args.action == 'init':
        init_run(args.config, args.state)
    elif args.action == 'bind':
        bind_run(args.state, require_reservations=False)
    elif args.action in ('assemble', 'run'):
        result = execute_run(args.state, assemble=args.action == 'assemble')
        raise SystemExit(result if result is not None and result >= 0 else 86)
    elif args.action == 'start':
        start_pane(args.state)
    elif args.action == 'release':
        from preflight import run_probe
        run_probe(args.state, action='release')
    elif args.action == 'stop':
        stop_run(args.state)
    elif args.action == 'attach':
        pane = json.loads((args.state / 'owned-pane.json').read_text())
        subprocess.run(['tmux', 'select-pane', '-t', pane['pane_id']], check=True)
        subprocess.run(['tmux', 'attach-session', '-t', pane['session']], check=True)
    elif args.action == 'state':
        print(json.dumps([{'path': str(p.relative_to(args.state)), 'bytes': p.stat().st_size,
                           'mode': oct(p.stat().st_mode & 0o777)}
                          for p in args.state.iterdir() if p.is_file()], indent=2))
    elif args.action == 'clean':
        clean_run(args.state)
    elif args.action == 'abandon':
        abandon_run(args.state)


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
    def stop(*_):
        raise StopRequested()
    originals = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)}
    stopped_by = 'failure'
    try:
        child.wait(timeout=args.seconds)
    except subprocess.TimeoutExpired:
        stopped_by = 'limit'
    except (KeyboardInterrupt, StopRequested):
        stopped_by = 'parent'
    finally:
        for sig in (*originals, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        absent = stop_owned_group(child)
        write_json(state / 'runtime-exit.json', {'exit_code': child.returncode,
                   'signal': -child.returncode if child.returncode is not None and child.returncode < 0 else None,
                   'group_absent': absent is True,
                   'group_check': 'unknown' if absent is None else 'observed', 'stopped_by': stopped_by})
        for sig, handler in originals.items():
            signal.signal(sig, handler)
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
    if len(sys.argv) > 1 and sys.argv[1] in ('init', 'bind', 'assemble', 'start', 'run', 'release', 'stop', 'attach', 'state', 'clean', 'abandon'):
        try:
            run_cli()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise SystemExit('Managed run step failed; preserve state, do not retry automatically')
    else:
        main()
