"""Thin dsh adapter command and real saved-evidence contrasts, no services."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from integrations.dsh import managed_lifecycle as d


class DshAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.state = self.root / 'state'
        self.calls = []
    def fake_run(self, argv, *, timeout=60, check=True, background=False):
        self.calls.append((argv, background))
        if 'init' in argv:
            self.state.mkdir()
        return SimpleNamespace(returncode=0, stdout='{}')
    def put(self, name, value):
        self.state.mkdir(exist_ok=True); d.save(self.state / name, value)
    def test_prepare_uses_one_canonical_register_and_keyless_preflight(self):
        template = self.root / 'template.json'; d.save(template, {'backend_id': 'deepseek'})
        with patch.object(d, 'status', return_value={'ready': False}):
            d.prepare(state=self.state, config_file=template, backend_id='strata',
                      task={'prompt': 'task'}, worktree=self.root / 'work', parent='ParentAgent', run=self.fake_run)
        self.assertEqual([Path(c[0][2]).name for c in self.calls], ['launch.py', 'mail_lifecycle.py', 'preflight.py'])
        self.assertEqual(sum('register' in c[0] for c in self.calls), 1)
        self.assertFalse(any('start' in c[0] for c in self.calls))
        with self.assertRaises(ValueError):
            d.prepare(state=self.state, config_file=template, backend_id='strata', task={},
                      worktree=self.root / 'work', parent='ParentAgent', run=self.fake_run)
    def test_ready_requires_real_pid_and_full_preflight(self):
        self.put('attempt.json', {'attempt_id': 'attempt', 'phase': 'run', 'agent_name': 'Child'})
        self.put('task.json', {'attempt_id': 'attempt', 'task_id': 'task'})
        self.put('runtime-process.json', {'pid': 987, 'attempt_id': 'attempt'})
        self.put('preflight-complete.json', {'attempt_id': 'attempt', 'ok': True, 'schema': True, 'backend': True})
        row = {'phase': 'plugin-ready', 'attempt_id': 'attempt', 'task_id': 'task', 'process_id': 986}
        (self.state / 'events.jsonl').write_text(json.dumps(row) + '\n')
        self.assertFalse(d.status(self.state)['ready'])
        row['process_id'] = 987
        (self.state / 'events.jsonl').write_text(json.dumps(row) + '\n')
        self.assertTrue(d.status(self.state)['ready'])
        self.assertFalse(d.status(self.state)['terminal'])
        self.put('preflight-complete.json', {'attempt_id': 'old', 'ok': True, 'schema': True, 'backend': True})
        self.assertFalse(d.status(self.state)['ready'])
    def test_partial_start_abandon_release_retire_confirm_in_order(self):
        self.put('attempt.json', {'attempt_id': 'attempt'})
        self.put('mail-identity.json', {'agent_name': 'Child'})
        self.put('reservation-intent.json', {})
        with patch.object(d, 'status', return_value={'runtime_success': True}):
            result = d.finalize(self.state, parent_call=lambda *a: {'name': 'Child', 'retired_at': 'date'}, run=self.fake_run)
        self.assertTrue(result['runtime_success'])
        actions = [next((a for a in c[0] if a in ('abandon', 'release', 'retire', 'confirm-retire')), None) for c in self.calls]
        self.assertEqual(actions, ['abandon', 'release', 'retire', 'confirm-retire'])
        self.assertEqual(d.read(self.state / 'parent-whois.json')['name'], 'Child')
    def test_no_parent_proof_means_no_confirm_or_clean(self):
        self.put('attempt.json', {'attempt_id': 'attempt'})
        self.put('mail-identity.json', {'agent_name': 'Child'})
        self.put('runtime-process.json', {'pid': 123})
        with patch.object(d, 'status', return_value={'runtime_success': None}):
            value = d.finalize(self.state, parent_call=None, run=self.fake_run)
        self.assertTrue(value['cleanup_pending']); self.assertIsNone(value['runtime_success'])
        self.assertFalse(any('confirm-retire' in c[0] or 'clean' in c[0] for c in self.calls))



class StartupAbsenceTests(unittest.TestCase):
    def test_stop_before_runtime_marker_keeps_live_startup_unknown(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            wrapper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                d.save(state / 'start-intent.json', {'pid': wrapper.pid})
                result = subprocess.run([sys.executable, str(d.HERE / 'launch.py'), 'abandon', '--state', str(state)],
                    capture_output=True, text=True, timeout=10, env={'PATH': '/usr/bin:/bin'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(wrapper.poll())
                receipt = d.read(state / 'runtime-exit.json')
                self.assertIsNone(receipt['group_absent'])
                self.assertEqual(receipt['group_check'], 'unknown')
                self.assertIsNot(receipt['runtime_not_started'], True)
                for name in ('release-receipt.json', 'retire-receipt.json', 'cleaned.json'):
                    self.assertFalse((state / name).exists())
            finally:
                wrapper.terminate(); wrapper.wait(timeout=5)

    def test_start_marks_intent_before_the_background_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            def run(argv, **kwargs):
                self.assertTrue((Path(tmp) / 'start-intent.json').exists())
                self.assertTrue(kwargs['background'])
                return SimpleNamespace(pid=123)
            d.start(Path(tmp), run=run)

class InteractiveAdapterTests(unittest.TestCase):
    setUp = DshAdapterTests.setUp
    put = DshAdapterTests.put

    def test_turn_end_keeps_runtime_alive_and_plugin_ready_is_attempt_scoped(self):
        self.put('attempt.json', {'attempt_id': 'attempt', 'phase': 'run', 'agent_name': 'Child'})
        self.put('task.json', {'attempt_id': 'attempt', 'task_id': 'task', 'task_mode': 'conversation'})
        self.put('runtime-process.json', {'pid': 987, 'attempt_id': 'attempt'})
        self.put('preflight-complete.json', {'attempt_id': 'attempt', 'ok': True, 'schema': True, 'backend': True})
        rows = [{'phase': 'plugin-ready', 'attempt_id': 'attempt', 'task_id': None, 'process_id': 987},
                {'phase': 'turn-end', 'attempt_id': 'attempt', 'task_id': None, 'reason_kind': 'completed', 'turn': 2}]
        (self.state / 'events.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        result = d.status(self.state)
        self.assertTrue(result['ready']); self.assertFalse(result['terminal'])
        self.assertIsNone(result['task_success']); self.assertEqual(result['activity'], 'idle')
        self.put('runtime-exit.json', {'attempt_id': 'old'})
        self.assertFalse(d.status(self.state)['terminal'])
        self.put('runtime-exit.json', {'attempt_id': 'attempt'})
        self.assertTrue(d.status(self.state)['terminal'])

    def test_unconfirmed_usage_does_not_freeze_native_completed_assignment(self):
        self.put('attempt.json', {'attempt_id':'attempt','agent_name':'Child','phase':'run'})
        self.put('task.json', {'attempt_id':'attempt','task_id':'task','task_mode':'assignment','report_policy':'pane','expected':[]})
        events=[{'phase':'turn-end','attempt_id':'attempt','task_id':'task','turn':1,'reason_kind':'completed'},
                {'phase':'task-report-finalized','attempt_id':'attempt','task_id':'task'}]
        (self.state/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in events))
        audit=[{'phase':'provider-request-start','attempt_id':'attempt','task_id':'task','turn':1,'seq':1}]
        (self.state/'audit.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in audit))
        self.assertIsNone(d.status(self.state)['task_success'])
        self.assertFalse((self.state/'task-reports/task.json').exists())
        audit += [{'phase':'provider-response','attempt_id':'attempt','task_id':'task','turn':1,'seq':1,'status':200,'usage':{'input':2,'output':0}},
                  {'phase':'stream-end','attempt_id':'attempt','task_id':'task','turn':1,'seq':1,'ok':True,'terminal':True,'usage_confirmed':True}]
        (self.state/'audit.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in audit))
        self.assertTrue(d.status(self.state)['task_success'])
        self.assertTrue(d.read(self.state/'task-reports/task.json')['provider_usage_verified'])

    def test_report_snapshot_survives_following_turn_and_artifact_edit(self):
        self.put('attempt.json', {'attempt_id': 'attempt', 'agent_name': 'Child', 'phase': 'run'})
        self.put('task.json', {'attempt_id': 'attempt', 'task_id': 'task'})
        completed = {'success': True, 'provider_usage_verified': True, 'attempt_id': 'attempt', 'task_id': 'task', 'checks': {}}
        with patch.object(d, 'evaluate', side_effect=lambda state, runtime=False: {'success': None} if runtime else completed):
            self.assertTrue(d.status(self.state)['task_success'])
        saved = d.read(self.state / 'task-reports/task.json')
        self.assertEqual(saved['agent_name'], 'Child')
        self.assertTrue(d.evaluate(self.state)['success'])
        self.put('task.json', {'attempt_id': 'attempt', 'task_id': 'next-task'})
        self.assertIsNot(d.evaluate(self.state)['success'], True)

class InteractiveEmptyReleaseTests(unittest.TestCase):
    setUp = DshAdapterTests.setUp
    put = DshAdapterTests.put
    fake_run = DshAdapterTests.fake_run
    def test_no_initial_grants_still_releases_the_dynamic_ledger(self):
        self.put('attempt.json', {'attempt_id': 'attempt'})
        self.put('mail-identity.json', {'agent_name': 'Child'})
        self.put('runtime-process.json', {'pid': 123})
        self.put('reservation-ledger.json', {'attempt_id': 'attempt', 'agent_name': 'Child', 'receipts': []})
        with patch.object(d, 'status', return_value={'runtime_success': True}):
            d.finalize(self.state, parent_call=lambda *a: {'name': 'Child', 'retired_at': 'date'}, run=self.fake_run)
        actions = [next((a for a in c[0] if a in ('stop', 'release', 'retire', 'confirm-retire')), None) for c in self.calls]
        self.assertEqual(actions, ['stop', 'release', 'retire', 'confirm-retire'])


if __name__ == '__main__':
    unittest.main()
