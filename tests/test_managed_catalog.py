"""Pure managed catalog fixtures; no machine configuration or network."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import shlex
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.managed_catalog import catalog_providers
from dashboard.managed_catalog import load_catalog_inputs
from dashboard.managed_catalog import _hermes_ready


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('dashboard.managed_catalog.sys.platform', 'darwin'))
        self.backends = {
            'deepseek': {'model': 'deepseek-flash', 'efforts': ['low', 'high'], 'default_effort': 'low'},
            'strata': {'model': 'fixture-qwen', 'configured_context': 131072,
                       'server': 'Configured local server', 'efforts': ['low', 'high'], 'default_effort': 'high'},
        }
        self.availability = {name: {h: {'available': True, 'reason': None} for h in ('dsh', 'hermes')}
                             for name in self.backends}

    def build(self, **kwargs):
        return catalog_providers(self.backends, self.availability, mode=kwargs.get('mode', 'legacy'), enabled=kwargs.get('enabled', True))

    def test_disabled_or_missing_configuration_adds_no_choices(self):
        self.assertEqual(self.build(enabled=False), [])
        self.assertEqual(catalog_providers({}, {}, mode='legacy', enabled=True), [])

    def test_four_combinations_keep_route_and_harness_separate(self):
        rows = self.build()
        self.assertEqual([r['id'] for r in rows], ['deepseek', 'strata'])
        self.assertEqual(sum(h['available'] for r in rows for h in r['models'][0]['harnesses']), 4)
        for row in rows:
            self.assertTrue(row['managed'])
            self.assertEqual([h['id'] for h in row['models'][0]['harnesses'] if h['default']], ['dsh'])
            self.assertEqual(row['helper_models'], [{'id': 'same', 'label': '主と同じ'}])

    def test_global_mode_is_never_advertised_as_legacy(self):
        for mode in ('global', 'unknown', None):
            for row in self.build(mode=mode):
                self.assertTrue(all(not h['available'] and not h['default'] and h['reason'] == 'unsupported_mode'
                                    for h in row['models'][0]['harnesses']))

    def test_missing_runtime_is_unavailable_and_default_moves_to_ready_harness(self):
        self.availability['strata']['dsh'] = {'available': False, 'reason': 'patch_missing'}
        row = self.build()[1]['models'][0]['harnesses']
        self.assertEqual(row[0]['reason'], 'patch_missing')
        self.assertFalse(row[0]['default'])
        self.assertTrue(row[1]['default'])
        self.availability.clear()
        self.assertTrue(all(not h['available'] for r in self.build() for h in r['models'][0]['harnesses']))

    def test_context_is_unreported_and_private_fields_are_not_copied(self):
        self.backends['strata'].update(base_url='https://private.invalid', keychain={'account': 'private-account'},
                                        executable='/private/runtime', token='dummy-private-token')
        result = self.build()
        self.assertIsNone(result[1]['context'])
        self.assertEqual(result[1]['context_source'], 'unreported')
        self.assertEqual(result[1]['context_note'], 'server 報告値は未取得')
        text = json.dumps(result)
        for private in ['131072', 'private.invalid', 'private-account', '/private/runtime', 'dummy-private-token']:
            self.assertNotIn(private, text)

    def test_inputs_are_unchanged(self):
        before = copy.deepcopy((self.backends, self.availability))
        self.build()
        self.assertEqual((self.backends, self.availability), before)

    def test_private_loader_is_off_without_opt_in(self):
        self.assertEqual(load_catalog_inputs(None, mode='legacy'), ({}, {}, False))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / 'manifest.json'
            manifest.write_text('{"enabled":false}')
            self.assertEqual(load_catalog_inputs(manifest, mode='legacy'), ({}, {}, False))
            manifest.write_text('{broken')
            self.assertEqual(load_catalog_inputs(manifest, mode='legacy'), ({}, {}, False))

    def test_loader_checks_prepared_routes_and_uses_public_error_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('bin/agentstack-preregister-child', 'hooks/child_tools.py',
                         'hooks/cleanup-child-agent.sh', 'integrations/codex_app/plugin/scripts/run-mcp.sh'):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('offline fixture')
                file.chmod(0o700)
            template = root / 'template.json'
            template.write_text(json.dumps({'product_root': str(root)}))
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'enabled': True, 'backends': self.backends,
                'harnesses': {h: {'config_file': str(template), 'python': sys.executable} for h in ('dsh', 'hermes')}}))
            with patch('dashboard.managed_catalog._dsh_ready', return_value=None), patch('dashboard.managed_catalog._hermes_ready', return_value=None):
                metadata, availability, enabled = load_catalog_inputs(manifest, mode='legacy')
                self.assertTrue(enabled)
                self.assertTrue(all(s['available'] for row in availability.values() for s in row.values()))
                global_metadata, global_availability, _ = load_catalog_inputs(manifest, mode='global')
                self.assertTrue(all(s['reason'] == 'unsupported_mode' for row in global_availability.values() for s in row.values()))
                with patch('dashboard.managed_catalog.sys.platform', 'linux'):
                    _, unsupported, _ = load_catalog_inputs(manifest, mode='legacy')
                    self.assertTrue(all(s['reason'] == 'unsupported_platform' for row in unsupported.values() for s in row.values()))
            metadata, availability, _ = load_catalog_inputs(manifest, mode='legacy')
            self.assertTrue(all(s['reason'] == 'runtime_not_prepared' for row in availability.values() for s in row.values()))
            self.assertNotIn(str(root), json.dumps(catalog_providers(metadata, availability, mode='legacy', enabled=True)))

    def test_hermes_template_only_profile_is_projected_without_executing_key_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            install = root / 'managed/installs' / hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
            environment = install / 'environments/fixture'
            (environment / 'bin').mkdir(parents=True)
            python = environment / 'bin/python'
            python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
            python.chmod(0o700)
            (environment / 'pyvenv.cfg').write_text('fixture\n')
            (install / 'facts.json').write_text(json.dumps({'packages': {'venv': {'environment': str(environment)}}}))
            template_file = root / 'template.json'
            marker = root / 'key-command-was-executed'
            template_file.write_text(json.dumps({'model': {'provider': 'fixture', 'default': 'fixture-qwen'},
                'providers': {'fixture': {'base_url': 'https://provider.invalid/v1', 'api_mode': 'chat_completions',
                                         'key_cmd': 'touch ' + str(marker)}}}))
            config = {'profiles': {'strata': {'template': str(template_file), 'context_length': 32768}},
                'hermes_executable': sys.executable, 'hermes_source': str(source), 'managed_root': str(root / 'managed')}
            self.assertIsNone(_hermes_ready(config, 'fixture-qwen', 'strata'))
            self.assertEqual(_hermes_ready(config, 'wrong-model', 'strata'), 'model_mismatch')
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
