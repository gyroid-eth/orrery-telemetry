"""Check reporting regressions without launching the harness or services."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('dsh_check', Path(__file__).resolve().parents[1] / 'integrations/dsh/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckTests(unittest.TestCase):
    def test_unapproved_skip_is_failure_and_approved_reason_is_explicit(self):
        skipped = [{'test': 'fixture.test', 'reason': 'Windows-only fixture'}]
        self.assertFalse(check.summarize(2, 0, 0, skipped, [])['success'])
        approved = check.summarize(2, 0, 0, skipped, ['Windows-only fixture'])
        self.assertTrue(approved['success'])
        self.assertEqual(approved['skips'], skipped)
        self.assertEqual(approved['unapproved_skips'], [])

    def test_empty_or_failed_suite_cannot_be_green(self):
        for args in [(0, 0, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1)]:
            result = check.summarize(*args, [], [])
            self.assertFalse(result['success'])
            self.assertEqual(result['passed'], 0)

    def test_node_tap_reports_failures_skips_and_incomplete_output(self):
        text = 'ok 1 - fixture # SKIP Windows-only fixture\n# tests 1\n# fail 0\n# skipped 1\n# cancelled 0\n'
        self.assertEqual(check.node_result(text, 0), (1, 0, 0, [{'test': 'node:fixture', 'reason': 'Windows-only fixture'}]))
        self.assertEqual(check.node_result(text, 1)[2], 1)
        with self.assertRaises(ValueError):
            check.node_result('', 0)


if __name__ == '__main__':
    unittest.main()
