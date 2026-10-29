"""Prepared synthetic native runtimes, real generators/hooks/ORM/proxy; no API keys."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
from unittest.mock import patch

import pytest

from agentstack_mail.fresh_child import family, handoff, status
from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_runtime import _read_private
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_guard import live
from test_fresh_proxy import environment
import test_dsh_thin_config as dsh_fixture
import test_fresh_child as common_child
from test_managed_harness_install import CAFixture, fixture_stamp

ROOT=Path(__file__).resolve().parents[3]


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value));path.chmod(0o600)


NATIVE=r'''
import importlib.util,json,os,pathlib,subprocess,sys,types,uuid
provider=PROVIDER
root=pathlib.Path(os.environ['AGENTSTACK_HOME'])
session=str(uuid.uuid4())
if provider=='dsh':
    rows=json.loads(pathlib.Path(sys.argv[sys.argv.index('--patch')+1]).read_text())
    inserted=[item for row in rows for item in row.get('insert',[])]
    config=next(item['config'] for item in inserted if item['id']=='orrery-tools')
    proxy=next(item['config'] for item in inserted if item['id']=='orrery-mcp-orrery-mail')
    code=r"""
import {pathToFileURL} from 'node:url';
const [plugin,configText,session]=process.argv.slice(1);
const mod=await import(pathToFileURL(plugin));const config=JSON.parse(configText);
const events={};const ctx={on:(name,fn)=>events[name]=fn,workingDirectory:{ensure:async()=>config.cwd}};
mod.apply(ctx,config);
const agent={session:{id:session,header:{}},options:{cwd:config.cwd}};
await events['agent/created']({agent});
const good={agent,name:'write',arguments:{path:'note.py'}};
let ran=false;await events['tools/pre-execute'](good,async()=>{ran=true});
if(!ran)throw Error('reserved write refused');
const absent={agent,name:'write',arguments:{path:'unreserved.py'}};
const denial=await events['tools/pre-execute'](absent,async()=>{throw Error('unreserved write allowed')});
if(denial.kind!=='deny')throw Error('missing denial');
const sensitive={agent,name:'read',arguments:{path:JSON.parse(process.env.AGENTSTACK_LLM_KEY_FILES)[0]}};
const keyDenied=await events['tools/pre-execute'](sensitive,async()=>{throw Error('key read allowed')});
if(keyDenied.kind!=='deny')throw Error('missing key denial');
await events['tools/result'](good,{isError:false});
console.log(JSON.stringify({native_hook:true,reserved:true,unreserved_denied:true}));
"""
    checks=subprocess.run([NODE,'--input-type=module','-e',code,str(root/'integrations/dsh/orrery_tools.mjs'),json.dumps(config),session],
        capture_output=True,text=True,timeout=60)
else:
    config=json.loads((pathlib.Path(os.environ['HERMES_MANAGED_DIR'])/'config.yaml').read_text())
    proxy=config['mcp_servers']['orrery-mail']
    agent=types.ModuleType('agent');agent.__path__=[];sys.modules['agent']=agent
    delegated=types.ModuleType('agent.delegation_context');delegated.is_delegated_child_context=lambda:False
    sys.modules[delegated.__name__]=delegated
    tools=types.ModuleType('tools');tools.__path__=[];sys.modules['tools']=tools
    paths=types.ModuleType('tools.file_tools_paths');paths._resolve_base_dir=lambda _:os.getcwd()
    sys.modules[paths.__name__]=paths
    spec=importlib.util.spec_from_file_location('native_guard',root/'integrations/hermes/thin_guard.py')
    guard=importlib.util.module_from_spec(spec);sys.modules[spec.name]=guard;spec.loader.exec_module(guard)
    guard.session_started(session)
    assert guard.before('write_file',{'path':'note.py'},session_id=session) is None
    assert guard.before('write_file',{'path':'unreserved.py'},session_id=session)['action']=='block'
    assert guard.before('read_file',{'path':json.loads(os.environ['AGENTSTACK_LLM_KEY_FILES'])[0]},session_id=session)['action']=='block'
    guard.after('write_file',{},args={'path':'note.py'},status='ok',session_id=session)
    checks=subprocess.CompletedProcess([],0,'','')
assert checks.returncode==0,checks.stderr
event=dict(tool_name='Bash',tool_input={},cwd=os.getcwd(),session_id=session)
for changes in ({'AGENTSTACK_FRESH_LAUNCH_NONCE':'wrong'}, {'AGENTSTACK_FRESH_CLIENT_CONTEXT':''},
                {'AGENTSTACK_CLIENT_CONFIG':'/fixture/mixed-old-context'}):
    refused=subprocess.run(['/bin/bash',str(root/'hooks/check-agent-registered.sh')],
        input=json.dumps(event),env=dict(os.environ,**changes),capture_output=True,text=True,timeout=15)
    assert refused.returncode!=0,'invalid native caller was accepted'
wrong_session=subprocess.run(['/bin/bash',str(root/'hooks/check-agent-registered.sh')],
    input=json.dumps({**event,'session_id':'other-native-session'}),capture_output=True,text=True,timeout=15)
assert wrong_session.returncode!=0
requests=[dict(jsonrpc='2.0',id=1,method='initialize'),
    dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='runtime_status',arguments={})),
    dict(jsonrpc='2.0',id=3,method='tools/call',params=dict(name='send_message',arguments=dict(
        recipients=[{'target_agent_id':PARENT}],subject='managed-result',body_md='task done')))]
result=subprocess.run([proxy['command'],*proxy.get('args',[])],env=dict(os.environ,**proxy.get('env',{})),
    input=''.join(json.dumps(row)+'\n' for row in requests),capture_output=True,text=True,timeout=30)
assert result.returncode==0,result.stderr
replies=[json.loads(line) for line in result.stdout.splitlines()]
assert all(not row.get('error') and not row.get('result',{}).get('isError') for row in replies)
status=json.loads(replies[-2]['result']['content'][0]['text'])
pathlib.Path(TRACE).write_text(json.dumps(dict(provider=provider,native_hook=True,
    reserved=True,unreserved_denied=True,key_read_denied=True,invalid_callers_denied=True,
    status=status,context=os.environ['AGENTSTACK_FRESH_CLIENT_CONTEXT'])))
'''


def product_copy(target):
    for name in ('bin','hooks','templates','codex'):
        shutil.copytree(ROOT/name,target/name,dirs_exist_ok=True)
    for name in ('dsh','hermes'):
        shutil.copytree(ROOT/'integrations'/name,target/'integrations'/name,dirs_exist_ok=True)
    (target/'dashboard').mkdir(exist_ok=True)
    for name in ('harness_activity.py','harness_context.py'):
        shutil.copy2(ROOT/'dashboard'/name,target/'dashboard'/name)


def fixture(program,tmp,parent_id):
    node=shutil.which('node')
    if program=='dsh':assert node,'existing node required for the actual dsh plugin'
    native='#!'+sys.executable+'\n'+NATIVE.replace('provider=PROVIDER','provider='+repr(program)).replace('NODE',repr(node)).replace('PARENT',str(parent_id)).replace('TRACE',repr(str(tmp/'native-result.json')))
    if program=='dsh':
        # macOS TMPDIR may use /var, a symlink to /private/var. Fresh
        # selection requires a canonical manifest and all receipts must agree.
        case=dsh_fixture.ThinTests()
        with patch.object(dsh_fixture.tempfile,'tempdir',str(tmp.resolve())):
            case.setUp()
        product_copy(case.product)
        case.node.write_text(native);case.node.chmod(0o700)
        save(case.runtime.parent/'evidence/node-runtime.json',dict(version='v24.3.0',execPath=str(case.node),sha256=hashlib.sha256(case.node.read_bytes()).hexdigest()))
        manifest=json.loads(case.manifest.read_text());manifest['harnesses']['dsh']['python']=sys.executable
        save(case.manifest,manifest)
        return case.product,case.manifest,'a',case.doCleanups
    with CAFixture(None,tmp/'ca') as ca:
        product=ca.repo
        # Keep the synthetic CA policy; copy the other unchanged product files.
        for name in ('bin','hooks','templates','codex'):
            shutil.copytree(ROOT/name,product/name,dirs_exist_ok=True)
        (product/'dashboard').mkdir(exist_ok=True)
        for name in ('harness_activity.py','harness_context.py'):
            shutil.copy2(ROOT/'dashboard'/name,product/'dashboard'/name)
        managed=tmp/'managed';source=managed/'source';ca.source(source);ca.ca.apply(product,source)
        (source/'hermes_cli').mkdir()
        (source/'hermes_cli/auth.py').write_text("_REGISTRY_ROWS: tuple = [('deepseek',)]\n")
        (source/'hermes_cli/config_defaults.py').write_text('DEFAULT_CONFIG = {"auxiliary": {}}\n')
        key=hashlib.sha256(str(source).encode()).hexdigest()[:16]
        generation=managed/'installs'/key/'environments/fixture'
        (generation/'bin').mkdir(parents=True)
        for name in ('hermes','python'):
            (generation/'bin'/name).write_text(native if name=='hermes' else '#!/bin/sh\nexit 0\n')
            (generation/'bin'/name).chmod(0o700)
        (generation/'pyvenv.cfg').write_text('fixture')
        paths=ca.material(managed,source,generation)
        stamp=fixture_stamp(source)
        details=dict(hermes_executable=str(generation/'bin/hermes'),hermes_source=str(source),managed_root=str(managed),
            ca_recipe=ca.ca.load(product)[2],ca_evidence=ca.ca.runtime_evidence(product,managed,source,generation,ca.target,paths),**stamp)
        receipt=tmp/'owner.json';save(receipt,dict(harness='hermes',variant='plain',thin_config_contract=1,complete=True,details=details))
        template=tmp/'template.json';save(template,{**details,'generation_receipt':str(receipt),
            'hermes_python':str(generation/'bin/python'),'profiles':{'strata':dict(provider='fixture-api',model='a',base_url='https://fixture.invalid/v1',api_mode='chat_completions',key_cmd='/fixture/no-key-call')}})
        manifest=tmp/'manifest.json';save(manifest,dict(enabled=True,backends={'strata':{'model':'a','models':['a']}},
            harnesses={'hermes':{'config_file':str(template),'python':sys.executable}}))
        return product,manifest,'a',lambda:None


async def entry(parent,root,script,*args):
    return await asyncio.to_thread(subprocess.run,['/bin/bash',str(root/script),*map(str,args)],cwd=root,
        env=environment(parent,AGENTSTACK_RUNTIME_MODE='fresh',AGENTSTACK_PYTHON=sys.executable),
        text=True,capture_output=True,timeout=60)


@pytest.mark.parametrize('program',['dsh','hermes'])
def test_managed_two_entries_native_hooks_proxy_guard_and_parent_result(tmp_path,program):
    async def run():
        async with live(tmp_path) as (runtime,_,parent,_):
            parent_context=parent.path.read_bytes()
            root,manifest,model,cleanup=fixture(program,tmp_path,parent.context['mail_agent_id'])
            try:
                prereg=('--child-client-name','worker','--name','ManagedChild','--program',program,'--model',model,
                    '--task-description','bounded managed task','--manifest',manifest,'--backend','strata','--prepare-only')
                result=await entry(parent,root,'bin/agentstack-preregister-child',*prereg)
                assert result.returncode==0,result.stdout+result.stderr
                child=FreshBoundClient(json.loads(result.stdout)['context_path'])
                again=await entry(parent,root,'bin/agentstack-preregister-child',*prereg)
                assert again.returncode==0 and json.loads(again.stdout)==json.loads(result.stdout)
                work=tmp_path/'outside';work.mkdir();task=tmp_path/'task.md';task.write_text('Literal `x` $(touch forbidden)')
                spawn=('--child-client-name','worker','--embed-task','--task-file',task,'--harness',program,'--backend','strata','--resources','note.py','--prepare-only',work)
                reserved=await entry(parent,root,'hooks/spawn_child.sh',*spawn)
                assert reserved.returncode==0,reserved.stderr
                class Transport:
                    identity=(1,2);path=tmp_path/'not-a-socket'
                    def command(self,*args):
                        self.argv=args[-1];return subprocess.CompletedProcess([],0,'','')
                transport=Transport()
                await handoff(parent,root,slot='worker',cwd=work,task=task.read_text(),resources=['note.py'],transport=transport)
                invoked=await asyncio.to_thread(subprocess.run,['/bin/bash','-c',transport.argv],cwd=work,
                    env=environment(parent,AGENTSTACK_RUNTIME_MODE='fresh',AGENTSTACK_PYTHON=sys.executable),
                    text=True,capture_output=True,timeout=90)
                assert invoked.returncode==0,invoked.stdout+invoked.stderr
                _,base,_=family(child);observed=status(child,base)
                assert observed['execution']=='ended' and observed['native_ready']
                inbox=await parent.call('fetch_inbox',{})
                assert len(inbox)==1 and inbox[0]['subject']=='managed-result' and inbox[0]['sender_id']==child.context['mail_agent_id']
                proof=json.loads((tmp_path/'native-result.json').read_text())
                assert proof['context']==str(child.path) and proof['reserved'] and proof['unreserved_denied']
                assert proof['key_read_denied'] and proof['invalid_callers_denied']
                coverage=await child.call('guard_edit',{'paths':[str(work/'note.py')],'extend_seconds':1800})
                assert coverage['covered'] # Post-edit retained this owner's explicit-release/TTL lease.
                await child.call('release_reservations',{'paths':[str(work/'note.py')]})
                with pytest.raises(B1Error,match='RESERVATION_REQUIRED'):
                    await child.call('guard_edit',{'paths':[str(work/'note.py')],'extend_seconds':1800})
                assert not (work/'forbidden').exists()
                assert child.context['mail_instance_id']==parent.context['mail_instance_id'] and child.context['mail_agent_id']!=parent.context['mail_agent_id']
                assert parent.path.read_bytes()==parent_context
                # A changed manifest cannot re-route an already prepared child.
                original=manifest.read_bytes();manifest.write_bytes(original+b'\n')
                from agentstack_mail.fresh_launch import provider_setup
                with pytest.raises(B1Error,match='FRESH_HARNESS_SELECTION_CHANGED'):
                    provider_setup(child,root,program)
                manifest.write_bytes(original)
                (tmp_path/'managed-proof.json').write_text(json.dumps(dict(program=program,real_entries=True,
                    real_generators=True,synthetic_native=True,actual_plugins=True,current_guard=True,parent_result=True,
                    notifications_connected=False,actual_tmux=False)))
            finally:cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('program',['dsh','hermes'])
def test_fresh_local_selected_route_and_one_prelaunch_model_check(tmp_path,program,monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from agentstack_mail import fresh_harness as managed
    from agentstack_mail.fresh_runtime import _directory
    calls=[];payload={'data':[{'id':'local-model','meta':{'n_ctx':131072}}]}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            assert self.headers['Authorization']=='Bearer synthetic-key'
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(payload).encode())
        def log_message(self,*args):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
    async def run():
        async with live(tmp_path) as (_,_,parent,_):
            root,manifest,_,cleanup=fixture(program,tmp_path,parent.context['mail_agent_id'])
            try:
                url='http://127.0.0.1:'+str(http.server_port)
                data=json.loads(manifest.read_text())
                data['backends']['strata']={'model_source':'server','urls':['https://unused.invalid/v1',url]}
                save(manifest,data)
                api=managed.helper(root)
                monkeypatch.setattr(api['native_server_models'],'read_key',lambda *args:'synthetic-key')
                monkeypatch.setattr(managed,'helper',lambda _:api)
                if program=='hermes':
                    # HTTPS is an intentional existing Hermes boundary (#377).
                    bad_http=dict(manifest_file=str(manifest),backend_id='strata',url_index=1,
                                  url_key=api['native_server_models'].url_key(url))
                    with pytest.raises(B1Error):managed.selection(root,program,'local-model',bad_http)
                    url='https://local-fixture.invalid'
                    data['backends']['strata']['urls'][1]=url;save(manifest,data)
                    def lookup(selected,key):
                        assert selected==url and key=='synthetic-key'
                        calls.append(selected)
                        return api['native_server_models'].parse_models(payload)
                    monkeypatch.setattr(api['native_server_models'],'fetch_models',lookup)
                fingerprint=api['native_server_models'].url_key(url)
                args=('--child-client-name','local','--name','LocalChild','--program',program,'--model','local-model',
                    '--task-description','private Local check','--manifest',manifest,'--backend','strata',
                    '--url-index','1','--url-key',fingerprint,'--prepare-only')
                result=await entry(parent,root,'bin/agentstack-preregister-child',*args)
                assert result.returncode==0,result.stdout+result.stderr
                child=FreshBoundClient(json.loads(result.stdout)['context_path'])
                _,_,ready=family(child)
                selected=ready['managed']['selection']
                assert (selected['url_index'],selected['url_key'],selected['server_url'])==(1,fingerprint,url)
                assert calls==[]
                assert (await entry(parent,root,'bin/agentstack-preregister-child',*args)).returncode==0
                changed=list(args);changed[changed.index('local-model')]='another-model'
                refused=await entry(parent,root,'bin/agentstack-preregister-child',*changed)
                assert refused.returncode!=0 and 'FRESH_CHILD_INTENT_CONFLICT' in refused.stderr
                _,_,settings=managed.setup(child,root,program)
                work=tmp_path/'local-work';work.mkdir()
                async def plan(number):
                    folder=tmp_path/('plan-'+str(number));folder.mkdir(mode=0o700);_directory(folder)
                    return await asyncio.to_thread(managed.plan,child,root,program,settings,folder,work,'literal task',None)
                native=await plan(0)
                lookup_call=url if program=='hermes' else '/v1/models'
                assert calls==[lookup_call] and 'synthetic-key' not in json.dumps(native)
                if program=='dsh':
                    overlay=json.loads((Path(native['config_dir'])/'native.patch.json').read_text())
                    route=next(r['config'] for r in overlay if r.get('id')=='llm-deepseek')
                    assert route['baseURL']==url and route['models'][0]['id']=='local-model'
                    assert route['models'][0]['contextWindow']==131072
                else:
                    config=json.loads((Path(native['env']['HERMES_MANAGED_DIR'])/'config.yaml').read_text())
                    route=config['providers'][native['argv'][3]]
                    assert route['base_url']==url+'/v1' and config['model']['default']=='local-model'
                    assert route['context_length']==131072
                payload['data']=[{'id':'other'}]
                with pytest.raises(B1Error,match='FRESH_HARNESS_CONFIGURE_REQUIRED'):await plan(1)
                payload['data']=[{'id':'local-model','status':{'value':'unloaded'}}]
                with pytest.raises(B1Error,match='FRESH_HARNESS_CONFIGURE_REQUIRED'):await plan(2)
                # Missing/unknown status and absent context retain the public fallback contract.
                for number,row in enumerate(({'id':'local-model'}, {'id':'local-model','status':{'value':'future'}}),3):
                    payload['data']=[row];await plan(number)
                assert calls==[lookup_call]*5
                bad={**selected,'url_key':'0'*64}
                with pytest.raises(B1Error):managed.selection(root,program,'local-model',bad)
                data['backends']['strata']['urls'].reverse();save(manifest,data)
                with pytest.raises(B1Error):managed.selection(root,program,'local-model',selected)
                assert len(calls)==5
                (tmp_path/'local-model-proof.json').write_text(json.dumps(dict(program=program,
                    lookup='synthetic-HTTPS-inventory' if program=='hermes' else 'actual-private-HTTP',
                    selected_once_per_plan=True,missing_and_unloaded_refused=True,optional_status_context=True,
                    hermes_http_refused=program=='hermes',real_credentials=False)))
            finally:cleanup()
    try:asyncio.run(run())
    finally:http.shutdown();http.server_close();worker.join()


@pytest.mark.parametrize('provider',['claude','codex'])
def test_shared_fresh_registered_and_post_edit_hooks_keep_common_clients(tmp_path,provider,monkeypatch):
    """The newly shared shell branches must not strand a prepared existing client."""
    async def run():
        async with live(tmp_path) as (_,_,parent,_):
            _,binary=common_child.binary_for_child(parent,provider,tmp_path)
            marker='requests = ['
            extra="""
