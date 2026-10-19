"""Thin CLI adapter for the managed-spawn controller; no registration of its own.

The controller supplies run(argv, *, timeout, check, background) and its existing
parent_call(tool, args). This module never creates a Mail connection or polls one.
"""
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
CAPABILITIES = {'tools': ['read_file', 'write_file', 'mcp__orrery_mail__send_message'],
                'scope': 'relative-files', 'expected_checks': ['contains', 'sha256'],
                'reservation_guard': 'synchronous-pre-tool', 'helper_models': ['same'],
                'context_source': 'unreported'}
FAILURE_CODES = {'profile_link_unapproved', 'profile_path_mismatch', 'profile_symlink',
                 'current_profile_escape', 'process_failed', 'filesystem_failed',
                 'missing_state', 'runtime_failed', 'validation_failed'}


class StepFailure(RuntimeError):
    def __init__(self, action, reason):
        self.managed_step = action
        self.managed_reason = reason if isinstance(reason, str) and reason in FAILURE_CODES else 'step_failed'
        super().__init__('Hermes step failed: ' + action + ' (' + self.managed_reason + ')')


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:
        json.dump(value, handle)
        handle.write('\n')


def step(state, action, *, run, config_file=None, profile=None, input_file=None, background=False):
    state = Path(state)
    config = read(state / 'run-config.json', {})
    argv = [config.get('runner_python', sys.executable), '-B', str(HERE / 'runner.py'), action, '--state', str(state)]
    for flag, value in (('--config', config_file), ('--profile', profile), ('--input', input_file)):
        if value is not None:
            argv += [flag, str(value)]
    try:
        result = run(argv, timeout=120, check=False, background=background)
    except Exception:
        raise StepFailure(action, 'process_failed') from None
    if background:
        return result
    if result.returncode not in ((0, 1, 2) if action in ('complete', 'complete-runtime') else (0,)):
        try:
            reason = json.loads(result.stderr).get('error_code')
        except (TypeError, ValueError, AttributeError):
            reason = None
        raise StepFailure(action, reason)
    try:
        return json.loads(result.stdout)['result']
    except (TypeError, KeyError, ValueError):
        raise StepFailure(action, 'validation_failed') from None


def prepare(*, state, config_file, backend_id, task, worktree, parent, run):
    state = Path(state).resolve()
    if state.exists():
        raise ValueError('Existing attempt must be resumed through status, never registered again')
    config = read(config_file)
    if backend_id not in ('strata', 'deepseek') or backend_id not in config['profiles']:
        raise ValueError('Configured managed backend required')
    config.update(worktree=str(Path(worktree).resolve()), parent=parent, credential_mode='owned-once')
    inputs = state.parent / (state.name + '-hermes-input')
    inputs.mkdir(mode=0o700, parents=True, exist_ok=False)
    config_path, task_path = inputs / 'run-config.json', inputs / 'task.json'
    save(config_path, config)
    save(task_path, task)
    step(state, 'init-task', run=run, config_file=config_path, profile=backend_id, input_file=task_path)
    for action in ('preregister', 'configure', 'probe', 'reserve', 'preflight'):
        step(state, action, run=run)
    return status(state)


def start(state, *, run):
    # A short-lived wrapper starts a continuing owned tmux supervisor. The
    # controller must derive readiness/termination from status, never this exit.
    return step(state, 'start', run=run, background=True)


