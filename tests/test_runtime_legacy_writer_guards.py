"""Ordinary legacy writers must not consume a reserved preview namespace.

Real helpers, private fake owner/token, fake curl only. No native provider,
Keychain, real Mail or live install. A reserved field with null is NOT absent.
"""
import importlib
import fcntl
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hooks'))
sys.path.insert(0,str(ROOT/'integrations/codex_app/src'))
NAME='FixtureCurie'
TOKEN='fixture-owner-token-0000000000000000000000'
MINTED='fixture-minted-token-000000000000000000000'
PROJECT='/fixture/project'


@pytest.fixture
def prepared(tmp_path):
    runtime=tmp_path/'runtime';root=runtime/'child-agents';root.mkdir(parents=True,mode=0o700)
    runtime.chmod(0o700);root.chmod(0o700)
    state=root/(NAME+'.json');token=runtime/('agent_token_'+NAME)
    value={'schema_version':1,'launch_origin':'child','provider':'codex','agent_id':17,'agent_name':NAME,
        'project_key':PROJECT,'program':'codex','codex_mcp_profile':'orrery-only','registration_token':TOKEN,
        'retired_at':'2026-09-21T00:00:00Z','resume_expires_at':'2099-01-01T00:00:00Z'}
    state.write_text(json.dumps(value));state.chmod(0o600);token.write_text(TOKEN);token.chmod(0o600)
    config=root/(NAME+'.mcp.json');config.write_text('{}');config.chmod(0o600)
    home=root/(NAME+'.codex-home');home.mkdir(mode=0o700);(home/'keep').write_text('generated fixture')
    for suffix in ('resume.lock','tools-change.lock'):
        lock=root/('.'+NAME+'.'+suffix);lock.write_text('same inode');lock.chmod(0o400)
    managed=runtime/'managed_agents.txt';managed.write_text(NAME+'\nOtherOwner\n')
    source=tmp_path/'handoff';source.write_text(TOKEN);source.chmod(0o600)
    binding=tmp_path/'handoff.binding.json'
    binding.write_text(json.dumps({k:value[k] for k in ('agent_id','agent_name','project_key','program')}));binding.chmod(0o600)
    tools=tmp_path/'tools';tools.mkdir();log=tmp_path/'unexpected-effects'
    curl=tools/'curl'
    curl.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
        'p=json.load(sys.stdin);tool=p.get("params",{}).get("name","")\n'
        'with open(os.environ["GUARD_EFFECT_LOG"],"a") as f:f.write(tool+"\\n")\n'
        'value={"id":7,"human_key":"/fixture/project"} if tool=="ensure_project" else '
        '{"id":17,"name":"FixtureCurie","project_id":7,"program":"codex","model":"fixture-model","registration_token":'+repr(MINTED)+'}\n'
        'data=json.dumps({"result":{"structuredContent":value}})\n'
        'a=sys.argv[1:]\n'
        'if "--output" in a:\n with open(a[a.index("--output")+1],"w") as f:f.write(data)\n sys.stdout.write("200")\n'
        'else:sys.stdout.write(data)\n');curl.chmod(0o700)
    for directory in ('home','tmp'):(tmp_path/directory).mkdir(mode=0o700)
    env={'HOME':str(tmp_path/'home'),'PATH':str(tools)+':'+str(Path(sys.executable).parent)+':/usr/bin:/bin',
        'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.preview-writer-guards',
        'TMPDIR':str(tmp_path/'tmp'),'AGENTSTACK_RUNTIME_MODE':'legacy','AGENTSTACK_HOME':str(tmp_path/'missing-install'),
        'AGENTSTACK_ENV_FILE':str(tmp_path/'missing-env'),'AGENTSTACK_RUNTIME_DIR':str(runtime),
        'AGENTSTACK_HOOKS_DIR':str(ROOT/'hooks'),'AGENTSTACK_REGISTER_LIB':str(ROOT/'bin/lib/agentstack-register.sh'),
        'AGENTSTACK_PROJECT_KEY':PROJECT,'PROJECT_KEY':PROJECT,'AGENTSTACK_MCP_URL':'http://fixture.invalid/mcp',
        'AGENTSTACK_MAIL_HTTP_BEARER_MODE':'disabled','AGENTSTACK_CONTACT_POLICY':'off',
        'AGENTSTACK_PYTHON':sys.executable,'AGENT_NAME':NAME,'PARENT_AGENT':'FixtureParent',
        'CHILD_REGISTRATION_TOKEN':TOKEN,'GUARD_EFFECT_LOG':str(log),'TARGET_TOKEN':MINTED}
    return runtime,state,source,binding,env,log


