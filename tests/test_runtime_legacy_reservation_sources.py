"""Pinned shared parser/matcher, not a connected edit hook or OS acceptance."""
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy

import pytest

from test_runtime_legacy_configure import prepared, plan, context_fixture
from local_model_auth import LocalModelError


FILES = ('check-reservation-conflicts.py', 'reservation-patterns.py')


def contract(prepared):
    client, _, _, _, _, _ = prepared
    root = Path(client.config['wrapper_root'])
    intent = plan(prepared)['intent']
    module = runpy.run_path(str(root / 'bin/lib/runtime_legacy_proxy.py'))
    loader = module.get('_reservation_contract')
    assert callable(loader), 'PINNED_RESERVATION_LOADER_MISSING'
    return root, intent, module, loader


def check(loaded):
    reader, matcher = loaded
    rows = [{'agent': 'AnotherAgent', 'path_pattern': 'scratch/**', 'exclusive': True,
        'expires_ts': '2030-01-01T00:01:00Z', 'released_ts': None}]
    document = {'result': {'contents': [{'text': json.dumps(rows)}]}}
    return reader(document, agent='FixtureChild', targets=('scratch/note.md',),
        now=datetime(2030, 1, 1, tzinfo=timezone.utc), matcher_factory=lambda: matcher)


def test_configuration_pins_both_real_installed_helpers(prepared):
    client, _, _, _, _, _ = prepared
    root = Path(client.config['wrapper_root'])
    sources = plan(prepared)['intent']['configuration_evidence']['renderer_sources']
    for name in FILES:
        assert sources.get(str(root / 'hooks' / name), {}).get('state') == 'present'


def test_loader_uses_real_parser_and_matcher_without_legacy_transport(prepared, monkeypatch):
    root, intent, module, loader = contract(prepared)
    import runpy as runpy_module
    monkeypatch.setattr(runpy_module, 'run_path', lambda *a, **k: pytest.fail('ambient matcher path used'))
    assert check(loader(intent, root)) == {'AnotherAgent'}


@pytest.mark.parametrize('name', FILES)
def test_changed_helper_is_rejected_before_any_new_bytes_execute(prepared, name):
    root, intent, _, loader = contract(prepared)
    assert check(loader(intent, root)) == {'AnotherAgent'}
    marker = root / 'unverified-helper-ran'
    (root / 'hooks' / name).write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ").touch()\n")
    with pytest.raises(LocalModelError): loader(intent, root)
    assert not marker.exists()


@pytest.mark.parametrize('name', FILES)
def test_changed_read_bytes_are_rejected_even_with_unchanged_path_evidence(prepared, monkeypatch, name):
    root, intent, _, loader = contract(prepared)
    assert check(loader(intent, root)) == {'AnotherAgent'}
    namespace = loader.__globals__; original = namespace['secure_read']; replaced = []
    marker = root / 'unverified-read-bytes-ran'
    def changed(path, *args, **kwargs):
        raw = original(path, *args, **kwargs)
        if Path(path).name == name:
            replaced.append(True)
            return b'from pathlib import Path\nPath(' + repr(str(marker)).encode() + b').touch()\n'
        return raw
    monkeypatch.setitem(namespace, 'secure_read', changed)
    with pytest.raises(LocalModelError): loader(intent, root)
    assert replaced and not marker.exists()
