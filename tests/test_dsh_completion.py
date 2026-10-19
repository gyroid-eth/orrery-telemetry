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
                     'expected': [{'path': 'result.txt', 'contains': 'done'}], 'report_to': 'FixtureParent',
                     'reservation_receipts': [{'id': 31}, {'id': 32}]}
        self.events = [self.event('turn-end', reason_kind='completed', turn=1),
                       self.event('mail-sent', ok=True, verified_sender=True,
                                  message_id=123, to=['FixtureParent'], **{'from': 'FixtureChild'})]
        self.audit = [self.event('provider-request-start', seq=1, turn=1),
                      self.event('provider-response', seq=1, turn=1, status=200, usage={'input':11,'output':3}),
                      self.event('stream-end', seq=1, turn=1, ok=True, terminal=True, usage_confirmed=True)]
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

    def test_optional_parent_capture_must_match_the_verified_send(self):
        receipt = {'source': 'parent-bound-fetch-inbox', 'attempt_id': 'current',
                   'task_id': 'task-1', 'agent_name': 'FixtureChild', 'from': 'FixtureChild',
                   'to': ['FixtureParent'], 'message_id': 123}
        path = self.root / 'parent-mail-receipt.json'
        path.write_text(json.dumps(receipt))
        self.assertTrue(self.result()['success'])
        receipt['message_id'] = 999
        path.write_text(json.dumps(receipt))
        self.events.append(self.event('mail-sent', ok=False, verified_sender=False,
                                     message_id=999, to=['FixtureParent'], **{'from': 'FixtureChild'}))
        self.assertFalse(self.result()['success'])

    def test_task_succeeds_while_runtime_has_not_exited(self):
        self.assertTrue(self.result()['success'])
        self.assertIsNone(completion.evaluate_runtime(self.root)['success'])

    def test_pane_assignment_requires_final_usage_and_terminal_for_this_turn(self):
        self.task.update(task_mode='assignment', report_policy='pane', expected=[], report_to=None)
        self.events = self.events[:1]
        for change in ({'usage_confirmed':False}, {'terminal':False}, {'ok':False}):
            with self.subTest(change=change):
                original=self.audit[-1].copy();self.audit[-1].update(change)
                self.assertFalse(self.result()['success'])
                self.audit[-1]=original
        self.assertTrue(self.result()['success'])
        self.audit[-2]['usage']['output']=None
        self.assertFalse(self.result()['success'])

    def test_missing_end_is_pending_and_another_turn_cannot_supply_usage(self):
        self.task.update(task_mode='assignment', report_policy='pane', expected=[], report_to=None)
        self.events = self.events[:1]
        self.audit[-1]['turn']=2
        self.assertIsNone(self.result()['success'])
        self.audit[-1]['turn']=1
        self.audit.append(self.event('stream-end',seq=1,turn=1,ok=False,terminal=False,usage_confirmed=False))
        self.assertFalse(self.result()['success'])

    def test_prior_request_success_does_not_confirm_the_latest_sequence(self):
        self.task.update(task_mode='assignment', report_policy='pane', expected=[], report_to=None)
        self.events = self.events[:1]
        self.audit.append(self.event('provider-request-start',seq=2,turn=1))
        self.assertIsNone(self.result()['success'])

    def test_old_success_snapshot_without_usage_proof_is_not_adopted(self):
        self.task.update(task_mode='assignment', report_policy='pane', expected=[], report_to=None)
        self.events = self.events[:1]
        self.save();saved=completion.evaluate_task(self.root)
        saved.update(source='managed-task-evaluator',agent_name='FixtureChild')
        saved.pop('provider_usage_verified')
        reports=self.root/'task-reports';reports.mkdir();(reports/'task-1.json').write_text(json.dumps(saved))
        self.audit=self.audit[:1]
        self.assertIsNone(self.result()['success'])

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
        self.assertIsNone(completion.evaluate_runtime(self.root)['success'])
        self.lifecycle_receipts()
        self.assertTrue(completion.evaluate_runtime(self.root)['success'])

    def lifecycle_receipts(self, release_changes=None, retire_changes=None):
        release = {'attempt_id': 'current', 'agent_name': 'FixtureChild', 'reservation_ids': [31, 32],
                   'released_ids': [31, 32], 'confirmed': True, 'source': 'product-bound-proxy',
                   'checked_at': '2026-01-01T00:00:00Z'}
        retire = {'attempt_id': 'current', 'agent_name': 'FixtureChild', 'cleanup_exit_code': 0,
                  'retired_at': '2026-01-01T00:00:00Z', 'confirmed': True,
                  'source': 'parent-bound-whois', 'checked_at': '2026-01-01T00:00:01Z'}
        release.update(release_changes or {})
        retire.update(retire_changes or {})
        for name, value in [('release-receipt.json', release), ('retire-receipt.json', retire)]:
            (self.root / name).write_text(json.dumps(value))
        (self.root / 'runtime-exit.json').write_text('{"group_absent":true}')

    def test_runtime_ignores_other_attempt_agent_or_receipt_source(self):
        for change in [{'attempt_id': 'old'}, {'agent_name': 'Other'}, {'source': 'offline-fixture'}]:
            with self.subTest(change=change):
                self.lifecycle_receipts(change, change)
                result = completion.evaluate_runtime(self.root)
                self.assertIsNone(result['success'])
                self.assertIsNone(result['reservations_released'])
                self.assertEqual(result['retire'], 'unknown')

    def test_partial_release_cannot_report_runtime_success(self):
        for change in [{'released_ids': [31]}, {'reservation_ids': [31], 'released_ids': [31]},
                       {'confirmed': False, 'released_ids': []}]:
            with self.subTest(change=change):
                self.lifecycle_receipts(change)
                self.assertFalse(completion.evaluate_runtime(self.root)['success'])

    def test_unknown_release_response_stays_pending(self):
        for change in [{'confirmed': None}, {'released_ids': None}]:
            with self.subTest(change=change):
                self.lifecycle_receipts(change)
                self.assertIsNone(completion.evaluate_runtime(self.root)['success'])

    def test_cleanup_exit_zero_without_whois_is_not_retirement(self):
        for change in [{'retired_at': None}, {'confirmed': False}]:
            with self.subTest(change=change):
                self.lifecycle_receipts(retire_changes=change)
                result = completion.evaluate_runtime(self.root)
                self.assertIsNone(result['success'])
                self.assertEqual(result['retire'], 'pending')

    def test_spike_lifecycle_fixtures_cannot_be_live_runtime_success(self):
        self.lifecycle_receipts()
        self.attempt['phase'] = 'spike'
        self.save()
        self.assertIsNone(completion.evaluate_runtime(self.root)['success'])