def snapshot(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('field',['launch_intent','local_registration','local_execution','local_resolution'])
@pytest.mark.parametrize('entry',['prepare','stage','retire','retire-zero','purge','discard','resume','tools-lock',
    'token-read','token-store','register','reregister','cleanup','proxy'])
def test_reserved_field_is_not_silently_reinterpreted_as_an_ordinary_child(prepared,field,entry):
    runtime,state,source,binding,env,log=prepared
    value=json.loads(state.read_text());value[field]=None;state.write_text(json.dumps(value));state.chmod(0o600)
    before=snapshot(runtime)
    if entry in ('token-read','token-store','register'):
        commands={'token-read':'ags_load_registration_token "$AGENT_NAME"',
            'token-store':'ags_store_registration_token "$AGENT_NAME" "$TARGET_TOKEN"',
            'register':'ags_register_session "$PROJECT_KEY" codex fixture-model cx "$PWD" "$AGENT_NAME" reserved'}
        reply=subprocess.run(['/bin/bash','-c','. "$1"; '+commands[entry],
            'guard-test',str(ROOT/'bin/lib/agentstack-register.sh')],env=env,capture_output=True,text=True,timeout=15)
        assert reply.returncode!=0 and TOKEN not in reply.stdout+reply.stderr
    elif entry in ('reregister','cleanup'):
        args=([str(ROOT/'bin/agentstack-reregister'),NAME,'codex','fixture-model'] if entry=='reregister'
            else [str(ROOT/'hooks/cleanup-child-agent.sh'),NAME])
        reply=subprocess.run(['/bin/bash',*args],env=env,capture_output=True,text=True,timeout=15)
        assert reply.returncode!=0 and TOKEN not in reply.stdout+reply.stderr
    elif entry=='proxy':
        from agentstack_codex_app.mcp_server import ProxyConfig,ProxyError,load_direct_owner_token
        with pytest.raises(ProxyError):
            load_direct_owner_token(ProxyConfig(runtime_dir=runtime/'codex-app',endpoint=env['AGENTSTACK_MCP_URL'],
                agent_name=NAME,project_key=PROJECT))
    else:
        module=importlib.import_module('child_resume')
        with pytest.raises(module.ResumeStateError,match='local preview'):
            if entry=='prepare':module.prepare_active_state(runtime,NAME,project_key=PROJECT,mcp_profile='orrery-only',program='codex')
            elif entry=='stage':module.stage_registration(runtime,NAME,project_key=PROJECT,program='codex',
                generation='a'*32,source=source,binding=binding)
            elif entry=='retire':module.mark_retired(runtime,NAME,retention_days=30)
            elif entry=='retire-zero':module.mark_retired(runtime,NAME,retention_days=0)
            elif entry=='purge':module.purge_one(runtime,NAME,reason='purged')
            elif entry=='discard':module.discard_generated(runtime,NAME)
            elif entry=='resume':module.begin_resume(runtime,NAME,agent_id=17,project_key=PROJECT,program='codex')
            elif entry=='tools-lock':
                with module._AgentLock(state.with_name('.'+NAME+'.tools-change.lock'),exclusive=True):pass
    assert not log.exists(), 'ordinary path made an external tool call before the local refusal'
    assert snapshot(runtime)==before


def test_old_finish_cannot_consume_new_pending_kind_even_if_it_resembles_an_old_record(prepared):
    runtime,state,source,binding,env,log=prepared
    pending=state.with_name('.'+NAME+'.registration-pending.json')
    pending.write_text(json.dumps({'kind':'orrery-legacy-preview-registration-v1','generation':'a'*32,'agent_id':17}));pending.chmod(0o600)
    before=snapshot(runtime);module=importlib.import_module('child_resume')
    with pytest.raises(module.ResumeStateError,match='local preview'):
        module.finish_registration(runtime,NAME,generation='a'*32,rollback=False)
    assert snapshot(runtime)==before


def test_ordinary_tools_lock_control_is_not_already_refusing_the_fixture(prepared):
    runtime,state,source,binding,env,log=prepared;module=importlib.import_module('child_resume')
    with module._AgentLock(state.with_name('.'+NAME+'.tools-change.lock'),exclusive=True):pass


@pytest.mark.parametrize('field',['launch_intent','local_registration','local_execution','local_resolution','pending'])
def test_preregister_rechecks_namespace_after_remote_response_before_publication(prepared,field):
    from test_preregister_child_naming import _FAKE_LIB
    runtime,state,source,binding,env,log=prepared
    before=snapshot(runtime);value=json.loads(state.read_text())
    if field=='pending':
        changed=state.with_name('.'+NAME+'.registration-pending.json')
        value={'kind':'orrery-legacy-preview-registration-v1','sentinel':'must survive'}
    else:changed=state;value[field]=None
    lib=runtime.parent/'fake-register.sh'
    lib.write_text(_FAKE_LIB+'''
original_mcp_call=$(declare -f ags_mcp_call)
eval "${original_mcp_call/ags_mcp_call/standin_mcp_call}"
ags_mcp_call() {
  if [[ "$1" == register_agent ]]; then
    "$AGENTSTACK_PYTHON" -c 'import os,pathlib; p=pathlib.Path(os.environ["GUARD_RACE_PATH"]);p.write_text(os.environ["GUARD_RACE_VALUE"]);p.chmod(0o600)'
  fi
  standin_mcp_call "$@"
}
''')
    env={**env,'AGENTSTACK_REGISTER_LIB':str(lib),'GUARD_RACE_PATH':str(changed),
        'GUARD_RACE_VALUE':json.dumps(value)}
    reply=subprocess.run(['/bin/bash',str(ROOT/'bin/agentstack-preregister-child'),
        '--name',NAME,'--program','codex','--model','fixture-model',
        '--token-file-out',str(runtime.parent/'new-handoff')],env=env,capture_output=True,text=True,timeout=15)
    assert reply.returncode!=0 and not reply.stdout
    assert changed.read_bytes()==json.dumps(value).encode()
    expected={**before,str(changed.relative_to(runtime)):json.dumps(value).encode()}
    assert snapshot(runtime)==expected
    assert TOKEN not in reply.stdout+reply.stderr


@pytest.mark.parametrize('field',['launch_intent','local_registration','local_execution','local_resolution','pending'])
def test_cleanup_rechecks_namespace_after_remote_retire_before_local_deletion(prepared,field):
    runtime,state,source,binding,env,log=prepared
    before=snapshot(runtime);value=json.loads(state.read_text())
    if field=='pending':
        changed=state.with_name('.'+NAME+'.registration-pending.json')
        value={'kind':'orrery-legacy-preview-registration-v1','sentinel':'must survive'}
    else:changed=state;value[field]=None
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            tool=request['params']['name'];calls.append(tool)
            if tool=='retire_agent':changed.write_text(json.dumps(value));changed.chmod(0o600)
            data=b'{"result":{"structuredContent":{"ok":true}}}'
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=HTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=False);worker.start()
    env={**env,'AGENTSTACK_MANAGED_AGENTS_FILE':str(runtime/'managed_agents.txt'),
        'AGENTSTACK_MCP_URL':'http://127.0.0.1:'+str(server.server_port)+'/mcp'}
    try:
        reply=subprocess.run(['/bin/bash',str(ROOT/'hooks/cleanup-child-agent.sh'),NAME],
            env=env,capture_output=True,text=True,timeout=15)
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)
        assert not worker.is_alive()
    assert reply.returncode!=0
    assert calls==['release_file_reservations','retire_agent']
    expected={**before,str(changed.relative_to(runtime)):json.dumps(value).encode()}
    assert snapshot(runtime)==expected
    assert TOKEN not in reply.stdout+reply.stderr


