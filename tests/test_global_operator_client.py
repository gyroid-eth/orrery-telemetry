"""Operator/root and child entries use one same-intent registration journal."""
import json
from pathlib import Path
import runpy

import pytest
from test_global_child_client import server

ROOT=Path(__file__).resolve().parents[1]
api=runpy.run_path(str(ROOT/'bin/lib/runtime_operator.py'))


@pytest.fixture
def operator(server,monkeypatch):
    monkeypatch.setenv('AGENTSTACK_MCP_URL',server['url'])
    return api['RuntimeOperator'](server['config_path'])


def test_absent_root_only_registration_and_lookup_are_explicit(operator):
    assert operator.status('fresh')['stage']=='absent'
    assert not operator.clients_parent.exists()
    operator.prepare('fresh')
    path=operator.path_for('fresh')
    original=path.read_bytes()
    assert operator.status('fresh')['stage']=='root_only'
    assert path.read_bytes()==original
    result=operator.create('fresh',agent_name='OperatorFresh',program='daemon',model='deterministic')
    assert result['stage']=='registered_unconfigured'
    assert result['agent_id']
    assert operator.lookup('OperatorFresh')['matches']==[{'client_name':'fresh','agent_id':result['agent_id'],'stage':'registered_unconfigured'}]
    refused=operator.create('another',agent_name='OperatorFresh',program='daemon',model='deterministic')
    assert refused['reason']=='EXISTING_CLIENT_REQUIRED'
    assert not operator.path_for('another').parent.exists()
    assert 'registration_token' not in json.dumps(operator.lookup())


@pytest.mark.parametrize('point',['before-request','request','row','credential','context','metadata'])
def test_every_registration_interruption_has_one_explicit_resume(operator,monkeypatch,point):
    operator.prepare('recover')
    client=operator.client('recover')
    original_rpc=client.rpc
    original_write=client.write_output
    before=operator.read({'kind':'health'})['as_of_revision']
    def rpc(method,params):
        if point=='before-request' and method=='tools/call' and params.get('name')=='register_agent':
            raise operator.api['ClientError']('TRANSPORT_FAILED')
        value=original_rpc(method,params)
        if point=='request' and method=='tools/call' and params.get('name')=='register_agent':
            raise operator.api['ClientError']('TRANSPORT_FAILED')
        return value
    def write(path,value,role):
        if point=='row' and role=='registration' and 'row' in value:raise OSError('interrupted')
        original_write(path,value,role)
        if role==point:raise OSError('interrupted')
    monkeypatch.setattr(client,'rpc',rpc)
    monkeypatch.setattr(client,'write_output',write)
    with pytest.raises((RuntimeError,OSError)):
        client.create('OperatorRecovered','daemon','deterministic')
    pending=operator.api['read_json'](client.outputs['registration'])
    token=pending['registration_token']
    state=operator.status('recover')
    assert state['stage']==('registration_pending' if point in {'before-request','request','row'} else 'registration_saved'),state
    result=operator.create('recover',agent_name='OperatorRecovered',program='daemon',model='deterministic')
    assert result['stage']=='registered_unconfigured',result
    recovered=operator.client('recover')
    assert recovered.local_owner()==token
    assert operator.read({'kind':'health'})['as_of_revision']==before+1
    assert not recovered.outputs['registration'].exists()
    assert recovered.observe()['agent_id']==result['agent_id']


def test_parallel_root_prepare_does_not_overwrite_identity(operator):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(operator.prepare,['same','same']))
    result=operator.create('same',agent_name='OperatorSame',program='daemon',model='deterministic')
    before=operator.path_for('same').read_bytes()
    operator.prepare('same')
    assert operator.path_for('same').read_bytes()==before
    assert operator.status('same')['agent_id']==result['agent_id']


