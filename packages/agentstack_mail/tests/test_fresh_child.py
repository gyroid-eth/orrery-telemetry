"""Automatic child host entries: synthetic native providers; no real agents."""
from __future__ import annotations

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
from uuid import uuid4

import pytest

from agentstack_mail.fresh_child import family, handoff, launch_task, prepare as prepare_child, status
from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_runtime import _read_private
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_guard import live
from test_fresh_launch import NATIVE, prepare
from test_fresh_proxy import environment

ROOT = Path(__file__).resolve().parents[3]


def binary_for_child(parent, provider, tmp_path):
    home, binary = prepare(parent, provider, tmp_path)
    request = dict(jsonrpc='2.0', id=3, method='tools/call', params=dict(name='send_message', arguments=dict(
        recipients=[{'target_agent_id': parent.context['mail_agent_id']}], subject='child-result', body_md='task complete')))
    source = NATIVE.replace("proxied = subprocess.run(proxy_cmd,", "requests.insert(1, " + repr(request) + ")\nproxied = subprocess.run(proxy_cmd,")
    prefix = 'import os\nos.environ.update(' + repr(dict(TEST_PROVIDER=provider, TEST_TRACE=str(tmp_path / 'child-native.json'))) + ')\n'
    binary.write_text('#!' + sys.executable + '\n' + prefix + source)
    return home, binary


async def entry(parent, binary, provider, script, *args):
    env = environment(parent, AGENTSTACK_RUNTIME_MODE='fresh', AGENTSTACK_PYTHON=sys.executable,
        **{'AGENTSTACK_' + provider.upper() + '_BIN':str(binary)})
    return await asyncio.to_thread(subprocess.run, ['/bin/bash', str(ROOT / script), *map(str,args)],
        env=env, cwd=ROOT, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=45)


async def registered(parent, binary, provider, slot='worker'):
    args = ('--child-client-name',slot,'--name','ChildWorker','--program','codex' if provider == 'codex' else 'claude-code',
            '--model','fixture-model','--task-description','bounded child task','--prepare-only')
    result = await entry(parent,binary,provider,'bin/agentstack-preregister-child',*args)
    assert result.returncode == 0,result.stdout+result.stderr
    value=json.loads(result.stdout)
    again=await entry(parent,binary,provider,'bin/agentstack-preregister-child',*args)
    assert again.returncode == 0 and json.loads(again.stdout)==value
    assert value['prepared'] and not value['running']
    child=FreshBoundClient(value['context_path'])
    assert child.context['mail_instance_id']==parent.context['mail_instance_id']
    assert child.context['mail_agent_id']!=parent.context['mail_agent_id'] and child.path!=parent.path
    return child


async def prepared_task(parent,binary,provider,tmp_path):
    child=await registered(parent,binary,provider)
    work=tmp_path/'outside-folder';work.mkdir()
    task=tmp_path/'task.md';task.write_text('Read this literally: `touch SHOULD_NOT_EXIST` $(touch ALSO_NOT_CREATED)\n')
    args=['--pre-registered','ChildWorker','--embed-task','--task-file',str(task),
          '--resources','note.py','--prepare-only',str(work)]
    if provider=='codex':args[0:0]=['--codex','--model','fixture-model']
    result=await entry(parent,binary,provider,'hooks/spawn_child.sh',*args)
    assert result.returncode==0,result.stdout+result.stderr
    value=json.loads(result.stdout);assert value==dict(prepared=True,running=False,native_ready=False,name='ChildWorker')
    return child,work,task,args