@pytest.mark.parametrize('field',['launch_intent','local_registration','local_execution','local_resolution','pending'])
def test_register_cannot_report_success_when_post_response_token_publication_is_refused(prepared,field):
    runtime,state,source,binding,env,log=prepared
    before=snapshot(runtime);value=json.loads(state.read_text())
    if field=='pending':
        changed=state.with_name('.'+NAME+'.registration-pending.json')
        value={'kind':'orrery-legacy-preview-registration-v1','sentinel':'must survive'}
    else:changed=state;value[field]=None
    curl=Path(env['PATH'].split(':')[0])/'curl'
    raw=curl.read_text().replace('value={',
        'if tool=="register_agent":\n import pathlib\n p=pathlib.Path(os.environ["GUARD_RACE_PATH"])\n'
        ' p.write_text(os.environ["GUARD_RACE_VALUE"]);p.chmod(0o600)\nvalue={',1)
    curl.write_text(raw);curl.chmod(0o700)
    env={**env,'GUARD_RACE_PATH':str(changed),'GUARD_RACE_VALUE':json.dumps(value)}
    reply=subprocess.run(['/bin/bash','-c',
        '. "$1"; ags_register_session "$PROJECT_KEY" codex fixture-model cx "$PWD" "$AGENT_NAME" reserved',
        'guard-test',str(ROOT/'bin/lib/agentstack-register.sh')],env=env,capture_output=True,text=True,timeout=15)
    assert log.read_text().splitlines()==['ensure_project','register_agent']
    assert reply.returncode!=0 and not reply.stdout
    assert snapshot(runtime)=={**before,str(changed.relative_to(runtime)):json.dumps(value).encode()}
    assert TOKEN not in reply.stderr and MINTED not in reply.stderr


