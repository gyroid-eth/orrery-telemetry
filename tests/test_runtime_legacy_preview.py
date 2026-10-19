"""Pure execution data and the existing private owner transaction, not CLI proof."""
from copy import deepcopy
import fcntl
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from local_model_auth import LocalModelError, intent_digest
from runtime_intent_store import owner_layout, owner_lock, read_owner, save_launch_intent, write_owner


def execution_module():
    try:return importlib.import_module('runtime_legacy_execution')
    except ModuleNotFoundError:pytest.fail('LEGACY_EXECUTION_NOT_IMPLEMENTED')


@pytest.mark.parametrize('name',['Fixture.Curie','-Fixture','_Fixture'])
def test_unconfigured_cloud_keeps_existing_legacy_name_grammar(tmp_path,name):
    from runtime_legacy_preview import registration_preconditions
    assert registration_preconditions(tmp_path,name,'codex','gpt-fixture') is None
    with pytest.raises(LocalModelError):
        registration_preconditions(tmp_path,name,'claude-code','local:example',preview_requested=True)


@pytest.mark.parametrize('mode',[0o755,0o750])
def test_unconfigured_cloud_private_state_does_not_require_new_intent_directory_mode(tmp_path,mode):
    from runtime_legacy_preview import registration_preconditions
    parent=tmp_path/'child-agents';parent.mkdir(mode=mode);parent.chmod(mode)
    owner=parent/'FixtureCurie.json'
    owner.write_text(json.dumps({'agent_name':'FixtureCurie','program':'codex','project_key':'/fixture'}))
    owner.chmod(0o600)
    assert registration_preconditions(tmp_path,'FixtureCurie','codex','gpt-fixture') is None


@pytest.mark.parametrize('key',['launch_intent','local_execution','local_registration','local_resolution'])
def test_legacy_cloud_name_compatibility_does_not_ignore_reserved_metadata(tmp_path,key):
    from runtime_legacy_preview import registration_preconditions
    parent=tmp_path/'child-agents';parent.mkdir(mode=0o700)
    owner=parent/'Fixture.Curie.json';owner.write_text(json.dumps({key:None}));owner.chmod(0o600)
    with pytest.raises(LocalModelError):
        registration_preconditions(tmp_path,'Fixture.Curie','codex','gpt-fixture')


def intent(revision=1):
    value={'schema_version':1,'revision':revision,'name':'FixtureCurie','provider':'claude',
        'model':{'kind':'local-profile','ref':'local:example'},
        'execution_policy':{'kind':'local-preview-v1','native_scope':'unverified',
            'native_isolation':'unverified','operator_decision':'operator:fixture'}}
    value['intent_digest']=intent_digest(value)
    return value


def planned():
    return execution_module().new_execution(intent(),launch_id='a'*32,receipt_nonce='b'*43)


def running():
    row=planned();row['phase']='running'
    row['execution']['runner']={'pid':11,'process_start':'fixture-runner-birth'}
    row['execution']['provider']={'pid':12,'process_start':'fixture-provider-birth'}
    row['session_receipt']={'session_id':'fixture-session','phase':'started'}
    return row


def ended():
    row=running();row['phase']='ended'
    row['execution']['outcome']={'state':'ended','returncode':0}
    row['session_receipt']['phase']='ended'
    row['termination']={'phase':'ended','lease_release':'complete','retire':'complete'}
    return row


def test_planned_is_data_only_nonsecret_not_idle_or_native_approval():
    m=execution_module();row=planned();before=deepcopy(row)
    result=m.checked_execution(row,intent())
    assert result==row and result is not row
    assert row['execution']=={'runner':None,'provider':None,'outcome':{'state':'pending','returncode':None}}
    assert row['preview']['isolation_verified'] is False
    assert not m.execution_is_idle(row,intent())
    assert 'endpoint' not in str(row) and 'auth_command' not in str(row)
    result['phase']='running';assert row==before


