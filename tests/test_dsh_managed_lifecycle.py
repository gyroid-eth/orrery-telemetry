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


if __name__ == '__main__':
    unittest.main()
