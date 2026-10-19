"""Offline OS snapshots and lifecycle fixtures only: no sockets, tmux or keys."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('observe', Path(__file__).resolve().parents[1] / 'integrations/dsh/observe.py')
observe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observe)


class FakeMac:
    def __init__(self, state, text='', failure=False):
        self.state, self.text, self.failure = state, text, failure
        self.calls, self.started, self.count = [], False, 0

    def table(self):
        self.calls.append('table')
        if not self.started or self.count >= 2:
            return {}
        return observe.processes('20 1 20 python\n21 20 21 node\n22 21 21 proxy\n99 1 99 unrelated\n')

    def start(self, state):
        self.calls.append('start')
        self.started = True
        observe.write(state / 'owned-pane.json', {'attempt_id': 'one', 'pane_id': '%42'})
        observe.write(state / 'runtime-process.json', {'attempt_id': 'one', 'pid': 21, 'pgid': 21, 'supervisor_pid': 20})
        class Child:
            pid = 10
            def poll(self):
                return 0
        return Child()

    def pane_pid(self, pane):
        return 20

    def connections(self, pids):
        self.calls.append(('connections', sorted(pids)))
        if self.failure:
            raise ValueError('fixture permission refusal')
        self.count += 1
        if self.count == 2:
            observe.write(self.state / 'runtime-exit.json', {'group_absent': True, 'group_check': 'observed'})
        return observe.sockets(self.text) if pids else []


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        observe.write(self.state / 'attempt.json', {'attempt_id': 'one', 'phase': 'run'})
        backend = self.state / 'backend.json'
        observe.write(backend, {'backends': {'strata': {'base_url': 'https://strata.invalid'}}})
        observe.write(self.state / 'run-config.json', {'backend_file': str(backend), 'backend_id': 'strata',
                      'connection': {'mcp_url': 'http://127.0.0.1:12345/mcp'}})

    def run_fixture(self, text='', failure=False):
        system = FakeMac(self.state, text, failure)
        code = observe.observe(self.state, system=system, resolve=lambda *a, **k: [(None, None, None, None, ('192.0.2.1', 443))],
                               sleep=lambda _: None, interval=0)
        return code, observe.read(self.state / 'network-observation.json'), system

    def test_owned_window_starts_before_launch_and_finishes_after_group_disappearance(self):
        text = 'p21\ncnode\nf5\nPTCP\nn10.0.0.1:5000->192.0.2.1:443\nTST=ESTABLISHED\n'
        code, result, system = self.run_fixture(text)
        self.assertEqual(code, 0)
        self.assertEqual(result['verdict'], 'none-observed')
        self.assertTrue(result['completed'])
        self.assertEqual(system.calls[:2], ['table', 'start'])
        self.assertIn(('connections', [20, 21, 22]), system.calls)
        self.assertNotIn(99, system.calls[2][1] if isinstance(system.calls[2], tuple) else [])

    def test_forbidden_destination_is_reported_and_wrapper_never_signals(self):
        code, result, _ = self.run_fixture('p22\ncproxy\nf5\nPTCP\nn127.0.0.1:5000->127.0.0.1:54321\n')
        self.assertEqual(code, 1)
        self.assertEqual(result['verdict'], 'forbidden-observed')
        self.assertFalse(hasattr(FakeMac, 'kill'))

    def test_observation_failure_stays_unknown(self):
        code, result, _ = self.run_fixture(failure=True)
        self.assertEqual(code, 2)
        self.assertEqual(result['verdict'], 'unknown')
        self.assertFalse(result['completed'])

    def test_ipv6_mail_and_listener_classification(self):
        permitted = {('2001:db8::1', 443), ('127.0.0.1', 12345)}
        for remote in ['[2001:db8::1]:443', '127.0.0.1:12345']:
            self.assertTrue(observe.allowed({'protocol': 'TCP', 'connection': 'local:1->' + remote}, permitted))
        for row in [{'protocol': 'TCP', 'connection': '*:12345'},
                    {'protocol': 'UDP', 'connection': 'local:1->127.0.0.1:12345'},
                    {'protocol': 'TCP', 'connection': 'local:1->192.0.2.2:443'}]:
            self.assertFalse(observe.allowed(row, permitted))

    def test_nonloopback_mail_stops_before_any_launch(self):
        config = observe.read(self.state / 'run-config.json')
        config['connection']['mcp_url'] = 'https://other.invalid/mcp'
        observe.write(self.state / 'run-config.json', config)
        system = FakeMac(self.state)
        with self.assertRaises(ValueError):
            observe.observe(self.state, system=system)
        self.assertEqual(system.calls, [])


if __name__ == '__main__':
    unittest.main()
