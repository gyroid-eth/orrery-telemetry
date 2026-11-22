"""CLI end shares the dashboard exit request; never signal the pane runner."""
import contextlib
from pathlib import Path
import runpy
import time
import types

import pytest

ROOT=Path(__file__).resolve().parents[2]
API=runpy.run_path(str(ROOT/'bin/lib/runtime_client.py'))
TMUX=API['runtime_tmux_api']()
TARGET=runpy.run_path(str(ROOT/'bin/lib/runtime_target.py'))


@pytest.mark.parametrize('provider',['claude','codex','gemini'])
@pytest.mark.parametrize('reused',[False,True])
def test_actual_target_end_uses_existing_provider_exit_request(monkeypatch,provider,reused):
    effects=[];starts={'value':'birth'}
    class Channel:
        path=Path('/fixture/socket');argv=['tmux','-S',str(path)]
        def command(self,*args,**kwargs):
            if args[0]=='send-keys':effects.append(('keys',args));return types.SimpleNamespace(returncode=0,stdout='')
            if args[0]=='capture-pane':text='❯\nctx: 14% used'
            elif '#{window_id}' in args:text='@1'
            elif '#{session_id}\t#{session_name}\t#{pane_id}\t#{pane_pid}' in args:text='$1\tBeta\t%1\t400\n'
            else:text='99'
            return types.SimpleNamespace(returncode=0,stdout=text)
    channel=Channel();obj=object.__new__(TARGET['Targets'])
    obj.deadline=time.monotonic()+5;obj.observations=types.SimpleNamespace(deadline=obj.deadline)
    binding={'expected_server_instance_id':'fixture','candidate_generation':'generation','authority_epoch':'epoch'}
    obj.operator=types.SimpleNamespace(binding=binding)
    client=types.SimpleNamespace(operation_mutex=contextlib.nullcontext)
    obj.api={**API,'RuntimeClient':lambda *a:client,'process_start':lambda *a,**kw:starts['value']}
    obj.tmux={**TMUX,'compact_readiness':lambda *a:{'state':'idle','input_empty':True,'context_used_percent':14},'compact_refusal':lambda *a:None}
    obj.tmux['module_api']=lambda _: {'unproven_app':lambda *a:False}
    value={'launch':dict(provider=provider,launch_id='launch',receipt_nonce='nonce',tmux_transport='global-instance'),
           'context':{},'child':{},'lives':[dict(pid=400,process_start='birth',pane_id='%1',launch_id='launch',receipt_nonce='nonce')]}
    obj.snapshot=lambda *a,**kw:(Path('/fixture/root'),value,'a'*64,dict(agent_id=2,name='Beta',credential_generation=1,retired_at=None))
    obj.principal=lambda *a,**kw:(dict(kind='parent',uid=501,agent_id=1),'b'*64)
    obj.endpoints=lambda *a:[('global-instance',channel)]
    original=TARGET['Targets'].end
    def before_effect(*args,**kw):
        if reused:starts['value']='reused-birth'
        return original(obj,*args,**kw)
    monkeypatch.setattr(obj,'end',before_effect)
    scope=original.__globals__;os_module=scope['os']
    monkeypatch.setitem(scope,'os',types.SimpleNamespace(**{**vars(os_module),'kill':lambda pid,sig:effects.append(('signal',pid,int(sig)))}))
    expected=obj.resolve(2)['state_sha256']
    with pytest.raises(TMUX['TargetError']):obj.execute('end',2,expected=expected)
    if reused:
        assert effects==[]
    elif provider=='gemini':
        assert effects==[('signal',400,2)]
    else:
        assert effects==[('keys',('send-keys','-t','%1','-l','/exit')),('keys',('send-keys','-t','%1','C-m'))]


@pytest.mark.parametrize('start,expected',[(90,400),(400,400),(310,None)])
def test_sdk_parent_reader_stops_below_runner_and_before_launcher(monkeypatch,start,expected):
    scope=API['provider_ancestor'].__globals__;module_os=scope['os']
    monkeypatch.setitem(scope,'os',types.SimpleNamespace(**{**vars(module_os),'getppid':lambda:start}))
    table={90:'400 /bin/sh',400:'500 /fixture/native-python',500:'310 /fixture/launcher-python',310:'1 python'}
    def ps(args,**kw):
        return types.SimpleNamespace(returncode=0,stdout=table[int(args[2])])
    monkeypatch.setitem(scope,'subprocess',types.SimpleNamespace(run=ps))
    assert API['provider_ancestor'](runner_pid=310)==expected


@pytest.mark.parametrize('probe', ['absent','active','denied','changed'])
def test_creation_replaces_only_a_proven_stale_socket(monkeypatch,probe):
    import subprocess
    delivery_module=runpy.run_path(str(ROOT/'bin/lib/runtime_delivery.py'))
    cls=delivery_module['RuntimeDelivery'];obj=object.__new__(cls);obj.error=API['ClientError']
    obj.client=types.SimpleNamespace(config={'agentstack_home':'/fixture'},binding={'expected_server_instance_id':'instance'})
    before=types.SimpleNamespace(st_dev=1,st_ino=10)
    after=types.SimpleNamespace(st_dev=1,st_ino=11)
    calls=[];reads=iter([before,after if probe=='changed' else before,after])
    def invoke(argv,**kwargs):
        calls.append(argv)
        if 'list-sessions' in argv:
            return subprocess.CompletedProcess(argv,0 if probe=='active' else 1,'',
                'permission denied' if probe=='denied' else 'no server running on /fixture/socket')
        return subprocess.CompletedProcess(argv,0,'','')
    tmux={**TMUX,'global_socket':lambda *a,**kw:Path('/fixture/socket'),
          'lstat_if_present':lambda *a:next(reads),'validate_socket':lambda *a:None}
    obj.api={'runtime_tmux_api':lambda:tmux}
    monkeypatch.setitem(cls.launch_command.__globals__,'subprocess',types.SimpleNamespace(run=invoke))
    if probe=='absent':assert obj.launch_command('new-session','-d','-s','Alpha').returncode==0
    else:
        with pytest.raises(API['ClientError'],match='^'+('TMUX_TARGET_UNAVAILABLE' if probe=='denied' else 'TMUX_SOCKET_CHANGED')+'$'):
            obj.launch_command('new-session','-d','-s','Alpha')
    assert len(calls)==(1 if probe in ('changed','denied') else 2)
