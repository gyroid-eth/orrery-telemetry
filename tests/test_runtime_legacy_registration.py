"""Real private Store, fake transport/process observations; no CLI or keys."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
sys.path.insert(0,str(Path(__file__).parent))
from local_model_auth import LocalModelError, intent_digest
from runtime_intent_store import read_owner, write_owner
from test_runtime_legacy_preview import intent, running

CANDIDATE='fixture-candidate-credential-000000000000'
ADOPTED='fixture-server-credential-000000000000000'


class Interrupted(BaseException):pass


@pytest.fixture
def store(tmp_path):
    try:module=importlib.import_module('runtime_legacy_registration')
    except ModuleNotFoundError:pytest.fail('LEGACY_REGISTRATION_LIFECYCLE_NOT_IMPLEMENTED')
    tmp_path.chmod(0o700);runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    directory=runtime/'child-agents';directory.mkdir(mode=0o700)
    owner=directory/'FixtureCurie.json';value=intent()
    value['model']['resolved']={'model':'fixture-wire-model'}
    value['legacy_context']={'schema_version':1,'install_root':str(tmp_path/'install'),
        'runtime_dir':str(runtime),'owner':str(owner),'name':'FixtureCurie',
        'project_key':'/fixture-project','project_id':7,'mail_url':'http://127.0.0.1:45679/mcp/',
        'parent':{'id':42,'name':'FixtureParent','program':'claude-code','model':'fixture-parent-model'}}
    value['intent_digest']=intent_digest(value)
    write_owner(owner,{'launch_intent':value,'sentinel':'must-keep'})
    return module.Store(runtime,'FixtureCurie')


def reply():
    return {'id':41,'name':'FixtureCurie','program':'claude-code','model':'fixture-wire-model',
        'project_id':7,'registration_token':ADOPTED}


def query_identity(token):
    if token!=ADOPTED:raise OSError('stand-in child authentication refused')
    return {key:value for key,value in reply().items() if key!='registration_token'}


def begin(store):return store.begin(expected_digest=store.status()['state_digest'],candidate_token=CANDIDATE)


@pytest.mark.parametrize('mutation',['extra-field','parent-shape','relative-install','nonloopback-http'])
def test_status_rejects_malformed_closed_target_before_projection(store,mutation):
    value=read_owner(store.owner);context=value['launch_intent']['legacy_context']
    if mutation=='extra-field':context['registration_token']='fixture-context-secret'
    elif mutation=='parent-shape':context['parent']['extra']='ignored'
    elif mutation=='relative-install':context['install_root']='relative-install'
    else:context['mail_url']='http://remote.invalid/mcp/'
    value['launch_intent']['intent_digest']=intent_digest(value['launch_intent'])
    write_owner(store.owner,value)
    with pytest.raises(LocalModelError):store.status()


def send(store):
    begin(store)
    return store.send_once(lambda request,token:reply(),expected_digest=store.status()['state_digest'])


def test_status_is_readonly_nonsecret_and_prepare_precedes_remote_effects(store):
    root=store.runtime;before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    status=store.status()
    assert before=={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert status['phase']=='configured'
    begin(store);pending=read_owner(store.pending)
    assert pending['phase']=='prepared' and pending['kind']=='orrery-legacy-preview-registration-v1'
    assert pending['request']=={'name':'FixtureCurie','project_key':'/fixture-project',
        'program':'claude-code','model':'fixture-wire-model','task_description':'Delegated local preview'}
    assert store.token.read_text()==CANDIDATE and store.token.stat().st_mode & 0o777==0o600
    assert CANDIDATE not in json.dumps(store.status())
    assert 'agent_id' not in read_owner(store.owner)


def test_lost_commit_response_is_sticky_and_never_registers_twice(store):
    begin(store);calls=[]
    def committed(request,token):
        calls.append((request,token));raise OSError('lost response after commit')
    with pytest.raises(LocalModelError,match='REGISTRATION_UNKNOWN'):
        store.send_once(committed,expected_digest=store.status()['state_digest'])
    assert store.status()['phase']=='sent-unknown'
    with pytest.raises(LocalModelError):
        store.send_once(committed,expected_digest=store.status()['state_digest'])
    assert len(calls)==1 and store.token.read_text()==CANDIDATE


@pytest.mark.parametrize('point',['response-saved','canonical-token-published','owner-published'])
def test_interruption_finalizes_same_registration_only_after_child_authentication(store,point):
    calls=[]
    def register(request,token):calls.append(request);return reply()
    begin(store)
    def interrupt(stage):
        if stage==point:raise Interrupted()
    if point=='response-saved':
        with pytest.raises(Interrupted):
            store.send_once(register,expected_digest=store.status()['state_digest'],checkpoint=interrupt)
    else:
        store.send_once(register,expected_digest=store.status()['state_digest'])
        with pytest.raises(Interrupted):
            store.finalize(query_identity,expected_digest=store.status()['state_digest'],checkpoint=interrupt)
    assert store.pending.exists() and len(calls)==1
    result=store.finalize(query_identity,expected_digest=store.status()['state_digest'])
    assert result['phase']=='registered' and result['remote_effect']=='confirmed'
    frame=read_owner(store.owner)
    assert frame['agent_id']==41 and frame['sentinel']=='must-keep'
    assert frame['registration_token']==store.token.read_text()==ADOPTED
    assert frame['launch_intent']['model']['ref']=='local:example'
    assert 'local_execution' not in frame and not store.pending.exists()
    assert ADOPTED not in json.dumps(result)


@pytest.mark.parametrize('field,bad',[('id',99),('name','OtherCurie'),('program','codex'),
    ('model','cloud-model'),('project_id',88)])
def test_different_child_authentication_cannot_finalize(store,field,bad):
    send(store);before=store.owner.read_bytes(),store.token.read_bytes(),store.pending.read_bytes()
    def other(token):return {**query_identity(token),field:bad}
    with pytest.raises(LocalModelError):store.finalize(other,expected_digest=store.status()['state_digest'])
    assert (store.owner.read_bytes(),store.token.read_bytes(),store.pending.read_bytes())==before


def test_lost_response_with_server_replaced_token_does_not_adopt_parent_lookup_or_not_found(store):
    begin(store)
    with pytest.raises(LocalModelError):
        store.send_once(lambda *args:(_ for _ in ()).throw(OSError()),expected_digest=store.status()['state_digest'])
    with pytest.raises(LocalModelError):store.finalize(query_identity,expected_digest=store.status()['state_digest'])
    result=store.abandon(lambda pid:pytest.fail('no actor was recorded'),expected_digest=store.status()['state_digest'])
    assert result['phase']=='abandoned' and result['remote_effect']=='unknown'
    assert store.pending.exists() and 'agent_id' not in read_owner(store.owner)
    with pytest.raises(LocalModelError):begin(store)
    with pytest.raises(LocalModelError):store.finalize(query_identity,expected_digest=store.status()['state_digest'])


def test_authenticated_lost_response_can_finalize_without_resending_register(store):
    begin(store)
    with pytest.raises(LocalModelError):
        store.send_once(lambda *args:(_ for _ in ()).throw(OSError()),expected_digest=store.status()['state_digest'])
    queried=[]
    def owner_query(token):
        queried.append(token)
        if token!=CANDIDATE:raise OSError()
        return {key:value for key,value in reply().items() if key!='registration_token'}
    assert store.finalize(owner_query,expected_digest=store.status()['state_digest'])['phase']=='registered'
    assert queried==[CANDIDATE] and store.token.read_text()==CANDIDATE


def test_prepared_cancellation_cannot_claim_remote_success(store):
    begin(store)
    result=store.abandon(lambda pid:pytest.fail('no actor was recorded'),expected_digest=store.status()['state_digest'])
    assert result['phase']=='cancelled' and result['remote_effect']=='not-sent'
    assert 'agent_id' not in read_owner(store.owner)


@pytest.mark.parametrize('observation,reason',[(None,'OBSERVATION_INCOMPLETE'),
    ('fixture-runner-birth','PROCESS_STILL_RUNNING')])
def test_live_or_unobservable_runner_cannot_be_resolved(store,observation,reason):
    frame=read_owner(store.owner);row=running()
    row.update(intent_digest=frame['launch_intent']['intent_digest'])
    frame['local_execution']=row;write_owner(store.owner,frame);before=store.owner.read_bytes()
    with pytest.raises(LocalModelError,match=reason):
        store.abandon(lambda pid:observation,expected_digest=store.status()['state_digest'])
    assert store.owner.read_bytes()==before


def test_runner_death_after_provider_spawn_only_becomes_abandoned_not_success(store):
    frame=read_owner(store.owner);row=running();row['intent_digest']=frame['launch_intent']['intent_digest']
    frame['local_execution']=row;write_owner(store.owner,frame);seen=[]
    result=store.abandon(lambda pid:seen.append(pid) or 'absent',expected_digest=store.status()['state_digest'])
    assert seen==[11,12] and result['phase']=='abandoned'
    assert read_owner(store.owner)['local_execution']==row  # Never manufacture wait/returncode/VALID.
    assert result['execution_outcome']=='unknown' and result['remote_cleanup']=='unknown'
    with pytest.raises(LocalModelError):begin(store)


def test_stale_digest_refuses_before_auth_observation_or_any_effect(store):
    stale=store.status()['state_digest'];frame=read_owner(store.owner);frame['sentinel']='changed';write_owner(store.owner,frame)
    before=store.owner.read_bytes()
    with pytest.raises(LocalModelError):store.begin(expected_digest=stale,candidate_token=CANDIDATE)
    with pytest.raises(LocalModelError):store.finalize(lambda token:pytest.fail('auth attempted'),expected_digest=stale)
    with pytest.raises(LocalModelError):store.abandon(lambda pid:pytest.fail('observation attempted'),expected_digest=stale)
    assert store.owner.read_bytes()==before and not store.pending.exists() and not store.token.exists()


def test_owner_change_during_remote_authentication_is_not_overwritten(store):
    send(store)
    def changed(token):
        frame=read_owner(store.owner);frame['sentinel']='concurrent-change';write_owner(store.owner,frame)
        return query_identity(token)
    with pytest.raises(LocalModelError):store.finalize(changed,expected_digest=store.status()['state_digest'])
    assert read_owner(store.owner)['sentinel']=='concurrent-change' and store.pending.exists()


@pytest.mark.parametrize('field,value',[
    ('local_registration',{'phase':'secret-not-a-phase'}),
    ('local_resolution',{'phase':'secret-not-a-phase'}),
    ('local_execution',{'phase':'secret-not-a-phase'}),
])
def test_status_never_projects_unvalidated_private_fields(store,field,value):
    frame=read_owner(store.owner);frame[field]=value;write_owner(store.owner,frame)
    with pytest.raises(LocalModelError) as error:store.status()
    assert 'secret-not-a-phase' not in str(error.value)


@pytest.mark.parametrize('role',['phase','response','credential_ref','extra'])
def test_malformed_pending_cannot_be_used_or_reported_as_success(store,role):
    begin(store);pending=read_owner(store.pending)
    pending[role]={'secret-not-output':'bad'}
    write_owner(store.pending,pending)
    with pytest.raises(LocalModelError) as error:store.status()
    assert 'secret-not-output' not in str(error.value)
