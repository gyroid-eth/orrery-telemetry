"""Shared auth/Store/schema/observer code with fake OS responses, no key/CLI.

This is reader/ancestry contract evidence only. Actual P/Q producers, actual
ps, provider/native enforcement and credential retrieval are NOT exercised.
"""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
import local_model_auth as auth
from runtime_intent_store import read_owner,write_owner,write_private_bytes
from runtime_legacy_registration import Store
from runtime_legacy_execution import new_execution
from test_runtime_legacy_configure import prepared as configure_fixture,plan
from test_runtime_legacy_context import prepared as context_fixture

TOKEN='fixture-child-owner-0000000000000000000000'


@pytest.fixture
def bound(configure_fixture,monkeypatch):
    client,ws,args,configure,_,_=configure_fixture
    key=ws.tmp/'credential-resource-only'
    key.write_text('#!/bin/sh\nexit 91\n');key.chmod(0o500)
    ws.profile.write_text(ws.profile.read_text().replace('[claude]',
        '[claude]\nauth_command = ['+repr(str(key)).replace("'",'"')+']'))
    value=plan(configure_fixture)
    configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    intent=value['intent'];target=intent['legacy_context'];store=Store(target['runtime_dir'],target['name'])
    identity={'id':73,'project_id':target['project_id'],'name':target['name'],
        'program':'claude-code','model':intent['model']['resolved']['model']}
    store.begin(expected_digest=store.status()['state_digest'],candidate_token=TOKEN)
    store.send_once(lambda *args:{**identity,'registration_token':TOKEN},expected_digest=store.status()['state_digest'])
    store.finalize(lambda token:identity,expected_digest=store.status()['state_digest'])
    row=new_execution(intent,launch_id='a'*32,receipt_nonce='b'*43)
    row['phase']='exec-unknown'
    row['execution']['runner']={'pid':11,'process_start':'Sat Oct 17 12:00:00 2026'}
    row['execution']['provider']={'pid':12,'process_start':'Sat Oct 17 12:00:00 2026'}
    frame=read_owner(store.owner);frame['local_execution']=row;write_owner(store.owner,frame)
    for name,value in {'PATH':'/usr/bin:/bin','AGENTSTACK_LAUNCH_ID':row['launch_id'],
            'AGENTSTACK_LAUNCH_NONCE':row['receipt_nonce'],'AGENTSTACK_PROVIDER_PID':'12'}.items():
        monkeypatch.setenv(name,value)
    return SimpleNamespace(ws=ws,intent=intent,store=store,row=row,
        runner=Path(intent['model']['resolved']['auth']['runner']['path']),key=key)


def observation(bound,monkeypatch,mutation=None):
    calls=[];starts={};chain_count=0;helper=os.getpid();uid=os.getuid()
    def run(argv,**options):
        nonlocal chain_count
        calls.append(argv)
        if argv[:2]==['ps','-p']:
            pid=int(argv[2]);starts[pid]=starts.get(pid,0)+1
            assert argv[3:]==['-o','lstart='] and pid in (11,12,helper)
            utc=options.get('env',{}).get('TZ')=='UTC0'
            hour='03' if utc else '12';second='01' if pid==helper else '00'
            stamp='Sat Oct 17 '+hour+':00:'+second+' 2026'
            if mutation=='unknown' and pid==12:return subprocess.CompletedProcess(argv,1,'','observation denied')
            if mutation=='absent' and pid==12:return subprocess.CompletedProcess(argv,1,'','')
            if mutation=='saved-provider' and pid==12 and not utc:stamp='Sat Oct 17 12:00:02 2026'
            if mutation=='reused-provider' and pid==12 and starts[pid]>2:stamp='Sat Oct 17 12:00:02 2026'
            if mutation=='older-provider' and pid==12 and utc:stamp='Sat Oct 17 02:59:59 2026'
            if mutation=='row-change':
                frame=read_owner(bound.store.owner);frame['local_execution']['receipt_nonce']='c'*43
                write_owner(bound.store.owner,frame)
            if mutation=='owner-change':
                frame=read_owner(bound.store.owner);frame['agent_id']=74;write_owner(bound.store.owner,frame)
            if mutation=='token-change':
                frame=read_owner(bound.store.owner);frame['registration_token']='rotated-owner-000000000000000000000'
                write_private_bytes(bound.store.token,frame['registration_token'].encode());write_owner(bound.store.owner,frame)
            if mutation=='profile-change':
                bound.ws.profile.write_text(bound.ws.profile.read_text()+'\n# observed change\n[unexpected]\nchanged=true\n')
            if mutation=='task-change':bound.ws.task.write_text('changed during observation')
            return subprocess.CompletedProcess(argv,0,stamp+'\n','')
        assert argv==['ps','-axo','pid=,ppid=,uid=']
        chain_count+=1
        owner=uid+1 if mutation=='other-uid' else uid
        parent=99 if mutation=='other-runner' else 11
        helper_parent=14 if mutation=='reparent' and chain_count>2 else 12
        data=f'{helper} {helper_parent} {uid}\n14 12 {uid}\n12 {parent} {owner}\n11 1 {uid}\n'
        return subprocess.CompletedProcess(argv,0,data,'')
    monkeypatch.setattr(auth.subprocess,'run',run)
    return calls


