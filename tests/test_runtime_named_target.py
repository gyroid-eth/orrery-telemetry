"""Named wrappers cannot turn another name into an operation on self."""
import json
import os
from pathlib import Path

import pytest

from test_global_runtime_client import api, client, prepared, run
from test_global_child_client import server as child_server


@pytest.mark.parametrize('name',[None,'','Beta','bad/name'])
def test_missing_global_context_keeps_existing_reason_before_name_or_constructor(name):
    import runpy
    guard=runpy.run_path(str(Path(__file__).resolve().parents[1]/'bin/lib/runtime_named_target.py'))
    def forbidden(*args,**kwargs):pytest.fail('missing binding constructed a client')
    with pytest.raises(RuntimeError,match='^CONTEXT_UNAVAILABLE$'):
        guard['require_named_target']({'mode':'global','context':None},name,
                                     {'ClientError':RuntimeError,'RuntimeClient':forbidden})


def snapshot(root):
    return {str(path.relative_to(root)):path.read_bytes()
            for path in root.rglob('*') if path.is_file() and not path.is_symlink()}


@pytest.mark.parametrize('wrapper', ['bin/agentstack-purge-child-resume', 'bin/agentstack-reregister'])
@pytest.mark.parametrize('requested', ['Beta', 'UnknownChild', '', 'bad/name'])
def test_real_wrapper_rejects_foreign_or_unknown_target_before_any_network_or_self_operation(prepared, tmp_path, wrapper, requested):
    c = client(prepared)
    # A real other server identity already exists in this migration fixture.
    # Its separate local sentinel tree must also remain byte-identical.
    other = c.clients_parent/'other'
    other.mkdir(mode=0o700)
    for filename in ('credential.json','profile.json','runtime-client.json'):
        (other/filename).write_text(json.dumps({'fixture_owner':'Beta', 'sentinel':filename}))
        (other/filename).chmod(0o600)
    (other/'runtime').mkdir(mode=0o700)
    for filename in ('mutation-pending.json','launch.json'):
        (other/'runtime'/filename).write_text(json.dumps({'fixture_pending':'stable', 'slot':filename}))
    probe = tmp_path/'probe'
    probe.mkdir()
    called = tmp_path/'unexpected-calls'
    # Instrument all wrapper Python processes without replacing any real
    # entry/parser/selector/guard. No egress is possible even with a mutant.
    (probe/'sitecustomize.py').write_text(
        'import socket,sys\nfrom pathlib import Path\n'
        f'p=Path({str(called)!r})\n'
        'def network(*args,**kwargs):\n'
        '    p.write_text("network attempted")\n'
        '    raise AssertionError("non-fixture network attempted")\n'
        'socket.socket.connect=network\nsocket.socket.connect_ex=network\n'
        'def trace(frame,event,arg):\n'
        '    if event=="call" and frame.f_code.co_name in ("purge_child","reconnect"):\n'
        '        p.write_text("self operation attempted")\n'
        'sys.setprofile(trace)\n')
    before = snapshot(c.clients_parent)
    args = [requested] if requested else []
    output = run(prepared, wrapper, *args, bash=True,
                 extra={'PYTHONPATH':str(probe)+os.pathsep+os.environ.get('PYTHONPATH','')})
    assert output.returncode != 0
    assert any(reason in output.stderr for reason in (
        'REGISTRATION_IDENTITY_MISMATCH','TARGET_UNAVAILABLE'))
    assert not called.exists(), 'named wrapper performed network or a self operation'
    assert snapshot(c.clients_parent) == before


def test_correct_reregister_target_preserves_existing_metadata_contract(prepared):
    c = client(prepared)
    result = run(prepared, 'bin/agentstack-reregister', c.identity['name'],
                 'claude-code', 'fixture-model', bash=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(c.metadata_path.read_text())['model'] == 'fixture-model'


def test_direct_purge_entry_also_checks_explicit_name(prepared):
    c = client(prepared)
    before = snapshot(c.client_root)
    result = run(prepared, 'bin/lib/runtime_client.py', 'entry', 'purge-child', 'Beta')
    assert result.returncode != 0 and 'REGISTRATION_IDENTITY_MISMATCH' in result.stderr
    assert snapshot(c.client_root) == before


@pytest.mark.parametrize('operation',['reconnect','purge_child'])
def test_context_rename_between_admission_and_mutex_stops_before_fresh_network(prepared,monkeypatch,operation):
    c = client(prepared)
    c._explicit_target_name = c.identity['name']
    api['atomic_json'](c.path,{**c.config,'identity':{**c.identity,'name':'Beta'}})
    before = snapshot(c.client_root)
    def forbidden(*args,**kwargs):
        pytest.fail('fresh owner network reached despite changed explicit target')
    monkeypatch.setattr(c,'inspect_owner',forbidden)
    monkeypatch.setattr(c,'management',forbidden)
    with pytest.raises(api['ClientError'],match='REGISTRATION_IDENTITY_MISMATCH'):
        getattr(c,operation)()
    assert snapshot(c.client_root) == before


@pytest.mark.parametrize('wrapper', ['bin/agentstack-purge-child-resume', 'bin/agentstack-reregister'])
def test_wrong_name_never_initializes_an_operation_lock(prepared, wrapper):
    root = prepared['client_path'].parent
    lock = root/'runtime/child.lock'
    assert not lock.exists()
    before = snapshot(root)
    result = run(prepared, wrapper, 'Beta', bash=True)
    assert result.returncode != 0 and 'REGISTRATION_IDENTITY_MISMATCH' in result.stderr
    assert not lock.exists()
    assert snapshot(root) == before


def test_real_save_profile_keeps_the_single_launch_intent(prepared, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'bin/lib'))
    from local_model_auth import intent_digest
    from runtime_intent_store import save_launch_intent
    c = client(prepared)
    c.save_profile()
    value = {'schema_version':1, 'revision':1, 'name':c.identity['name'],
             'provider':'codex','model':{'kind':'catalog','id':'fixture-model'}}
    value['intent_digest'] = intent_digest(value)
    save_launch_intent(c.profile_path, value, mode='global')
    c.save_profile()
    assert json.loads(c.profile_path.read_text())['launch_intent'] == value
    assert sum('"launch_intent"' in path.read_text() for path in c.client_root.rglob('*.json')) == 1


def test_correct_purge_wrapper_uses_its_retained_child_and_preserves_credentials(child_server):
    from test_global_child_client import api as child_api, client as child_client, shell
    parent = child_client(child_server)
    prepared_child = parent.prepare_child({'child_client_name':'target', 'name':'TargetChild',
                                          'program':'codex','model':'fixture-model','prepare_only':True})
    child = child_api['RuntimeClient'](prepared_child['client_config'])
    state = child_api['read_json'](child.outputs['child'])
    child.call('retire_agent')
    child.write_output(child.outputs['child'], {**state,'phase':'retained',
                        'resume_expires_at':'2000-01-01T00:00:00+00:00'}, 'child')
    child.outputs['task'].write_text('fixture task')
    child.outputs['task'].chmod(0o600)
    # Explicit fixture evidence of no live sessions. A missing directory is
    # correctly unknown and must not become sufficient purge admission.
    (child.runtime_dir/'live-sessions').mkdir(mode=0o700)
    credential = child.credential.read_bytes()
    result = shell(child, 'bin/agentstack-purge-child-resume', ['TargetChild'])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['purged'] is True
    assert child.credential.read_bytes() == credential
    assert not child.outputs['task'].exists()