for common_hook in ('check-agent-registered.sh','release-file-reservation.sh'):
    decision=subprocess.run(['/bin/bash',str(pathlib.Path(os.environ['AGENTSTACK_HOME'])/'hooks'/common_hook)],
        input=json.dumps(dict(session_id=session,cwd=os.getcwd(),tool_name='Write',tool_input={'file_path':'note.py'},
                             tool_result={'success':True})),capture_output=True,text=True,timeout=15)
    assert decision.returncode==0,common_hook+': '+decision.stderr
"""
            assert marker in binary.read_text()
            binary.write_text(binary.read_text().replace(marker,extra+'\n'+marker,1))
            child,work,task,_=await common_child.prepared_task(parent,binary,provider,tmp_path)
            monkeypatch.setenv('AGENTSTACK_'+provider.upper()+'_BIN',str(binary))
            class Transport:
                identity=(1,2);path=tmp_path/'synthetic-socket'
                def command(self,*args):
                    self.argv=args[-1];return subprocess.CompletedProcess([],0,'','')
            transport=Transport()
            await handoff(parent,ROOT,slot='worker',cwd=work,task=task.read_text(),resources=['note.py'],transport=transport)
            result=await asyncio.to_thread(subprocess.run,['/bin/bash','-c',transport.argv],cwd=work,
                env=environment(parent,AGENTSTACK_RUNTIME_MODE='fresh',AGENTSTACK_PYTHON=sys.executable),
                capture_output=True,text=True,timeout=75)
            assert result.returncode==0,result.stdout+result.stderr
            _,base,_=family(child)
            assert status(child,base)['native_ready']
    asyncio.run(run())
