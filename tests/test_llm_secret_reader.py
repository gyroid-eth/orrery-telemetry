"""偽の参照とrunnerだけで確認し、OS storeや実鍵には接続しない。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('secret_reader_fixture', ROOT / 'hooks/llm_secret_reader.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
REF = {'store': 'secret-service', 'attributes': {'service': 'fixture-service', 'account': 'fixture-account'}}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    tool = tmp_path / 'secret-tool'
    tool.write_text('synthetic executable, never run')
    tool.chmod(0o700)
    monkeypatch.setattr(reader, 'SECRET_TOOL', str(tool))
    monkeypatch.setattr(reader.sys, 'platform', 'linux')
    return tool


@pytest.mark.parametrize('ref', [None, {}, {'store': 'file', 'attributes': {}},
    {'store': 'secret-service', 'attributes': {}},
    {'store': 'secret-service', 'attributes': {'a': 'bad\nvalue'}},
    {'store': 'secret-service', 'attributes': {'a': 1}},
    {**REF, 'key': 'must-not-be-a-value'},
    {'store': 'secret-service', 'attributes': {'--all': 'x'}},
    {'store': 'secret-service', 'attributes': {'service': '--all'}}])
def test_rejects_unknown_or_value_shaped_references(ref):
    with pytest.raises(ValueError, match='^invalid_credential_ref$'):
        reader.validate_ref(ref)


def test_lookup_uses_selected_argv_and_minimal_context(prepared, monkeypatch):
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/fixture/bus')
    monkeypatch.setenv('XDG_RUNTIME_DIR', '/fixture/runtime')
    monkeypatch.setenv('AMBIENT_PROVIDER_KEY', 'ambient-must-not-be-used')
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'synthetic-reader-value\n')
    assert reader.read_ref(REF, runner=run) == 'synthetic-reader-value'
    argv, options = calls[0]
    assert argv == [str(prepared), 'lookup', 'account', 'fixture-account', 'service', 'fixture-service']
    assert options['stdin'] == subprocess.DEVNULL and options['timeout'] == 5
    assert options['env']['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/fixture/bus'
    assert options['env']['XDG_RUNTIME_DIR'] == '/fixture/runtime'
    assert set(options['env']) == {'HOME', 'PATH', 'LANG', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'}
    assert 'synthetic-reader-value' not in json.dumps((argv, options))


@pytest.mark.parametrize('rc,output', [(1, b'private-error'), (0, b''), (0, b'\xff'),
    (0, b'first\nsecond\n'), (0, b'key\x00'), (0, b'x' * 65537)])
def test_failure_never_returns_ambient_or_raw_output(prepared, rc, output):
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=rc, stdout=output)
    with pytest.raises(ValueError) as error:
        reader.read_ref(REF, runner=run)
    assert str(error.value) == 'credential_unavailable'
    assert calls == [1]


def test_timeout_is_fixed_and_has_no_helper_output(prepared):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(['private-canary'], 5, output=b'private-output')
    with pytest.raises(ValueError) as error:
        reader.read_ref(REF, runner=run)
    assert str(error.value) == 'credential_timeout'
    assert error.value.__suppress_context__


def test_static_check_does_not_lookup_and_rejects_missing_tool(prepared, monkeypatch):
    monkeypatch.setattr(reader.subprocess, 'run', lambda *a, **k: pytest.fail('static check read a key'))
    reader.validate_ref(REF)
    reader.validate_environment()
    prepared.unlink()
    with pytest.raises(ValueError, match='^credential_reader_unavailable$'):
        reader.validate_environment()
    monkeypatch.setattr(reader.sys, 'platform', 'darwin')
    with pytest.raises(ValueError, match='^unsupported_credential_store$'):
        reader.validate_environment()


def test_internal_reader_command_and_manifest_select_only_one_backend(tmp_path):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'backends': {'deepseek': {'credential_ref': REF},
        'strata': {'credential_ref': None}}}))
    assert reader.manifest_ref(manifest, 'deepseek') == REF
    with pytest.raises(ValueError, match='^invalid_credential_ref$'):
        reader.manifest_ref(manifest, 'strata')
    argv = reader.command(ROOT, manifest, 'deepseek', sys.executable)
    assert argv == [sys.executable, '-I', '-B', str(ROOT / 'hooks/llm_secret_reader.py'),
                    '--manifest', str(manifest), '--backend', 'deepseek']


def test_installed_reader_minimal_environment_with_synthetic_store(tmp_path):
    import os
    import shutil
    target = tmp_path / 'installed/hooks/llm_secret_reader.py'
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'hooks/llm_secret_reader.py', target)
    shutil.copyfile(ROOT / 'hooks/native_connections.py', target.with_name('native_connections.py'))
    store = tmp_path / 'fake-secret-tool'
    store.write_text('#!/bin/sh\nprintf "synthetic-e1-value\\n"\n')
    store.chmod(0o700)
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'backends': {'deepseek': {'credential_ref': REF}}}))
    code = ('import importlib.util,sys; '
            's=importlib.util.spec_from_file_location("reader",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s);s.loader.exec_module(m); '
            'm.SECRET_TOOL=sys.argv[2];m.sys.platform="linux";sys.argv=[sys.argv[1],"--manifest",sys.argv[3],"--backend","deepseek"]; '
            'raise SystemExit(m.main())')
    done = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(target), str(store), str(manifest)],
        cwd='/', env={'HOME': str(tmp_path), 'PATH': os.defpath}, stdin=subprocess.DEVNULL,
        text=True, capture_output=True, timeout=10)
    assert done.returncode == 0 and done.stdout == 'synthetic-e1-value\n' and done.stderr == ''
