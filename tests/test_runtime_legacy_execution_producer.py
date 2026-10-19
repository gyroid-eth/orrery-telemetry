"""Real owner/CAS/phase producer, fake current-process and wait observations.

No native provider, fork, key or Mail request. Public launch stays closed.
"""
from copy import deepcopy
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from local_model_auth import LocalModelError
from runtime_intent_store import read_owner,write_owner
from test_runtime_legacy_auth_binding import bound as auth_bound
from test_runtime_legacy_configure import prepared as configure_fixture
from test_runtime_legacy_context import prepared as context_fixture


class Current:
    def __init__(self):
        self.current=11;self.parent_pid=1;self.births={11:'runner-birth',12:'provider-birth'};self.calls=[];self.after=None
    def pid(self):return self.current
    def parent(self):return self.parent_pid
    def start(self,pid):
        self.calls.append(pid)
        value=self.births.get(pid,'absent')
        if self.after:self.after(pid)
        return value


@pytest.fixture
def prepared(auth_bound):
    bound=auth_bound;frame=read_owner(bound.store.owner);frame.pop('local_execution');write_owner(bound.store.owner,frame)
    module=importlib.import_module('runtime_legacy_execution')
    assert hasattr(module,'ExecutionStore'), 'LEGACY_EXECUTION_PRODUCER_NOT_IMPLEMENTED'
    current=Current()
    producer=module.ExecutionStore(bound.store.runtime,bound.store.name,_process=current)
    return bound,producer,current


def plan(prepared):
    bound,producer,current=prepared
    frame=read_owner(bound.store.owner)
    return producer.plan(expected_state_digest=bound.store.status()['state_digest'],
        intent_digest=bound.intent['intent_digest'],registration_generation=frame['local_registration']['generation'])


def claim(prepared,row):
    return prepared[1].claim_runner(launch_id=row['launch_id'],receipt_nonce=row['receipt_nonce'])


def before(prepared,row):
    return prepared[1].before_provider(launch_id=row['launch_id'],receipt_nonce=row['receipt_nonce'])


def provider(prepared,row):
    prepared[2].current=12;prepared[2].parent_pid=11
    return prepared[1].claim_provider(launch_id=row['launch_id'],receipt_nonce=row['receipt_nonce'])


def test_plan_is_saved_before_any_current_actor_and_duplicate_never_replans(prepared):
    bound,producer,current=prepared;row=plan(prepared)
    assert row['phase']=='planned' and row['execution']['runner'] is None and current.calls==[]
    assert read_owner(bound.store.owner)['local_execution']==row
    with pytest.raises(LocalModelError):plan(prepared)
    assert read_owner(bound.store.owner)['launch_intent']==bound.intent


@pytest.mark.parametrize('mutation',['missing-registration','stale-registration','wrong-intent'])
def test_unregistered_or_different_handoff_cannot_plan_an_execution(prepared,mutation):
    bound,producer,current=prepared;frame=read_owner(bound.store.owner)
    generation=frame['local_registration']['generation'];digest=bound.intent['intent_digest']
    if mutation=='missing-registration':
        frame.pop('local_registration');generation=None;write_owner(bound.store.owner,frame)
    elif mutation=='stale-registration':generation='c'*32
    else:digest='c'*64
    saved=bound.store.owner.read_bytes()
    with pytest.raises(LocalModelError):
        producer.plan(expected_state_digest=bound.store.status()['state_digest'],intent_digest=digest,
            registration_generation=generation)
    assert bound.store.owner.read_bytes()==saved and current.calls==[]


def test_runner_then_unknown_before_provider_then_same_pid_provider_claim(prepared):
    bound,producer,current=prepared;row=plan(prepared)
    claimed=claim(prepared,row)
    assert claimed['phase']=='gated' and claimed['execution']['runner']=={'pid':11,'process_start':'runner-birth'}
    pending=before(prepared,row)
    assert pending['phase']=='exec-unknown' and pending['execution']['outcome']=={'state':'unknown','returncode':None}
    assert pending['execution']['provider'] is None
    observed=provider(prepared,row)
    assert observed['phase']=='exec-unknown' and observed['execution']['outcome']=={'state':'pending','returncode':None}
    assert observed['execution']['provider']=={'pid':12,'process_start':'provider-birth'}
    with pytest.raises(LocalModelError):provider(prepared,row)


