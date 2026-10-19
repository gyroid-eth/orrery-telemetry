"""Offline saved receipts only; no registration, keys, network or TUI startup."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('dsh_completion', Path(__file__).resolve().parents[1] / 'integrations/dsh/completion.py')
completion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(completion)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / 'result.txt').write_text('fixture done\n')
        self.attempt = {'attempt_id': 'current', 'agent_name': 'FixtureChild', 'started_at_ns': 1, 'phase': 'run'}
        self.task = {'attempt_id': 'current', 'task_id': 'task-1', 'worktree': str(self.work),
                     'expected': [{'path': 'result.txt', 'contains': 'done'}], 'report_to': 'FixtureParent'}
        self.events = [self.event('turn-end', reason_kind='completed'),
                       self.event('mail-sent', ok=True, verified_sender=True,
                                  message_id=123, to=['FixtureParent'], **{'from': 'FixtureChild'})]
        self.audit = [{'attempt_id': 'current', 'phase': 'provider-request-start', 'seq': 1}]
        self.save()

    def event(self, phase, **values):
        return {'attempt_id': 'current', 'task_id': 'task-1', 'phase': phase, **values}

    def save(self):
        for name, value in [('attempt.json', self.attempt), ('task.json', self.task)]:
            (self.root / name).write_text(json.dumps(value))
        for name, rows in [('events.jsonl', self.events), ('audit.jsonl', self.audit)]:
            (self.root / name).write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def result(self):
        self.save()
        return completion.evaluate_task(self.root)

    def test_task_succeeds_while_runtime_has_not_exited(self):
        self.assertTrue(self.result()['success'])
        self.assertIsNone(completion.evaluate_runtime(self.root)['success'])

    def test_empty_exit_zero_does_not_complete_task(self):
        self.events = []
        self.audit = []
        (self.root / 'runtime-exit.json').write_text('{"exit_code":0,"group_absent":true}')
        self.assertIsNot(self.result()['success'], True)

    def test_corrupt_evidence_is_pending(self):
        (self.root / 'events.jsonl').write_text('{broken')
        self.assertIsNone(completion.evaluate_task(self.root)['success'])

    def test_wrong_attempt_and_old_event_are_not_adopted(self):
        self.task['attempt_id'] = 'old'
        self.events[0]['attempt_id'] = 'old'
        self.assertFalse(self.result()['success'])

    def test_missing_or_old_artifact_fails(self):
        (self.work / 'result.txt').unlink()
        self.assertFalse(self.result()['success'])
        (self.work / 'result.txt').write_text('done')
        self.attempt['started_at_ns'] = (self.work / 'result.txt').stat().st_mtime_ns + 1
        self.assertFalse(self.result()['success'])

    def test_expected_path_cannot_escape_worktree(self):
        self.task['expected'][0]['path'] = '../attempt.json'
        self.assertFalse(self.result()['success'])

    def test_fatal_or_blocked_request_is_not_success(self):
        for phase in ['runtime-fatal', 'blocked-network', 'limit-reached']:
            with self.subTest(phase=phase):
                self.audit.append({'attempt_id': 'current', 'phase': phase})
                self.assertFalse(self.result()['success'])
                self.audit.pop()

    def test_mail_must_be_real_receipt_for_child_and_parent(self):
        for values in [{'verified_sender': False}, {'ok': False}, {'to': ['Other']}, {'from': 'Other'}, {'message_id': 0}]:
            with self.subTest(values=values):
                original = self.events[-1].copy()
                self.events[-1].update(values)
                self.assertFalse(self.result()['success'])
                self.events[-1] = original

    def test_spike_never_becomes_live_task_success(self):
        self.attempt['phase'] = 'spike'
        self.assertIsNot(self.result()['success'], True)

    def test_absent_mail_is_pending(self):
        self.events.pop()
        self.assertIsNone(self.result()['success'])

    def test_failed_turn_stays_failed(self):
        self.events[0]['reason_kind'] = 'error'
        self.assertFalse(self.result()['success'])

    def test_runtime_requires_release_and_retire_evidence(self):
        receipt = {'exit_code': 0, 'group_absent': True}
        (self.root / 'runtime-exit.json').write_text(json.dumps(receipt))
        result = completion.evaluate_runtime(self.root)
        self.assertIsNone(result['success'])
        self.assertIsNone(result['reservations_released'])
        self.assertEqual(result['retire'], 'unknown')
        receipt.update(reservations_released=True, retire='confirmed')
        (self.root / 'runtime-exit.json').write_text(json.dumps(receipt))
        self.assertTrue(completion.evaluate_runtime(self.root)['success'])


if __name__ == '__main__':
    unittest.main()