@pytest.mark.parametrize('provider',['claude','codex'])
def test_two_real_entries_prepare_and_native_callback_with_synthetic_transport(tmp_path,provider,monkeypatch):
    async def run():
        async with live(tmp_path) as (_,_,parent,_):
            home,binary=binary_for_child(parent,provider,tmp_path)
            monkeypatch.setenv('AGENTSTACK_'+provider.upper()+'_BIN',str(binary))
            child,work,task,_=await prepared_task(parent,binary,provider,tmp_path)
            parent_inputs={path:path.read_bytes() for path in (parent.path,home/'AGENTS.md' if provider=='codex' else home/'CLAUDE.md',
                *([home/'config.toml',home/'hook-plan.json'] if provider=='codex' else []))}
            class Transport:
                identity=(1,1);path=tmp_path/'synthetic.sock';calls=[]
                def command(self,*args):
                    self.calls.append(args);return subprocess.CompletedProcess(args,0,'','')
            transport=Transport()
            before=await handoff(parent,ROOT,slot='worker',cwd=work,task=task.read_text(),resources=['note.py'],transport=transport)
            assert before['accepted'] and before['execution']=='unknown' and not before['native_ready']
            same=await handoff(parent,ROOT,slot='worker',cwd=work,task=task.read_text(),resources=['note.py'],transport=transport)
            assert same==before and len(transport.calls)==1
            argv=shlex.split(transport.calls[0][-1])
            # The actual native callback command is reused, with no tmux in this
            # synthetic transport run. Actual tmux/ps acceptance is separate.
            env=dict(os.environ);env.pop('TMUX',None);env.pop('TMUX_PANE',None)
            parent_owner,result=await asyncio.gather(parent.call('reconnect',{}),asyncio.to_thread(
                subprocess.run,argv,env=env,cwd=work,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=45))
            assert parent_owner['mail_agent_id']==parent.context['mail_agent_id']
            assert result.returncode==0,result.stdout+result.stderr
            trace=json.loads((tmp_path/'child-native.json').read_text())
            assert trace['home']==str(home) and trace['context']==str(child.path)
            assert trace['status']['mail_agent_id']==child.context['mail_agent_id']
            assert trace['session_hook_rc']==trace['guard_rc']==trace['proxy_rc']==0
            assert task.read_text() in trace['args'][-1] and '--model' in trace['args']
            assert not (work/'SHOULD_NOT_EXIST').exists() and not (work/'ALSO_NOT_CREATED').exists()
            assert all(path.read_bytes()==raw for path,raw in parent_inputs.items())
            inbox=await parent.call('fetch_inbox',{})
            assert len(inbox)==1 and inbox[0]['subject']=='child-result' and inbox[0]['sender_id']==child.context['mail_agent_id']
            unchanged=(tmp_path/'child-native.json').read_bytes()
            duplicate=await asyncio.to_thread(subprocess.run,argv,env=env,cwd=work,
                stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=15)
            assert duplicate.returncode==2 and 'FRESH_CHILD_LAUNCH_OUTCOME_UNKNOWN' in duplicate.stderr
            assert (tmp_path/'child-native.json').read_bytes()==unchanged
            assert len(await parent.call('fetch_inbox',{}))==1
            _,base,_=family(child);done=status(child,base)
            assert done['execution']=='ended' and done['native_ready']
            (tmp_path/'fresh-child-result.json').write_text(json.dumps(dict(provider=provider,
                real_two_entries=True, synthetic_transport=True, same_instance_separate_owner=True,
                task_not_shell_evaluated=True, parent_received_child_mail=True, provider_home=str(home),
                child_context=str(child.path), actual_tmux=False)))
    asyncio.run(run())


