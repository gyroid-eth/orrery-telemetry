"""Real HTTP admission → real private socket, with no dashboard SQLite access."""
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from service_teardown import TEST_LABEL_PREFIX
from dashboard import server as dashboard
from dashboard.http_operator import configure, read_capability
from test_global_child_client import server


@pytest.fixture
def ui(server,monkeypatch):
    home=server['root']/'agentstack'
    configure(home)
    monkeypatch.setenv('AGENTSTACK_HOME',str(home))
    monkeypatch.setenv('AGENTSTACK_LABEL_PREFIX',TEST_LABEL_PREFIX)
    monkeypatch.setenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',str(server['config_path']))
    monkeypatch.setenv('AGENTSTACK_MCP_URL',server['url'])
    monkeypatch.setattr(dashboard,'GLOBAL_MODE',True)
    # Process mode/config are pinned together at startup; this in-process
    # HTTP fixture explicitly supplies both instead of changing ambient env.
    monkeypatch.setattr(dashboard,'GLOBAL_SERVER_CONFIG',str(server['config_path']))
    monkeypatch.setattr(dashboard,'BIND_HOST','127.0.0.1')
    http=ThreadingHTTPServer(('127.0.0.1',0),dashboard.Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    # Public management transport must never use even a readonly SQLite handle.
    def forbidden(*a,**kw):raise AssertionError('dashboard SQLite opened')
    monkeypatch.setattr(dashboard.sqlite3,'connect',forbidden)
    try:yield server,http,read_capability(home)
    finally:http.shutdown();http.server_close();thread.join(timeout=5)


def request(ui,path,*,auth=True,method='GET',extra=None,body=None):
    state,http,token=ui
    headers={'Authorization':'Bearer '+token} if auth else {}
    headers.update(extra or {})
    if body is not None:headers['Content-Type']='application/json'
    url=f'http://127.0.0.1:{http.server_port}'+path
    try:
        with urlopen(Request(url,headers=headers,method=method,data=json.dumps(body).encode() if body is not None else None),timeout=5) as response:
            return response.status,response.read()
    except HTTPError as exc:return exc.code,exc.read()


def test_api_and_visible_message_bridge_without_sqlite(ui):
    code,raw=request(ui,'/api/version')
    assert code==200 and json.loads(raw)['api']==dashboard.API_GENERATION
    assert json.loads(raw)['capabilities']['global_dashboard_ui']==1
    code,raw=request(ui,'/api/agents?limit=1')
    page=json.loads(raw)
    assert code==200 and page['runtime_mode']=='global' and len(page['data']['items'])==1,raw
    assert 'registration_token' not in raw.decode()
    code,raw=request(ui,'/api/mail/recent?view_as_agent_id=2&limit=1')
    page=json.loads(raw)
    assert code==200,raw
    mid=page['data']['items'][0]['id']
    code,raw=request(ui,f'/api/mail/message?view_as_agent_id=2&id={mid}')
    assert code==200 and json.loads(raw)['data']['body_md'] is not None
    code,raw=request(ui,'/api/mail-watcher-health')
    assert code==200 and json.loads(raw)['data']['server_ready'] is True


@pytest.mark.parametrize('method',['GET','POST','PUT','PATCH','DELETE','OPTIONS'])
def test_every_nonstatic_route_passes_admission_before_dispatch(ui,method):
    for path in ['/api/agents','/api/quotas','/api/global/spawn','/api/unknown','/future-operation']:
        code,raw=request(ui,path,auth=False,method=method)
        assert code==403 and json.loads(raw)['reason']=='HTTP_OPERATOR_REQUIRED'
    assert request(ui,'/',auth=False)[0]==200


@pytest.mark.parametrize('path,reason',[
    ('/api/mail/recent','UI_VIEWPOINT_REQUIRED'),
    ('/api/mail/recent?view_as_agent_id=bad','QUERY_INVALID'),
    ('/api/mail/recent?view_as_agent_id=2&limit=101','QUERY_INVALID'),
    ('/api/mail/legacy-label?view_as_agent_id=2&label=old&source=1','QUERY_INVALID'),
    ('/api/mail/recent?view_as_agent_id=2&view_as_agent_id=1','QUERY_INVALID'),
])
def test_http_input_refusal_is_not_a_legacy_fallback(ui,path,reason):
    code,raw=request(ui,path)
    assert code==400 and json.loads(raw)['reason']==reason,raw


def test_wrong_origin_host_and_capability_rejected(ui):
    assert request(ui,'/api/version',extra={'Origin':'http://evil.example'})[0]==403
    assert request(ui,'/api/version',extra={'Host':'evil.example'})[0]==403
    assert request(ui,'/api/version',extra={'Authorization':'Bearer wrong'})[0]==403


def test_spawn_http_and_same_name_discovery_use_fixed_root(ui):
    import os
    body={'client_name':'http-new','name':'HttpOwner','program':'daemon',
        'model':'deterministic','cwd':os.environ['HOME']}
    code,raw=request(ui,'/api/global/spawn',method='POST',body=body)
    state=json.loads(raw)
    assert code==200 and state['stage']=='registered_unconfigured',raw
    literal=ui[0]['root']/'agentstack/clients/http-new/runtime-client.json'
    assert literal.exists()
    code,raw=request(ui,'/api/global/spawn-status?client_name=http-new')
    assert code==200 and json.loads(raw)['agent_id']==state['agent_id']
    code,raw=request(ui,'/api/global/clients?name=HttpOwner')
    assert code==200 and json.loads(raw)['matches'][0]['client_name']=='http-new'
    code,raw=request(ui,'/api/global/spawn',method='POST',body={**body,'client_name':'other'})
    assert code==409 and json.loads(raw)['reason']=='EXISTING_CLIENT_REQUIRED'
    assert not (literal.parent.parent/'other').exists()


def test_bad_spawn_input_is_rejected_before_any_root(ui):
    import os
    body={'client_name':'../escape','name':'HttpOwner','program':'daemon',
        'model':'deterministic','cwd':os.environ['HOME']}
    code,raw=request(ui,'/api/global/spawn',method='POST',body=body)
    assert code==400 and json.loads(raw)['reason']=='QUERY_INVALID'
    assert not (ui[0]['root']/'agentstack/clients').exists()


def test_malformed_json_is_input_error(ui):
    state,http,token=ui
    req=Request(f'http://127.0.0.1:{http.server_port}/api/global/spawn',method='POST',data=b'{',
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with pytest.raises(HTTPError) as error:urlopen(req,timeout=5)
    assert error.value.code==400
    assert json.loads(error.value.read())['reason']=='QUERY_INVALID'


def test_foreign_registration_name_returns_conflict_without_local_state(ui):
    import os
    body={'client_name':'foreign','name':'Alpha','program':'daemon','model':'fixture','cwd':os.environ['HOME']}
    code,raw=request(ui,'/api/global/spawn',method='POST',body=body)
    assert code==409 and json.loads(raw)['reason']=='NAME_CONFLICT',raw
    assert not (ui[0]['root']/'agentstack/clients/foreign').exists()


@pytest.mark.parametrize('program',['claude-code','codex'])
def test_http_provider_missing_instructions_returns_actionable_status(ui,program,monkeypatch):
    import os
    from dashboard.global_backend import OPERATOR_API
    # Keep this request on the status/configuration boundary, without provider binaries.
    operator=OPERATOR_API['RuntimeOperator'](ui[0]['config_path'])
    initial=operator.create('provider',agent_name='HttpProvider',program=program,model='fixture')
    assert initial['stage']=='registered_unconfigured'
    assert initial['reason']=='INSTRUCTION_PROVIDER_CONFIG_REQUIRED'
    delivery=operator.api['runtime_delivery'](operator.client('provider'))
    started=[]
    monkeypatch.setattr(type(delivery),'start',lambda self,*a,**kw:started.append((a,kw)))
    body={'client_name':'provider','name':'HttpProvider','program':program,'model':'fixture','cwd':os.environ['HOME']}
    code,raw=request(ui,'/api/global/spawn',method='POST',body=body)
    expected='configured' if program=='claude-code' else 'registered_unconfigured'
    assert code==200 and json.loads(raw)['stage']==expected,raw
    assert bool(started)==(program=='claude-code')
    code,raw=request(ui,'/api/global/spawn-status?client_name=provider')
    assert code==200 and json.loads(raw)['stage']==expected,raw
    if program=='codex':assert json.loads(raw)['reason']=='CODEX_HOOK_CONFIGURE_REQUIRED'


def test_http_registration_race_has_digest_checked_abandon_and_new_root(ui):
    import os
    from dashboard.global_backend import OPERATOR_API
    operator=OPERATOR_API['RuntimeOperator'](ui[0]['config_path'])
    operator.prepare('race')
    client=operator.client('race')
    # Simulate a competing registration between lookup and atomic register.
    with pytest.raises(RuntimeError,match='OWNER_REQUIRED'):
        client.create('Alpha','daemon','fixture')
    pending=client.outputs['registration']
    original=pending.read_bytes()
    code,raw=request(ui,'/api/global/spawn-status?client_name=race')
    state=json.loads(raw)
    assert code==200 and state['stage']=='registration_pending'
    assert state['registration_sha256']
    code,raw=request(ui,'/api/global/registration-abandon',auth=False,method='POST',
        body={'client_name':'race','expected_digest':state['registration_sha256']})
    assert code==403 and pending.read_bytes()==original
    code,raw=request(ui,'/api/global/registration-abandon',method='POST',
        body={'client_name':'race','expected_digest':'0'*64})
    assert code==409 and pending.read_bytes()==original
    code,raw=request(ui,'/api/global/registration-abandon',method='POST',
        body={'client_name':'race','expected_digest':state['registration_sha256']})
    assert code==200 and json.loads(raw)['terminal'] is True
    terminal=pending.read_bytes()
    code,raw=request(ui,'/api/global/spawn',method='POST',body={'client_name':'after-race',
        'name':'AfterRace','program':'daemon','model':'fixture','cwd':os.environ['HOME']})
    assert code==200 and json.loads(raw)['stage']=='registered_unconfigured',raw
    assert pending.read_bytes()==terminal


@pytest.mark.parametrize('reason',['OWNER_REQUIRED','REGISTRATION_PENDING_CONFLICT','EXISTING_IDENTITY_REQUIRED'])
def test_exception_registration_conflicts_are_http_409(ui,monkeypatch,reason):
    import os
    from dashboard.global_backend import OPERATOR_API
    def conflict(*a,**kw):raise OPERATOR_API['API']['ClientError'](reason)
    monkeypatch.setattr(OPERATOR_API['RuntimeOperator'],'spawn',conflict)
    code,raw=request(ui,'/api/global/spawn',method='POST',body={'client_name':'conflict',
        'name':'ConflictName','program':'daemon','model':'fixture','cwd':os.environ['HOME']})
    assert code==409 and json.loads(raw)['reason']==reason


@pytest.mark.parametrize('program',['claude-code','codex'])
def test_fresh_http_provider_spawn_uses_configuration_without_provider_binary(ui,program,monkeypatch):
    import os
    from dashboard.global_backend import OPERATOR_API
    delivery=OPERATOR_API['API']['delivery_contract']()['RuntimeDelivery']
    started=[]
    monkeypatch.setattr(delivery,'start',lambda self,*a,**kw:started.append((a,kw)))
    body={'client_name':'fresh-provider','name':'FreshHttpProvider','program':program,
        'model':'fixture','cwd':os.environ['HOME']}
    assert not (ui[0]['root']/'agentstack/clients/fresh-provider').exists()
    code,raw=request(ui,'/api/global/spawn',method='POST',body=body)
    expected='configured' if program=='claude-code' else 'registered_unconfigured'
    assert code==200 and json.loads(raw)['stage']==expected,raw
    code,raw=request(ui,'/api/global/spawn-status?client_name=fresh-provider')
    assert code==200 and json.loads(raw)['stage']==expected,raw
    assert bool(started)==(program=='claude-code')
    if program=='codex':assert json.loads(raw)['reason']=='CODEX_HOOK_CONFIGURE_REQUIRED'


@pytest.mark.parametrize('prepared',[False,True])
def test_abandon_without_registration_pending_is_not_found(ui,prepared):
    from dashboard.global_backend import OPERATOR_API
    operator=OPERATOR_API['RuntimeOperator'](ui[0]['config_path'])
    path=operator.path_for('no-pending')
    if prepared:operator.prepare('no-pending')
    before={str(p):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()} if prepared else {}
    health=operator.read({'kind':'health'})
    body={'client_name':'no-pending','expected_digest':'0'*64}
    assert request(ui,'/api/global/registration-abandon',auth=False,method='POST',body=body)[0]==403
    code,raw=request(ui,'/api/global/registration-abandon',method='POST',body=body)
    assert code==404 and json.loads(raw)['reason']=='TARGET_UNAVAILABLE',raw
    after={str(p):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()} if prepared else {}
    assert after==before
    assert path.parent.exists()==prepared
    assert operator.read({'kind':'health'})==health



def test_registration_replay_uses_status_digest_and_single_intent(ui,monkeypatch):
    import os
    from dashboard.global_backend import OPERATOR_API
    op=OPERATOR_API['RuntimeOperator'](ui[0]['config_path']);op.prepare('replay')
    c=op.client('replay');cls=type(c);rpc=cls.rpc
    def unsent(self,method,params):
        if params.get('name')=='register_agent':raise op.api['ClientError']('TRANSPORT_FAILED')
        return rpc(self,method,params)
    with monkeypatch.context() as m:
        m.setattr(cls,'rpc',unsent)
        with pytest.raises(RuntimeError,match='TRANSPORT_FAILED'):c.create('ReplayOwner','daemon','deterministic')
    pending=c.outputs['registration'];before=pending.read_bytes()
    status=json.loads(request(ui,'/api/global/spawn-status?client_name=replay')[1])
    body={'client_name':'replay','name':'ReplayOwner','program':'daemon','model':'deterministic',
          'cwd':os.environ['HOME'],'expected_digest':status['registration_sha256']}
    assert request(ui,'/api/global/spawn',method='POST',auth=False,body=body)[0]==403
    code,raw=request(ui,'/api/global/spawn',method='POST',body={**body,'expected_digest':'0'*64})
    assert code==409 and json.loads(raw)['reason']=='REGISTRATION_PENDING_CONFLICT',raw
    assert pending.read_bytes()==before
    code,raw=request(ui,'/api/global/spawn',method='POST',body={**body,'name':'DifferentIntent'})
    assert code==409 and json.loads(raw)['reason']=='REGISTRATION_PENDING_CONFLICT',raw
    assert pending.read_bytes()==before
    token=json.loads(before)['registration_token']
    code,raw=request(ui,'/api/global/spawn',method='POST',body=body)
    assert code==200 and json.loads(raw)['stage']=='registered_unconfigured',raw
    current=op.client('replay');assert current.local_owner()==token
    aid=current.identity['agent_id']
    assert op.read({'kind':'resolve_name','name':'ReplayOwner'})['data']['agent_id']==aid
    assert not pending.exists()
    code,raw=request(ui,'/api/global/spawn',method='POST',body=body)
    assert code==404 and json.loads(raw)['reason']=='TARGET_UNAVAILABLE',raw
    assert op.client('replay').identity['agent_id']==aid


def test_missing_persistent_profile_status_is_actionable_and_readonly(ui):
    from dashboard.global_backend import OPERATOR_API
    op=OPERATOR_API['RuntimeOperator'](ui[0]['config_path'])
    created=op.create('missing-profile',agent_name='MissingProfile',program='daemon',model='deterministic')
    client=op.client('missing-profile');assert not client.profile_path.exists()
    code,raw=request(ui,'/api/global/spawn-status?client_name=missing-profile')
    status=json.loads(raw)
    assert code==200 and status['stage']=='registered_unconfigured'
    assert status['reason']=='PERSISTENT_EXECUTION_REQUIRED',status
    assert status['agent_id']==created['agent_id']
    assert not client.profile_path.exists()
    # Follow the documented operator CLI path from this absent profile.
    import os,subprocess,sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'AGENTSTACK_CLIENT_CONFIG':str(client.path)}
    saved=subprocess.run([sys.executable,str(root/'bin/lib/runtime_client.py'),'save-profile'],env=env,capture_output=True,text=True,timeout=20)
    assert saved.returncode==0,saved.stdout+saved.stderr
    execution={'argv':[sys.executable],'interaction':'interactive','cwd':str(client.isolation)}
    configured=subprocess.run([sys.executable,str(root/'bin/agentstack-persistent'),'configure','--profile',str(client.profile_path),'--operator'],env=env,input=json.dumps(execution),capture_output=True,text=True,timeout=20)
    assert configured.returncode==0,configured.stdout+configured.stderr
    code,raw=request(ui,'/api/global/spawn-status?client_name=missing-profile')
    assert code==200 and json.loads(raw)['stage']=='configured',raw
    assert json.loads(raw)['agent_id']==created['agent_id']
