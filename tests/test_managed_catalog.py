"""Pure native catalog fixtures; no machine configuration or network."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.managed_catalog import catalog_providers
from dashboard.managed_catalog import load_standard_inputs


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
            self.assertNotIn('managed', row)
            self.assertEqual([h['id'] for h in row['models'][0]['harnesses'] if h['default']], ['dsh'])

    def test_public_default_catalog_strings_are_ascii_and_english(self):
        for mode in ('legacy', 'global'):
            with self.subTest(mode=mode):
                rows = self.build(mode=mode)
                self.assertTrue(json.dumps(rows, ensure_ascii=False).isascii())
                for row in rows:
                    self.assertEqual(row['context_note'], 'Server-reported context not available yet')

    def test_global_mode_is_never_advertised_as_legacy(self):
        for mode in ('global', 'unknown', None):
            for row in self.build(mode=mode):
                self.assertTrue(all(not h['available'] and not h['default'] and h['reason'] == 'unsupported_mode'
                                    for h in row['models'][0]['harnesses']))

    def test_missing_runtime_is_unavailable_and_default_moves_to_ready_harness(self):
        self.availability['strata']['dsh'] = {'available': False, 'reason': 'plain_generation_required'}
        row = self.build()[1]['models'][0]['harnesses']
        self.assertEqual(row[0]['reason'], 'plain_generation_required')
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
        self.assertEqual(result[1]['context_note'], 'Server-reported context not available yet')
        text = json.dumps(result)
        for private in ['131072', 'private.invalid', 'private-account', '/private/runtime', 'dummy-private-token']:
            self.assertNotIn(private, text)

    def test_harness_cache_is_the_only_source_of_effort_choices_and_default(self):
        self.availability['deepseek']['dsh'].update(
            efforts=['off', 'low', 'high', 'max'], default_effort='high', effort_source='harness')
        result = self.build()
        self.assertEqual(result[0]['efforts'], ['off', 'low', 'high', 'max'])
        self.assertEqual(result[0]['effort_default'], 'high')
        dsh, hermes = result[0]['models'][0]['harnesses']
        self.assertEqual(dsh['effort_source'], 'harness')
        self.assertIsNone(hermes['efforts'])
        self.assertIsNone(hermes['default_effort'])
        self.assertEqual(hermes['effort_source'], 'unknown')
        self.assertEqual(result[1]['efforts'], [])  # Handwritten low/high is ignored.
        self.assertNotIn('effort_default', result[1])

    def test_inputs_are_unchanged(self):
        before = copy.deepcopy((self.backends, self.availability))
        self.build()
        self.assertEqual((self.backends, self.availability), before)

    def test_private_loader_is_off_without_opt_in(self):
        self.assertEqual(load_standard_inputs(None, mode='legacy'), ({}, {}, False))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / 'manifest.json'
            manifest.write_text('{"enabled":false}')
            self.assertEqual(load_standard_inputs(manifest, mode='legacy'), ({}, {}, False))
            manifest.write_text('{broken')
            self.assertEqual(load_standard_inputs(manifest, mode='legacy'), ({}, {}, False))



if __name__ == '__main__':
    unittest.main()


def test_dynamic_catalog_is_static_and_keeps_connection_labels(tmp_path, monkeypatch):
    from dashboard import managed_catalog as catalog
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'enabled': True, 'backends': {
        name: {'kind': 'strata', 'label': label, 'model_source': 'server',
               'urls': ['https://one.invalid/v1', 'https://two.invalid/v1', 'https://three.invalid/v1']}
        for name, label in [('local-one', 'Remote server'), ('local-two', 'Workstation')]}}))
    monkeypatch.setattr(catalog, 'thin_launcher', lambda: (_ for _ in ()).throw(AssertionError('static acquisition')))
    rows = catalog.standard_catalog(manifest, mode='legacy')
    assert [(r['id'], r['label'], r['models']) for r in rows] == [
        ('local-one', 'Remote server', []), ('local-two', 'Workstation', [])]
    assert all(r['dynamic_models'] for r in rows)
    assert 'one.invalid' not in json.dumps(rows)


def test_dynamic_picker_three_real_endpoints_optional_status_and_no_stale(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from dashboard import managed_catalog as catalog
    from hooks import native_server_models as models
    calls = []
    payload = {'data': [{'id': 'same'}, {'id': 'future', 'status': {'value': 'ready'}},
        {'id': 'sleeping', 'status': {'value': 'unloaded'}}]}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True); worker.start()
    manifest = tmp_path / 'manifest.json'
    urls = ['http://127.0.0.1:' + str(http.server_port) + '/' + path for path in ('one', 'two', 'three')]
    manifest.write_text(json.dumps({'enabled': True, 'backends': {'local-one': {
        'kind': 'strata', 'model_source': 'server', 'urls': urls}}}))
    generator = tmp_path / 'generator.py'
    generator.write_text('def validate_input(document): return {}\n')
    monkeypatch.setattr(catalog, 'dynamic_selection', lambda *a, **kw: {'python': sys.executable})
    monkeypatch.setattr(catalog, 'thin_launcher', lambda: SimpleNamespace(native_server_models=models,
        ROOT=tmp_path, config_generator=lambda *args: generator))
    monkeypatch.setattr(models, 'read_key', lambda *args: 'synthetic-key')
    try:
        static = catalog.standard_catalog(manifest, mode='legacy')
        public = catalog.discover_catalog(static, manifest, mode='legacy')[0]
        assert len(calls) == 3 and len(public['models']) == 9
        assert [(r['url_index'], r['id']) for r in public['models'] if r['id'] == 'same'] == [(0, 'same'), (1, 'same'), (2, 'same')]
        assert all(h['available'] == (r['id'] != 'sleeping') for r in public['models'] for h in r['harnesses'])
        assert all(r['context_source'] == 'default' for r in public['models'])
        assert 'synthetic-key' not in json.dumps(public) and urls[0] not in json.dumps(public)
        payload.clear()
        failed = catalog.discover_catalog(static, manifest, mode='legacy')[0]
        assert not failed['models'] and failed['model_error']
    finally:
        http.shutdown(); http.server_close(); worker.join()
