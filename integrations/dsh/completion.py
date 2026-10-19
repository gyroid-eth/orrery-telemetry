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


def _result(checks, *, applicable_checks=None, **metadata):
    # Known failures remain failures; absent evidence can never become success.
    applicable = list(checks) if applicable_checks is None else applicable_checks
    values = [checks[key] for key in applicable]
    success = False if False in values else None if None in values or not applicable else True
    return {**metadata, 'success': success, 'checks': checks,
            'applicable_checks': applicable,
            'missing': [key for key in applicable if checks[key] is not True]}


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
    if task.get('task_mode', 'assignment') == 'conversation':
        return _result(checks, applicable_checks=[], evaluation='not_applicable', **metadata)
    # A completed assignment remains a report about that moment. Further turns
    # or edits in the same runtime cannot rewrite the already reported result.
    if isinstance(tid, str) and Path(tid).name == tid and tid not in ('.', '..'):
        saved = _read(root / 'task-reports' / (tid + '.json'))
        if (saved and saved.get('attempt_id') == aid and saved.get('task_id') == tid
                and saved.get('agent_name') == attempt.get('agent_name')
                and type(saved.get('success')) is bool and saved.get('source') == 'managed-task-evaluator'
                and task.get('attempt_id') == aid
                and (saved.get('success') is not True or saved.get('provider_usage_verified') is True)):
            if task.get('report_policy', 'mail') == 'mail' and (root / 'parent-mail-receipt.json').exists():
                parent = _read(root / 'parent-mail-receipt.json')
                valid_parent = parent and (parent.get('source') == 'parent-bound-fetch-inbox'
                    and parent.get('attempt_id') == aid and parent.get('task_id') == tid
                    and parent.get('agent_name') == attempt.get('agent_name')
                    and parent.get('from') == attempt.get('agent_name') and parent.get('to') == [task.get('report_to')]
                    and any(r.get('message_id') == parent.get('message_id') for r in saved.get('mail_receipts', [])))
                if not valid_parent:
                    return _result({**saved['checks'], 'mail_receipt': False}, **metadata)
            return saved
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
        turn = turns[-1].get('turn') if turns else None
        relevant = [row for row in audit if row.get('task_id') == tid and row.get('turn') == turn]
        bad = any(row.get('phase', '').startswith('blocked-') or row.get('phase') in ('runtime-fatal', 'limit-reached')
                  for row in relevant + [row for row in audit if 'task_id' not in row])
        requests = {row.get('seq') for row in relevant if row.get('phase') == 'provider-request-start'}
        responses = {row.get('seq'): row for row in relevant if row.get('phase') == 'provider-response'}
        ends = {row.get('seq'): row for row in relevant if row.get('phase') == 'stream-end'}
        if not same_attempt or bad:
            checks['provider_audit'] = False
        elif requests:
            confirmed = []
            for seq in requests:
                response, end = responses.get(seq), ends.get(seq)
                if not response or not end:
                    confirmed.append(None)
                    continue
                usage = response.get('usage')
                counts = (isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
                                                        for key in ('input', 'output')))
                confirmed.append(end.get('ok') is True and end.get('terminal') is True
                                 and end.get('usage_confirmed') is True and counts
                                 and type(response.get('status')) is int and 200 <= response['status'] < 300)
            checks['provider_audit'] = False if False in confirmed else None if None in confirmed else True
        metadata['provider_usage_verified'] = checks['provider_audit'] is True
    if events is not None:
        receipts = [row for row in scoped if row.get('phase') == 'mail-sent']
        parent, child = task.get('report_to'), attempt.get('agent_name')
        if receipts and parent and child and attempt['phase'] == 'run':
            matching = [row for row in receipts if (
                row.get('ok') is True and row.get('verified_sender') is True
                and row.get('from') == child and row.get('to') in (parent, [parent])
                and type(row.get('message_id')) is int and row['message_id'] > 0
                )]
            checks['mail_receipt'] = bool(matching)
            # A supplied canonical parent capture is stronger evidence: never
            # ignore a mismatch merely because the server accepted a send.
            if (root / 'parent-mail-receipt.json').exists():
                received = _read(root / 'parent-mail-receipt.json')
                checks['mail_receipt'] = checks['mail_receipt'] and bool(received) and (
                    received.get('source') == 'parent-bound-fetch-inbox'
                    and received.get('attempt_id') == aid and received.get('task_id') == tid
                    and received.get('agent_name') == child and received.get('from') == child
                    and received.get('to') == [parent]
                    and any(row.get('message_id') == received.get('message_id') for row in matching))
    applicable = list(checks)
    if task.get('task_mode') == 'assignment' and task.get('expected') == []:
        applicable.remove('expected_artifacts')
    if task.get('report_policy', 'mail') == 'pane':
        applicable.remove('mail_receipt')
        checks['mail_receipt'] = None
    return _result(checks, applicable_checks=applicable, **metadata)


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
    retired = (scoped('retire-receipt.json', 'dashboard-bound-whois')
               or scoped('retire-receipt.json', 'parent-bound-whois'))
    task = _read(root / 'task.json')
    released = None
    if release and type(release.get('confirmed')) is bool:
        reservations = task.get('reservation_receipts') if task and task.get('attempt_id') == aid else None
        expected = [row.get('id') for row in reservations if isinstance(row, dict)] if isinstance(reservations, list) else None
        ledger = _read(root / 'reservation-ledger.json')
        if ledger and ledger.get('attempt_id') == aid and ledger.get('agent_name') == child:
            rows = ledger.get('receipts')
            if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
                expected = list(set((expected or []) + [row.get('id') for row in rows]))
        requested, actual = release.get('reservation_ids'), release.get('released_ids')
        valid = lambda ids: isinstance(ids, list) and all(type(n) is int and n > 0 for n in ids)
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
