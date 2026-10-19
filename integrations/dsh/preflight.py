"""Keyless full preflight and saved-input gate. No credential lookup.

Schema availability is not backend authentication. Ordinary outages and stale
configuration must stop before a key lookup or a model request.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent


def read(file):
    return json.loads(Path(file).read_text())


def write(file, value):
    with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def environment(config):
    return {'HOME': config['home'], 'PATH': str(Path(config['node']).parent) + ':/usr/bin:/bin',
            'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TERM': 'xterm-256color'}


def run_probe(state, action='probe', fixture=''):
    state = Path(state).resolve()
    config = read(state / 'run-config.json')
    result = subprocess.run([config['node'], str(HERE / 'mail_probe.mjs'), str(state), action, fixture],
        env=environment(config), cwd=config['worktree'], capture_output=True, timeout=30)
    name = f'mail-probe-{fixture}.json' if fixture else 'mail-probe.json'
    report = read(state / name)
    if fixture:
        if result.returncode != 2 or report.get('backend') is not False or report.get('ok') is not False:
            raise ValueError('Mail negative fixture did not stop')
        if report.get('schema') is not (fixture == 'backend-unreachable'):
            raise ValueError('Mail negative fixture did not distinguish schema from backend')
    elif result.returncode != 0 or report.get('schema') is not True or report.get('backend') is not True or report.get('ok') is not True:
        raise ValueError('Authenticated Mail preflight did not pass')
    return report


def inputs(state):
    state = Path(state).resolve()
    config = read(state / 'run-config.json')
    files = [state / name for name in ('run-config.json', 'attempt.json', 'task.json', 'mail-identity.json',
             'mcp.json', 'mail.patch.json', 'reservations.json', 'selected.patch.json',
             'dsh/profiles/orrery-managed/package.json', 'dsh/profiles/orrery-managed/cordis.patch.yml')]
    files += [Path(config[key]) for key in ('node', 'backend_file', 'patch_receipt', 'proxy_python')]
    files += list(HERE.glob('*.py')) + list(HERE.glob('*.mjs')) + list(HERE.glob('*.yml')) + [HERE / 'tui-managed.patch']
    runtime = Path(config['runtime']) / 'node_modules'
    for package in ('@deepseek-ai/dsh', '@deepseek-harness-tui/dsh-tui'):
        files.append(runtime / package / 'package.json')
    receipt = read(config['patch_receipt'])
    files += [runtime / '@deepseek-harness-tui/dsh-tui' / row['file'] for row in receipt['changes']]
    product = Path(config['product_root'])
    files += [product / 'hooks/child_tools.py', product / 'hooks/cleanup-child-agent.sh',
              product / 'integrations/codex_app/plugin/scripts/run-mcp.sh']
    files += list((product / 'integrations/codex_app/src/agentstack_codex_app').rglob('*.py'))
    # Only configured, known code/configuration files. Never open token_file,
    # connection.mail_env or the Keychain; don't crawl product runtime state.
    return [{'file': str(p.absolute()), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(set(files))]


def verify_reservations(state, *, seconds=630):
    state = Path(state)
    attempt, task, reservation = [read(state / file) for file in ('attempt.json', 'task.json', 'reservations.json')]
    if (attempt['phase'] != 'run' or task['attempt_id'] != attempt['attempt_id'] or
        reservation.get('complete') is not True or reservation['attempt_id'] != attempt['attempt_id'] or reservation['agent_name'] != attempt['agent_name'] or
        task.get('reservation_source') != 'product-bound-proxy' or
        task.get('reservation_receipts') != reservation['receipts'] or not reservation['receipts']):
        raise ValueError('Actual task reservation binding required')
    wanted = set(task['allowed_paths'])
    for row in reservation['receipts']:
        expiry = datetime.fromisoformat(row['expires_ts'].replace('Z', '+00:00')).timestamp()
        if type(row['id']) is not int or row['id'] <= 0 or row['owner'] != attempt['agent_name'] or expiry <= time.time() + seconds:
            raise ValueError('Reservation scope/owner/remaining lifetime invalid')
    if {row['path_pattern'] for row in reservation['receipts']} != wanted:
        raise ValueError('Reservation scope does not match the task')


def verify(state):
    state = Path(state)
    if (state / 'run-started.json').exists():
        raise ValueError('A real run has already been attempted; no automatic retry')
    proof = read(state / 'preflight-complete.json')
    attempt = read(state / 'attempt.json')
    if proof.get('ok') is not True or proof.get('attempt_id') != attempt['attempt_id'] or proof.get('inputs') != inputs(state):
        raise ValueError('Full preflight proof missing, stale, or input changed')
    verify_reservations(state)


def full(state):
    from launch import bind_run
    state = Path(state).resolve()
    if (state / 'run-started.json').exists():
        raise ValueError('Do not preflight over a started attempt')
    config, attempt, task = bind_run(state, require_reservations=False)
    if 'DEEPSEEK_API_KEY' in os.environ:
        raise ValueError('Keyless preflight must not inherit a model key')
    # Discard only the previous proof. Never preserve a success after failure.
    (state / 'preflight-complete.json').unlink(missing_ok=True)
    run_probe(state)
    run_probe(state, fixture='backend-unreachable')
    run_probe(state, fixture='process-unreachable')
    if not (state / 'reservations.json').exists():
        run_probe(state, action='reserve')
    bind_run(state)
    verify_reservations(state)
    log = state / 'preflight.tty'
    result = subprocess.run(['/usr/bin/script', '-q', str(log), sys.executable,
                             str(HERE / 'launch.py'), 'assemble', '--state', str(state)],
        env=environment(config), cwd=config['worktree'], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=65)
    if log.exists():
        log.chmod(0o600)
    audit = [json.loads(line) for line in (state / 'audit.jsonl').read_text().splitlines()]
    events = [json.loads(line) for line in (state / 'events.jsonl').read_text().splitlines()]
    expected = {'read', 'write', 'mcp__orrery_mail__send_message'}
    ready = any(row.get('phase') == 'plugin-ready' and set(row.get('tools', [])) == expected and row.get('retry_count') == 0 for row in events)
    captured = any(row.get('phase') == 'offline-capture' and row.get('forwarded') == 0 for row in audit)
    if not ready or not captured or any(row.get('phase') == 'provider-request-start' for row in audit):
        raise ValueError('Real managed tool assembly did not stop before provider')
    exit_receipt = read(state / 'runtime-exit.json')
    if exit_receipt.get('exit_code') != 73 or exit_receipt.get('group_absent') is not True or exit_receipt.get('group_check') != 'observed':
        raise ValueError('Preflight runtime did not stop and disappear as expected')
    # Keep preflight sessions and audit separate from the first real task's proof.
    evidence = state / 'preflight-evidence'
    evidence.mkdir(mode=0o700, exist_ok=True)
    for name in ('audit.jsonl', 'events.jsonl', 'runtime-exit.json', 'runtime-process.json', 'ui-mounted.json'):
        source = state / name
        if source.exists():
            source.replace(evidence / name)
    proof = {'ok': True, 'attempt_id': attempt['attempt_id'], 'agent_name': attempt['agent_name'],
             'schema': True, 'backend': True, 'negative_backend': True, 'negative_process': True,
             'actual_tools': sorted(expected), 'retry_max': 0, 'forwarded_requests': 0,
             'checked_at': datetime.now(timezone.utc).isoformat(), 'inputs': inputs(state)}
    write(state / 'preflight-complete.json', proof)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    try:
        if args.verify_only:
            verify(args.state)
        else:
            full(args.state)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit('Preflight failed; no credential lookup performed; preserve state')
    print('Keyless full preflight passed' if not args.verify_only else 'Saved preflight inputs match')


if __name__ == '__main__':
    main()
