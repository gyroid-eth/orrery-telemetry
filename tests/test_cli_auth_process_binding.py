"""Process observation unit controls, NOT real R2 or native acceptance."""
from copy import deepcopy
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
process = importlib.import_module('process_observation')


def utc(value):
    return {'clock':'UTC','lstart':value}


@pytest.mark.parametrize('parent,child,accepted', [
    (utc('Sat Oct 17 03:00:00 2026'),utc('Sat Oct 17 03:00:00 2026'),True),
    (utc('Sat Oct 17 03:00:00 2026'),utc('Sat Oct 17 03:00:01 2026'),True),
    (utc('Fri Oct 16 23:59:59 2026'),utc('Sat Oct 17 00:00:00 2026'),True),
    (utc('Sat Oct 17 03:00:01 2026'),utc('Sat Oct 17 03:00:00 2026'),False),
    ('Sat Oct 17 12:00:00 2026',utc('Sat Oct 17 03:00:00 2026'),False),
    (utc('Sat Oct 17 03:00:00 2026'),'Sat Oct 17 12:00:00 2026',False),
    ({'clock':'unknown','lstart':'Sat Oct 17 03:00:00 2026'},utc('Sat Oct 17 03:00:01 2026'),False),
    (utc('not-a-birth'),utc('Sat Oct 17 03:00:00 2026'),False),
    (utc('Sat Oct 17 03:00:00 2026'),None,False),
    (utc('absent'),utc('Sat Oct 17 03:00:00 2026'),False),
    (utc('Sat Oct 32 03:00:00 2026'),utc('Sat Oct 17 03:00:00 2026'),False),
])
def test_utc_birth_order_is_explicit_and_allows_same_second(parent,child,accepted):
    assert process.birth_not_before(parent,child) is accepted


def test_utc_observation_uses_existing_query_without_reinterpreting_saved_birth(monkeypatch):
    calls=[]
    def run(argv,**options):
        calls.append((argv,options))
        assert argv==['ps','-p','20','-o','lstart=']
        stamp='Sat Oct 17 03:00:00 2026' if options.get('env',{}).get('TZ')=='UTC0' else 'Sat Oct 17 12:00:00 2026'
        return subprocess.CompletedProcess(argv,0,stamp+'\n','')
    monkeypatch.setattr(process,'subprocess',SimpleNamespace(run=run,TimeoutExpired=subprocess.TimeoutExpired))
    assert process.process_start(20)=='Sat Oct 17 12:00:00 2026'
    observed=process.process_birth_utc(20)
    assert observed==utc('Sat Oct 17 03:00:00 2026')
    assert 'env' not in calls[0][1]
    assert calls[1][1]['env']['TZ']=='UTC0' and calls[1][1]['env']['LC_ALL']=='C'
    assert process.birth_not_before('Sat Oct 17 12:00:00 2026',observed) is False


@pytest.mark.parametrize('mutation', [None,'other-uid','other-runner','older-provider',
    'older-helper','reused-runner','reused-provider','reused-helper','reparent',
    'unknown-birth','absent','unknown-ancestry','unknown-utc'])
def test_auth_process_binding_checks_real_observer_components_twice(monkeypatch,mutation):
    # Unit boundary doubles only. Positive real ps is a separate outside R2
    # connection test, never claimed from these process-level literal controls.
    local={10:'Sat Oct 17 12:00:00 2026',20:'Sat Oct 17 12:00:00 2026',30:'Sat Oct 17 12:00:01 2026'}
    births={10:utc('Sat Oct 17 03:00:00 2026'),20:utc('Sat Oct 17 03:00:00 2026'),30:utc('Sat Oct 17 03:00:01 2026')}
    calls={pid:0 for pid in local}
    if mutation=='older-provider':births[20]=utc('Sat Oct 17 02:59:59 2026')
    if mutation=='older-helper':births[30]=utc('Sat Oct 17 02:59:59 2026')
    if mutation=='unknown-utc':births[20]=None
    def start(pid,**kwargs):
        assert not kwargs.get('clock'), 'default saved birth was replaced by UTC'
        calls[pid]+=1
        if mutation=='unknown-birth' and pid==20:return None
        if mutation=='absent' and pid==20:return 'absent'
        if mutation=='reused-'+{10:'runner',20:'provider',30:'helper'}[pid] and calls[pid]>1:
            return 'Sat Oct 17 12:00:02 2026'
        return local[pid]
    ancestry_calls=[]
    def ancestry(child,parent,uid,**kwargs):
        ancestry_calls.append((child,parent,uid))
        assert uid==501
        if mutation in ('other-uid','other-runner','unknown-ancestry'):return None
        result=(((30,25,501),(25,20,501),(20,10,501)) if child==30
                else ((20,10,501),(10,1,501)))
        if mutation=='reparent' and len(ancestry_calls)>2:
            result=(((30,26,501),(26,20,501),(20,10,501)) if child==30 else result)
        return result
    monkeypatch.setattr(process,'process_start',start)
    monkeypatch.setattr(process,'process_birth_utc',lambda pid,**kw:deepcopy(births[pid]))
    monkeypatch.setattr(process,'ancestry',ancestry)
    accepted=process.auth_process_binding(30,20,{'pid':10,'process_start':local[10]},501)
    assert accepted is (mutation is None)
    if mutation is None:
        assert ancestry_calls==[(30,20,501),(20,10,501)]*2
        assert calls=={10:2,20:2,30:2}


def test_shared_ancestry_preserves_bool_contract_and_same_uid(monkeypatch):
    result='30 25 501\n25 20 501\n20 10 501\n10 1 501\n'
    def run(argv,**options):
        assert argv==['ps','-axo','pid=,ppid=,uid=']
        return subprocess.CompletedProcess(argv,0,result,'')
    monkeypatch.setattr(process,'subprocess',SimpleNamespace(run=run,SubprocessError=subprocess.SubprocessError))
    assert process.ancestry(30,10,501)==((30,25,501),(25,20,501),(20,10,501),(10,1,501))
    assert process.belongs_to(30,10,501) is True
    assert process.ancestry(30,10,502) is None
    assert process.belongs_to(30,10,502) is False


def test_actual_utc_query_observes_new_child_near_utc_clock():
    """Real shared ps query. Sandbox refusal stays FAILED for outside runner."""
    from datetime import datetime,timezone
    before=datetime.now(timezone.utc).timestamp()
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'])
    try:
        observed=process.process_birth_utc(child.pid)
        after=datetime.now(timezone.utc).timestamp()
        assert observed is not None, 'requires real shared process observation outside sandbox'
        assert set(observed)=={'clock','lstart'} and observed['clock']=='UTC'
        stamp=datetime.strptime(observed['lstart'],'%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc).timestamp()
        assert before-1<=stamp<=after+1, 'UTC-tagged observation was not actually UTC'
    finally:
        child.terminate()
        child.wait(timeout=5)