def test_execution_records_ordinary_child_registration_limit_not_atomic_create_only():
    row=planned()
    assert row.get('registration_guarantee')=={'level':'ordinary-child','atomic_create_only':False}


@pytest.mark.parametrize('value',[None,{'level':'ordinary-child','atomic_create_only':True},
    {'level':'atomic','atomic_create_only':False},{'level':'ordinary-child','atomic_create_only':False,'extra':0}])
def test_execution_cannot_misstate_the_registration_limit(value):
    row=planned();row['registration_guarantee']=value
    with pytest.raises(LocalModelError):execution_module().checked_execution(row,intent())


@pytest.mark.parametrize('path,value',[
    (('schema_version',),True),(('schema_version',),2),(('extra',),'ignored'),
    (('launch_id',),'short'),(('receipt_nonce',),''),(('intent_digest',),'0'*64),
    (('intent_revision',),True),(('phase',),'abandoned'),
    (('execution','extra'),True),(('execution','runner'),{'pid':True,'process_start':'x'}),
    (('execution','provider'),{'pid':12,'process_start':''}),
    (('execution','outcome','extra'),True),(('preview','native_scope_verified'),True),
    (('session_receipt',),{'session_id':'x','phase':'guessed'}),
    (('termination','phase'),'ended'),(('termination','extra'),True),
])
def test_execution_is_closed_and_digest_linked(path,value):
    m=execution_module();row=running();target=row
    for key in path[:-1]:target=target[key]
    target[path[-1]]=value
    with pytest.raises(LocalModelError):m.checked_execution(row,intent())


@pytest.mark.parametrize('mutation',[
    'runner-missing','provider-missing','same-pid','running-no-session',
    'planned-with-provider','unknown-running','pending-returncode','ended-pending-outcome',
    'ended-without-returncode','complete-before-end','complete-without-session-end',
])
def test_phase_and_evidence_cannot_be_mixed(mutation):
    m=execution_module();row=running()
    if mutation=='runner-missing':row['execution']['runner']=None
    elif mutation=='provider-missing':row['execution']['provider']=None
    elif mutation=='same-pid':row['execution']['provider']['pid']=11
    elif mutation=='running-no-session':row['session_receipt']=None
    elif mutation=='planned-with-provider':row['phase']='planned'
    elif mutation=='unknown-running':row['execution']['outcome']['state']='unknown'
    elif mutation=='pending-returncode':row['execution']['outcome']['returncode']=0
    elif mutation=='ended-pending-outcome':row['phase']='ended'
    elif mutation=='ended-without-returncode':row=ended();row['execution']['outcome']['returncode']=None
    elif mutation=='complete-before-end':row['termination']=ended()['termination']
    else:row=ended();row['session_receipt']['phase']='started'
    with pytest.raises(LocalModelError):m.checked_execution(row,intent())


def test_exec_unknown_with_no_provider_is_valid_but_never_idle():
    m=execution_module();row=planned();row['phase']='exec-unknown'
    row['execution']['runner']={'pid':11,'process_start':'fixture-birth'}
    row['execution']['outcome']['state']='unknown'
    assert m.checked_execution(row,intent())==row
    assert not m.execution_is_idle(row,intent())


def test_ended_process_with_remote_cleanup_unknown_is_not_idle():
    m=execution_module();row=ended()
    assert m.execution_is_idle(row,intent())
    row['termination']={'phase':'unknown','lease_release':'unknown','retire':'pending'}
    assert m.checked_execution(row,intent())==row
    assert not m.execution_is_idle(row,intent())


def test_preexec_cancel_has_no_provider_or_returncode():
    m=execution_module();row=planned();row['phase']='cancelled'
    row['execution']['outcome']={'state':'ended','returncode':None}
    row['termination']={'phase':'ended','lease_release':'complete','retire':'complete'}
    assert m.execution_is_idle(row,intent())
    row['execution']['provider']={'pid':12,'process_start':'fixture-birth'}
    with pytest.raises(LocalModelError):m.checked_execution(row,intent())


