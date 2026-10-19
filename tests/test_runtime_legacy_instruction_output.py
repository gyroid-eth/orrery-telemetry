"""Explicit private instruction output; no provider/key/Mail/native execution."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat

import pytest

from _installed_client_payload import ROOT
from test_runtime_legacy_configure import prepared as configure_fixture, plan
from test_runtime_legacy_context import prepared as context_fixture
from test_runtime_configure_ownership import snapshot
from local_model_auth import LocalModelError


@pytest.fixture
def prepared(configure_fixture):
    client, ws, args, configure, _, _ = configure_fixture
    root = Path(client.config['wrapper_root'])
    template = root / 'claude/CLAUDE.md'
    template.parent.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'claude/CLAUDE.md', template)
    # An ordinary user file is not a source to adopt or replace implicitly.
    ordinary = ws.tmp / 'home/.claude/CLAUDE.md'
    ordinary.parent.mkdir(mode=0o700)
    ordinary.write_bytes(b'personal instructions\r\n')
    return configure_fixture, template, ordinary


def instruction(prepared, value):
    fixture, _, _ = prepared
    client = fixture[0]
    path = client.profile_path.with_suffix('.preview-home') / 'CLAUDE.md'
    found = [row for row in value['output_plan']['outputs'] if row['path'] == str(path)]
    assert len(found) == 1, 'LEGACY_PRIVATE_INSTRUCTION_OUTPUT_MISSING'
    return path, found[0]


def test_plan_uses_shared_renderer_and_pins_the_fixed_template_without_writes(prepared):
    fixture, template, ordinary = prepared
    client, ws, _, _, _, _ = fixture
    before = snapshot(ws.tmp)
    value = plan(fixture)
    path, output = instruction(prepared, value)
    assert snapshot(ws.tmp) == before and not path.exists()
    from instruction_blocks import render
    expected = render('claude', template.read_bytes(), str(template.parents[1]),
                      ws.context['legacy_context']['project_key'])
    assert output['content'].encode() == expected
    assert output['kind'] == 'provider-instructions' and output['mode'] == 0o600
    proof = value['intent']['configuration_evidence']['renderer_sources'][str(template)]
    assert proof['state'] == 'present' and proof['sha256'] == hashlib.sha256(template.read_bytes()).hexdigest()
    assert str(ordinary) not in value['intent']['configuration_evidence']['renderer_sources']


def test_apply_owns_only_generated_private_instructions_and_replays_readonly(prepared, monkeypatch):
    fixture, _, ordinary = prepared
    client, ws, _, configure, _, _ = fixture
    import instruction_blocks
    monkeypatch.setattr(instruction_blocks, 'provider_instructions',
                        lambda *a, **k: pytest.fail('imperative instruction migration called'))
    value = plan(fixture)
    path, output = instruction(prepared, value)
    old = (ordinary.read_bytes(), ordinary.stat().st_ino)
    result = configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    assert path.read_bytes() == output['content'].encode()
    manifest = json.loads((Path(client.config['wrapper_root']) / 'install-state.json').read_text())
    assert manifest['configure_owned'][str(path)] == output['after']
    assert str(path) in manifest['owned_files'] and str(ordinary) not in manifest['owned_files']
    assert (ordinary.read_bytes(), ordinary.stat().st_ino) == old
    assert result['launch_admissible'] is False and result['requires_native_validation'] is True
    before = snapshot(ws.tmp)
    assert configure.apply_local_configuration(client, value, approved_id=value['plan_id'])['changed'] is False
    assert snapshot(ws.tmp) == before


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'symlink', 'hardlink'])
def test_changed_template_cannot_publish_the_old_approved_plan(prepared, mutation):
    fixture, template, ordinary = prepared
    client, ws, _, configure, _, _ = fixture
    value = plan(fixture)
    if mutation == 'changed':
        template.write_bytes(template.read_bytes() + b'\nchanged instructions\n')
    elif mutation == 'missing':
        template.unlink()
    elif mutation == 'symlink':
        template.unlink(); template.symlink_to(ordinary)
    else:
        os.link(template, template.with_name('alias'))
    with pytest.raises(LocalModelError):
        configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    assert not client.profile_path.exists()
    assert not client.profile_path.with_suffix('.preview-home').exists()
    assert ordinary.read_bytes() == b'personal instructions\r\n'


@pytest.mark.parametrize('artifact', ['regular', 'symlink', 'hardlink'])
def test_unowned_private_instruction_is_not_adopted_or_replaced(prepared, artifact):
    fixture, _, ordinary = prepared
    client, ws, _, _, _, _ = fixture
    path = client.profile_path.with_suffix('.preview-home') / 'CLAUDE.md'
    path.parent.mkdir(mode=0o700)
    if artifact == 'regular':
        path.write_bytes(b'private but unowned\n'); path.chmod(0o600)
    elif artifact == 'symlink':
        path.symlink_to(ordinary)
    else:
        os.link(ordinary, path)
    before = snapshot(ws.tmp)
    with pytest.raises(LocalModelError):
        plan(fixture)
    assert snapshot(ws.tmp) == before


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'symlink', 'mode', 'hardlink'])
def test_registration_projection_rechecks_instruction_before_any_mail(prepared, mutation):
    fixture, _, ordinary = prepared
    client, ws, _, configure, _, _ = fixture
    value = plan(fixture)
    path, _ = instruction(prepared, value)
    configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    api = runpy.run_path(str(Path(client.config['wrapper_root']) / 'bin/lib/runtime_legacy_mail.py'))
    assert api['_registration_projection'](value['intent'], ws.caller) == ws.context['legacy_context']
    if mutation == 'changed':path.write_bytes(b'replaced instructions\n')
    elif mutation == 'missing':path.unlink()
    elif mutation == 'symlink':path.unlink(); path.symlink_to(ordinary)
    elif mutation == 'mode':path.chmod(0o644)
    else:os.link(path, path.with_name('alias'))
    before = snapshot(ws.tmp)
    with pytest.raises(LocalModelError):
        api['_registration_projection'](value['intent'], ws.caller)
    assert snapshot(ws.tmp) == before
    assert not list(ws.tmp.rglob('agent_token_*'))