class InteractiveCompletionTests(unittest.TestCase):
    setUp = CompletionTests.setUp
    save = CompletionTests.save
    event = CompletionTests.event
    result = CompletionTests.result
    lifecycle_receipts = CompletionTests.lifecycle_receipts

    def test_conversation_needs_no_artifact_or_mail_and_never_claims_task_success(self):
        self.task.update(task_mode='conversation', report_policy='pane', expected=[], report_to=None)
        self.events = []; self.audit = []
        result = self.result()
        self.assertIsNone(result['success'])
        self.assertEqual(result['evaluation'], 'not_applicable')
        self.assertEqual(result['applicable_checks'], [])

    def test_explicit_assignment_pane_checks_artifact_without_fabricating_mail(self):
        self.task.update(task_mode='assignment', report_policy='pane', report_to=None)
        self.events.pop()
        result = self.result()
        self.assertTrue(result['success'])
        self.assertNotIn('mail_receipt', result['applicable_checks'])
        self.assertIsNone(result['checks']['mail_receipt'])
        (self.work / 'result.txt').write_text('wrong')
        self.assertFalse(self.result()['success'])

    def test_empty_conversation_reservations_can_be_confirmed_released(self):
        self.task['reservation_receipts'] = []; self.save()
        (self.root / 'reservation-ledger.json').write_text(json.dumps({
            'attempt_id': 'current', 'agent_name': 'FixtureChild', 'receipts': [], 'released_ids': []}))
        self.lifecycle_receipts({'reservation_ids': [], 'released_ids': []}, {'source': 'dashboard-bound-whois'})
        self.assertTrue(completion.evaluate_runtime(self.root)['success'])

    def test_dynamic_grants_must_all_be_confirmed_released(self):
        (self.root / 'reservation-ledger.json').write_text(json.dumps({
            'attempt_id': 'current', 'agent_name': 'FixtureChild', 'receipts': [{'id': 33}]}))
        self.lifecycle_receipts()
        self.assertFalse(completion.evaluate_runtime(self.root)['success'])
        self.lifecycle_receipts({'reservation_ids': [31, 32, 33], 'released_ids': [31, 32, 33]})
        self.assertTrue(completion.evaluate_runtime(self.root)['success'])

class OptionalArtifactCheck(unittest.TestCase):
    setUp = CompletionTests.setUp
    save = CompletionTests.save
    event = CompletionTests.event
    result = CompletionTests.result
    def test_assignment_without_expected_does_not_fabricate_an_artifact_verdict(self):
        self.task.update(task_mode='assignment', expected=[])
        result = self.result()
        self.assertTrue(result['success'])
        self.assertNotIn('expected_artifacts', result['applicable_checks'])
        self.assertIsNone(result['checks']['expected_artifacts'])
        self.events = []
        self.assertIsNot(self.result()['success'], True)


if __name__ == '__main__':
    unittest.main()