@pytest.mark.parametrize('mutation',['unknown-runner','absent-runner','wrong-pid','stale-launch','stale-nonce'])
def test_bad_runner_cannot_advance_or_replace_planned_owner(prepared,mutation):
    bound,producer,current=prepared;row=plan(prepared)
    if mutation=='unknown-runner':current.births[11]=None
    elif mutation=='absent-runner':current.births[11]='absent'
    elif mutation=='wrong-pid':current.current=True
    elif mutation=='stale-launch':row['launch_id']='c'*32
    else:row['receipt_nonce']='c'*43
    saved=bound.store.owner.read_bytes()
    with pytest.raises(LocalModelError):claim(prepared,row)
    assert bound.store.owner.read_bytes()==saved


@pytest.mark.parametrize('mutation',['wrong-parent','changed-parent','runner-birth','unknown-runner',
    'unknown-provider','same-pid','wrong-phase'])
def test_provider_cannot_claim_a_different_parent_birth_or_phase(prepared,mutation):
    bound,producer,current=prepared;row=plan(prepared);claim(prepared,row)
    if mutation!='wrong-phase':before(prepared,row)
    current.current=12;current.parent_pid=11
    if mutation=='wrong-parent':current.parent_pid=99
    elif mutation=='changed-parent':current.after=lambda pid:setattr(current,'parent_pid',99)
    elif mutation=='runner-birth':current.births[11]='reused-runner'
    elif mutation=='unknown-runner':current.births[11]=None
    elif mutation=='unknown-provider':current.births[12]=None
    elif mutation=='same-pid':current.current=11
    saved=bound.store.owner.read_bytes()
    with pytest.raises(LocalModelError):producer.claim_provider(launch_id=row['launch_id'],receipt_nonce=row['receipt_nonce'])
    assert bound.store.owner.read_bytes()==saved


def test_owner_change_during_process_observation_is_not_overwritten(prepared):
    bound,producer,current=prepared;row=plan(prepared)
    def changed(pid):
        current.after=None;frame=read_owner(bound.store.owner);frame['concurrent']='keep';write_owner(bound.store.owner,frame)
    current.after=changed
    with pytest.raises(LocalModelError):claim(prepared,row)
    frame=read_owner(bound.store.owner)
    assert frame['concurrent']=='keep' and frame['local_execution']['phase']=='planned'


class Child:
    def __init__(self,pid,callback=None,code=0):self.pid=pid;self.callback=callback;self.code=code;self.waited=False
    def wait(self):
        self.waited=True
        if self.callback:self.callback()
        return self.code


def test_wait_is_outside_lock_and_preserves_real_session_changes_without_claiming_cleanup(prepared):
    bound,producer,current=prepared;row=plan(prepared);claim(prepared,row);before(prepared,row)
    # Q enters and hooks run while the parent's actual wait is in progress.
    def during_wait():
        provider(prepared,row)
        frame=read_owner(bound.store.owner);live=frame['local_execution']
        live['phase']='running';live['session_receipt']={'session_id':'fixture-session','phase':'ended'}
        write_owner(bound.store.owner,frame)
        current.current=11;current.parent_pid=1;current.births[12]='absent'
    child=Child(12,during_wait,code=7)
    result=producer.finish_wait(child,launch_id=row['launch_id'],receipt_nonce=row['receipt_nonce'])
    assert child.waited and result['phase']=='ended'
    assert result['execution']['outcome']=={'state':'ended','returncode':7}
    assert result['session_receipt']=={'session_id':'fixture-session','phase':'ended'}
    assert result['termination']=={'phase':'pending','lease_release':'pending','retire':'pending'}


@pytest.mark.parametrize('mutation',['live-provider','unknown-provider','bad-returncode','wrong-child','owner-change'])
def test_wait_uncertainty_never_becomes_ended_or_success(prepared,mutation):
    bound,producer,current=prepared;row=plan(prepared);claim(prepared,row);before(prepared,row);provider(prepared,row)
    current.current=11;current.parent_pid=1
    def during_wait():
        current.births[12]='provider-birth' if mutation=='live-provider' else None if mutation=='unknown-provider' else 'absent'
        if mutation=='owner-change':
            frame=read_owner(bound.store.owner);frame['agent_id']=99;write_owner(bound.store.owner,frame)
    child=Child(99 if mutation=='wrong-child' else 12,during_wait,code=True if mutation=='bad-returncode' else 0)
    with pytest.raises(LocalModelError):producer.finish_wait(child,launch_id=row['launch_id'],receipt_nonce=row['receipt_nonce'])
    assert read_owner(bound.store.owner)['local_execution']['phase']=='exec-unknown'
    if mutation=='wrong-child':assert not child.waited
