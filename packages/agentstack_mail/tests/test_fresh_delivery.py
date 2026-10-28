"""Mocked OS unit boundaries and separately selected real-platform acceptance.

The real_tmux tests require the parent outside the Codex ps/tmux sandbox.
Unit transport observations are not evidence of actual process ownership.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import pytest

from agentstack_mail import fresh_delivery as delivery
from agentstack_mail import fresh_notifications as notifications
from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_launch import identity, layout
from agentstack_mail.fresh_runtime import _write_private
from agentstack_mail.storage_origin_contract import B1Error, canonical
from test_fresh_guard import live
from test_fresh_launch import prepare
from test_fresh_mail import send_args, snapshot
from test_fresh_notifications import rows, running
from test_fresh_proxy import environment

ROOT = Path(__file__).resolve().parents[3]
WATCHER = ROOT/'hooks/watch_agent_mail_signals.sh'


async def prepared_route(client, tmp_path, monkeypatch):
    event = running(client, tmp_path, monkeypatch)
    from agentstack_mail.fresh_launch import record_session
    await record_session(client, event)
    base = layout(client);launch_id = json.loads((base/'current.json').read_text())['launch_id']
    run = base/'runs'/launch_id
    value = dict(kind='orrery-fresh-tmux-route-v1',context_digest=client.context_digest,
        launch_id=launch_id,socket_path='/private/tmp/fixture-only.sock',socket_identity=[11,22],
        pane=dict(session_id='$1',pane_id='%2',pid=100),pane_process_start='pane-birth',
        runner=dict(pid=101,process_start='runner-birth'),provider=dict(pid=102,process_start='provider-birth'),
        **identity(client))
    _write_private(run/'tmux.json',value)
    return run,value


def mocked_os(monkeypatch, *, phase=None, change=None, screen='❯ '):
    commands=[]
    terminal=runpy.run_path(str(ROOT/'dashboard/terminal_state.py'))
    api=runpy.run_path(str(ROOT/'bin/lib/runtime_delivery.py'))
    class Refused(RuntimeError):pass
    class Transport:
        identity=(11,22)
        def __init__(self,path):self.path=Path(path)
        def command(self,*args,**kwargs):
            commands.append((args,kwargs))
            if args[0]=='capture-pane':return subprocess.CompletedProcess(args,0,screen,'')
            if args[0]=='load-buffer' and change:change()
            return subprocess.CompletedProcess(args,1 if args[0]==phase else 0,'','')
    processes=dict(process_start=lambda pid,**kw:{100:'pane-birth',101:'runner-birth',102:'provider-birth'}.get(pid),
                   belongs_to=lambda child,parent,uid,**kw:(child,parent) in {(101,100),(102,101)})
    processes['process_snapshot']=lambda **kw:{100:(1,os.getuid(),'pane-birth'),101:(100,os.getuid(),'runner-birth'),102:(101,os.getuid(),'provider-birth')}
    processes['snapshot_belongs_to']=runpy.run_path(str(ROOT/'bin/lib/process_observation.py'))['snapshot_belongs_to']
    tmux=dict(Transport=Transport,TargetError=Refused,panes=lambda *a,**kw:[dict(session_id='$1',session='mutable-name',pane_id='%2',pid=100)],
              compact_readiness=terminal['compact_readiness'])
    monkeypatch.setattr(delivery,'helpers',lambda root:(tmux,processes,api))
    return commands,tmux,processes


def test_process_snapshot_checks_births_and_same_uid_ancestry(monkeypatch):
    api = runpy.run_path(str(ROOT/'bin/lib/process_observation.py'))
    observe = api['process_snapshot']
    uid = os.getuid()
    output = f'100 1 {uid} Mon Oct 26 10:00:00 2026\n101 100 {uid} Mon Oct 26 10:00:01 2026\n102 101 {uid} Mon Oct 26 10:00:02 2026\n'
    monkeypatch.setitem(observe.__globals__, 'subprocess', type('Fake', (), {
        'run': staticmethod(lambda *a, **k: subprocess.CompletedProcess(a, 0, output, '')),
        'TimeoutExpired': subprocess.TimeoutExpired,
    }))
    rows = observe()
    assert rows[102][2] == 'Mon Oct 26 10:00:02 2026'
    assert api['snapshot_belongs_to'](rows, 102, 100, uid)
    assert not api['snapshot_belongs_to'](rows, 102, 100, uid + 1)
    assert not api['snapshot_belongs_to'](rows, 102, 999, uid)
    output += 'malformed process row\n'
    assert observe() is None


def test_changed_birth_between_observations_refuses_target(tmp_path, monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            await prepared_route(client, tmp_path, monkeypatch)
            _, _, processes = mocked_os(monkeypatch)
            original = processes['process_snapshot']
            calls = []
            def changed():
                calls.append(True)
                rows = original()
                if len(calls) >= 3:
                    rows[102] = (101, os.getuid(), 'replacement-birth')
                return rows
            processes['process_snapshot'] = changed
            with pytest.raises(B1Error, match='FRESH_TMUX_TARGET_CHANGED'):
                delivery.target(client, ROOT)
            assert len(calls) == 3
    asyncio.run(run())


def test_mocked_os_delivery_completes_only_after_submit_and_does_not_set_read_ack(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            await prepared_route(client,tmp_path,monkeypatch)
            commands,_,_=mocked_os(monkeypatch,screen='✳ Working…\n❯ ')
            sent=await runtime.core.write_message('send',**send_args(runtime,people,
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}]))
            assert (await delivery.deliver(client,ROOT))['completed']==1
            names=[args[0] for args,kw in commands]
            assert names.index('load-buffer')<names.index('paste-buffer')<names.index('send-keys')
            assert await delivery.deliver(client,ROOT)==dict(status='ok',completed=0)
            facts,pairs=await rows(runtime)
            assert facts[0]['completed_ts'] and pairs==[(sent['message_id'],people[0]['agent_id'],None,None)]
    asyncio.run(run())


@pytest.mark.parametrize('admission', ['idle','work','not-ready','busy','disabled'])
def test_resume_keeps_pending_for_normal_watch_once_admission_and_wake(tmp_path, monkeypatch, admission):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            monkeypatch.delenv('AGENTSTACK_MAIL_DISABLED', raising=False)
            run_path,_ = await prepared_route(client,tmp_path,monkeypatch)
            receipt = json.loads((run_path/'session.json').read_text())
            event = dict(hook_event_name='SessionStart',source='resume',
                session_id=receipt['native_session_id'],transcript_path=receipt['transcript_path'])
            screen = '$ ' if admission == 'not-ready' else ('✳ Working…\n❯ ' if admission == 'work' else '❯ ')
            commands,_,_ = mocked_os(monkeypatch,screen=screen)
            sent = await runtime.core.write_message('send',**send_args(runtime,people,
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}],subject='resume-to-watcher'))
            from io import StringIO
            output = StringIO();before = await rows(runtime)
            saved = (run_path/'session.json').read_bytes()
            await notifications.hook(client,event,ROOT,output=output)
            assert output.getvalue() == '' and commands == [] and await rows(runtime) == before
            assert (run_path/'session.json').read_bytes() == saved
            if admission == 'busy':
                with notifications.consumer_lock(client) as acquired:
                    assert acquired
                    result = await delivery.watch_once(runtime.root/'config.json',ROOT)
            else:
                if admission == 'disabled':monkeypatch.setenv('AGENTSTACK_MAIL_DISABLED','1')
                result = await delivery.watch_once(runtime.root/'config.json',ROOT)
            aid = people[0]['agent_id']
            if admission in {'idle','work'}:
                assert result == dict(ok=True,routes=[dict(status='ok',completed=1,mail_agent_id=aid)])
                names = [args[0] for args,_ in commands]
                assert names.index('load-buffer') < names.index('paste-buffer') < names.index('send-keys')
                assert sum(name == 'send-keys' for name in names) == 1
                again = await delivery.watch_once(runtime.root/'config.json',ROOT)
                assert again == dict(ok=True,routes=[dict(status='ok',completed=0,mail_agent_id=aid)])
                assert sum(args[0] == 'send-keys' for args,_ in commands) == 1
                facts,pairs = await rows(runtime)
                assert facts[0]['completed_ts'] and pairs == [(sent['message_id'],aid,None,None)]
            else:
                if admission == 'not-ready':
                    assert result == dict(ok=False,routes=[dict(mail_agent_id=aid,reason='FRESH_TMUX_TARGET_NOT_READY')])
                else:
                    assert result == dict(ok=True,routes=[dict(status=admission,completed=0,mail_agent_id=aid)])
                assert await rows(runtime) == before
                assert not any(args[0] in {'load-buffer','paste-buffer','send-keys'} for args,_ in commands)
                assert (runtime.root/'signals/agents'/str(aid)/(str(sent['message_id'])+'.signal')).is_file()
    asyncio.run(run())


@pytest.mark.parametrize('order', ['post-first','watcher-first'])
def test_resume_post_and_normal_watcher_share_one_notification_handoff(tmp_path, monkeypatch, order):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            monkeypatch.delenv('AGENTSTACK_MAIL_DISABLED', raising=False)
            run_path,_ = await prepared_route(client,tmp_path,monkeypatch)
            receipt = json.loads((run_path/'session.json').read_text())
            event = dict(hook_event_name='SessionStart',source='resume',
                session_id=receipt['native_session_id'],transcript_path=receipt['transcript_path'])
            commands,_,_ = mocked_os(monkeypatch)
            await runtime.core.write_message('send',**send_args(runtime,people,
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}]))
            from io import StringIO
            resumed,post = StringIO(),StringIO()
            callbacks = [notifications.hook(client,dict(event,hook_event_name='PostToolUse'),ROOT,output=post),
                delivery.watch_once(runtime.root/'config.json',ROOT)]
            if order == 'watcher-first':callbacks.reverse()
            await asyncio.gather(notifications.hook(client,event,ROOT,output=resumed),*callbacks)
            assert resumed.getvalue() == ''
            assert int(bool(post.getvalue())) + sum(args[0] == 'send-keys' for args,_ in commands) == 1
            facts,pairs = await rows(runtime)
            assert facts[0]['completed_ts'] and pairs[0][2:] == (None,None)
            again = await delivery.watch_once(runtime.root/'config.json',ROOT)
            assert again['routes'] == [dict(status='ok',completed=0,mail_agent_id=people[0]['agent_id'])]
            assert int(bool(post.getvalue())) + sum(args[0] == 'send-keys' for args,_ in commands) == 1
    asyncio.run(run())


@pytest.mark.parametrize('phase',['load-buffer','paste-buffer','send-keys'])
def test_mocked_os_external_failure_keeps_pending_and_retries_wait_for_existing_policy(tmp_path,monkeypatch,phase):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            await prepared_route(client,tmp_path,monkeypatch)
            commands,_,_=mocked_os(monkeypatch,phase=phase)
            await runtime.core.write_message('send',**send_args(runtime,people,
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}]))
            with pytest.raises(B1Error):await delivery.deliver(client,ROOT)
            facts,pairs=await rows(runtime)
            assert facts[0]['completed_ts'] is None and pairs[0][2:]==(None,None)
            before=len([row for row in commands if row[0][0]=='load-buffer'])
            assert (await delivery.deliver(client,ROOT))['completed']==0
            assert len([row for row in commands if row[0][0]=='load-buffer'])==before
    asyncio.run(run())


@pytest.mark.parametrize('damage',['I','A','g','native','provider-birth','pane-birth','pane','socket','ended','bare-shell'])
def test_mocked_os_changed_proof_never_hands_off_or_consumes(tmp_path,monkeypatch,damage):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            run_path,original=await prepared_route(client,tmp_path,monkeypatch)
            commands,tmux,processes=mocked_os(monkeypatch,screen='$ ' if damage=='bare-shell' else '❯ ')
            await runtime.core.write_message('send',**send_args(runtime,people,
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}]))
            row=dict(original)
            if damage in {'I','A','g'}:
                key={'I':'mail_instance_id','A':'mail_agent_id','g':'credential_generation'}[damage]
                row[key]='foreign' if damage=='I' else 999
            elif damage=='native':
                p=run_path/'session.json';v=json.loads(p.read_text());v['native_session_id']=str(uuid4());p.write_text(canonical(v))
            elif damage=='provider-birth':row['provider']=dict(row['provider'],process_start='other')
            elif damage=='pane-birth':row['pane_process_start']='other'
            elif damage=='pane':row['pane']=dict(row['pane'],pane_id='%999')
            elif damage=='socket':row['socket_identity']=[99,99]
            elif damage=='ended':_write_private(run_path/'ended.json',dict(exit_code=0,launch_id=original['launch_id']))
            (run_path/'tmux.json').write_text(canonical(row))
            before=await snapshot(runtime)
            with pytest.raises(B1Error):await delivery.deliver(client,ROOT)
            assert not any(args[0] in {'load-buffer','paste-buffer','send-keys'} for args,kw in commands)
            assert await snapshot(runtime)==before
    asyncio.run(run())


def test_mocked_os_changed_target_after_load_never_pastes(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            run_path,original=await prepared_route(client,tmp_path,monkeypatch)
            def change():
                p=run_path/'tmux.json';v=json.loads(p.read_text());v['pane']['pane_id']='%999';p.write_text(canonical(v))
            commands,_,_=mocked_os(monkeypatch,change=change)
            await runtime.core.write_message('send',**send_args(runtime,people,
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}]))
            with pytest.raises(B1Error):await delivery.deliver(client,ROOT)
            assert not any(args[0] in {'paste-buffer','send-keys'} for args,kw in commands)
            assert (await rows(runtime))[0][0]['completed_ts'] is None
    asyncio.run(run())


def test_mocked_os_two_active_contexts_for_same_owner_do_not_choose_one(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            await prepared_route(client,tmp_path,monkeypatch)
            second=await FreshBoundClient.prepare(runtime.root/'config.json',people[0]['binding_path'],
                client.context['endpoint'],client_id=str(uuid4()))
            await prepared_route(second,tmp_path,monkeypatch)
            commands,_,_=mocked_os(monkeypatch)
            result=await delivery.watch_once(runtime.root/'config.json',ROOT)
            assert result['ok'] is False and result['routes']==[
                dict(mail_agent_id=people[0]['agent_id'],reason='FRESH_DELIVERY_ROUTE_AMBIGUOUS')]
            assert commands==[]
    asyncio.run(run())


def test_watcher_explicit_fresh_does_not_fall_back_to_legacy_on_missing_or_mixed_context(tmp_path):
    cases=[(['--fresh-config'],{'AGENTSTACK_RUNTIME_MODE':'fresh'}),
           ([],{'AGENTSTACK_RUNTIME_MODE':'fresh'}),
           (['--fresh-config',str(tmp_path/'config.json'),'--once'],{'AGENTSTACK_FRESH_CLIENT_CONTEXT':'foreign'})]
    for args,changes in cases:
        env=dict(os.environ,AGENTSTACK_PYTHON=sys.executable,**changes)
        result=subprocess.run(['/bin/bash',str(WATCHER),*args],env=env,capture_output=True,text=True,timeout=15)
        assert result.returncode==2 and 'FRESH_' in result.stderr and result.stdout==''


# Real platform cases below are selected by ProOpus, not the Codex sandbox.
NATIVE=r'''
import json,os,pathlib,select,subprocess,sys,termios,time,tty,uuid
provider=os.environ['TEST_PROVIDER'];args=sys.argv[1:]
home=pathlib.Path(os.environ['CODEX_HOME' if provider=='codex' else 'CLAUDE_CONFIG_DIR'])
session=str(uuid.uuid4());transcript=home/(session+'.jsonl')
transcript.write_text(json.dumps(dict(type='session_meta',payload=dict(id=session)))+'\n')
event=dict(hook_event_name='SessionStart',source='startup',session_id=session,transcript_path=str(transcript))
if provider=='codex':
 plan=json.loads((home/'hook-plan.json').read_text());command=['/bin/bash',str(pathlib.Path(plan['plugin_root'])/'scripts/run-hook.sh')]
else:
 settings=json.loads(pathlib.Path(args[args.index('--settings')+1]).read_text())
 command=['/bin/bash','-c',settings['hooks']['SessionStart'][0]['hooks'][0]['command']]
result=subprocess.run(command,input=json.dumps(event),capture_output=True,text=True,timeout=30)
trace=pathlib.Path(os.environ['TEST_TRACE'])
trace.write_text(json.dumps(dict(session=session,returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)))
if result.returncode:sys.exit(17)
tty.setraw(0);sys.stdout.write('\x1b[?2004h');sys.stdout.flush()
control=pathlib.Path(os.environ['TEST_CONTROL']);received=pathlib.Path(os.environ['TEST_RECEIVED'])
glyph='›' if provider=='codex' else '❯';last=None;raw=b''
pathlib.Path(os.environ['TEST_READY']).write_text(session)
while True:
 style=control.read_text() if control.exists() else 'busy'
 if style=='stop':break
 if style!=last:
  sys.stdout.write('\x1b[2J\x1b[H'+('$ ' if style=='bare' else ('• Working (1 s)\r\n' if style=='busy' else '')+glyph+' '));sys.stdout.flush();last=style
 if select.select([sys.stdin],[],[],.05)[0]:
  raw+=os.read(0,65536)
  if b'\r' in raw or b'\n' in raw:
   with received.open('a') as stream:stream.write(json.dumps(dict(session=session,text=raw.decode('utf-8',errors='replace')))+'\n')
   raw=b''
'''


@pytest.mark.parametrize('provider',['claude','codex'])
def test_real_tmux_watcher_uses_only_bound_native_and_rejects_respawn(tmp_path,provider):
    """External-only: actual private socket, native entry, ps and real watcher."""
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            home,binary=prepare(client,provider,tmp_path)
            binary.write_text('#!'+sys.executable+'\n'+NATIVE)
            private=Path(tempfile.mkdtemp(prefix='o213-owned-tmux-',dir='/private/tmp'))
            socket=private/'notify.sock'
            control=tmp_path/'control';control.write_text('busy')
            received,ready=tmp_path/'received.jsonl',tmp_path/'native.ready'
            env=environment(client,AGENTSTACK_RUNTIME_MODE='fresh',**{'AGENTSTACK_'+provider.upper()+'_BIN':str(binary)},
                TEST_PROVIDER=provider,TEST_TRACE=str(tmp_path/'native-trace.json'),TEST_CONTROL=str(control),
                TEST_RECEIVED=str(received),TEST_READY=str(ready),LC_ALL='en_US.UTF-8')
            tmux=shutil.which('tmux');assert tmux,'tmux unavailable'
            log=[]
            async def command(*args,check=True):
                result=await asyncio.to_thread(subprocess.run,[tmux,'-u','-S',str(socket),*args],
                    env=env,capture_output=True,text=True,timeout=15)
                log.append(dict(args=list(args),returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))
                (tmp_path/'tmux-commands.json').write_text(canonical(log))
                if check:assert result.returncode==0,result.stderr
                return result
            # This server and every pane belong exclusively to this fixture.
            created=False
            try:
                entry=ROOT/('bin/agent-start-codex' if provider=='codex' else 'bin/agent-start')
                started=await command('new-session','-d','-s','fixture','-P','-F','#{pane_id}',
                    'exec '+shlex.join(['/bin/bash',str(entry),str(tmp_path)]));created=True
                pane=started.stdout.strip()
                other=(await command('new-window','-d','-t','fixture','-P','-F','#{pane_id}','exec /bin/cat')).stdout.strip()
                deadline=time.monotonic()+40
                while not ready.exists():
                    assert time.monotonic()<deadline, (tmp_path/'native-trace.json').read_text() if (tmp_path/'native-trace.json').exists() else canonical(log)
                    await asyncio.sleep(.1)
                # Launch observation publication can follow native SessionStart.
                base=client.path.parent/'launch'
                current=json.loads((base/'current.json').read_text())['launch_id']
                run_path=base/'runs'/current
                while not (run_path/'tmux.json').exists():
                    assert time.monotonic()<deadline;await asyncio.sleep(.05)
                native=json.loads((run_path/'session.json').read_text())
                assert native['native_session_id']==ready.read_text()
                observed=json.loads((run_path/'tmux.json').read_text())
                assert observed['pane']['pane_id']==pane
                sent=await runtime.core.write_message('send',**send_args(runtime,people,
                    auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}],subject='owned-pane-only'))
                async def once():
                    watcher_env=dict(env)
                    watcher_env.pop('AGENTSTACK_FRESH_CLIENT_CONTEXT',None)
                    result=await asyncio.to_thread(subprocess.run,['/bin/bash',str(WATCHER),'--fresh-config',
                        str(runtime.root/'config.json'),'--once'],env=watcher_env,capture_output=True,text=True,timeout=60)
                    with (tmp_path/'watcher-output.jsonl').open('a') as stream:
                        stream.write(canonical(dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))+'\n')
                    return result
                first=await once();assert first.returncode==0,first.stdout+first.stderr
                assert json.loads(first.stdout)['routes']==[dict(status='ok',completed=1,mail_agent_id=people[0]['agent_id'])]
                while not received.exists():
                    assert time.monotonic()<deadline;await asyncio.sleep(.05)
                messages=[json.loads(s) for s in received.read_text().splitlines()]
                assert len(messages)==1 and 'owned-pane-only' in messages[0]['text'] and messages[0]['session']==native['native_session_id']
                assert 'owned-pane-only' not in (await command('capture-pane','-p','-t',other)).stdout
                again=await once();assert again.returncode==0 and json.loads(again.stdout)['routes'][0]['completed']==0
                facts,pairs=await rows(runtime)
                assert facts[0]['completed_ts'] and pairs[0][2:]==(None,None)
                # Keep a second message pending while proving terminal refusal.
                second=await runtime.core.write_message('send',**send_args(runtime,people,request_id='second',
                    auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}],subject='must-stay-pending'))
                control.write_text('bare');await asyncio.sleep(.2)
                bare=await once();assert bare.returncode==1 and 'FRESH_TMUX_TARGET_NOT_READY' in bare.stdout
                assert len(received.read_text().splitlines())==1
                # Reusing the same pane ID with a new process must not admit cat.
                await command('respawn-pane','-k','-t',pane,'exec /bin/cat')
                stale=await once();assert stale.returncode in (0,1)
                assert not any(row.get('completed') for row in json.loads(stale.stdout)['routes'])
                assert 'must-stay-pending' not in (await command('capture-pane','-p','-t',pane)).stdout
                facts,pairs=await rows(runtime)
                assert facts[1]['completed_ts'] is None and all(r[2:] == (None,None) for r in pairs)
            finally:
                if created:
                    stopped=await command('kill-server',check=False)
                    assert stopped.returncode==0,stopped.stderr
                    (tmp_path/'tmux-cleanup.json').write_text(canonical(dict(socket=str(socket),stopped=True)))
                    # No socket outside this owned private root is ever removed.
                    if socket.exists():socket.unlink()
                shutil.rmtree(private)
    asyncio.run(run())
