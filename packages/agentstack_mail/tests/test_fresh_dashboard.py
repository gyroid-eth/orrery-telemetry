"""Real dashboard Handler + private fresh ORM/HTTP and common launch handoff.

Native executables and Transport are synthetic; OS tmux, real provider HOME,
trust, login and services are deliberately not part of these tests.
"""
import asyncio
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request
from uuid import uuid4

import pytest

from dashboard.fresh_backend import Backend
from agentstack_mail.fresh_child import family, launch_task, prepare
from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_child import binary_for_child
from test_fresh_guard import live
from test_fresh_mail import snapshot
from test_fresh_harness import fixture as managed_fixture

ROOT=Path(__file__).resolve().parents[3]


def test_roster_display_cache_parallel_get_expiry_and_owner_changes_are_not_authority(tmp_path,monkeypatch):
    async def scenario():
        from agentstack_mail.fresh_targets import Targets
        from test_fresh_targets import cleanup_fixture
        import time
        async with cleanup_fixture(tmp_path,monkeypatch) as (runtime,people,client,operator,run,row,calls,*_):
            backend=Backend(operator.path,ROOT);entered=threading.Event();release=threading.Event();observed=[]
            def slow(self,selections,*,deadline):
                observed.append(deadline);entered.set();assert release.wait(5)
                return {people[0]['agent_id']:dict(category='finished',running=False,attached=False)}
            monkeypatch.setattr(Targets,'display_snapshot',slow)
            first=asyncio.create_task(asyncio.to_thread(lambda:asyncio.run(backend.roster())))
            assert await asyncio.to_thread(entered.wait,5)
            second=await backend.roster()
            row=next(a for a in second['agents'] if a['name']=='Alpha')
            assert row['category']=='unknown' and row['running'] is None and row['attached']
            release.set();await first
            good=await backend.roster();a=next(a for a in good['agents'] if a['name']=='Alpha')
            assert a['category']=='finished' and len(observed)==1
            key,stamp,facts=backend._display_cache
            backend._display_cache=(key,time.monotonic()-6,facts)
            backend._display_lock.acquire()
            try:
                stale=await backend.roster()
                assert next(a for a in stale['agents'] if a['name']=='Alpha')['category']=='unknown'
            finally:backend._display_lock.release()
            await runtime.core.recover(people[1]['agent_id'],expected_generation=0,new_token='roster-fixture-new-owner-secret')
            with pytest.raises(B1Error):await backend.roster()
            assert len(observed)==1
    asyncio.run(scenario())


class Transport:
    identity=(1,1)
    def __init__(self,tmp):self.path=tmp/'synthetic.sock';self.calls=[]
    def command(self,*args):
        self.calls.append(args)
        return subprocess.CompletedProcess(args,0,'','')


@contextmanager
def dashboard(parent,backend,monkeypatch,tmp):
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','fresh')
    monkeypatch.setenv('AGENTSTACK_FRESH_CLIENT_CONTEXT',str(parent.path))
    monkeypatch.setenv('AGENTSTACK_HOME',str(tmp/'not-live-stack'))
    monkeypatch.setenv('AGENTSTACK_SPAWN_ROOTS',str(tmp))
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    monkeypatch.delenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',raising=False)
    spec=importlib.util.spec_from_file_location('fresh_dashboard_'+uuid4().hex,ROOT/'dashboard/server.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    assert module.DB_PATH=='' and module.FRESH_MODE and not module.GLOBAL_MODE
    module._FRESH_BACKEND=backend
    monkeypatch.setattr(module,'_claude_catalog',lambda:SimpleNamespace(models=('claude-sonnet-4-6',),
        overflow=(),source='override',error=None))
    monkeypatch.setattr(module.codex_models,'resolve_launcher',lambda:module.codex_models.LauncherPolicy())
    monkeypatch.setenv('AGENTSTACK_CODEX_MODELS','gpt-6.1-sol')
    server=module.ThreadingHTTPServer(('127.0.0.1',0),module.Handler)
    assert server.server_port not in {8765,8770,8781,8791}
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(path,body=None,headers=None,raw=None):
        data=json.dumps(body).encode() if body is not None else raw
        req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,data=data,
            headers={'Content-Type':'application/json',**(headers or {})})
        try:
            with urllib.request.urlopen(req,timeout=60) as result:return result.status,json.loads(result.read())
        except urllib.error.HTTPError as error:
            data=error.read()
            try:return error.code,json.loads(data)
            except ValueError:return error.code,{'error':'HTTP admission refused'}
    try:yield module,request
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        assert not thread.is_alive()
        sys.modules.pop(spec.name,None)


