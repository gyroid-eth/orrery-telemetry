"""Managed-spawn adapter over the existing dsh lifecycle CLIs.

Ordinary interrupted starts can leave grants or a live pane. Preserve evidence
and use the product's stop/release/retire path rather than treating exit 0 as success.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
CAPABILITIES = {'interactive': True, 'scope': 'private-workspace', 'context_source': 'unreported'}


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream)
        stream.write('\n')


def step(state, script, action=None, *, run, extra=(), background=False):
    argv = [sys.executable, '-B', str(HERE / script)]
    if action:
        argv.append(action)
    argv += ['--state', str(state), *map(str, extra)]
    return run(argv, timeout=150 if script == 'preflight.py' else 65,
               check=True, background=background)


def prepare(*, state, config_file, backend_id, task, worktree, parent, run):
    state = Path(state)
    if state.exists():
        raise ValueError('Existing state cannot register again')
    config = read(config_file)
    config.update(backend_id=backend_id, task=task, worktree=str(worktree), parent=parent)
    path = state.parent / 'dsh-input.json'
    save(path, config)
    step(state, 'launch.py', 'init', run=run, extra=('--config', path))
    step(state, 'mail_lifecycle.py', 'register', run=run)
    step(state, 'preflight.py', run=run)
    return status(state)


def start(state, *, run):
    save(Path(state) / 'start-intent.json', {'phase': 'starting'})
    return step(state, 'launch.py', 'start', run=run, background=True)


def evaluate(state, runtime=False):
    spec = importlib.util.spec_from_file_location('_dsh_completion', HERE / 'completion.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (module.evaluate_runtime if runtime else module.evaluate_task)(state)


def status(state, *, persist=False):
    state = Path(state)
    attempt, task = read(state / 'attempt.json', {}), read(state / 'task.json', {})
    process, proof = read(state / 'runtime-process.json', {}), read(state / 'preflight-complete.json', {})
    try:
        events = [json.loads(line) for line in (state / 'events.jsonl').read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        events = []
    aid, tid = attempt.get('attempt_id'), task.get('task_id')
    same = [r for r in events if aid and r.get('attempt_id') == aid]
    loaded = any(r.get('phase') == 'plugin-ready' and process.get('attempt_id') == aid and type(process.get('pid')) is int
                 and r.get('process_id') == process['pid'] for r in same)
    ready = bool(loaded and proof.get('attempt_id') == aid and proof.get('ok') is True
                 and proof.get('schema') is True and proof.get('backend') is True)
    task_result, runtime_result = evaluate(state), evaluate(state, runtime=True)
    finalized = any(r.get('phase') == 'task-report-finalized' and r.get('task_id') == tid for r in same)
    if ((task_result['success'] is True and task_result.get('provider_usage_verified') is True
            or finalized and task_result['success'] is False)
            and isinstance(tid, str) and Path(tid).name == tid
            and tid not in ('.', '..')):
        reports = state / 'task-reports'
        reports.mkdir(mode=0o700, exist_ok=True)
        report = reports / (tid + '.json')
        previous = read(report, {})
        if not report.exists() or (previous.get('success') is True and previous.get('provider_usage_verified') is not True):
            # Trusted saved evidence, not an LLM completion claim. Retain the
            # report when later interactive turns edit those same artifacts.
            artifact_rows = [r for r in same if r.get('task_id') == tid and r.get('phase') == 'artifact-written']
            task_result = {**task_result, 'agent_name': attempt.get('agent_name'),
                           'artifacts': artifact_rows,
                           'mail_receipts': [r for r in same if r.get('task_id') == tid and r.get('phase') == 'mail-sent'],
                           'source': 'managed-task-evaluator'}
            save(report, task_result)
    if persist:
        save(state / 'task-completion.json', task_result)
        save(state / 'runtime-completion.json', runtime_result)
    runtime_exit = read(state / 'runtime-exit.json', {})
    terminal = runtime_exit.get('attempt_id') == aid and bool(aid)
    turns = [r for r in same if r.get('phase') in ('task-start', 'turn-start', 'turn-end')]
    last = turns[-1] if turns else None
    latest_turn = next((r for r in reversed(turns) if r['phase'] == 'turn-end'), None)
    activity = 'exited' if terminal else 'idle' if last and last['phase'] == 'turn-end' else 'thinking' if last else 'starting'
    return {'ready': ready, 'terminal': terminal, 'agent_name': attempt.get('agent_name'),
            'runtime_attempt_id': aid, 'task_success': task_result['success'],
            'task_evaluation': task_result.get('evaluation'), 'activity': activity,
            'latest_turn': {'id': latest_turn.get('turn'), 'completed': latest_turn.get('reason_kind') == 'completed',
                            'at': latest_turn.get('at')} if latest_turn else None,
            'runtime_success': runtime_result['success'], 'state_path': str(state),
            'cleaned': bool(aid and read(state / 'cleaned.json', {}).get('attempt_id') == aid)}


def finalize(state, *, parent_call, run):
    state = Path(state)
    if not (state / 'attempt.json').exists():
        return {'runtime_success': None, 'cleanup_pending': True}
    step(state, 'launch.py', 'stop' if (state / 'runtime-process.json').exists() else 'abandon', run=run)
    identity = read(state / 'mail-identity.json')
    if not identity:
        return {**status(state), 'cleanup_pending': (state / 'registration-intent.json').exists()}
    if (state / 'reservation-intent.json').exists() or (state / 'reservation-ledger.json').exists():
        step(state, 'launch.py', 'release', run=run)
    step(state, 'mail_lifecycle.py', 'retire', run=run)
    if parent_call is None:
        return {**status(state), 'cleanup_pending': True}
    capture = parent_call('whois', {'name': identity['agent_name']})
    save(state / 'parent-whois.json', capture)
    step(state, 'mail_lifecycle.py', 'confirm-retire', run=run,
         extra=('--input', state / 'parent-whois.json'))
    if (state / 'parent-inbox.json').exists():
        step(state, 'mail_lifecycle.py', 'record-mail', run=run,
             extra=('--input', state / 'parent-inbox.json'))
    return status(state, persist=True)


def clean(state, *, run):
    step(state, 'launch.py', 'clean', run=run)
    return status(state)
