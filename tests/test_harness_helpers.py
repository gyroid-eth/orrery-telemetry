import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.harness_helpers import helper_models, validate_helper_model, configured_models, model_rows


class HelperCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = {'backends': {
            'deepseek': {'model': 'main', 'models': ['main', 'flash', 'private-only']},
            'strata': {'model': 'qwen', 'models': ['qwen', 'local-helper']}}}

    def choices(self, harness='dsh', selection=None):
        return helper_models(selection, manifest=self.manifest, backend_id='deepseek',
                             public_models=['main', {'id': 'flash'}, 'foreign', 'flash'],
                             product_root=self.root, harness=harness)

    def generator(self, harness, source):
        file = self.root / 'integrations' / harness / 'thin_config.py'
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source)

    def test_old_missing_or_unavailable_generator_is_same_only(self):
        self.assertEqual(self.choices(selection={'config_file': '/not/read'}),
                         [{'id': 'same', 'label': 'Same as main'}])
        self.generator('dsh', 'THIN_CONFIG_CONTRACT=1\n')
        self.assertEqual(len(self.choices(selection={'config_file': '/not/read'})), 1)
        self.generator('dsh', 'HELPER_MODEL_CONTRACT=1\n')
        self.assertEqual(len(self.choices()), 1)

    def test_both_harnesses_only_same_backend_public_intersection_no_import(self):
        for harness in ('dsh', 'hermes'):
            with self.subTest(harness=harness):
                self.generator(harness, 'HELPER_MODEL_CONTRACT=1\nraise RuntimeError("must not execute")\n')
                choices = self.choices(harness, {'config_file': '/private/runtime'})
                self.assertEqual([r['id'] for r in choices], ['same', 'main', 'flash'])
                self.assertNotIn('/private', str(choices))

    def test_literal_integer_contract_only(self):
        for expression in ('True', '"1"', '2', 'int("1")'):
            self.generator('dsh', 'HELPER_MODEL_CONTRACT=' + expression)
            self.assertEqual(len(self.choices(selection={'config_file': 'private'})), 1)

    def test_single_model_legacy_config(self):
        self.assertEqual(configured_models({'backends': {'strata': {'model': 'qwen'}}}, 'strata'), ['qwen'])
        self.assertEqual(configured_models(self.manifest, 'absent'), [])
        for invalid in ('guessed-model', [None], [{'id': 'not-a-formal-string'}]):
            self.assertEqual(configured_models({'backends': {'strata': {
                'model': 'qwen', 'models': invalid}}}, 'strata'), [])

    def test_post_uses_same_choices_and_omitted_means_same(self):
        choices = [{'id': 'same'}, {'id': 'flash'}]
        self.assertEqual(validate_helper_model(None, choices), 'same')
        self.assertEqual(validate_helper_model('flash', choices), 'flash')
        for bad in ('foreign', 'local-helper', '', ' flash ', True, {}, []):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, '^invalid_helper_model$'):
                validate_helper_model(bad, choices)

    def test_each_model_uses_its_own_selection_effort_and_availability(self):
        self.generator('dsh', 'HELPER_MODEL_CONTRACT=1')
        runner = {'config_file': '/private/runtime'}
        rows = model_rows(self.manifest, 'deepseek', product_root=self.root,
            selections={('main', 'dsh'): runner, ('flash', 'dsh'): runner},
            statuses={('main', 'dsh'): {'available': True, 'efforts': ['low', 'high'],
                        'default_effort': 'high', 'effort_source': 'harness'},
                      ('flash', 'dsh'): {'available': True, 'efforts': None,
                        'default_effort': None, 'effort_source': 'unknown'}})
        self.assertEqual([r['id'] for r in rows], ['main', 'flash', 'private-only'])
        main, flash, unavailable = [r['harnesses'][0] for r in rows]
        self.assertEqual(main['efforts'], ['low', 'high'])
        self.assertIsNone(flash['efforts'])
        self.assertEqual(flash['effort_source'], 'unknown')
        self.assertEqual([r['id'] for r in flash['helper_models']],
                         ['same', 'main', 'flash'])
        self.assertFalse(unavailable['available'])
        self.assertIsNone(unavailable['efforts'])
        self.assertEqual(len(unavailable['helper_models']), 1)
        self.assertNotIn('/private', str(rows))


if __name__ == '__main__':
    unittest.main()