@pytest.mark.parametrize('provider',['claude','codex'])
@pytest.mark.parametrize('standalone',[False,True])
def test_handler_new_agent_native_common_handoff_and_roster(tmp_path,monkeypatch,provider,standalone):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            _,binary=binary_for_child(parent,provider,tmp_path)
            monkeypatch.setenv('AGENTSTACK_'+provider.upper()+'_BIN',str(binary))
            transport=Transport(tmp_path);backend=Backend(parent.path,ROOT,transport=lambda:transport)
            payload=dict(name='DashboardNative',task='Literal task `no-shell` $(no-eval)',dir=str(tmp_path),
                provider=provider,model='claude-sonnet-4-6' if provider=='claude' else 'gpt-6.1-sol',
                effort='high',resources=['note.py'],role='',group='',**({'standalone':True} if standalone else {'parent':'Alpha'}))
            with dashboard(parent,backend,monkeypatch,tmp_path) as (module,request):
                code,version=await asyncio.to_thread(request,'/api/version')
                assert code==200 and version['api']==34 and version['runtime_mode']=='fresh'
                assert version['capabilities']['terminal'] is True
                code,result=await asyncio.to_thread(request,'/api/spawn',payload)
                assert code==200 and result['ok'] and result['pending'],result
                assert result['child_name']=='DashboardNative' and result['mail_agent_id']!=parent.context['mail_agent_id']
                assert len(transport.calls)==1
                _,pending=await asyncio.to_thread(request,'/api/spawn-status?name=DashboardNative')
                assert pending['state']=='launching' and pending['result'] is None and pending['detail']=='unknown'
                child=backend.context_for(await backend.resolve(name='DashboardNative'))
                _,base,ready=family(child)
                assert bool(ready.get('standalone'))==standalone and ready['effort']=='high'
                task,prompt=await asyncio.to_thread(launch_task,child)
                assert task['task']==payload['task']
                assert ('Report to the parent Mail agent ID' in prompt) is not standalone
                argv=shlex.split(transport.calls[0][-1]);env=dict(os.environ)
                env.pop('TMUX',None);env.pop('TMUX_PANE',None)
                done=await asyncio.to_thread(subprocess.run,argv,env=env,cwd=tmp_path,
                    stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=60)
                assert done.returncode==0,done.stderr
                trace=json.loads((tmp_path/'child-native.json').read_text())
                assert trace['context']==str(child.path) and trace['status']['mail_agent_id']==result['mail_agent_id']
                if provider=='claude':assert trace['args'][trace['args'].index('--effort')+1]=='high'
                else:assert 'model_reasoning_effort="high"' in trace['args']
                before=await snapshot(runtime)
                _,roster=await asyncio.to_thread(request,'/api/agents?scope=live')
                _,finished=await asyncio.to_thread(request,'/api/spawn-status?name=DashboardNative')
                row=next(a for a in roster['agents'] if a['mail_agent_id']==result['mail_agent_id'])
                assert row['native_ready'] and row['state']=='finished'
                assert finished['state']=='ready' and finished['result']['native_ready'] and finished['detail']=='ended'
                assert await snapshot(runtime)==before
                wire=json.dumps([result,roster,finished])
                assert not any(key in wire for key in ('registration_token','binding_path','context_path','credential_generation'))
                assert len(transport.calls)==1
                refused,error=await asyncio.to_thread(request,'/api/spawn',payload)
                assert refused==409 and error['error_code']=='FRESH_SPAWN_NAME_OCCUPIED'
                assert len(transport.calls)==1
    asyncio.run(run())