def read(bound):
    return auth.read_auth_plan(bound.store.owner,bound.intent['intent_digest'],'claude',runner_path=bound.runner)


@pytest.mark.parametrize('phase',['exec-unknown','running'])
def test_active_preview_reads_the_same_plan_only_after_shared_process_binding(bound,monkeypatch,phase):
    frame=read_owner(bound.store.owner)
    if phase=='running':
        frame['local_execution']['phase']='running'
        frame['local_execution']['session_receipt']={'session_id':'fixture-session','phase':'started'}
        write_owner(bound.store.owner,frame)
    calls=observation(bound,monkeypatch)
    result=read(bound)
    assert result['argv']==[str(bound.key)]
    assert not bound.ws.tmp.joinpath('key-executed').exists()
    assert calls.count(['ps','-axo','pid=,ppid=,uid='])==4
    assert calls.count(['ps','-p','12','-o','lstart='])>=6


@pytest.mark.parametrize('mutation',['unknown','absent','saved-provider','reused-provider',
    'older-provider','other-uid','other-runner','reparent','row-change','owner-change','token-change','profile-change','task-change'])
def test_uncertain_or_changed_identity_never_releases_an_auth_plan(bound,monkeypatch,mutation):
    observation(bound,monkeypatch,mutation)
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):read(bound)


@pytest.mark.parametrize('mutation',['missing-row','missing-registration','bad-token','wrong-owner',
    'wrong-launch','wrong-nonce','wrong-pid','bool-pid','planned','unknown-outcome','ended'])
def test_unbound_owner_refuses_before_process_observation(bound,monkeypatch,mutation):
    frame=read_owner(bound.store.owner);row=frame['local_execution']
    if mutation=='missing-row':del frame['local_execution']
    elif mutation=='missing-registration':del frame['local_registration']
    elif mutation=='bad-token':frame['registration_token']='different-owner-token-000000000000'
    elif mutation=='wrong-owner':
        return _wrong_owner(bound,monkeypatch)
    elif mutation=='wrong-launch':monkeypatch.setenv('AGENTSTACK_LAUNCH_ID','c'*32)
    elif mutation=='wrong-nonce':monkeypatch.setenv('AGENTSTACK_LAUNCH_NONCE','c'*43)
    elif mutation=='wrong-pid':monkeypatch.setenv('AGENTSTACK_PROVIDER_PID','99')
    elif mutation=='bool-pid':row['execution']['provider']['pid']=True
    elif mutation=='planned':row['phase']='planned'
    elif mutation=='unknown-outcome':row['execution']['outcome']['state']='unknown'
    elif mutation=='ended':row['phase']='ended'
    write_owner(bound.store.owner,frame);calls=observation(bound,monkeypatch)
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):read(bound)
    assert calls==[]


def _wrong_owner(bound,monkeypatch):
    other=bound.store.owner.with_name('OtherChild.json');write_owner(other,read_owner(bound.store.owner))
    calls=observation(bound,monkeypatch)
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):
        auth.read_auth_plan(other,bound.intent['intent_digest'],'claude',runner_path=bound.runner)
    assert calls==[]


def test_pinned_contract_does_not_import_a_cached_or_path_selected_product_module(bound,monkeypatch):
    poison=SimpleNamespace(checked_execution=lambda *a:pytest.fail('unverified module cache used'))
    monkeypatch.setitem(sys.modules,'runtime_legacy_execution',poison)
    observation(bound,monkeypatch)
    assert read(bound)['argv']==[str(bound.key)]


def test_changed_contract_bytes_are_refused_before_any_of_that_code_runs(bound,monkeypatch):
    script=Path(bound.intent['legacy_context']['install_root'])/'bin/lib/runtime_legacy_execution.py'
    marker=bound.ws.tmp/'unverified-contract-ran'
    script.write_text('from pathlib import Path\nPath('+repr(str(marker))+").write_text('BAD')\n")
    calls=observation(bound,monkeypatch)
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):read(bound)
    assert not marker.exists() and calls==[]


def test_each_refresh_rechecks_execution_instead_of_caching_a_previous_plan(bound,monkeypatch):
    calls=observation(bound,monkeypatch)
    assert read(bound)==read(bound)
    assert calls.count(['ps','-axo','pid=,ppid=,uid='])==8
    before=len(calls);frame=read_owner(bound.store.owner)
    frame['local_execution']['phase']='ended'
    frame['local_execution']['execution']['outcome']={'state':'ended','returncode':0}
    write_owner(bound.store.owner,frame)
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):read(bound)
    assert len(calls)==before
