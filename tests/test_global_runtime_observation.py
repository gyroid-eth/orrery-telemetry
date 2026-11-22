"""Existing client truth -> admitted read projection; no provider dependency."""
import fcntl
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

import pytest
from dashboard import global_backend, server as dashboard
from test_global_dashboard_backend import ui, request
from test_global_runtime_delivery import server, launch
from test_global_runtime_client_s2a import context, api

ROOT = Path(__file__).resolve().parents[1]
OBS = global_backend.OPERATOR_API['API']


def put_live(client, sid='fixture-session', **values):
    directory = client.runtime_dir / 'live-sessions'
    directory.mkdir(mode=0o700, exist_ok=True)
    live = {'kind':'orrery-global-live-session-v1', **client.binding,
            'agent_id':client.identity['agent_id'], 'session_id':sid,
            'pid':12345, 'process_start':'fixture-start', 'pane_id':'%10', **values}
    api['atomic_json'](directory/(sid+'.json'), live)
    with client.operation_mutex():
        pass
    return live


def observe(ui, ids='2'):
    code, raw = request(ui, '/api/runtime-observations?agent_ids='+ids)
    assert code == 200, raw
    result = json.loads(raw)
    schema = json.loads((ROOT/'packages/agentstack_mail/fixtures/global-runtime-observation.json').read_text())['response_schema']
    OBS['wire_contract']()['validate_schema'](result, schema)
    return result['items']


@pytest.mark.parametrize('ids',['','0','1,1','-1','x','1&agent_ids=2',','.join(str(x) for x in range(1,202))])
def test_query_admission_is_closed(ui, ids):
    assert request(ui,'/api/runtime-observations?agent_ids='+ids,auth=False)[0] == 403
    code, body = request(ui,'/api/runtime-observations?agent_ids='+ids)
    assert code == 400 and json.loads(body)['reason'] == 'QUERY_INVALID'


@pytest.mark.parametrize('act,ctx,text',[
    ('work',42,'ctx: 42% used\n✻ Working…'),
    ('wait',0,'ctx: 0% used\nReady'),
    ('wait',100,'ctx: 100% used\nReady'),
    ('ask',42,'ctx: 42% used\nDo you want to run this command?\n❯ 1. Yes\n  2. No'),
    ('question',42,'ctx: 42% used\nEnter to select · Esc to cancel'),
])
def test_standalone_without_launch_is_observed_without_writing(ui,monkeypatch,act,ctx,text):
    client = context(ui[0],window=False)
    put_live(client)
    monkeypatch.setitem(OBS,'pane_evidence',lambda *a,**kw:('live','%10',text))
    before={p.relative_to(client.client_root):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}
    row=observe(ui)[0]
    assert row['liveness']=='live' and row['act_state']==act and row['ctx_used']==ctx
    assert row['session_count']==1 and row['parent_state']=='none'
    assert {p.relative_to(client.client_root):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}==before
    assert not client.outputs['launch'].exists()