def test_real_handler_security_input_and_unconnected_routes_do_not_mutate(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,_,parent,_):
            backend=Backend(parent.path,ROOT)
            with dashboard(parent,backend,monkeypatch,tmp_path) as (_,request):
                before=await snapshot(runtime)
                base=dict(task='task',name='NeverRegistered',dir=str(tmp_path),provider='claude',
                          model='claude-sonnet-4-6',standalone=True)
                for header,status in [({'Origin':'https://evil.invalid'},403),
                                      ({'Sec-Fetch-Site':'cross-site'},403),({'Content-Type':'text/plain'},415)]:
                    code,_=await asyncio.to_thread(request,'/api/spawn',base,header)
                    assert code==status
                code,_=await asyncio.to_thread(request,'/api/spawn',raw=b'{')
                assert code==400
                for changes in ({'auth':{}},{'expected_server_instance_id':'old'},
                                {'context_path':str(parent.path)},{'endpoint':'http://evil.invalid'},
                                {'standalone':'true'},{'worktree':True},{'base':'other'},
                                {'standalone':False,'parent':'Missing'},
                                {'standalone':False,'parent_ref':{'mail_instance_id':str(uuid4()),'mail_agent_id':1}}):
                    code,error=await asyncio.to_thread(request,'/api/spawn',{**base,**changes})
                    assert code==409 and not error['ok'],error
                for path,status in (('/api/graph',200),('/api/mobile-mail',200),('/api/mobile-message',400)):
                    code,error=await asyncio.to_thread(request,path)
                    assert code==status
                code,error=await asyncio.to_thread(request,'/api/jump',{'session':'Alpha'})
                assert code==409 and error['error']=='FRESH_TARGET_UNAVAILABLE'
                assert await snapshot(runtime)==before
    asyncio.run(run())