def test_ordinary_token_publication_rechecks_after_acquiring_the_existing_name_lock(prepared,monkeypatch):
    runtime,state,source,binding,env,log=prepared;module=importlib.import_module('child_resume')
    assert callable(getattr(module,'store_ordinary_registration_token',None)), 'LOCKED_TOKEN_PUBLICATION_MISSING'
    lock=state.with_name('.'+NAME+'.resume.lock');inode=lock.stat().st_ino
    held=os.open(lock,os.O_RDONLY);original=fcntl.flock;ready=threading.Event();errors=[]
    original(held,fcntl.LOCK_EX)
    def observed(fd,operation):
        if operation==fcntl.LOCK_EX:ready.set()
        return original(fd,operation)
    monkeypatch.setattr(fcntl,'flock',observed)
    def publish():
        try:module.store_ordinary_registration_token(runtime,NAME,MINTED.encode())
        except Exception as exc:errors.append(exc)
    worker=threading.Thread(target=publish,daemon=False);worker.start()
    try:
        assert ready.wait(timeout=5), 'token writer did not reach the shared lock'
        value=json.loads(state.read_text());value['local_execution']=None
        state.write_text(json.dumps(value));before=snapshot(runtime)
    finally:
        original(held,fcntl.LOCK_UN);os.close(held);worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors)==1 and isinstance(errors[0],module.ResumeStateError)
    assert 'local preview' in str(errors[0])
    assert snapshot(runtime)==before and lock.stat().st_ino==inode


def reserve_during_dashboard(prepared,field):
    runtime,state,source,binding,env,log=prepared
    if field=='pending':
        path=state.with_name('.'+NAME+'.registration-pending.json')
        value={'kind':'orrery-legacy-preview-registration-v1','sentinel':'must survive'}
    else:
        path=state;value=json.loads(state.read_text());value[field]=None
    path.write_text(json.dumps(value));path.chmod(0o600)
    return path,path.read_bytes()


