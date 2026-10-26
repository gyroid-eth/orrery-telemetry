"""Configured Local model discovery uses ordinary OpenAI model rows."""
import importlib.util
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_server_models', ROOT / 'hooks/native_server_models.py')
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)


def test_optional_status_context_and_explicit_unloaded():
    rows = models.parse_models({'data': [
        {'id': 'ordinary'}, {'id': 'future', 'status': {'value': 'ready'}},
        {'id': 'loaded', 'display_name': 'Readable model', 'status': {'value': 'loaded'}, 'meta': {'n_ctx': 131072}},
        {'id': 'sleeping', 'status': {'value': 'unloaded'}},
    ]})
    assert [(r['id'], r['available']) for r in rows] == [
        ('ordinary', True), ('future', True), ('loaded', True), ('sleeping', False)]
    assert models.context({}, rows[2]) == (131072, 'server')
    assert models.context({}, rows[0]) == (64000, 'default')
    assert models.context({'context_override': 96000}, rows[2]) == (131072, 'server')
    assert models.context({'context_override': 96000}, rows[0]) == (96000, 'override')
    assert rows[2]['display_name'] == 'Readable model' and rows[2]['id'] == 'loaded'


def test_credential_lookup_uses_only_canonical_reference_and_redacts_failure(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def lookup(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b'synthetic-key\n')
    monkeypatch.setattr(models.subprocess, 'run', lookup)
    facts = {'route': {'key_cmd': '/usr/bin/security find-generic-password -s fixture -a user -w'}}
    assert models.read_key(facts, 'hermes') == 'synthetic-key'
    assert calls == [['/usr/bin/security', 'find-generic-password', '-s', 'fixture', '-a', 'user', '-w']]
    with pytest.raises(ValueError, match='unsupported_credential_selector'):
        models.read_key({'route': {'key_cmd': '/arbitrary/helper --secret'}}, 'hermes')
    assert len(calls) == 1
    monkeypatch.setattr(models.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=44, stdout=b'synthetic-secret'))
    with pytest.raises(ValueError) as failure:
        models.read_key(facts, 'hermes')
    assert str(failure.value) == 'credential_unavailable'


@pytest.mark.parametrize('payload', [None, {}, {'data': []}, {'data': [{'id': ''}]},
    {'data': [{'id': 'bad\nvalue'}]}, {'data': [{'id': 'same'}, {'id': 'same'}]}])
def test_invalid_or_ambiguous_inventory_refused(payload):
    with pytest.raises(ValueError, match='invalid_model_catalog'):
        models.parse_models(payload)


def test_three_urls_and_original_route_pin():
    config = {'model_source': 'server', 'urls': ['https://one.invalid/v1',
              'https://two.invalid', 'http://127.0.0.1:8080/v1']}
    assert models.selection(config, {'model': 'same', 'url_index': 2})['server_url'] == config['urls'][2]
    assert models.api_base(config['urls'][1]) == 'https://two.invalid/v1'
    with pytest.raises(ValueError, match='server_url_changed'):
        models.selection(config, {'model': 'same', 'url_index': 0, 'server_url': config['urls'][1]})
    with pytest.raises(ValueError, match='url_selection_required'):
        models.selection(config, {'model': 'same'})
    picked = models.selection(config, {'model': 'same', 'url_index': 0})
    config['urls'][0], config['urls'][1] = config['urls'][1], config['urls'][0]
    with pytest.raises(ValueError, match='server_url_changed'):
        models.selection(config, {'model': 'same', 'url_index': 0, 'url_key': picked['url_key']})


def test_real_http_selected_id_one_lookup_redirect_and_response_cap():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization')))
            if self.path.startswith('/redirect'):
                self.send_response(302); self.send_header('Location', '/v1/models'); self.end_headers(); return
            raw = json.dumps({'data': [{'id': 'selected'}, {'id': 'sleeping',
                'status': {'value': 'unloaded'}}]}).encode()
            if self.path.startswith('/large'):
                raw = json.dumps({'data': [{'id': 'selected'}], 'padding': 'x' * models.MAX_RESPONSE_BYTES}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    try:
        assert models.require_model(base, 'selected', 'synthetic-key')['id'] == 'selected'
        assert calls == [('/v1/models', 'Bearer synthetic-key')]
        with pytest.raises(ValueError, match='model_not_served'):
            models.require_model(base, 'other', 'synthetic-key')
        with pytest.raises(ValueError, match='model_not_loaded'):
            models.require_model(base, 'sleeping', 'synthetic-key')
        with pytest.raises(ValueError, match='model_discovery_failed'):
            models.fetch_models(base + '/redirect', 'synthetic-key')
        with pytest.raises(ValueError, match='model_discovery_failed'):
            models.fetch_models(base + '/large', 'synthetic-key')
        assert len(calls) == 5
    finally:
        server.shutdown(); server.server_close(); thread.join()