def test_parent_selector_counts_retired_aliases_and_explicit_id_stays_fixed(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            backend=Backend(parent.path,ROOT)
            # Use the actual ORM to make a second alias-key row with a distinct
            # ID; the census rules count retired rows as well.
            alias=await runtime.register_local(request_id=str(uuid4()),name='ALPHA',program='fixture',model='fixture')
            await runtime.core.retire(alias['agent_id'])
            # Directory ledger validates the existing records' current names.
            with pytest.raises(B1Error,match='TARGET_NAME_AMBIGUOUS'):await backend.resolve(name='Alpha')
            ref={k:parent.context[k] for k in ('mail_instance_id','mail_agent_id')}
            selected=await backend.resolve(ref=ref)
            assert backend.context_for(selected).path==parent.path
            with pytest.raises(B1Error,match='TARGET_RETIRED'):
                await backend.resolve(ref=dict(ref,mail_agent_id=alias['agent_id']))
            with pytest.raises(B1Error,match='INSTANCE_MISMATCH'):
                await backend.resolve(ref=dict(ref,mail_instance_id=str(uuid4())))
    asyncio.run(run())


@pytest.mark.parametrize('program',['dsh','hermes'])
@pytest.mark.parametrize('standalone',[False,True])
def test_handler_managed_selection_native_plugin_and_logical_parent(tmp_path,monkeypatch,program,standalone):
    async def run():
        async with live(tmp_path) as (runtime,_,parent,_):
            root,manifest,model,cleanup=managed_fixture(program,tmp_path,parent.context['mail_agent_id'])
            transport=Transport(tmp_path)
            backend=Backend(parent.path,root,transport=lambda:transport)
            monkeypatch.setenv('AGENTSTACK_MANAGED_SPAWN_CONFIG',str(manifest))
            payload=dict(name='DashboardManaged',task='managed task',dir=str(tmp_path),provider='strata',
                harness=program,model=model,resources=['note.py'],
                **({'standalone':True} if standalone else {'parent_ref':{k:parent.context[k] for k in ('mail_instance_id','mail_agent_id')}}))
            try:
                with dashboard(parent,backend,monkeypatch,tmp_path) as (module,request):
                    from agentstack_mail.fresh_harness import helper
                    # The synthetic CA receipt belongs to this private payload,
                    # not the author's checkout. Keep real selection/generators.
                    api=SimpleNamespace(**helper(root))
                    monkeypatch.setattr(module._standard_catalog_module(),'thin_launcher',lambda:api)
                    code,result=await asyncio.to_thread(request,'/api/spawn',payload)
                    assert code==200 and result['ok'],result
                    child=backend.context_for(await backend.resolve(name='DashboardManaged'))
                    _,base,ready=family(child)
                    assert ready['managed']['selection']['backend_id']=='strata'
                    _,prompt=await asyncio.to_thread(launch_task,child)
                    assert ('Report to the parent Mail agent ID' in prompt) is not standalone
                    assert payload['task'] in prompt
                    argv=shlex.split(transport.calls[0][-1]);env=dict(os.environ)
                    env.pop('TMUX',None);env.pop('TMUX_PANE',None)
                    done=await asyncio.to_thread(subprocess.run,argv,env=env,cwd=tmp_path,
                        stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=60)
                    assert done.returncode==0,done.stderr
                    trace=json.loads((tmp_path/'native-result.json').read_text())
                    assert trace['native_hook'] and trace['key_read_denied'] and trace['unreserved_denied']
                    assert trace['status']['mail_agent_id']==result['mail_agent_id']
                    _,finished=await asyncio.to_thread(request,'/api/spawn-status?name=DashboardManaged')
                    assert finished['state']=='ready' and finished['result']['native_ready'] and finished['detail']=='ended'
                    assert len(transport.calls)==1
            finally:cleanup()
    asyncio.run(run())


def test_handler_local_route_pins_and_actual_http_prelaunch_once(tmp_path,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from agentstack_mail import fresh_harness as managed
    calls=[]
    class Models(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            assert self.headers['Authorization']=='Bearer synthetic-key'
            self.send_response(200);self.end_headers()
            self.wfile.write(b'{"data":[{"id":"local-model"}]}')
        def log_message(self,*args):pass
    models=ThreadingHTTPServer(('127.0.0.1',0),Models)
    worker=threading.Thread(target=models.serve_forever,daemon=True);worker.start()
    async def run():
        async with live(tmp_path) as (_,_,parent,_):
            root,manifest,_,cleanup=managed_fixture('dsh',tmp_path,parent.context['mail_agent_id'])
            try:
                url='http://127.0.0.1:'+str(models.server_port)
                value=json.loads(manifest.read_text());value['backends']['strata']={
                    'model_source':'server','urls':['https://unused.invalid/v1',url]}
                manifest.write_text(json.dumps(value))
                api=managed.helper(root)
                monkeypatch.setattr(api['native_server_models'],'read_key',lambda *args:'synthetic-key')
                monkeypatch.setattr(managed,'helper',lambda _:api)
                key=api['native_server_models'].url_key(url)
                monkeypatch.setenv('AGENTSTACK_MANAGED_SPAWN_CONFIG',str(manifest))
                transport=Transport(tmp_path);backend=Backend(parent.path,root,transport=lambda:transport)
                payload=dict(standalone=True,name='DashboardLocal',dir=str(tmp_path),task='local task',
                    provider='strata',harness='dsh',model='local-model',url_index=1,url_key=key)
                with dashboard(parent,backend,monkeypatch,tmp_path) as (module,request):
                    monkeypatch.setattr(module._standard_catalog_module(),'thin_launcher',lambda:SimpleNamespace(**api))
                    for changes in ({'url_key':'0'*64},{'harness':'hermes'}):
                        code,error=await asyncio.to_thread(request,'/api/spawn',{**payload,**changes})
                        assert code==409 and not error['ok']
                    code,result=await asyncio.to_thread(request,'/api/spawn',payload)
                    assert code==200 and result['ok'],result
                    child=backend.context_for(await backend.resolve(name='DashboardLocal'))
                    _,_,ready=family(child)
                    selected=ready['managed']['selection']
                    assert (selected['url_index'],selected['url_key'],selected['backend_id'])==(1,key,'strata')
                    assert calls==[]
                    _,_,settings=managed.setup(child,root,'dsh')
                    run_dir=tmp_path/'local-plan';run_dir.mkdir(mode=0o700)
                    native=await asyncio.to_thread(managed.plan,child,root,'dsh',settings,run_dir,tmp_path,'local task',None)
                    assert calls==['/v1/models']
                    overlay=json.loads((Path(native['config_dir'])/'native.patch.json').read_text())
                    route=next(item['config'] for item in overlay if item.get('id')=='llm-deepseek')
                    assert route['baseURL']==url and route['models'][0]['id']=='local-model'
                    assert 'synthetic-key' not in json.dumps(native)
            finally:cleanup()
    try:asyncio.run(run())
    finally:models.shutdown();models.server_close();worker.join(timeout=5)


def test_public_status_drives_real_new_agent_watch_and_preserves_unconfirmed(tmp_path,monkeypatch):
    from test_fresh_notifications import running
    from agentstack_mail.fresh_launch import record_session
    from agentstack_mail.fresh_child import provider_home
    async def run():
        async with live(tmp_path) as (_,_,parent,_):
            _,binary=binary_for_child(parent,'claude',tmp_path)
            monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(binary))
            transport=Transport(tmp_path);backend=Backend(parent.path,ROOT,transport=lambda:transport)
            payload=dict(standalone=True,name='DashboardVerdict',task='verdict test',dir=str(tmp_path),
                         provider='claude',model='claude-sonnet-4-6')
            statuses=[]
            with dashboard(parent,backend,monkeypatch,tmp_path) as (_,request):
                code,result=await asyncio.to_thread(request,'/api/spawn',payload)
                assert code==200 and result['ok']
                async def observe():
                    code,value=await asyncio.to_thread(request,'/api/spawn-status?name=DashboardVerdict')
                    assert code==200
                    statuses.append(value)
                    return value
                unknown=await observe()
                assert unknown['state']=='launching' and unknown['result'] is None and unknown['detail']=='unknown'
                child=backend.context_for(await backend.resolve(name='DashboardVerdict'))
                _,base,_=family(child)
                # Synthetic native lifecycle only; real status/owner/HTTP and
                # actual UI callback below. No provider or OS process starts.
                with monkeypatch.context() as event_env:
                    event_env.setenv('AGENTSTACK_FRESH_CLIENT_CONTEXT',str(child.path))
                    event=running(child,tmp_path,event_env)
                    # Children use the prepared root's shared provider HOME.
                    # Put the synthetic native transcript there, as a real
                    # callback would, retaining the production path check.
                    native=provider_home(child,'claude')/(event['session_id']+'.jsonl')
                    native.write_text('synthetic native lifecycle\n')
                    event['transcript_path']=str(native)
                    await record_session(child,event)
                    launch_id=os.environ['AGENTSTACK_FRESH_LAUNCH_ID']
                    run_dir=child.path.parent/'launch/runs'/launch_id
                path=run_dir/'intent.json';intent=json.loads(path.read_text())
                intent['child_spawn_id']=json.loads((base/'task.json').read_text())['spawn_id']
                path.write_text(json.dumps(intent))
                started=await observe()
                assert started['state']=='ready' and started['detail']=='started' and started['result']['native_ready']
                (run_dir/'ended.json').write_text('{}')
                ended=await observe()
                assert ended['state']=='ready' and ended['detail']=='ended' and ended['result']['native_ready']
                (run_dir/'session.json').unlink()
                unconfirmed=await observe()
                assert unconfirmed['state']=='failed' and not unconfirmed['result']['native_ready']
                assert unconfirmed['error']=='FRESH_NATIVE_STARTUP_UNCONFIRMED'
                assert len(transport.calls)==1
            text=(ROOT/'dashboard/index.html').read_text()
            start=text.index('async function watchSpawnLaunch(')
            function=text[start:text.index('\nfunction renderSpawnDirs(',start)]
            script="""
const vm=require('node:vm');const fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{const results=[];for(const row of input.statuses){
 let now=0;const events={state:row.state,detail:row.detail,cleared:0,toasts:[]};
 const ctx={SPAWN_WATCH_DEFAULT_MS:140000,Date:{now:()=>now+=200},
   setTimeout:fn=>{fn();return 0;},fetch:async()=>({json:async()=>row}),
   clearSpawnTaskDraft:()=>events.cleared++,toast:(...args)=>events.toasts.push(args),
   tick:()=>{},netTick:()=>{},view:'deck',encodeURIComponent};
 vm.createContext(ctx);vm.runInContext(input.function,ctx);
 await ctx.watchSpawnLaunch('DashboardVerdict','/fixture',1000);results.push(events);
}console.log(JSON.stringify(results));})().catch(()=>process.exit(1));
"""
            node=shutil.which('node')
            assert node,'existing Node runtime required'
            done=await asyncio.to_thread(subprocess.run,[node,'-e',script],
                input=json.dumps(dict(function=function,statuses=statuses)),text=True,capture_output=True,timeout=10)
            assert done.returncode==0,done.stderr
            events=json.loads(done.stdout)
            assert [e['cleared'] for e in events]==[0,1,1,0]
            assert all(e['toasts'][0][0]=='▸ SPAWNED' for e in events[1:3])
            assert events[3]['toasts'][0][0]=='✕ SPAWN FAILED'
            assert all('SPAWNED' not in t[0] for t in events[0]['toasts'])
            (tmp_path/'api1-verdict-proof.json').write_text(json.dumps(dict(
                actual_handler_status=True,actual_watchSpawnLaunch=True,synthetic_native_lifecycle=True,
                actual_node=node,native_processes=0,tmux_processes=0,cases=events)))
    asyncio.run(run())