@pytest.mark.parametrize('placement',['client-home','installed-config','installed-helper'])
def test_tmux_global_protection_reads_existing_snapshot_without_new_state(operator,placement):
    operator.create('protected',agent_name='ProtectedStandalone',program='daemon',model='deterministic')
    client=operator.client('protected')
    with client.operation_mutex():
        pass
    directory=client.runtime_dir/'live-sessions'
    directory.mkdir(mode=0o700,exist_ok=True)
    live={'kind':'orrery-global-live-session-v1',**client.binding,
          'agent_id':client.identity['agent_id'],'session_id':'standalone',
          'pid':400,'process_start':'fixture-start','pane_id':'%4'}
    operator.api['atomic_json'](directory/'standalone.json',live)
    before={p.relative_to(client.client_root):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}
    revision=operator.read({'kind':'health'})['as_of_revision']
    helper=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux.py'))
    home=operator.clients_parent.parent
    if placement in ('installed-config','installed-helper'):
        import shlex
        home=operator.isolation/'legacy-wrapper';home.mkdir(mode=0o755);home.chmod(0o755)
        (home/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=global\nexport AGENTSTACK_GLOBAL_SERVER_CONFIG='
            +shlex.quote(str(operator.path))+'\nexport UNRELATED=$(touch '+shlex.quote(str(home/'must-not-execute'))+')\n')
    if placement=='installed-helper':
        from _installed_client_payload import copy_client_library
        installed=operator.isolation/'installed-tmux-helper'
        copy_client_library(installed)
        helper=runpy.run_path(str(installed/'bin/lib/runtime_tmux.py'))
        proofs=helper['global_proofs'](home)
    else:
        proofs=helper['global_proofs'](home,api=operator.api)
    assert len(proofs)==1 and proofs[0]['live']==live
    assert 'registration_token' not in json.dumps(proofs)
    assert operator.read({'kind':'health'})['as_of_revision']==revision
    assert {p.relative_to(client.client_root):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}==before
    assert not client.outputs['launch'].exists()
    assert not (home/'must-not-execute').exists()


def test_foreign_name_pending_requires_explicit_digest_abandon(operator):
    import hashlib
    operator.prepare('foreign')
    client=operator.client('foreign')
    with pytest.raises(RuntimeError,match='OWNER_REQUIRED'):
        client.create('Alpha','daemon','deterministic')
    pending=client.outputs['registration'];original=pending.read_bytes()
    with pytest.raises(RuntimeError,match='REGISTRATION_PENDING_CONFLICT'):
        client.abandon_registration('0'*64)
    assert pending.read_bytes()==original
    result=client.abandon_registration(hashlib.sha256(original).hexdigest())
    assert result['terminal'] is True
    assert operator.status('foreign')['stage']=='unavailable'


