"""Evaluate saved task evidence; a live TUI need not exit for a task to succeed.

Interruptions and empty exit 0 happen normally and can cause false success.
No credentials, environment lookup, provider or Mail calls are used here.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def _read(path: Path, lines=False):
    try:
        value = [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if lines else json.loads(path.read_text())
        if lines and not all(isinstance(row, dict) for row in value):
            return None
        if not lines and not isinstance(value, dict):
            return None
        return value
    except (OSError, ValueError, UnicodeError):
        return None


def _result(checks, **metadata):
    # Known failures remain failures; absent evidence can never become success.
    success = False if False in checks.values() else None if None in checks.values() else True
    return {**metadata, 'success': success, 'checks': checks,
            'missing': [key for key, value in checks.items() if value is not True]}


def evaluate_task(state_root):
    root = Path(state_root)
    attempt = _read(root / 'attempt.json')
    task = _read(root / 'task.json')
    events = _read(root / 'events.jsonl', lines=True)
    audit = _read(root / 'audit.jsonl', lines=True)
    checks = dict.fromkeys(('attempt_binding', 'completed_turn', 'expected_artifacts', 'provider_audit', 'mail_receipt'))
    aid = attempt.get('attempt_id') if attempt else None
    tid = task.get('task_id') if task else None
    metadata = {'attempt_id': aid, 'task_id': tid, 'runtime_phase': attempt.get('phase') if attempt else None}
    if not attempt or not task or not aid or not tid or attempt.get('phase') not in ('spike', 'run'):
        return _result(checks, **metadata)
    checks['attempt_binding'] = task.get('attempt_id') == aid
    scoped = [row for row in events or [] if row.get('attempt_id') == aid and row.get('task_id') == tid]
    turns = [row for row in scoped if row.get('phase') == 'turn-end']
    checks['completed_turn'] = turns[-1].get('reason_kind') == 'completed' if turns else None
    expected = task.get('expected')
    artifacts = []
    if isinstance(expected, list) and expected and isinstance(task.get('worktree'), str) and isinstance(attempt.get('started_at_ns'), int):
        work = Path(task['worktree']).resolve()
        for spec in expected:
            try:
                rel = Path(spec['path'])
                target = (work / rel).resolve()
                if rel.is_absolute() or not target.is_relative_to(work) or '.git' in rel.parts:
                    artifacts.append(False)
                    continue
                if not target.is_file() or (work / rel).is_symlink():
                    artifacts.append(False)
                    continue
                data = target.read_bytes()
                fresh = target.stat().st_mtime_ns >= attempt['started_at_ns']
                if 'sha256' in spec:
                    artifacts.append(fresh and hashlib.sha256(data).hexdigest() == spec['sha256'])
                elif isinstance(spec.get('contains'), str):
                    artifacts.append(fresh and spec['contains'].encode() in data)
                else:
                    artifacts.append(None)
            except (KeyError, TypeError, OSError, ValueError):
                artifacts.append(None)
        checks['expected_artifacts'] = False if False in artifacts else None if None in artifacts else True
    if audit is not None:
        same_attempt = all(row.get('attempt_id') == aid for row in audit)
        bad = any(row.get('phase', '').startswith('blocked-') or row.get('phase') in ('runtime-fatal', 'limit-reached') for row in audit)
        requests = any(row.get('phase') == 'provider-request-start' for row in audit)
        checks['provider_audit'] = same_attempt and not bad and requests
    if events is not None:
        receipts = [row for row in scoped if row.get('phase') == 'mail-sent']
        parent, child = task.get('report_to'), attempt.get('agent_name')
        if parent and child and attempt['phase'] == 'run':
            matching = [row for row in receipts if (
                row.get('ok') is True and row.get('verified_sender') is True
                and row.get('from') == child and (row.get('to') == parent
                    or isinstance(row.get('to'), list) and parent in row['to'])
                and type(row.get('deliveries')) is int and row['deliveries'] >= 1
                and type(row.get('message_id')) is int and row['message_id'] > 0)]
            received = _read(root / 'parent-mail-receipt.json')
            checks['mail_receipt'] = bool(matching)
            if (root / 'parent-mail-receipt.json').exists():
                checks['mail_receipt'] = bool(matching) and bool(received) and (
                    received.get('source') == 'parent-bound-fetch-inbox' and received.get('attempt_id') == aid
                    and received.get('task_id') == tid and received.get('agent_name') == child
                    and received.get('from') == child and received.get('to') == [parent]
                    and any(row['message_id'] == received.get('message_id') for row in matching))
            # A canonical parent receipt strengthens the server-send proof when
            # available; a malformed or mismatching existing receipt fails.
    return _result(checks, **metadata)


def evaluate_runtime(state_root):
    root = Path(state_root)
    attempt = _read(root / 'attempt.json')
    receipt = _read(root / 'runtime-exit.json')
    gone = receipt.get('group_absent') if receipt else None
    gone = gone if type(gone) is bool else None
    # Cleanup exit 0 is ordinary and does not prove remote release or retirement.
    aid = attempt.get('attempt_id') if attempt else None
    child = attempt.get('agent_name') if attempt else None

    def scoped(name, source):
        value = _read(root / name)
        return value if (aid and child and attempt.get('phase') == 'run' and value
                         and value.get('attempt_id') == aid and value.get('agent_name') == child
                         and value.get('source') == source) else None

    release = scoped('release-receipt.json', 'product-bound-proxy')
    retired = scoped('retire-receipt.json', 'parent-bound-whois')
    task = _read(root / 'task.json')
    released = None
    if release and type(release.get('confirmed')) is bool:
        reservations = task.get('reservation_receipts') if task and task.get('attempt_id') == aid else None
        expected = [row.get('id') for row in reservations if isinstance(row, dict)] if isinstance(reservations, list) else None
        requested, actual = release.get('reservation_ids'), release.get('released_ids')
        valid = lambda ids: isinstance(ids, list) and bool(ids) and all(type(n) is int and n > 0 for n in ids)
        if not release['confirmed']:
            released = False
        elif all(valid(ids) for ids in (expected, requested, actual)):
            released = set(expected) == set(requested) == set(actual)
    retire = 'unknown'
    if retired:
        timestamp = retired.get('retired_at')
        retire = 'confirmed' if (retired.get('confirmed') is True
                                 and isinstance(timestamp, str) and timestamp.strip()) else 'pending'
    checks = {'owned_group_absent': gone, 'reservations_released': released,
              'retire_confirmed': True if retire == 'confirmed' else None}
    return _result(checks, attempt_id=aid,
                   group_absent=gone, reservations_released=released, retire=retire)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state_root', type=Path)
    parser.add_argument('--runtime', action='store_true')
    args = parser.parse_args()
    result = evaluate_runtime(args.state_root) if args.runtime else evaluate_task(args.state_root)
    name = 'runtime-completion.json' if args.runtime else 'task-completion.json'
    (args.state_root / name).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] is True else 1 if result['success'] is False else 2


if __name__ == '__main__':
    raise SystemExit(main())