def test_noncreating_shared_mutex_readers_and_writer_exclusion(ui):
    client=context(ui[0],window=False)
    path=client.outputs['operation_lock']
    path.unlink()
    row=observe(ui)[0]
    assert row['reason']=='OBSERVATION_MUTEX_UNAVAILABLE'
    assert not path.exists()
    with client.operation_mutex():pass
    with OBS['observation_inode'](path),OBS['observation_inode'](path):pass
    # A distinct process holds the writer EX, independent of Python API instances.
    script='import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.read()'
    process=subprocess.Popen([sys.executable,'-c',script,str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='ready'
        started=time.monotonic();row=observe(ui,'2,1')
        assert time.monotonic()-started < 5
        assert row[0]['reason']=='OBSERVATION_BUSY'
        assert row[1]['agent_id']==1 and row[1]['reason']!='OBSERVATION_BUSY'
    finally:
        process.communicate('',timeout=5)


def test_multiple_sessions_unknown_and_completed_history_does_not_hide_current(ui,monkeypatch):
    client=context(ui[0],window=False)
    put_live(client,'old',ended=True)
    put_live(client,'standalone')
    monkeypatch.setitem(OBS,'pane_evidence',lambda *a,**kw:('live','%10','ctx: 42% used'))
    assert observe(ui)[0]['ctx_used']==42
    put_live(client,'second')
    row=observe(ui)[0]
    assert row['reason']=='MULTIPLE_LIVE_SESSIONS' and row['act_state'] is None and row['ctx_used'] is None


def test_large_history_and_other_provider_home_do_not_require_recursive_scan(ui,monkeypatch):
    client=context(ui[0],window=False)
    for i in range(1100):put_live(client,'ended-'+str(i),ended=True)
    put_live(client,'current')
    home=client.runtime_dir/'provider-home';home.mkdir(mode=0o700,exist_ok=True)
    for i in range(1100):(home/str(i)).write_text('not observation truth')
    monkeypatch.setitem(OBS,'pane_evidence',lambda *a,**kw:('live','%10','ctx: 17% used'))
    assert observe(ui)[0]['ctx_used']==17


@pytest.mark.parametrize('state',['working','waiting','dormant'])
def test_app_real_desktop_metadata_and_snapshot_never_prove_provider_live(ui,monkeypatch,state):
    client=context(ui[0],window=False)
    put_live(client,app_state=state,app_subagent=False)
    transcript=client.runtime_dir/'rollout-fixture.jsonl'
    transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':'fixture-session','originator':'Codex Desktop'}})+'\n')
    snapshot=client.runtime_dir/'snapshot.json'
    canonical=runpy.run_path(str(ROOT/'integrations/codex_app/src/agentstack_codex_app/snapshot.py'))
    record=canonical['runtime_record']({'external_id':'codex:fixture-session','session_id':'fixture-session',
        'agent_id':2,'parent_external_id':None,'agent_name':'Beta','project_key':str(client.isolation),
        'last_seen_at':'2099-01-01T00:00:00Z'},{},state=state)
    canonical['SnapshotStore'](snapshot).upsert(record)
    from dashboard.providers.codex_app import CodexAppRuntimeProvider
    provider=CodexAppRuntimeProvider(snapshot)
    assert provider.list_runtimes()[0].live==''
    def forbidden(*args,**kwargs):raise AssertionError('App must not use bridge PID or tmux as provider proof')
    monkeypatch.setitem(OBS,'pane_evidence',forbidden)
    before=(transcript.read_bytes(),snapshot.read_bytes())
    row=observe(ui)[0]
    assert (row['liveness'],row['act_state'],row['ctx_used'],row['activity_freshness'],row['reason'])==('unknown',None,None,'unknown','APP_ACTIVITY_UNPROVEN')
    assert (transcript.read_bytes(),snapshot.read_bytes())==before


def test_new_public_assets_and_capability_are_installed_static(ui):
    version=json.loads(request(ui,'/api/version')[1])
    assert version['api']==dashboard.API_GENERATION
    assert version['capabilities']['global_runtime_observation']==1
    for name in ('operator_session.js','mail_controller.js','operation_controller.js','mail_flow.css','runtime_node.js'):
        code,body=request(ui,'/'+name,auth=False)
        assert code==200 and body==(ROOT/'dashboard'/name).read_bytes()


def test_typed_parent_and_cycle_are_read_from_existing_child_state(ui,monkeypatch):
    parent=context(ui[0],window=False)
    result=parent.prepare_child(dict(child_client_name='child',name='FixtureChild',program='codex',model='fixture',prepare_only=True))
    child=type(parent)(parent.clients_parent/'child/runtime-client.json')
    put_live(parent);put_live(child)
    monkeypatch.setitem(OBS,'pane_evidence',lambda *a,**kw:('live','%10','ctx: 27% used'))
    rows=observe(ui,'2,'+str(result['agent_id']))
    assert rows[0]['parent_state']=='none'
    assert rows[1]['parent_state']=='known' and rows[1]['parent_agent_id']==2
    # Cyclic typed state does not fabricate a parent relationship.
    copied=api['read_json'](child.outputs['child'])
    copied['parent']['client_name']='child';copied['parent']['agent_id']=child.identity['agent_id']
    api['atomic_json'](parent.outputs['child'],copied)
    rows=observe(ui,'2,'+str(result['agent_id']))
    assert all(x['parent_state']=='unknown' and x['parent_agent_id'] is None for x in rows)


@pytest.mark.parametrize('state',['absent','unknown'])
def test_provider_proof_is_required_and_never_inferred_from_receipt(ui,monkeypatch,state):
    client=context(ui[0],window=False);put_live(client)
    monkeypatch.setitem(OBS,'pane_evidence',lambda *a,**kw:(state,None,None))
    row=observe(ui)[0]
    assert row['liveness']==state and row['act_state'] is None and row['ctx_used'] is None


def test_changed_snapshot_and_stale_owner_are_unknown(ui,monkeypatch):
    client=context(ui[0],window=False);put_live(client)
    def changed(*a,**kw):
        put_live(client,'new-session')
        return 'live','%10','ctx: 42% used'
    monkeypatch.setitem(OBS,'pane_evidence',changed)
    assert observe(ui)[0]['reason']=='LOCAL_OBSERVATION_CHANGED'
    credential=api['read_json'](client.credential)
    credential['registration_token']='different-token'
    api['atomic_json'](client.credential,credential)
    assert observe(ui)[0]['reason']=='CREDENTIAL_BINDING_MISMATCH'


def test_live_slot_changes_invalidate_cached_activity(ui,monkeypatch):
    client=context(ui[0],window=False);put_live(client)
    calls=[]
    monkeypatch.setitem(OBS,'pane_evidence',lambda *a,**kw:(calls.append(1) or ('live','%10','ctx: 42% used')))
    assert observe(ui)[0]['ctx_used']==42
    assert observe(ui)[0]['ctx_used']==42 and len(calls)==1
    live=api['read_json'](client.runtime_dir/'live-sessions/fixture-session.json')
    live['process_start']='changed-start'
    api['atomic_json'](client.runtime_dir/'live-sessions/fixture-session.json',live)
    assert observe(ui)[0]['ctx_used']==42 and len(calls)==2


def test_existing_portrait_fallback_remains_behind_admission(ui):
    assert request(ui,'/portrait?name=Fixture',auth=False)[0]==403
    code,raw=request(ui,'/portrait?name=Fixture')
    assert code==200 and b'<svg' in raw
    assert request(ui,'/portrait?name=bad%2Fname')[0]==404


def test_probe_uses_private_capability_without_exposing_it(ui):
    from dashboard.http_operator import probe
    state,http,token=ui
    result=probe(state['root']/'agentstack',service='telemetry',
        telemetry_url=f'http://127.0.0.1:{http.server_port}',cockpit_port=1,config_path=state['config_path'])
    assert result['ok'] and result['cutover_ready'] is False and result['cockpit_ready'] is False
    assert result['binding']['expected_server_instance_id']==state['config']['mail_instance_id']
    assert token not in json.dumps(result) and 'fixture-owner-two' not in json.dumps(result)


def test_request_deadline_bounds_remaining_agents(ui,monkeypatch):
    client=context(ui[0],window=False);put_live(client)
    def expired(live,*,deadline,**kwargs):
        time.sleep(max(0,deadline-time.monotonic())+.01)
        OBS['remaining'](deadline)
    monkeypatch.setitem(OBS,'pane_evidence',expired)
    started=time.monotonic();rows=global_backend.Backend(ui[0]['config_path']).observe([2,1],dashboard._parse_runtime)['items']
    assert time.monotonic()-started < 6
    assert all(row['liveness']=='unknown' and row['ctx_used'] is None for row in rows)


def test_record_live_refresh_preserves_only_matching_current_launch_proof(launch,monkeypatch):
    client,delivery,row=launch
    old=put_live(client,launch_id=row['launch_id'],receipt_nonce=row['receipt_nonce'])
    monkeypatch.setenv('AGENTSTACK_PROVIDER_PID',str(old['pid']))
    monkeypatch.setenv('TMUX_PANE',old['pane_id'])
    monkeypatch.setitem(client.record_live.__globals__,'process_start',lambda pid:old['process_start'])
    client.record_live({'session_id':'fixture-session'})
    current=api['read_json'](client.runtime_dir/'live-sessions/fixture-session.json')
    assert current['launch_id']==row['launch_id'] and current['receipt_nonce']==row['receipt_nonce']
    row['launch_id']=str(__import__('uuid').uuid4())
    api['atomic_json'](client.outputs['launch'],row)
    client.record_live({'session_id':'fixture-session'})
    assert 'launch_id' not in api['read_json'](client.runtime_dir/'live-sessions/fixture-session.json')


def test_network_adapter_preserves_literal_states_and_does_not_default_unknown_to_wait():
    import shutil
    node=shutil.which('node')
    if node is None:pytest.skip('node unavailable')
    script='global.window={};'+(ROOT/'dashboard/runtime_node.js').read_text()+r'''
const assert=require('node:assert/strict'),adapt=window.OrreryRuntimeNode.adapt;
const roster=[1,2,3,4,5].map(agent_id=>({agent_id,name:'Fixture'+agent_id}));
const rows=[{agent_id:1,liveness:'live',session_count:1,act_state:'work',ctx_used:42,parent_state:'none'},
{agent_id:2,liveness:'live',session_count:1,act_state:'wait',ctx_used:0,parent_state:'known',parent_agent_id:1},
{agent_id:3,liveness:'unknown',session_count:0,act_state:null,ctx_used:null,parent_state:'unknown'},
{agent_id:4,liveness:'live',session_count:2,act_state:null,ctx_used:null,reason:'MULTIPLE_LIVE_SESSIONS'},
{agent_id:5,liveness:'absent',session_count:0,act_state:null,ctx_used:null,parent_state:'none'}];
const result=adapt(roster,rows);
assert.deepEqual([result.get(1).state,result.get(1).actState,result.get(1).ctxUsed,result.get(1).role],['run','work',42,'parent']);
assert.deepEqual([result.get(2).state,result.get(2).actState,result.get(2).ctxUsed,result.get(2).role],['run','wait',0,'child']);
for(const id of [3,4])assert.deepEqual([result.get(id).state,result.get(id).actState,result.get(id).ctxUsed],['unknown',null,null]);
assert.equal(result.get(4).annot.role,'MULTIPLE SESSIONS');
assert.deepEqual([result.get(5).state,result.get(5).running,result.get(5).annot.role],['gone',false,'NOT RUNNING']);
'''
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_actual_tmux_projection_uses_provider_process_start_and_own_socket(ui,monkeypatch):
    import shutil,shlex,uuid
    if shutil.which('tmux') is None:pytest.skip('tmux unavailable')
    client=context(ui[0],window=False)
    worker=client.isolation/'observation-provider.py'
    worker.write_text('import time\nprint("ctx: 42% used",flush=True)\nprint("✻ Working…",flush=True)\ntime.sleep(30)\n')
    name='fixture-6r-'+uuid.uuid4().hex[:10]
    monkeypatch.setenv('TMUX_TMPDIR',str(client.isolation))
    def tmux(*args):
        return subprocess.run(['tmux','-L',name,*args],capture_output=True,text=True,check=True,timeout=10)
    tmux('new-session','-d','-s','fixture',shlex.join([sys.executable,str(worker)]))
    try:
        pane,pid,socket,server_pid=tmux('display-message','-p','-t','fixture','#{pane_id} #{pane_pid} #{socket_path} #{pid}').stdout.strip().split()
        monkeypatch.setenv('TMUX',socket+','+server_pid+',0')
        start=api['process_start'](int(pid))
        assert start and start!='absent','actual ps proof requires sandbox-external execution'
        put_live(client,pid=int(pid),process_start=start,pane_id=pane)
        deadline=time.monotonic()+5
        while '✻ Working…' not in tmux('capture-pane','-p','-t',pane).stdout:
            assert time.monotonic()<deadline
            time.sleep(.03)
        row=observe(ui)[0]
        assert (row['liveness'],row['act_state'],row['ctx_used'],row['session_count'])==('live','work',42,1)
    finally:tmux('kill-server')


@pytest.mark.parametrize('change',['symlink','hardlink','mode','replace'])
def test_observation_mutex_rejects_unsafe_or_replaced_inode(ui,monkeypatch,change):
    client=context(ui[0],window=False);path=client.outputs['operation_lock']
    original=path.read_bytes()
    if change=='symlink':
        destination=client.client_root/'saved-lock';path.rename(destination);path.symlink_to(destination)
    elif change=='hardlink':os.link(path,client.client_root/'alias-lock')
    elif change=='mode':path.chmod(0o644)
    else:
        flock=fcntl.flock
        def replace(fd,flags):
            flock(fd,flags)
            replacement=client.client_root/'replacement-lock'
            api['atomic_json'](replacement,{'kind':'orrery-client-operation-mutex-v1'})
            os.replace(replacement,path)
        monkeypatch.setattr(fcntl,'flock',replace)
    with pytest.raises(RuntimeError):
        with OBS['observation_inode'](path):pytest.fail('unsafe mutex yielded')
    assert path.read_bytes()==original


@pytest.mark.parametrize('change',['none','capability','binding'])
def test_cockpit_probe_checks_actual_admitted_capabilities_and_binding(ui,change):
    from dashboard.http_operator import probe,AdmissionError
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    state,telemetry,token=ui
    binding=global_backend.Backend(state['config_path']).operator.binding
    capabilities={k:1 for k in ('operator_http_admission','global_mail_bridge','instruction_migration','websocket_first_frame_auth','global_runtime_observation')}
    upstream={'binding':binding,'capabilities':{'operator_session':1,'global_runtime_observation':1}}
    if change=='capability':capabilities.pop('global_mail_bridge')
    if change=='binding':upstream['binding']={**binding,'authority_epoch':binding['authority_epoch']+'-different'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path=='/telemetry/health'
            assert self.headers['Authorization']=='Bearer '+token
            self.send_response(200);self.end_headers()
            self.wfile.write(json.dumps({'runtime_mode':'global','capabilities':capabilities,'upstream':upstream}).encode())
        def log_message(self,*args):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
    try:
        args=dict(service='cockpit',telemetry_url=f'http://127.0.0.1:{telemetry.server_port}',cockpit_port=http.server_port,config_path=state['config_path'])
        if change=='none':
            result=probe(state['root']/'agentstack',**args)
            assert result['cockpit_ready'] and result['cutover_ready'] is False
        else:
            with pytest.raises(AdmissionError,match='COCKPIT_GLOBAL_CAPABILITY_REQUIRED'):probe(state['root']/'agentstack',**args)
    finally:http.shutdown();http.server_close();worker.join(timeout=5)


def test_retirement_after_roster_read_is_unknown(ui,monkeypatch):
    client=context(ui[0],window=False)
    put_live(client)
    exchange=OBS['management_exchange']
    def retired(*args,**kwargs):
        result=exchange(*args,**kwargs)
        result['retired_at']='2026-10-12T00:00:00Z'
        return result
    monkeypatch.setitem(OBS,'management_exchange',retired)
    row=observe(ui)[0]
    assert row['liveness']=='unknown' and row['act_state'] is None
    assert row['reason']=='AGENT_UNAVAILABLE'


@pytest.mark.parametrize('available',[True,False])
def test_probe_never_sends_capability_through_environment_proxy(ui,monkeypatch,available):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    from dashboard.http_operator import probe,AdmissionError
    state,http,token=ui
    requests=[]
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            value={'api':18,'runtime_mode':'global','capabilities':{'global_runtime_observation':1,'operator_session':1}} if self.path.endswith('/api/version') else {'binding':global_backend.Backend(state['config_path']).operator.binding,'data':{'server_ready':True}}
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
        def log_message(self,*args):pass
    proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
    worker=threading.Thread(target=proxy.serve_forever,daemon=True);worker.start()
    try:
        for key in ('http_proxy','HTTP_PROXY'):
            monkeypatch.setenv(key,f'http://127.0.0.1:{proxy.server_port}')
        for key in ('no_proxy','NO_PROXY'):monkeypatch.setenv(key,'')
        kwargs=dict(service='telemetry',telemetry_url=f'http://127.0.0.1:{http.server_port if available else 1}',cockpit_port=1,config_path=state['config_path'])
        if available:assert probe(state['root']/'agentstack',**kwargs)['ok']
        else:
            with pytest.raises(AdmissionError,match='HTTP_OPERATOR_PROBE_FAILED'):
                probe(state['root']/'agentstack',**kwargs)
        assert requests==[], 'loopback capability must never reach a proxy'
    finally:
        proxy.shutdown();proxy.server_close();worker.join(5)


def test_old_launch_absent_slot_does_not_hide_current_activity(ui,monkeypatch):
    client=context(ui[0],window=False)
    put_live(client,'old',launch_id='00000000-0000-4000-8000-000000000000',receipt_nonce='x'*43)
    put_live(client,'current')
    calls=[]
    def evidence(live,**kwargs):
        calls.append(live['session_id'])
        return ('absent',None,None) if live['session_id']=='old' else ('live','%10','ctx: 42% used\n✻ Working…')
    monkeypatch.setitem(OBS,'pane_evidence',evidence)
    row=observe(ui)[0]
    assert row['liveness']=='live' and row['act_state']=='work' and row['ctx_used']==42
    assert sorted(calls)==['current','old']


def test_retired_grandparent_does_not_hide_direct_parent(ui,monkeypatch):
    top=context(ui[0],window=False)
    mid_row=top.prepare_child(dict(child_client_name='mid',name='FixtureMid',program='codex',model='fixture',prepare_only=True))
    mid=type(top)(top.clients_parent/'mid/runtime-client.json')
    leaf_row=mid.prepare_child(dict(child_client_name='leaf',name='FixtureLeaf',program='codex',model='fixture',prepare_only=True))
    leaf=type(top)(top.clients_parent/'leaf/runtime-client.json')
    for client in (top,mid,leaf):put_live(client)
    monkeypatch.setitem(OBS,'pane_evidence',lambda *a,**kw:('live','%10','ctx: 42% used\n✻ Working…'))
    original=OBS['operator_exchange']
    def retired(config,binding,query,**kwargs):
        result=original(config,binding,query,**kwargs)
        if query.get('kind')=='roster':
            for row in result['data']['items']:
                if row['agent_id']==top.identity['agent_id']:row['retired_at']='2026-10-12T00:00:00Z'
        return result
    monkeypatch.setitem(OBS,'operator_exchange',retired)
    rows=observe(ui,f"{mid_row['agent_id']},{leaf_row['agent_id']}")
    assert rows[0]['parent_state']=='unknown'
    assert rows[1]['parent_state']=='known' and rows[1]['parent_agent_id']==mid_row['agent_id']


@pytest.mark.parametrize('failure', [KeyError, AttributeError])
def test_unexpected_inventory_failure_is_unknown_without_blocking_roster(ui, monkeypatch, failure):
    observations = global_backend.OBSERVATION_API['Observations']

    def unavailable(self):
        raise failure('synthetic observation failure')

    monkeypatch.setattr(observations, 'roots', unavailable)
    assert request(ui, '/api/agents?limit=200')[0] == 200
    rows = observe(ui, '1,2')
    assert [row['agent_id'] for row in rows] == [1, 2]
    assert all(row['liveness'] == 'unknown' and row['reason'] == 'OBSERVATION_INCOMPLETE' for row in rows)


def test_observation_request_reuses_roster_and_root_inventory(ui, monkeypatch):
    observations = global_backend.OBSERVATION_API['Observations']
    counts = dict(roster=0, roots=0)
    for name in counts:
        original = getattr(observations, name)

        def counted(self, name=name, original=original):
            counts[name] += 1
            return original(self)

        monkeypatch.setattr(observations, name, counted)
    assert len(observe(ui, '1,2')) == 2
    assert counts == dict(roster=1, roots=1)