def test_legacy_intent_lock_is_the_existing_per_name_resume_inode(tmp_path):
    tmp_path.chmod(0o700);owner=tmp_path/'child-agents/FixtureCurie.json'
    lock=owner_layout(owner,'legacy')[1]
    assert lock==owner.with_name('.FixtureCurie.resume.lock')
    with owner_lock(owner,'legacy'):
        fd=os.open(lock,os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(fd)
        other=owner.with_name('OtherCurie.json')
        with owner_lock(other,'legacy'):assert owner_layout(other,'legacy')[1]!=lock
    assert not owner.with_name('.lock').exists()


def test_existing_readonly_private_resume_mutex_is_reused_without_rewriting(tmp_path):
    tmp_path.chmod(0o700);directory=tmp_path/'child-agents';directory.mkdir(mode=0o700)
    owner=directory/'FixtureCurie.json';lock=directory/'.FixtureCurie.resume.lock'
    lock.write_text('existing-lock');lock.chmod(0o400);before=lock.stat().st_ino,lock.read_bytes(),lock.stat().st_mode
    with owner_lock(owner,'legacy'):assert (lock.stat().st_ino,lock.read_bytes(),lock.stat().st_mode)==before


@pytest.mark.parametrize('case',['null-intent','scalar-model','orphan-execution','orphan-resolution','orphan-pending'])
def test_damaged_local_metadata_is_not_absent_cloud_configuration(tmp_path,case):
    from runtime_legacy_preview import registration_preconditions
    tmp_path.chmod(0o700);runtime=tmp_path/'runtime';directory=runtime/'child-agents'
    runtime.mkdir(mode=0o700);directory.mkdir(mode=0o700);owner=directory/'FixtureCurie.json'
    frame={};model='cloud-model'
    if case=='null-intent':frame['launch_intent']=None
    elif case=='scalar-model':
        value=intent();value['model']=17;value['intent_digest']=intent_digest(value)
        frame['launch_intent']=value;model='local:example'
    elif case=='orphan-execution':frame['local_execution']={}
    elif case=='orphan-resolution':frame['local_resolution']={}
    else:write_owner(directory/'.FixtureCurie.registration-pending.json',{'phase':'prepared'})
    write_owner(owner,frame);before=owner.read_bytes()
    with pytest.raises(LocalModelError):
        registration_preconditions(runtime,'FixtureCurie','claude-code',model)
    assert owner.read_bytes()==before


@pytest.mark.parametrize('state',['planned','gated','exec-unknown','running','remote-unknown','malformed'])
def test_existing_store_refuses_reconfigure_over_execution_without_changing_bytes(tmp_path,state):
    tmp_path.chmod(0o700);owner=tmp_path/'child-agents/FixtureCurie.json'
    save_launch_intent(owner,intent(),mode='legacy')
    row=running() if state=='running' else planned()
    if state in ('gated','exec-unknown'):
        row['phase']=state;row['execution']['runner']={'pid':11,'process_start':'fixture-birth'}
    if state=='remote-unknown':
        row=ended();row['termination']={'phase':'unknown','lease_release':'unknown','retire':'pending'}
    if state=='malformed':row['unknown-field']=True
    frame=read_owner(owner);frame.update(local_execution=row,sentinel='keep')
    write_owner(owner,frame);before=owner.read_bytes()
    with pytest.raises(LocalModelError):save_launch_intent(owner,intent(2),mode='legacy')
    assert owner.read_bytes()==before and read_owner(owner)['sentinel']=='keep'


def test_identical_intent_replay_never_repairs_or_overwrites_pending_execution(tmp_path):
    tmp_path.chmod(0o700);owner=tmp_path/'child-agents/FixtureCurie.json'
    save_launch_intent(owner,intent(),mode='legacy')
    frame=read_owner(owner);frame['local_execution']=running();write_owner(owner,frame)
    before=owner.read_bytes()
    assert save_launch_intent(owner,intent(),mode='legacy')==intent()
    assert owner.read_bytes()==before