def status(state):
    state = Path(state)
    attempt, task = read(state / 'attempt.json', {}), read(state / 'task.json', {})
    identity, probe = read(state / 'mail-identity.json', {}), read(state / 'mail-probe.json', {})
    preflight, process = read(state / 'preflight-complete.json', {}), read(state / 'runtime-process.json', {})
    events = []
    if (state / 'events.jsonl').exists():
        events = [json.loads(line) for line in (state / 'events.jsonl').read_text().splitlines() if line.strip()]
    same = [row for row in events if row.get('attempt_id') == attempt.get('attempt_id')
            and row.get('task_id') == task.get('task_id')]
    loaded = any(row.get('phase') == 'plugin-ready' and type(process.get('pid')) is int
                 and row.get('process_id') == process['pid'] for row in same)
    ready = bool(attempt.get('attempt_id') and task.get('task_id') and loaded
                 and probe.get('schema') is True and probe.get('backend') is True
                 and probe.get('ok') is True and probe.get('agent_name') == identity.get('agent_name')
                 and preflight.get('attempt_id') == attempt.get('attempt_id') and preflight.get('guard_loaded') is True)
    if read(state / 'run-config.json', {}).get('credential_mode') == 'owned-once':
        credential = read(state / 'credential-read.json', {})
        ready = ready and credential.get('status') == 'available' and credential.get('attempt_id') == attempt.get('attempt_id') \
            and credential.get('process_id') == process.get('pid')
    runtime = read(state / 'runtime-exit.json')
    task_result, runtime_result = read(state / 'task-completion.json', {}), read(state / 'runtime-completion.json', {})
    current = lambda row: row.get('success') if row.get('attempt_id') == attempt.get('attempt_id') and type(row.get('success')) is bool else None
    # turn-end precedes Hermes's hard-exit diagnostic scan. Let the supervisor
    # observe exit before the controller stops it, preserving that final scan.
    return {'ready': ready, 'terminal': runtime is not None,
            'task_success': current(task_result), 'runtime_success': current(runtime_result),
            'agent_name': identity.get('agent_name'), 'runtime_attempt_id': attempt.get('attempt_id'),
            'state_path': str(state), 'cleaned': read(state / 'cleaned.json', {}).get('attempt_id') == attempt.get('attempt_id')
               and bool(attempt.get('attempt_id'))}


def finalize(state, *, parent_call, run):
    state = Path(state)
    identity = read(state / 'mail-identity.json')
    if not identity:
        return {**status(state), 'cleanup_pending': (state / 'registration-intent.json').exists()}
    if (state / 'start-intent.json').exists():
        step(state, 'stop', run=run)
    errors = []
    config = read(state / 'run-config.json')
    if (state / 'credential-read.json').exists() or (state / 'runtime-process.json').exists():
        try:
            step(state, 'scan-diagnostics', run=run)
        except Exception:
            errors.append('diagnostics_unavailable')
    reservations = read(state / 'reservations.json', {})
    release = read(state / 'release-receipt.json', {})
    attempt = read(state / 'attempt.json', {})
    ids = {row['id'] for row in reservations.get('receipts', [])}
    released = (release.get('confirmed') is True and release.get('attempt_id') == attempt.get('attempt_id')
                and release.get('agent_name') == identity['agent_name'] and release.get('source') == 'product-bound-proxy'
                and set(release.get('reservation_ids', [])) == ids and set(release.get('released_ids', [])) == ids)
    if reservations.get('receipts') and not released:
        step(state, 'release', run=run)
    # Real grants are required even when reserve was partial and task binding
    # never completed. Cleanup may not erase an unreleased grant.
    step(state, 'retire', run=run)
    if parent_call is None:
        errors.append('parent_whois_unavailable')
    else:
        capture = parent_call('whois', {'name': identity['agent_name']})
        save(state / 'parent-whois.json', capture)
        step(state, 'confirm-retire', run=run, input_file=state / 'parent-whois.json')
    # D2 remains optional unless the existing parent context explicitly offers a
    # canonical saved capture. Never fetch the parent's inbox through a child.
    if (state / 'parent-inbox.json').exists():
        step(state, 'record-mail', run=run, input_file=state / 'parent-inbox.json')
    step(state, 'complete', run=run)
    step(state, 'complete-runtime', run=run)
    result = {**status(state), 'errors': errors}
    save(state / 'managed-finalization.json', result)
    return result


def clean(state, *, run):
    return step(state, 'clean', run=run)


def close_unstarted(state, *, run):
    proof = step(state, 'complete-runtime', run=run)
    if proof.get('success') is not True or proof.get('runtime_conclusion') != 'not-started-retired':
        raise ValueError('Only confirmed unstarted reservation-free attempts can be closed')
    clean(state, run=run)
    return status(state)
