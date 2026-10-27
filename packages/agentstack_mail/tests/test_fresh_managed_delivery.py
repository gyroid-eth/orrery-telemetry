"""Managed readiness and existing watcher; real ps/tmux cases are parent-only."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from agentstack_mail import fresh_delivery as delivery
from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_harness import readiness
from agentstack_mail.fresh_runtime import _read_private
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_delivery import mocked_os, prepared_route
from test_fresh_guard import live
from test_fresh_harness import fixture, entry
from test_fresh_mail import send_args
from test_fresh_notifications import rows
from test_fresh_proxy import environment

ROOT=Path(__file__).resolve().parents[3]
DSH='╭────────────╮\n⌸ ❯  ⛶\n╰────────────╯\nctx 19.5% (12.5k/64k)\n? for shortcuts'
HERMES='────────────\n❯\n────────────'


@pytest.mark.parametrize('program,text,state,empty',[
    ('dsh',DSH,'idle',True),('dsh',DSH.replace('? for shortcuts','esc to interrupt'),'work',True),
    ('dsh',DSH.replace('❯  ','❯ typed '),'idle',False),
    ('dsh','❯','unknown',False),('dsh',DSH.replace('ctx 19.5% (12.5k/64k)','unknown footer'),'unknown',True),
    ('hermes',HERMES,'idle',True),('hermes',HERMES.replace('❯','☤'),'work',True),
    ('hermes',HERMES.replace('❯','profile ❯'),'idle',True),
    ('hermes',HERMES.replace('❯','❯ typed'),'idle',False),
    ('hermes',HERMES.replace('❯','☤ typed'),'work',False),
    ('hermes',HERMES.replace('❯','⚠ ❯'),'unknown',False),('hermes','❯','unknown',False),
])
def test_native_readiness_requires_activity_and_empty_current_frame(program,text,state,empty):
    assert readiness(ROOT,text,program)==dict(state=state,input_empty=empty)


async def managed_route(client,tmp_path,monkeypatch,program):
    run,value=await prepared_route(client,tmp_path,monkeypatch)
    # A native opaque ID is valid for managed CLIs, not required to be a UUID.
    session='native-managed-'+program
    for name in ('intent.json','session.json'):
        path=run/name;row=json.loads(path.read_text());row['provider']=program
        if name=='session.json':
            old=row['native_session_id'];row['native_session_id']=session
        path.write_text(json.dumps(row))
    path=run.parent.parent/'sessions'/(old+'.json')
    row=json.loads(path.read_text());row.update(provider=program,native_session_id=session)
    path.rename(path.with_name(session+'.json'));path.with_name(session+'.json').write_text(json.dumps(row))
    return run,value


@pytest.mark.parametrize('program',['dsh','hermes'])
def test_managed_current_recipient_completion_read_ack_and_refusal(tmp_path,monkeypatch,program):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            saved,_=await managed_route(client,tmp_path,monkeypatch,program)
            screen=DSH if program=='dsh' else HERMES
            commands,_,_=mocked_os(monkeypatch,screen=screen)
            sent=await runtime.core.write_message('send',**send_args(runtime,people,
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}],subject='managed-only'))
            assert (await delivery.deliver(client,ROOT))['completed']==1
            assert any(args[0]=='send-keys' for args,_ in commands)
            facts,pairs=await rows(runtime)
            assert facts[0]['completed_ts'] and pairs==[(sent['message_id'],people[0]['agent_id'],None,None)]
            assert (await delivery.deliver(client,ROOT))['completed']==0
            await runtime.core.write_message('send',**send_args(runtime,people,request_id='pending-second',
                auth=people[1]['auth'],recipients=[{'target_name':'Alpha'}],subject='stay-pending'))
            for invalid in (screen.replace('❯','❯ typed'), 'unrecognized screen'):
                rejected,_,_=mocked_os(monkeypatch,screen=invalid)
                with pytest.raises(B1Error,match='FRESH_TMUX_TARGET_NOT_READY'):await delivery.deliver(client,ROOT)
                assert not any(args[0]=='load-buffer' for args,_ in rejected)
            (saved/'ended.json').write_text('{}')
            with pytest.raises(B1Error,match='FRESH_TMUX_TARGET_ENDED'):await delivery.deliver(client,ROOT)
            facts,pairs=await rows(runtime)
            assert facts[1]['completed_ts'] is None and all(pair[2:]==(None,None) for pair in pairs)
    asyncio.run(run())


TUI=r'''
import select,termios,time,tty
tty.setraw(0);sys.stdout.write('\x1b[?2004h');sys.stdout.flush()
control=pathlib.Path(os.environ['TEST_CONTROL']);received=pathlib.Path(os.environ['TEST_RECEIVED'])
pathlib.Path(os.environ['TEST_READY']).write_text(session)
last=None;raw=b''
while True:
    mode=control.read_text()
    if mode=='stop':break
    if mode!=last:
        screen=SCREEN if mode=='ready' else (SCREEN.replace('❯','❯ typed') if mode=='typed' else 'unknown screen')
        sys.stdout.write('\x1b[2J\x1b[H'+screen.replace('\n','\r\n'));sys.stdout.flush();last=mode
    if select.select([sys.stdin],[],[],.05)[0]:
        raw+=os.read(0,65536)
        if b'\r' in raw or b'\n' in raw:
            with received.open('a') as stream:stream.write(json.dumps(dict(session=session,text=raw.decode('utf-8',errors='replace')))+'\n')
            raw=b''
'''


@pytest.mark.parametrize('program',['dsh','hermes'])
def test_external_managed_child_existing_watcher_private_tmux(tmp_path,program):
    """Parent-only: actual child entry, native plugin, watcher and private socket."""
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            root,manifest,model,cleanup=fixture(program,tmp_path,parent.context['mail_agent_id'])
            private=Path(tempfile.mkdtemp(prefix='o213-managed-tmux-',dir='/private/tmp'));socket=private/'mail.sock'
            created=False
            try:
                # Only the fixture's synthetic native is replaced; receipts stay tied to its bytes.
                template=json.loads(Path(json.loads(manifest.read_text())['harnesses'][program]['config_file']).read_text())
                binary=Path(template['node' if program=='dsh' else 'hermes_executable'])
                native=binary.read_text().replace('session=str(uuid.uuid4())','session='+repr('native-managed-'+program))
                native+='\n'+TUI.replace('SCREEN',repr(DSH if program=='dsh' else HERMES))
                binary.write_text(native)
                if program=='dsh':
                    evidence=Path(template['runtime']).parent/'evidence/node-runtime.json'
                    value=json.loads(evidence.read_text())
                    import hashlib
                    value['sha256']=hashlib.sha256(binary.read_bytes()).hexdigest();evidence.write_text(json.dumps(value))
                result=await entry(parent,root,'bin/agentstack-preregister-child',
                    '--child-client-name','worker','--name','ManagedChild','--program',program,'--model',model,
                    '--task-description','managed notification','--manifest',manifest,'--backend','strata','--prepare-only')
                assert result.returncode==0,result.stdout+result.stderr
                child=FreshBoundClient(json.loads(result.stdout)['context_path'])
                work=tmp_path/'work';work.mkdir();task=tmp_path/'task.md';task.write_text('literal managed notification task')
                control=tmp_path/'control';control.write_text('ready')
                ready=tmp_path/'ready';received=tmp_path/'received.jsonl'
                env=environment(parent,AGENTSTACK_RUNTIME_MODE='fresh',AGENTSTACK_PYTHON=sys.executable,
                    TEST_CONTROL=str(control),TEST_READY=str(ready),TEST_RECEIVED=str(received),LC_ALL='C')
                tmux=shutil.which('tmux');assert tmux
                commands=[]
                async def command(*args,check=True):
                    done=await asyncio.to_thread(subprocess.run,[tmux,'-u','-S',str(socket),*args],env=env,
                        capture_output=True,text=True,timeout=15)
                    commands.append(dict(args=list(args),returncode=done.returncode,stdout=done.stdout,stderr=done.stderr))
                    (tmp_path/'tmux-commands.json').write_text(json.dumps(commands))
                    if check:assert done.returncode==0,done.stderr
                    return done
                argv=['/bin/bash',str(root/'hooks/spawn_child.sh'),'--child-client-name','worker','--embed-task',
                    '--task-file',str(task),'--harness',program,'--backend','strata','--resources','note.py',str(work)]
                driver=tmp_path/'parent.py'
                driver.write_text('import json,pathlib,subprocess,time\nr=subprocess.run('+repr(argv)+',capture_output=True,text=True,timeout=60)\n'
                    +'pathlib.Path('+repr(str(tmp_path/'spawn.json'))+').write_text(json.dumps(dict(returncode=r.returncode,stdout=r.stdout,stderr=r.stderr)))\ntime.sleep(180)\n')
                await command('new-session','-d','-s','parent','-c',str(work),shlex.join([sys.executable,str(driver)]));created=True
                other=(await command('new-window','-d','-t','parent','-P','-F','#{pane_id}','exec /bin/cat')).stdout.strip()
                deadline=time.monotonic()+75
                while not ready.exists():
                    assert time.monotonic()<deadline,(tmp_path/'spawn.json').read_text() if (tmp_path/'spawn.json').exists() else commands
                    await asyncio.sleep(.1)
                base=child.path.parent/'launch';current=_read_private(base/'current.json')['launch_id'];run=base/'runs'/current
                while not (run/'tmux.json').exists():
                    assert time.monotonic()<deadline;await asyncio.sleep(.05)
                observed=_read_private(run/'tmux.json');native_receipt=_read_private(run/'session.json')
                assert native_receipt['native_session_id']=='native-managed-'+program
                async def once():
                    watcher=dict(env);watcher.pop('AGENTSTACK_FRESH_CLIENT_CONTEXT',None)
                    done=await asyncio.to_thread(subprocess.run,['/bin/bash',str(root/'hooks/watch_agent_mail_signals.sh'),
                        '--fresh-config',str(runtime.root/'config.json'),'--once'],env=watcher,capture_output=True,text=True,timeout=60)
                    with (tmp_path/'watcher.jsonl').open('a') as output:
                        output.write(json.dumps(dict(returncode=done.returncode,stdout=done.stdout,stderr=done.stderr))+'\n')
                    return done
                await parent.call('send_message',dict(recipients=[{'target_agent_id':child.context['mail_agent_id']}],subject='managed-pane-only',body_md='Private fixture notification.'))
                first=await once();assert first.returncode==0,first.stdout+first.stderr
                assert any(row.get('completed')==1 and row['mail_agent_id']==child.context['mail_agent_id'] for row in json.loads(first.stdout)['routes'])
                while not received.exists():
                    assert time.monotonic()<deadline;await asyncio.sleep(.05)
                messages=[json.loads(line) for line in received.read_text().splitlines()]
                assert len(messages)==1 and 'managed-pane-only' in messages[0]['text']
                assert messages[0]['session']==native_receipt['native_session_id']
                assert 'managed-pane-only' not in (await command('capture-pane','-p','-t',other)).stdout
                again=await once();assert again.returncode==0
                assert not any(row.get('completed') for row in json.loads(again.stdout)['routes'])
                await parent.call('send_message',dict(recipients=[{'target_agent_id':child.context['mail_agent_id']}],subject='stay-pending',body_md='Private fixture pending notification.'))
                for mode in ('typed','unknown'):
                    control.write_text(mode);await asyncio.sleep(.3)
                    rejected=await once();assert rejected.returncode==1 and 'FRESH_TMUX_TARGET_NOT_READY' in rejected.stdout
                    assert len(received.read_text().splitlines())==1
                facts,pairs=await rows(runtime)
                own=[row for row in facts if row['agent_id']==child.context['mail_agent_id']]
                assert len(own)==2 and own[0]['completed_ts'] and own[1]['completed_ts'] is None
                assert all(pair[2:]==(None,None) for pair in pairs)
                await command('respawn-pane','-k','-t',observed['pane']['pane_id'],'exec /bin/cat')
                stale=await once();assert not any(row.get('completed') for row in json.loads(stale.stdout)['routes'])
                (tmp_path/'managed-notification-proof.json').write_text(json.dumps(dict(program=program,actual_tmux=True,
                    synthetic_native=True,actual_plugin=True,recipient=child.context['mail_agent_id'],
                    native_session=native_receipt['native_session_id'],displayed_once=True,pending_on_refusal=True,read_ack_unchanged=True)))
            finally:
                if created:
                    stopped=await command('kill-server',check=False);assert stopped.returncode==0,stopped.stderr
                    (tmp_path/'tmux-cleanup.json').write_text(json.dumps(dict(socket=str(socket),stopped=True)))
                shutil.rmtree(private);cleanup()
    asyncio.run(run())
