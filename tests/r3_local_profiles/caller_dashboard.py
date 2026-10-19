"""TEST-only actual HTTP prepare boundary, never HTTP foreground execution."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading


def prepare_dashboard(probe, data):
    from .caller_resume import checked_saved_intent
    owner = Path(data['child_context']).parent/'profile.json'
    task = owner.parent/'runtime/task.md'
    assert not task.exists() and not task.is_symlink(), 'dashboard borrowed an earlier task'
    saved = checked_saved_intent(owner, None)
    probe.last_dashboard_before = {'intent':saved, 'owner_bytes':owner.read_bytes()}
    data['dashboard_intent_digest'] = saved['intent_digest']


def invoke_http(data):
    """One actual Handler invocation on this main thread; no production patches."""
    from http.server import HTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, build_opener, ProxyHandler
    from dashboard import server
    from dashboard.http_operator import read_capability
    assert threading.current_thread() is threading.main_thread()
    root = Path(data['provider_binary']).parent.parent
    assert Path(server.__file__) == root/'install/dashboard/server.py'
    assert server.GLOBAL_MODE is True and server.GLOBAL_SERVER_CONFIG == data['server_config']
    home = root/'isolation/agentstack'
    assert os.environ['AGENTSTACK_HOME'] == str(home)
    caller = deepcopy(data['caller'])
    caller['tools'] = data.get('invalid_api_tools', caller['tools'])
    caller.update(data.get('dashboard_caller_overrides', {}))
    body = {'client_name':'child_01', 'parent_client_name':'parent',
            'caller':caller, 'prepare_only':True, 'task_delivery':data['task_delivery']}
    result, errors = [], []
    # Explicit fixture configure already created this capability during setup.
    # Keep it in memory, never argv/env/stdout/trace/captures.
    capability = read_capability(home)
    with HTTPServer(('127.0.0.1', 0), server.Handler) as http:
        http.timeout = 15
        origin = f'http://127.0.0.1:{http.server_port}'
        def client():
            try:
                request = Request(origin+'/api/global/spawn', method='POST',
                    data=json.dumps(body).encode(), headers={
                        'Authorization':'Bearer '+capability,
                        'Content-Type':'application/json', 'Origin':origin})
                try:
                    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
                        status, raw = response.status, response.read(65537)
                except HTTPError as response:
                    with response:
                        status, raw = response.code, response.read(65537)
                assert len(raw) <= 65536
                result.append({'status':status, 'body':json.loads(raw)})
            except Exception as error:
                # Do not serialize headers, request objects or raw exceptions.
                errors.append(type(error).__name__)
        thread = threading.Thread(target=client)
        thread.start()
        try:
            http.handle_request()
        finally:
            thread.join(timeout=20)
    assert not thread.is_alive(), 'R3_REAL_HTTP_CLIENT_DID_NOT_STOP'
    assert not errors and len(result)==1, 'R3_REAL_HTTP_RESPONSE_MISSING'
    return result[0]


def check_dashboard_task(probe, data, response, rows):
    from .caller_resume import checked_saved_intent
    owner = Path(data['child_context']).parent/'profile.json'
    task = owner.parent/'runtime/task.md'
    # A successful-looking root no-op must stop here before any R2 helper.
    if not task.is_file() or task.is_symlink():
        raise RuntimeError('R3_REAL_CALLER_REJECTED: dashboard: R3_REAL_DASHBOARD_TASK_PREPARATION_MISSING')
    assert task.read_bytes() == Path(data['caller']['task']).read_bytes()
    before = probe.last_dashboard_before
    assert owner.read_bytes() == before['owner_bytes']
    intent = checked_saved_intent(owner, before['intent'])
    assert hashlib.sha256(task.read_bytes()).hexdigest() == intent['task_sha256']
    assert response['status'] == 200 and response['body']['ok'] is True
    body = response['body']
    assert body['stage'] == 'configured' and body['reason'] is None
    assert body['launch_status'] is None
    context = json.loads(Path(data['child_context']).read_text())
    assert context['client_name'] == body['client_name'] == 'child_01'
    identity = context['identity']
    assert identity and body['agent_id'] == identity['agent_id']
    assert identity['name'] == data['caller']['name']
    state = json.loads((owner.parent/'runtime/child-state.json').read_text())
    assert state['phase'] == 'prepared'
    parent = json.loads(Path(data['parent_context']).read_text())
    assert state['parent'] == {'client_name':'parent',
        'server_instance_id':parent['expected_server_instance_id'],
        'agent_id':parent['identity']['agent_id']}
    root = probe.install
    pids = {row['pid'] for row in rows if row['event']=='real-call'
            and row['file']==str(root/'dashboard/server.py') and row['name']=='do_spawn'}
    assert len(pids)==1, 'actual dashboard root was not called in one process'
    pid = next(iter(pids))
    for file, names in [
        ('dashboard/http_operator.py', ('http','network','authenticate')),
        ('dashboard/server.py', ('parse_request','do_POST','do_spawn')),
        ('dashboard/global_backend.py', ('spawn',)),
        ('bin/lib/runtime_operator.py', ('spawn','_spawn_caller')),
        ('bin/lib/runtime_client.py', ('prepare_child','_prepare_child_launch'))]:
        for name in names:
            assert any(row['event']=='real-call' and row['file']==str(root/file)
                       and row['name']==name and row['pid']==pid for row in rows), \
                'actual HTTP preparation was not reached in entry process: '+file+':'+name
    probe.last_dashboard_preparation = {'pid':pid, 'intent':intent, 'response':response, 'rows':rows}
