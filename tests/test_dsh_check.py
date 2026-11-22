"""Check reporting regressions without launching the harness or services."""
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dsh_check', Path(__file__).resolve().parents[1] / 'integrations/dsh/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckTests(unittest.TestCase):
    def native_runtime(self, directory, variant):
        root = Path(directory)
        node = root / 'node'
        node.write_text('synthetic executable; version is stubbed')
        node.chmod(0o700)
        runtime = root / 'runtime'
        for pin in json.loads((check.HERE / 'runtime-pins.json').read_text()):
            file = runtime / 'node_modules' / pin['name'] / 'package.json'
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({'version': pin['version']}))
        evidence = root / 'evidence'
        evidence.mkdir()
        (evidence / 'node-runtime.json').write_text(json.dumps({'sha256': hashlib.sha256(node.read_bytes()).hexdigest()}))
        relative = 'lib/types/utils/paths.js'
        source = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui' / relative
        source.parent.mkdir(parents=True)
        source.write_text('synthetic upstream source')
        receipt = evidence / 'upstream-runtime.json'
        receipt.write_text(json.dumps({'variant': variant,
            'source_patch': 'tui-datadir-v1' if variant == 'plain-datadir' else None,
            'recipe_sha256': hashlib.sha256((check.HERE / 'tui-datadir.patch.json').read_bytes()).hexdigest(),
            'files': [{'file': relative, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]}))
        return node, runtime, receipt

    def test_prepare_uses_plain_and_datadir_native_receipts(self):
        for variant in ('plain', 'plain-datadir'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                node, runtime, receipt = self.native_runtime(directory, variant)
                with patch.object(check.subprocess, 'check_output', return_value='v24.3.0'):
                    env, proof = check.prepare(node, runtime, Path(directory) / 'out')
                self.assertEqual(Path(proof['runtime_receipt']).resolve(), receipt.resolve())
                self.assertEqual(proof['variant'], variant)
                self.assertNotIn('patch_receipt', proof)
                self.assertNotIn('ORRERY_DSH_TEST_BACKEND', env)

    def test_prepare_rejects_legacy_receipt_without_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            node, runtime, receipt = self.native_runtime(directory, 'managed')
            output = Path(directory) / 'out'
            with patch.object(check.subprocess, 'check_output', return_value='v24.3.0'):
                with self.assertRaisesRegex(ValueError, 'Plain dsh/TUI runtime receipt is required'):
                    check.prepare(node, runtime, output)
                receipt.unlink()
                (receipt.parent / 'tui-patch-applied.json').write_text('{}')
                with self.assertRaisesRegex(ValueError, 'Plain dsh/TUI runtime receipt is required'):
                    check.prepare(node, runtime, output)
            self.assertFalse(output.exists())

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