def test_child_conflicting_intent_changed_parent_and_missing_preparation_refuse(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            home,binary=binary_for_child(parent,'claude',tmp_path)
            monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(binary))
            child,work,task,_=await prepared_task(parent,binary,'claude',tmp_path)
            with pytest.raises(B1Error,match='FRESH_CHILD_INTENT_CONFLICT'):
                await prepare_child(parent,'worker','OtherWorker','claude-code','fixture-model','bounded child task')
            with pytest.raises(B1Error,match='FRESH_CHILD_INTENT_CONFLICT'):
                await handoff(parent,ROOT,slot='worker',cwd=work,task='changed task',resources=['note.py'],prepare_only=True)
            instructions=home/'CLAUDE.md';original=instructions.read_bytes();instructions.write_text('missing preparation')
            with pytest.raises(B1Error,match='INSTRUCTION_PROVIDER_CONFIG_REQUIRED'):
                await handoff(parent,ROOT,slot='worker',cwd=work,task=task.read_text(),resources=['note.py'],prepare_only=True)
            instructions.write_bytes(original)
            await runtime.core.recover(people[0]['agent_id'],expected_generation=people[0]['credential_generation'],new_token='fixture-rotated-parent')
            refused=await entry(parent,binary,'claude','hooks/spawn_child.sh','--pre-registered','ChildWorker',
                '--embed-task','--task-file',task,'--resources','note.py','--prepare-only',work)
            assert refused.returncode==2 and 'FRESH CHILD:' in refused.stderr
            assert not (child.path.parent/'launch/current.json').exists()
    asyncio.run(run())