def test_read_status_does_not_write_context_or_provider_configuration(operator):
    operator.create('stable',agent_name='OperatorStable',program='daemon',model='deterministic')
    root=operator.path_for('stable').parent
    before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    operator.lookup('OperatorStable');operator.status('stable')
    after={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert before==after


def test_repeated_same_completed_create_is_readonly_and_different_metadata_refuses(operator):
    first=operator.create('repeat',agent_name='OperatorRepeat',program='daemon',model='deterministic')
    root=operator.path_for('repeat').parent
    before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    revision=operator.read({'kind':'health'})['as_of_revision']
    assert operator.create('repeat',agent_name='OperatorRepeat',program='daemon',model='deterministic')['agent_id']==first['agent_id']
    with pytest.raises(RuntimeError,match='EXISTING_IDENTITY_REQUIRED'):
        operator.create('repeat',agent_name='OperatorRepeat',program='daemon',model='another')
    assert operator.read({'kind':'health'})['as_of_revision']==revision
    assert {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}==before


def test_installed_operator_uses_installer_payload_and_shared_schema(operator,monkeypatch):
    import os,re,shutil,subprocess,sys
    from _installed_client_payload import copy_client_library
    installed=operator.isolation/'installed'
    copy_client_library(installed)
    (installed/'dashboard').mkdir(mode=0o700,exist_ok=True)
    shutil.copy2(ROOT/'dashboard/http_operator.py',installed/'dashboard/http_operator.py')
    declaration=re.search(r'^RUNTIME_PUBLIC_ENTRY_SOURCES=\(\n(.*?)^\)',(ROOT/'scripts/install.sh').read_text(),re.M|re.S)
    sources=re.findall(r'"\$REPO_ROOT/([^"\n]+)"',declaration.group(1))
    assert set(sources)=={'bin/agentstack-http-operator','bin/agentstack-ui-client'}
    for source in sources:shutil.copy2(ROOT/source,installed/source)
    env={**os.environ,'AGENTSTACK_GLOBAL_SERVER_CONFIG':str(operator.path),'AGENTSTACK_MCP_URL':os.environ['AGENTSTACK_MCP_URL']}
    result=subprocess.run([sys.executable,str(installed/'bin/agentstack-ui-client'),'register','--client-name','installed',
        '--name','InstalledOwner','--program','daemon','--model','deterministic'],env=env,text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['stage']=='registered_unconfigured'
    config=json.loads((operator.isolation/'agentstack/clients/installed/runtime-client.json').read_text())
    assert config['wrapper_root']==str(installed)


@pytest.mark.parametrize('retired',[False,True])
def test_existing_cancelled_launch_can_resume_same_retired_identity(operator,monkeypatch,retired):
    import sys
    operator.create('resume',agent_name='OperatorResume',program='daemon',model='deterministic')
    client=operator.client('resume')
    client.save_profile()
    delivery=operator.api['runtime_delivery'](client)
    delivery.configure_execution({'argv':[sys.executable], 'interaction':'interactive','cwd':str(client.runtime_dir)})
    client.record_session({'session_id':'fixture-session','cwd':str(client.runtime_dir),'tool_response':client.call('whois')})
    row=delivery.prepare_launch('daemon',cwd=client.runtime_dir)
    status=delivery.status(row['launch_id'])
    delivery.operator('launch-cancel',{'launch_id':row['launch_id'],'expected_state_sha256':status['state_sha256'],'confirm':True})
    if retired:client.call('retire_agent')
    before=operator.read({'kind':'health'})['as_of_revision']
    assert operator.status('resume')['stage']=='launch_present'
    assert operator.read({'kind':'health'})['as_of_revision']==before
    launched=[]
    monkeypatch.setattr(operator.api['RuntimeClient'],'session_liveness',lambda self:'absent')
    monkeypatch.setattr(type(delivery),'start',lambda self,*args,**kw:launched.append((args,kw)))
    result=operator.resume('resume')
    assert result['agent_id']==client.identity['agent_id']
    assert launched[0][1]['launch_kind']=='resume'
    assert launched[0][1]['provider_session_id']=='fixture-session'
    assert operator.client('resume').management('inspect',agent_id=client.identity['agent_id'])['retired_at'] is None
    assert operator.client('resume').local_owner()==client.local_owner()


@pytest.mark.parametrize('entry',['operator','client'])
@pytest.mark.parametrize('change,reason',[
    ('schema','RESPONSE_INVALID'),
    ('binding','MANAGEMENT_BINDING_MISMATCH'),
    ('query','RESPONSE_INVALID'),
])
def test_received_operator_projection_is_checked_by_one_contract(operator,monkeypatch,entry,change,reason):
    operator.create('read-contract',agent_name='ReadContract',program='daemon',model='deterministic')
    client=operator.client('read-contract')
    namespace=operator.api['operator_exchange'].__globals__
    original=namespace['management_exchange']
    def exchange(transport,request):
        reply=original(transport,request)
        if request['action']=='ui_read':
            if change=='schema':reply['as_of_revision']='invalid'
            elif change=='binding':reply['binding']['authority_epoch']='different'
            else:reply['query_kind']='roster'
        return reply
    monkeypatch.setitem(namespace,'management_exchange',exchange)
    read=operator.read if entry=='operator' else client.operator_read
    with pytest.raises(operator.api['ClientError'],match=reason):
        read({'kind':'health'})


def test_foreign_mail_name_is_rejected_before_root_or_pending(operator):
    before=operator.read({'kind':'health'})['as_of_revision']
    result=operator.create('unused',agent_name='Alpha',program='daemon',model='deterministic')
    assert result['reason']=='NAME_CONFLICT'
    assert not operator.path_for('unused').parent.exists()
    assert operator.read({'kind':'health'})['as_of_revision']==before
    assert operator.create('unused',agent_name='AvailableName',program='daemon',model='deterministic')['ok']


def test_bad_and_terminal_roots_do_not_block_unrelated_creation(operator):
    import hashlib
    operator.prepare('foreign')
    client=operator.client('foreign')
    with pytest.raises(RuntimeError,match='OWNER_REQUIRED'):
        client.create('Alpha','daemon','deterministic')
    client.abandon_registration(hashlib.sha256(client.outputs['registration'].read_bytes()).hexdigest())
    original=client.outputs['registration'].read_bytes()
    (operator.clients_parent/'.DS_Store').write_bytes(b'opaque')
    operator.prepare('broken')
    operator.path_for('broken').write_bytes(b'broken')
    result=operator.create('unrelated',agent_name='UnrelatedName',program='daemon',model='deterministic')
    assert result['ok']
    lookup=operator.lookup()
    assert {r['client_name'] for r in lookup['diagnostics']}=={'.DS_Store','broken','foreign'}
    assert client.outputs['registration'].read_bytes()==original
    assert operator.create('another-alpha',agent_name='Alpha',program='daemon',model='deterministic')['reason']=='NAME_CONFLICT'
    assert not operator.path_for('another-alpha').parent.exists()
    assert operator.status('broken')['stage']=='unavailable'
    with pytest.raises(RuntimeError):
        operator.create('broken',agent_name='AnotherName',program='daemon',model='deterministic')


@pytest.mark.parametrize('program',['claude-code','codex'])
def test_missing_instruction_configuration_is_a_status_not_exception(operator,program):
    result=operator.create('provider',agent_name='ProviderOwner',program=program,model='fixture')
    assert result['stage']=='registered_unconfigured'
    assert result['reason']=='INSTRUCTION_PROVIDER_CONFIG_REQUIRED'
    assert result['next_operations']==['configure-provider']
    assert operator.lookup('ProviderOwner')['matches'][0]['agent_id']==result['agent_id']


def test_retired_identity_without_launch_is_still_discoverable(operator):
    result=operator.create('retired',agent_name='RetiredOwner',program='daemon',model='deterministic')
    operator.client('retired').call('retire_agent')
    assert operator.lookup('RetiredOwner')['matches'][0]['agent_id']==result['agent_id']
    assert operator.create('other',agent_name='OtherOwner',program='daemon',model='deterministic')['ok']


@pytest.mark.parametrize('action',['retry','abandon'])
def test_registration_resolution_unsupported_server_has_one_reason(operator,monkeypatch,action):
    import hashlib
    operator.prepare('unsupported')
    client=operator.client('unsupported')
    monkeypatch.setattr(client,'operator_read',lambda query: (_ for _ in ()).throw(operator.api['ClientError']('MANAGEMENT_ACTION_UNSUPPORTED')))
    with pytest.raises(RuntimeError,match='^NAME_CONFLICT$'):
        client.create('Alpha','daemon','deterministic')
    original=client.outputs['registration'].read_bytes()
    with pytest.raises(RuntimeError,match='^NAME_CONFLICT$'):
        if action=='retry':client.create('Alpha','daemon','deterministic')
        else:client.abandon_registration(hashlib.sha256(original).hexdigest())
    assert client.outputs['registration'].read_bytes()==original
