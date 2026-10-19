"""Small shared-receipt tests; the opt-in real-Hermes probe is separate."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('hermes_guard', Path(__file__).resolve().parents[1] /
                                            'integrations/hermes/reservation_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ReservationContract(unittest.TestCase):
    def test_receipts_and_startup_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            attempt = {'attempt_id': 'attempt', 'agent_name': 'FixtureChild', 'phase': 'spike'}
            task = {'attempt_id': 'attempt', 'task_id': 'task', 'worktree': directory,
                    'allowed_paths': ['allowed/**'], 'reservation_receipts': [
                        {'id': 1, 'owner': 'FixtureChild', 'path_pattern': 'allowed/**',
                         'expires_ts': '2099-01-01T00:00:00+00:00'}]}
            events = []
            callback = guard.create_guard(attempt, task, events.append)
            self.assertIsNone(callback('write_file', {'path': directory + '/allowed/ok'}))
            self.assertEqual(callback('write_file', {'path': directory + '/other'})['message'], 'not-reserved')
            self.assertEqual(callback('write_file', {'path': directory + '/../outside'})['message'], 'outside-worktree')
            task['reservation_receipts'][0]['expires_ts'] = '2000-01-01T00:00:00Z'
            callback = guard.create_guard(attempt, task, events.append)
            self.assertEqual(callback('write_file', {'path': directory + '/allowed/expired'})['message'], 'reservation-expired')
            self.assertEqual(callback('terminal', {})['message'], 'not-in-wire-tools')
            task['reservation_receipts'][0]['expires_ts'] = 'broken'
            callback = guard.create_guard(attempt, task, events.append)
            self.assertEqual(callback('write_file', {'path': directory + '/allowed/error'})['message'], 'reservation-guard-error')
            class Manager:
                def iter_hook_callbacks(self, name):
                    return ()
            with self.assertRaisesRegex(RuntimeError, 'not registered'):
                guard.require_guard(Manager(), callback)

    def test_owner_symlink_and_fixture_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'work'
            root.mkdir()
            outside = Path(directory) / 'outside'
            outside.write_text('private')
            (root / 'escape').symlink_to(outside)
            attempt = {'attempt_id': 'attempt', 'agent_name': 'FixtureChild', 'phase': 'spike'}
            task = {'attempt_id': 'attempt', 'task_id': 'task', 'worktree': str(root),
                    'allowed_paths': ['allowed/**'], 'reservation_source': 'offline-fixture',
                    'reservation_receipts': [{'id': 1, 'owner': 'OtherChild', 'path_pattern': 'allowed/**',
                                              'expires_ts': '2099-01-01T00:00:00Z'}]}
            callback = guard.create_guard(attempt, task, lambda event: None)
            self.assertEqual(callback('write_file', {'path': str(root / 'escape')})['message'], 'outside-worktree')
            self.assertEqual(callback('write_file', {'path': str(root / 'allowed/file')})['message'], 'not-reserved')
            with self.assertRaisesRegex(ValueError, 'fixture receipt'):
                guard.create_guard({**attempt, 'phase': 'run'}, task, lambda event: None)


if __name__ == '__main__':
    unittest.main()
