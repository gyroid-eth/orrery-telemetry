"""Exercise the generated stdio command after native-style env filtering."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentstack_mail import fresh_harness, fresh_proxy
from agentstack_mail.fresh_runtime import _read_private, _write_private

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('provider', ['hermes', 'dsh'])
def test_managed_mcp_pins_mail_python_through_native_safe_env(tmp_path, monkeypatch, provider):
    root = tmp_path / 'product'
    (root / 'bin').mkdir(parents=True)
    shutil.copy2(ROOT / 'bin/agentstack-runtime-client', root / 'bin/agentstack-runtime-client')
    fake_bin = tmp_path / 'path-bin';fake_bin.mkdir()
    wrong = fake_bin / 'python3'
    wrong.write_text('#!/bin/sh\necho wrong-path-python >&2\nexit 97\n');wrong.chmod(0o700)
    chosen = tmp_path / 'mail-python'
    chosen.write_text('#!/bin/sh\n[ "$1" = -m ] && [ "$2" = agentstack_mail.fresh_client ] && [ "$3" = proxy ] || exit 98\n'
                      'printf "selected-mail-python\\n"\n')
    chosen.chmod(0o700)
    monkeypatch.setattr(sys, 'executable', str(chosen))
    client_home = tmp_path / 'client';client_home.mkdir(mode=0o700)
    run = tmp_path / 'run';run.mkdir(mode=0o700)
    client = SimpleNamespace(path=client_home / 'context.json')

    async def verified(_):
        return {'agent_name': 'FixtureOwner'}
    monkeypatch.setattr(fresh_proxy, 'verified_owner', verified)

    def generate(document, _):
        _write_private(Path(document['state_dir']) / 'launch.json', {'argv': ['unused'], 'env': {}})
    api = dict(fresh_instructions=lambda *_: tmp_path / 'instructions', dynamic_prelaunch=lambda _: None,
               generate_plan=generate, read=lambda p: json.loads(Path(p).read_text()), validate_plan=lambda *_: None)
    monkeypatch.setattr(fresh_harness, 'helper', lambda _: api)
    settings = dict(model='fixture-model', runner={'connection_kind': 'api-key'},
                    spec={'selection': {'manifest_file': '/fixture/manifest.json', 'backend_id': 'fixture'}})
    fresh_harness.plan(client, root, provider, settings, run, tmp_path, '', None)
    mcp = _read_private(run / 'managed/mail.json')['mcpServers']['orrery-mail']
    assert mcp['env'] == dict(AGENTSTACK_RUNTIME_MODE='fresh',
        AGENTSTACK_FRESH_CLIENT_CONTEXT=str(client.path), AGENTSTACK_PYTHON=str(chosen))

    # Hermes' safe baseline deliberately drops a parent's AGENTSTACK_PYTHON.
    # Run the REAL shell entry: merely checking the provider's Popen env hid
    # this defect. The two interpreters are inert fixture executables.
    parent = dict(HOME=str(tmp_path), PATH=str(fake_bin) + os.pathsep + os.defpath,
                  AGENTSTACK_PYTHON=str(chosen), PRIVATE_FIXTURE_TOKEN='never-forward')
    safe_keys = {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TERM', 'SHELL', 'TMPDIR'}
    safe = {key: value for key, value in parent.items() if key in safe_keys}
    selection = {key: value for key, value in mcp['env'].items() if key != 'AGENTSTACK_PYTHON'}
    command = [mcp['command'], *mcp['args']]
    missing = subprocess.run(command, env={**safe, **selection}, capture_output=True, text=True, timeout=10)
    assert missing.returncode == 97 and 'wrong-path-python' in missing.stderr
    passed = subprocess.run(command, env={**safe, **mcp['env']}, capture_output=True, text=True, timeout=10)
    assert passed.returncode == 0 and passed.stdout == 'selected-mail-python\n'
    assert 'PRIVATE_FIXTURE_TOKEN' not in mcp['env'] and 'PYTHONPATH' not in mcp['env']
