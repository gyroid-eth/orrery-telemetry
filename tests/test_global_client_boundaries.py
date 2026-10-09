"""Explicit blocked provider boundaries; no real Windows/process/LLM launch."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/windows'))
    spec = importlib.util.spec_from_file_location('global_windows_boundary', ROOT / 'scripts/windows/codex_launcher.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'require_private', lambda path: None)
    return module


@pytest.mark.parametrize('change,reason', [('none', 'GLOBAL_S1_UNIX_CONTRACT_UNSUPPORTED'),
    ('quiescing', 'WRITER_FENCED'), ('retired', 'WRITER_FENCED'), ('epoch', 'WRITER_FENCED'),
    ('invalid', 'GLOBAL_CONTEXT_INVALID')])
def test_native_global_has_fixed_reason_before_legacy_registration(windows, tmp_path, monkeypatch, change, reason):
    authority = {'kind': 'orrery-global-authority-v1', 'phase': 'active', 'root_status': 'active',
                 'runtime_root': str(tmp_path), 'mail_instance_id': 'fixture-instance',
                 'candidate_generation': 'fixture-candidate', 'authority_epoch': 'fixture-epoch'}
    config = {'kind': 'orrery-runtime-client-s1', 'mode': 'global', 'activation_enabled': False,
              'authority': str(tmp_path / 'authority.json'), 'runtime_root': str(tmp_path),
              'expected_server_instance_id': 'fixture-instance', 'candidate_generation': 'fixture-candidate',
              'authority_epoch': 'fixture-epoch'}
    if change == 'quiescing':
        authority['phase'] = 'quiescing'
    if change == 'retired':
        authority['root_status'] = 'retired'
    if change == 'epoch':
        authority['authority_epoch'] = 'stale'
    if change == 'invalid':
        config['kind'] = 'unknown'
    (tmp_path / 'authority.json').write_text(json.dumps(authority))
    path = tmp_path / 'client.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv('AGENTSTACK_CLIENT_CONFIG', str(path))
    monkeypatch.setenv('AGENTSTACK_PROJECT_KEY', 'obsolete-project')
    with pytest.raises(RuntimeError, match='^' + reason + '$'):
        windows.reject_global_preparation()
    with pytest.raises(RuntimeError, match='^' + reason + '$'):
        windows.launch(None)


def test_native_absent_context_keeps_legacy(windows, monkeypatch):
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG', raising=False)
    assert windows.reject_global_preparation() is None
