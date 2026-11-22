"""Native offline-check setup and skip acceptance, without installed dependencies."""
import hashlib
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('hermes_check', ROOT / 'integrations/hermes/check.py')
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


class NativeCheck(unittest.TestCase):
    def test_explicit_installed_source_and_python_pass_valid_setup(self):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not source or not python:
            self.skipTest('Explicit installed dependency interpreter/source required')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            template, output = root / 'runtime.json', root / 'result.json'
            template.write_text(json.dumps({'hermes_source': source, 'managed_root': str(Path(source).parent)}))
            self.assertEqual(Path(check.committed_python(json.loads(template.read_text()))).resolve(), Path(python).resolve())
            suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
            with patch.object(sys, 'argv', ['check.py', '--config', str(template), '--out', str(output)]), \
                    patch.object(check.os, 'execve', side_effect=AssertionError('already in committed interpreter')), \
                    patch.object(check.unittest.defaultTestLoader, 'discover', return_value=suite), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(check.main(), 0)
            report = json.loads(output.read_text())
            self.assertTrue(report['success'])
            self.assertEqual(report['tests_run'], 1)
            self.assertEqual(report['skips'], [])

    def test_skip_reasons_require_explicit_approval(self):
        class Skipped(unittest.TestCase):
            def runTest(self):
                self.skipTest('approved-platform')
        result = unittest.TestResult()
        Skipped().run(result)
        self.assertFalse(check.report(result, [])['success'])
        self.assertEqual(check.report(result, [])['unapproved_skips'][0]['reason'], 'approved-platform')
        self.assertTrue(check.report(result, ['approved-platform'])['success'])
        with self.assertRaises(ValueError):
            check.report(result, [{'reason': 'approved-platform'}])

    def test_empty_or_failed_suite_cannot_succeed(self):
        self.assertFalse(check.report(unittest.TestResult(), [])['success'])
        class Failed(unittest.TestCase):
            def runTest(self):
                self.fail('fixture failure')
        result = unittest.TestResult()
        Failed().run(result)
        self.assertEqual(check.report(result, [])['failures'], 1)
        self.assertFalse(check.report(result, [])['success'])

    def test_committed_environment_is_read_without_import_or_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            source.mkdir()
            # Importing this source would fail; resolution must only read facts.
            (source / 'hermes_constants.py').write_text("raise RuntimeError('must not import')\n")
            store = root / 'installs' / hashlib.sha256(str(source).encode()).hexdigest()[:16]
            environment = store / 'environments/fixture'
            (environment / 'bin').mkdir(parents=True)
            (environment / 'pyvenv.cfg').write_text('# fixture\n')
            python = environment / 'bin/python'
            python.write_text('#!/bin/sh\nexit 99\n')
            python.chmod(0o700)
            facts = store / 'facts.json'
            facts.write_text(json.dumps({'packages': {'venv': {'environment': str(environment)}}}))
            config = {'hermes_source': str(source), 'managed_root': str(root)}
            self.assertEqual(check.committed_python(config), str(python))
            python.chmod(0o600)
            with self.assertRaises(ValueError):
                check.committed_python(config)
            python.chmod(0o700)
            (environment / 'pyvenv.cfg').unlink()
            with self.assertRaises(ValueError):
                check.committed_python(config)
            outside = root / 'foreign-environment'
            (outside / 'bin').mkdir(parents=True)
            (outside / 'pyvenv.cfg').write_text('# otherwise valid, outside committed store\n')
            (outside / 'bin/python').symlink_to(sys.executable)
            facts.write_text(json.dumps({'packages': {'venv': {'environment': str(outside)}}}))
            with self.assertRaises(ValueError):
                check.committed_python(config)

    def test_missing_dependencies_exit_nonzero_with_failure_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            template, output = root / 'template.json', root / 'result.json'
            template.write_text(json.dumps({'hermes_source': str(root / 'source'), 'managed_root': str(root)}))
            result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'integrations/hermes/check.py'),
                '--config', str(template), '--out', str(output)], capture_output=True, text=True,
                env={'PATH': os.defpath, 'HOME': str(root)}, timeout=10)
            self.assertEqual(result.returncode, 1)
            report = json.loads(output.read_text())
            self.assertFalse(report['success'])
            self.assertEqual(report['tests_run'], 0)
            self.assertEqual(report['errors'], 1)
            self.assertEqual(report['skips'], [])
            self.assertEqual(report['setup_failure'], 'FileNotFoundError')


if __name__ == '__main__':
    unittest.main()