@pytest.mark.parametrize('field',['launch_intent','local_registration','local_execution','local_resolution','pending'])
def test_dashboard_recovery_cannot_promote_into_a_reserved_namespace(prepared,monkeypatch,field):
    from dashboard import server
    runtime,state,source,binding,env,log=prepared
    token=runtime/('agent_token_'+NAME);token.write_text('')
    reserve_during_dashboard(prepared,field);before=snapshot(runtime)
    monkeypatch.setattr(server,'RUNTIME_DIR',str(runtime))
    assert server._promote_spawn_credential(str(token),MINTED,'empty') is False
    assert snapshot(runtime)==before


def test_dashboard_ordinary_recovery_control_can_still_promote_its_handoff(prepared,monkeypatch):
    from dashboard import server
    runtime,state,source,binding,env,log=prepared
    state.unlink();token=runtime/('agent_token_'+NAME);token.write_text('')
    monkeypatch.setattr(server,'RUNTIME_DIR',str(runtime))
    assert server._promote_spawn_credential(str(token),MINTED,'empty') is True
    assert token.read_text()==MINTED and not state.exists()


@pytest.mark.parametrize('field',['launch_intent','local_registration','local_execution','local_resolution','pending'])
@pytest.mark.parametrize('failure_mode',['exit','timeout'])
def test_dashboard_failed_launcher_preserves_namespace_created_after_registration(prepared,monkeypatch,field,failure_mode):
    from dashboard import server
    runtime,state,source,binding,env,log=prepared;changed=[]
    before=snapshot(runtime);signals=[]
    class FailedLauncher:
        pid=4242
        waits=0
        def wait(self,timeout=None):
            self.waits+=1
            if failure_mode=='timeout' and self.waits<3:raise subprocess.TimeoutExpired('fixture launcher',timeout)
            return 1 if failure_mode=='exit' else -9
    def launch(*args,**kwargs):
        changed.append(reserve_during_dashboard(prepared,field))
        return FailedLauncher()
    monkeypatch.setattr(server,'GLOBAL_MODE',False)
    monkeypatch.setattr(server,'RUNTIME_DIR',str(runtime))
    monkeypatch.setattr(server,'HERE',str(runtime.parent))
    monkeypatch.setattr(server,'_project_key',lambda:PROJECT)
    monkeypatch.setattr(server,'_spawn_name_status',lambda _: 'available')
    monkeypatch.setattr(server,'_mcp_call',lambda method,args,**kw:{'ok':True,'data':
        {'id':17,'name':NAME,'registration_token':TOKEN} if method=='register_agent' else {}})
    monkeypatch.setattr(server.subprocess,'Popen',launch)
    monkeypatch.setattr(server,'_tmux_run',lambda *a,**k:type('R',(),{'returncode':0,'stdout':''})())
    # Scope signal replacement to server.os, not the process-wide os module.
    monkeypatch.setattr(server,'os',SimpleNamespace(**{**vars(os),'killpg':lambda *args:signals.append(args)}))
    spec=server.SpawnLaunchSpec(provider='gemini',program='antigravity',model='fixture-model',script=str(source),
        signal_process_group=failure_mode=='timeout',termination_grace=1 if failure_mode=='timeout' else None)
    reply=server.spawn_with_launch_spec({'standalone':True,'name':NAME,'task':'fixture work',
        'dir':str(runtime.parent)},spec)
    assert len(changed)==1 and reply['ok'] is False and reply['registration_retained'] is True
    assert reply['owner_namespace_preserved'] is True and reply['cleanup_child_agent_can_recover'] is False
    path,data=changed[0]
    assert path.read_bytes()==data
    for name in ('agent_token_'+NAME,'managed_agents.txt','child-agents/'+NAME+'.codex-home/keep'):
        assert (runtime/name).read_bytes()==before[name]
    if failure_mode=='exit':
        assert not list((runtime/'spawn-tokens').iterdir()), 'only the failed launcher handoff should be removed'
        assert signals==[]
    else:
        import signal
        assert signals==[(4242,signal.SIGTERM),(4242,signal.SIGKILL)]
        handoff=Path(reply['recovery_credential'])
        assert list((runtime/'spawn-tokens').iterdir())==[handoff]
        assert handoff.read_text()==TOKEN and handoff.stat().st_mode & 0o777==0o600
    assert TOKEN not in json.dumps(reply)