@pytest.mark.parametrize('provider',['claude','codex'])
def test_external_private_tmux_child_two_entries_and_parent_mail(tmp_path,provider):
    """Run only by the parent outside the sandbox, on a fixture-owned socket."""
    async def run():
        async with live(tmp_path) as (_,_,parent,_):
            _,binary=binary_for_child(parent,provider,tmp_path)
            child,work,task,args=await prepared_task(parent,binary,provider,tmp_path)
            marker = "\nprovider = os.environ['TEST_PROVIDER']"
            assert marker in binary.read_text()
            binary.write_text(binary.read_text().replace(marker,
                '\npathlib.Path('+repr(str(tmp_path/'fake-entry.json'))+').write_text(json.dumps(dict('
                'entered=True, mode=os.environ.get("AGENTSTACK_RUNTIME_MODE"), '
                'LC_ALL=os.environ.get("LC_ALL"), LC_CTYPE=os.environ.get("LC_CTYPE"))))'+marker,1))
            private=Path(tempfile.mkdtemp(prefix='o213-child-tmux-',dir='/private/tmp'));private.chmod(0o700)
            socket=private/'tmux.sock';tmux=shutil.which('tmux');assert tmux
            env=environment(parent,AGENTSTACK_RUNTIME_MODE='fresh',AGENTSTACK_PYTHON=sys.executable,LC_ALL='C',
                **{'AGENTSTACK_'+provider.upper()+'_BIN':str(binary)})
            env.pop('TMUX',None);env.pop('TMUX_PANE',None)
            argv=['/bin/bash',str(ROOT/'hooks/spawn_child.sh'),*[a for a in args if a!='--prepare-only']]
            output=tmp_path/'spawn-output.json';release=tmp_path/'parent-release'
            runner=tmp_path/'parent-pane.py'
            runner.write_text('config='+repr(dict(tmux=tmux,socket=str(socket),argv=argv,cwd=str(work),
                root=str(ROOT),output=str(output),proof=str(tmp_path/'parent-transport.json'),release=str(release)))+'\n'+r'''
import json,os,runpy,subprocess,time
from pathlib import Path
def publish(name,value):
    path=Path(name);temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value));temporary.replace(path)
inherited={key:os.environ.get(key) for key in ('TMUX','TMUX_PANE')}
proof=dict(environment=inherited,caller_locale={key:os.environ.get(key) for key in ('LC_ALL','LC_CTYPE')},calls=[])
publish(config['proof'],proof)
def observed_run(argv,**kwargs):
    result=subprocess.run(argv,**kwargs)
    proof['calls'].append(dict(command=argv,returncode=result.returncode,stdout=result.stdout,stderr=result.stderr,
        child_locale={key:kwargs['env'].get(key) for key in ('LC_ALL','LC_CTYPE')}))
    publish(config['proof'],proof)
    return result
try:
    endpoint=(inherited['TMUX'] or '').rsplit(',',2)
    assert len(endpoint)==3 and endpoint[0]==config['socket'] and endpoint[1].isdigit()
    assert inherited['TMUX_PANE']
    api=runpy.run_path(config['root']+'/bin/lib/runtime_tmux.py')
    transport=api['legacy_transport'](binary=config['tmux'],run=observed_run)
    proof['panes']=api['panes'](transport,pane=inherited['TMUX_PANE'])
    # Fixture-only: retain an exited child pane so the first failure stays observable.
    retained=transport.command('set-option','-g','remain-on-exit','on')
    assert retained.returncode==0,retained.stderr
    publish(config['proof'],proof)
    spawned=subprocess.run(config['argv'],cwd=config['cwd'],stdin=subprocess.DEVNULL,
        capture_output=True,text=True,timeout=40)
    publish(config['output'],dict(returncode=spawned.returncode,stdout=spawned.stdout,stderr=spawned.stderr))
except Exception as exc:
    publish(config['output'],dict(returncode=-1,stdout='',stderr=repr(exc)))
deadline=time.monotonic()+100
while not Path(config['release']).exists() and time.monotonic()<deadline:
    time.sleep(.1)
''')
            async def command(*values):
                return await asyncio.to_thread(subprocess.run,[tmux,'-S',str(socket),*values],
                    env=env,capture_output=True,text=True,timeout=15)
            created=False
            try:
                start=await command('new-session','-d','-s','fixture-parent',
                    'exec '+shlex.join([sys.executable,str(runner)]))
                assert start.returncode==0,start.stderr
                created=True
                deadline=time.monotonic()+45
                while not output.exists():
                    assert time.monotonic()<deadline,'private parent pane did not publish spawn result'
                    await asyncio.sleep(.1)
                spawned=json.loads(output.read_text())
                assert spawned['returncode']==0,spawned
                _,base,_=family(child);deadline=time.monotonic()+45
                while True:
                    observed=status(child,base)
                    if observed['execution']=='ended':break
                    assert time.monotonic()<deadline,observed
                    await asyncio.sleep(.1)
                assert observed['native_ready']
                proof=json.loads((tmp_path/'parent-transport.json').read_text())
                assert proof['caller_locale']['LC_ALL']=='C' and len(proof['panes'])==1
                assert all(call['returncode']==0 and call['child_locale']=={'LC_ALL':None,'LC_CTYPE':'C.UTF-8'} for call in proof['calls'])
                entered=json.loads((tmp_path/'fake-entry.json').read_text())
                assert entered['entered'] and entered['LC_ALL']=='C'
                inbox=await parent.call('fetch_inbox',{})
                assert len(inbox)==1 and inbox[0]['subject']=='child-result' and inbox[0]['sender_id']==child.context['mail_agent_id']
                (tmp_path/'external-child-result.json').write_text(json.dumps(dict(provider=provider,
                    actual_private_tmux=True, real_two_entries=True, native_ready=True,parent_received_child_mail=True)))
            finally:
                release.touch()
                if created:
                    try:
                        session='=orrery-'+child.context['client_id']
                        state=await command('list-panes','-t',session,'-F','#{pane_id}|#{pane_pid}|#{pane_dead}|#{pane_dead_status}')
                        screen=await command('capture-pane','-p','-t',session,'-S','-40')
                        trace=tmp_path/'child-native.json'
                        callbacks=json.loads(trace.read_text()) if trace.exists() else {}
                        (tmp_path/'child-process.json').write_text(json.dumps(dict(
                            pane_query=dict(returncode=state.returncode,stdout=state.stdout,stderr=state.stderr),
                            terminal_combined_output=dict(returncode=screen.returncode,stdout=screen.stdout[-16384:],stderr=screen.stderr),
                            fake_provider_entered=(tmp_path/'fake-entry.json').exists(),
                            callback_results={key:callbacks.get(key) for key in ('session_hook_rc','guard_rc','proxy_rc')})))
                    finally:
                        stopped=await command('kill-server');assert stopped.returncode==0,stopped.stderr
                shutil.rmtree(private)
                (tmp_path/'tmux-cleanup.json').write_text(json.dumps(dict(socket=str(socket),stopped=created)))
    asyncio.run(run())
