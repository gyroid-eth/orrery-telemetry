"""Launch/delivery acceptance against the actual isolated S2c HTTP server."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid
import pytest

from test_global_runtime_client_s2a import api, context as unconfigured_context
from service_teardown import TEST_LABEL_PREFIX
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'packages/agentstack_mail/tests'))
spec=importlib.util.spec_from_file_location('delivery_server_fixture',ROOT/'packages/agentstack_mail/tests/test_global_server_s2c.py')
s2=importlib.util.module_from_spec(spec);spec.loader.exec_module(s2)
spec2=importlib.util.spec_from_file_location('delivery_message_fixture',ROOT/'packages/agentstack_mail/tests/test_global_server_s2b.py')
messages=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(messages)

def context(*args, **kwargs):
    # Fixture operator preparation, separate from normal product launch.
    client=unconfigured_context(*args, **kwargs)
    for provider in ('claude','codex'):
        api['runtime_delivery'](client).instruction_configure(provider)
    return client

@pytest.fixture
def server(monkeypatch):
    prep=s2.prepared.__wrapped__(monkeypatch);state=next(prep)
    service=s2.server.__wrapped__(state)
    try:
        next(service);yield state
    finally:
        service.close();prep.close()

@pytest.fixture
def short_socket_server(monkeypatch):
    # Keep fixture and child validation on the same short temporary base even
    # when the runner's TMPDIR is deep. No production path limit is relaxed.
    import tempfile
    monkeypatch.setenv('TMPDIR','/tmp')
    monkeypatch.setattr(tempfile,'tempdir','/tmp')
    yield from server.__wrapped__(monkeypatch)

@pytest.fixture
def launch(server,monkeypatch):
    client=context(server,window=False)
    monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN','/usr/bin/true')
    delivery=api['runtime_delivery'](client)
    row=delivery.prepare_launch('claude',cwd=server['root'])
    delivery.bind_window()
    row.update(phase='running')
    delivery.save(client.outputs['launch'],row,'launch_intent')
    return client,delivery,row

def send(server):
    import asyncio
    return asyncio.run(messages.http(server,'send_message',messages.send(server)))['message_id']

def test_window_only_context_and_old_credential_extra_ignored(launch):
    client,delivery,row=launch
    credential=json.loads(client.credential.read_text())
    assert 'window_uuid' not in credential
    assert client.identity['window_uuid']
    assert set(row)=={'kind','launch_id','receipt_nonce','phase','provider','launch_kind','cwd','task_sha256','error','operator_resolution'}
    credential['window_uuid']='old-mismatched-copy'
    api['atomic_json'](client.credential,credential)
    previous=client.credential.read_bytes()
    assert client.observe()['window_ready']
    assert client.credential.read_bytes()==previous
    client.save_profile()
    assert 'window_uuid' not in json.loads(client.profile_path.read_text())

def test_prepare_unknown_retry_and_confirm(server,launch):
    client,delivery,row=launch;mid=send(server)
    before=delivery.prepare(mid,'tmux',admissible=False)
    assert before['state']=='retry-wait' and before['attempt_id'] is None
    first=delivery.prepare(mid,'tmux')
    assert first['state']=='handoff' and 'complete' in first['notification']
    assert delivery.prepare(mid,'tmux')['state']=='retry-wait'
    with delivery.message(mid) as path:
        intent=delivery.local(path,'delivery_intent')
        intent.update(next_attempt_at='2000-01-01T00:00:00+00:00',attempt_expires_at='2000-01-01T00:00:00+00:00')
        delivery.save(path,intent,'delivery_intent')
    second=delivery.prepare(mid,'tmux')
    assert second['duplicate_possible'] and second['attempt_id']!=first['attempt_id']
    with pytest.raises(api['ClientError'],match='DELIVERY_ROUTE_CHANGED'):
        delivery.callback(mid,first['attempt_id'],confirmed=True)
    assert delivery.callback(mid,second['attempt_id'],confirmed=True)['state']=='complete'
    assert delivery.prepare(mid,'tmux')['state']=='noop'

def test_confirmed_retry_never_reinjects(server,launch,monkeypatch):
    client,delivery,row=launch;mid=send(server)
    first=delivery.prepare(mid,'tmux')
    original=delivery.completion
    def fail(*args):raise OSError('synthetic socket loss')
    monkeypatch.setattr(delivery,'completion',fail)
    with pytest.raises(OSError):delivery.callback(mid,first['attempt_id'],confirmed=True)
    monkeypatch.setattr(delivery,'completion',original)
    assert delivery.prepare(mid,'tmux')['state']=='noop'

def test_status_digest_current_change_and_offline_abandon(launch,monkeypatch):
    client,delivery,row=launch
    row.update(phase='exec-unknown')
    delivery.save(client.outputs['launch'],row,'launch_intent')
    first=delivery.status(row['launch_id'])
    credential=api['read_json'](client.credential);credential['registration_token']='changed-valid-format'
    api['atomic_json'](client.credential,credential)
    request={'launch_id':row['launch_id'],'expected_state_sha256':first['state_sha256'],
             'confirm':True,'resolution':{'action':'abandon','operator_evidence':'external process cannot be proved'}}
    with pytest.raises(api['ClientError'],match='RESOLUTION_DIGEST_MISMATCH'):
        delivery.operator('launch-resolve',request)
    current=delivery.status(row['launch_id'])
    assert not current['remote_owner_verified'] and not current['runtime_ready']
    request['expected_state_sha256']=current['state_sha256']
    assert delivery.operator('launch-resolve',request)['phase']=='abandoned'

def test_proxy_schema_injects_owner_and_has_no_bootstrap_side_effects(launch):
    import io
    client,delivery,row=launch
    source=io.StringIO(json.dumps({'id':1,'method':'tools/list'})+'\n')
    output=io.StringIO();delivery.proxy(source,output)
    result=json.loads(output.getvalue())['result']['tools']
    names={x['name'] for x in result}
    assert len(names)==28 and 'bind_window_identity' not in names and 'runtime_status' in names and 'show_files' in names
    assert all('registration_token' not in x['inputSchema']['properties'] for x in result)

@pytest.mark.parametrize('relation', ['ancestor', 'descendant'])
def test_consumer_rejects_different_server_isolation_before_output(server, relation):
    client=context(server,window=False)
    config_path=client.runtime/'server-config.json'
    config=api['read_json'](config_path)
    config['isolation_root']=str(client.isolation.parent if relation=='ancestor' else client.isolation/'nested')
    api['atomic_json'](config_path,config)
    consumer=client.client_home/'global-consumer'
    with pytest.raises(api['ClientError'],match='CONSUMER_ISOLATION_MISMATCH'):
        api['runtime_delivery'](client).consumer_agent()
    assert not consumer.exists()


def test_diagnostics_does_not_create_consumer_state_and_clients_share_root(server):
    first=context(server,'first',window=False)
    second=context(server,'second',window=False)
    one,two=api['runtime_delivery'](first),api['runtime_delivery'](second)
    assert one.diagnostics()==[]
    assert not (first.client_home/'global-consumer').exists()
    assert one.consumer_agent()==two.consumer_agent()
    assert not one.consumer_agent().is_relative_to(first.runtime)
    assert not one.consumer_agent().is_relative_to(first.clients_parent)


def test_session_start_records_one_binding_and_proxy_rejects_old_launch(launch,monkeypatch):
    import io
    client,delivery,row=launch
    row['phase']='exec-unknown';delivery.save(client.outputs['launch'],row,'launch_intent')
    monkeypatch.setenv('AGENTSTACK_LAUNCH_ID',row['launch_id'])
    monkeypatch.setenv('AGENTSTACK_LAUNCH_NONCE',row['receipt_nonce'])
    delivery.session_start({'session_id':'provider-session','cwd':str(client.isolation),'provider_pid':None})
    receipt=api['read_json'](client.runtime_dir/'session_index/self.json')
    assert receipt['session_id']=='provider-session' and 'window_uuid' not in receipt
    assert delivery.status(row['launch_id'])['runtime_ready']
    requests=[{'id':1,'method':'tools/call','params':{'name':'bootstrap','arguments':{'session_id':'provider-session'}}},
              {'id':2,'method':'tools/call','params':{'name':'fetch_inbox','arguments':{}}}]
    output=io.StringIO();delivery.proxy(io.StringIO('\n'.join(json.dumps(r) for r in requests)),output)
    results=[json.loads(r) for r in output.getvalue().splitlines()]
    assert all('result' in r for r in results)
    monkeypatch.setenv('AGENTSTACK_LAUNCH_ID',str(uuid.uuid4()))
    with pytest.raises(api['ClientError'],match='LAUNCH_RECEIPT_MISMATCH'):
        delivery.process_binding()


def test_codex_session_header_mismatch_does_not_mark_running(launch,monkeypatch):
    client,delivery,row=launch
    row.update(provider='codex',phase='exec-unknown');delivery.save(client.outputs['launch'],row,'launch_intent')
    monkeypatch.setenv('AGENTSTACK_LAUNCH_ID',row['launch_id']);monkeypatch.setenv('AGENTSTACK_LAUNCH_NONCE',row['receipt_nonce'])
    transcript=client.runtime_dir/'provider-home/codex/record.jsonl'
    transcript.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':'another'}})+'\n')
    with pytest.raises(api['ClientError'],match='PROVIDER_SESSION_REQUIRED'):
        delivery.session_start({'session_id':'wanted','transcript_path':str(transcript)})
    assert delivery.launch()['phase']=='exec-unknown'
    assert not (client.runtime_dir/'session_index/self.json').exists()


def test_persistent_execution_requires_typed_operator_config(launch):
    client,delivery,row=launch
    client.save_profile()
    before=client.profile_path.read_bytes()
    with pytest.raises(api['ClientError'],match='PERSISTENT_EXECUTION_REQUIRED'):
        delivery.execution()
    with pytest.raises(api['ClientError'],match='OUTPUT_SCHEMA_INVALID'):
        delivery.configure_execution({'argv':'python script.py','interaction':'headless','cwd':str(client.isolation)})
    assert client.profile_path.read_bytes()==before
    configured={'argv':[sys.executable,'-u','worker.py'],'interaction':'headless','cwd':str(client.isolation)}
    assert delivery.configure_execution(configured)['configured']
    assert delivery.execution()==configured
    client.save_profile()
    assert delivery.execution()==configured


def test_persistent_bot_reply_boundary_and_deterministic_reply_uuid(server,launch,monkeypatch):
    import socket
    import threading
    client,delivery,row=launch
    client.save_profile()
    delivery.configure_execution({'argv':[sys.executable],'interaction':'headless','cwd':str(client.isolation)})
    home=delivery.directory(client.runtime_dir/'provider-home/daemon')
    path=home/'wake.sock'
    captured=[]; errors=[]
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path));path.chmod(0o600);listener.listen(1)
        def bot():
            try:
                with listener.accept()[0] as stream:
                    data=b''
                    while not data.endswith(b'\n'):data+=stream.recv(4096)
                    request=json.loads(data);captured.append(request)
                    # Reply through the one existing mutation journal, with the
                    # UUID supplied by the consumer, before confirming success.
                    result=client.call('reply_message',{'message_id':request['message']['id'],'body_md':'bot reply','request_id':request['reply_request_id']})
                    stream.sendall(json.dumps({'version':1,'contract':request['contract'],'status':'replied','message_id':request['message']['id']}).encode()+b'\n')
                    assert result['message_id']>request['message']['id']
            except BaseException as exc:errors.append(exc)
        worker=threading.Thread(target=bot);worker.start()
        mid=send(server)
        assert delivery.deliver_persistent(mid)['state']=='complete'
        worker.join(5)
        assert not worker.is_alive() and not errors
    assert captured[0]['reply_request_id']==str(uuid.uuid5(uuid.NAMESPACE_OID,client.binding['expected_server_instance_id']+':2:'+str(mid)))
    assert delivery.pending_messages(mid)==[]


def test_global_app_bridge_uses_external_reference_and_canonical_client_only(server,monkeypatch):
    sys.path.insert(0,str(ROOT/'integrations/codex_app/src'))
    from agentstack_codex_app.global_bridge import GlobalBridge
    template=context(server,'bridge',window=False)
    monkeypatch.setenv('AGENTSTACK_CODEX_BIN','/usr/bin/true')
    monkeypatch.setenv('CODEX_HOME',str(template.runtime_dir/'provider-home/codex'))
    bridge=GlobalBridge(api,template)
    root=bridge.delivery.directory(bridge.config.codex_sessions_root)
    sid='desktop-session'
    transcript=root/('rollout-'+sid+'.jsonl')
    transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'originator':'Codex Desktop'}})+'\n')
    event={'schema_version':1,'session_id':sid,'agent_id':None,'cwd':str(template.isolation),'model':'provider-model','hook_event_name':'SessionStart','turn_id':None}
    bridge.process_event(event)
    target=bridge.delivery.external_client(sid)
    assert target.identity['agent_id']!=template.identity['agent_id']
    route=api['read_json'](bridge.delivery.external_route(sid))
    assert set(route)=={'kind','client_name'}
    assert target.registration_metadata()=={'kind':api['METADATA_KIND'],**target.binding,'agent_id':target.identity['agent_id'],'program':'codex-app','model':'provider-model'}
    assert api['runtime_delivery'](target).status(api['runtime_delivery'](target).launch()['launch_id'])['runtime_ready']
    assert not (bridge.config.runtime_dir/'delivery.sqlite3').exists()
    assert not (bridge.config.runtime_dir/'identity/secrets').exists()
    aid=target.identity['agent_id']
    bridge.process_event(event)
    assert bridge.delivery.external_client(sid).identity['agent_id']==aid


def test_persistent_configure_run_and_actual_bot_reply(short_socket_server):
    server=short_socket_server
    import os
    import subprocess
    import time
    client=context(server,'persistent-e2e',window=False)
    client.save_profile()
    delivery=api['runtime_delivery'](client)
    worker=client.isolation/'fixture-reply-worker.py'
    worker.write_text('''import json, os, runpy, socket, sys
from pathlib import Path
api=runpy.run_path(sys.argv[1]);client=api['RuntimeClient'](Path(os.environ['AGENTSTACK_CLIENT_CONFIG']))
delivery=api['runtime_delivery'](client)
sid=os.environ['AGENTSTACK_LAUNCH_ID']
delivery.session_start({'session_id':sid,'provider_pid':None,'cwd':os.getcwd()})
path=Path(os.environ['AGENTSTACK_PERSISTENT_WAKE_SOCKET'])
try:
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path));path.chmod(0o600);listener.listen(1);listener.settimeout(15)
        with listener.accept()[0] as stream:
            raw=b''
            while not raw.endswith(b'\\n'):raw+=stream.recv(4096)
            request=json.loads(raw)
            result=client.call('reply_message',{'message_id':request['message']['id'],'body_md':'actual process reply','request_id':request['reply_request_id']})
            stream.sendall(json.dumps({'version':1,'contract':request['contract'],'status':'replied','message_id':request['message']['id']}).encode()+b'\\n')
finally:
    path.unlink(missing_ok=True)
    client.end_session({'session_id':sid})
''')
    delivery.configure_execution({'argv':[sys.executable,str(worker),str(ROOT/'bin/lib/runtime_client.py')],
        'interaction':'headless','cwd':str(client.isolation)})
    env={**os.environ,'HOME':str(client.isolation/'home'),'AGENTSTACK_CLIENT_CONFIG':str(client.path),
         'AGENTSTACK_HOME':str(ROOT), 'AGENTSTACK_LABEL_PREFIX':TEST_LABEL_PREFIX}
    process=subprocess.Popen([sys.executable,str(ROOT/'bin/agentstack-persistent'),'run','--profile',str(client.profile_path)],
        env=env,cwd=client.isolation,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        socket_path=client.runtime_dir/'provider-home/daemon/wake.sock'
        deadline=time.monotonic()+15
        while not socket_path.exists():
            if process.poll() is not None:
                out,err=process.communicate();pytest.fail(f'bot exited {process.returncode}: {out} {err}')
            if time.monotonic()>=deadline:pytest.fail('bot socket did not become ready')
            time.sleep(.03)
        mid=send(server)
        assert delivery.deliver_persistent(mid)['state']=='complete'
        out,err=process.communicate(timeout=15)
        assert process.returncode==0,(out,err)
        assert delivery.launch()['phase']=='ended'
        rows=client.call('search_messages',{'query':'actual process reply','limit':10})
        assert rows['items'] and rows['items'][0]['reply_to']==mid
        assert delivery.pending_messages(mid)==[]
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.communicate()


def test_codex_launch_reads_approved_fixed_plan_without_approving(server,monkeypatch):
    client=context(server,'codex-provision',window=False)
    delivery=api['runtime_delivery'](client)
    monkeypatch.setenv('AGENTSTACK_CODEX_BIN','/usr/bin/true')
    before={str(p):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}
    with pytest.raises(api['ClientError'],match='CODEX_HOOK_CONFIGURE_REQUIRED'):
        delivery.start('codex',client.isolation)
    assert before=={str(p):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}
    assert not client.outputs['launch'].exists()
    home=delivery.directory(client.runtime_dir/'provider-home/codex')
    plugin=delivery.directory(home/'plugins/cache/fixture')
    delivery.directory(plugin/'hooks')
    hooks={'hooks':{'SessionStart':[]}}
    (plugin/'hooks/hooks.json').write_text(json.dumps(hooks));(plugin/'hooks/hooks.json').chmod(0o600)
    trust=delivery.plugin_helpers()[0]
    plan={'schema_version':1,'approved':True,'enable_hooks':True,'shared_codex_home':str(home),
        'cwd':str(client.isolation),'plugin_id':'agentstack-codex-app@agentstack-global','plugin_root':str(plugin),'payload_digest':trust['payload_digest'](plugin),
        'declared_hooks':hooks,'hooks':[{'key':'fixture-key','currentHash':'sha256:fixture'}]}
    api['atomic_json'](home/'hook-plan.json',plan)
    (home/'config.toml').write_text('[plugins."agentstack-codex-app@agentstack-global"]\nenabled = true\n[hooks.state."fixture-key"]\nenabled = true\ntrusted_hash = "sha256:fixture"\n')
    (home/'config.toml').chmod(0o600)
    assert delivery.codex_trust()==plan
    row=delivery.prepare_launch('codex',cwd=client.isolation)
    assert delivery.provider_command(row)[0]=='/usr/bin/true'
    (plugin/'hooks/hooks.json').write_text(json.dumps({'hooks':{'Stop':[]}}))
    with pytest.raises(api['ClientError'],match='CODEX_HOOK_CONFIGURE_REQUIRED'):
        delivery.provider_command(row)


def test_global_selftest_runs_real_fixed_profile_bot(short_socket_server,monkeypatch,capsys):
    server=short_socket_server
    import runpy
    client=context(server,'selftest-parent',window=False)
    monkeypatch.setenv('AGENTSTACK_HOME',str(ROOT))
    api_test=runpy.run_path(str(ROOT/'scripts/selftest.py'))
    assert api_test['global_selftest'](api,client)==0
    assert 'isolated global launch, bot reply' in capsys.readouterr().out


def test_operator_confirm_and_importance_do_not_clear_unhanded_hints(server,launch,monkeypatch):
    client,delivery,row=launch
    mid=send(server)
    monkeypatch.setenv('AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE','urgent')
    blocked=delivery.prepare(mid,'tmux')
    assert blocked['state']=='blocked' and blocked['reason']=='BELOW_MIN_IMPORTANCE'
    assert delivery.pending_messages(mid)
    monkeypatch.delenv('AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE')
    notice=delivery.prepare(mid,'tmux')
    result=delivery.operator('notification-confirm',{'message_id':mid,'attempt_id':notice['attempt_id'],'outcome':'tmux-submitted'})
    assert result['state']=='complete'


def test_actual_claude_launch_runs_generated_session_and_notification_hooks(server,monkeypatch):
    import os
    import subprocess
    client=context(server,'claude-hooks',window=False)
    worker=client.isolation/'synthetic-provider-worker.py'
    worker.write_text('#!'+sys.executable+'\n'+r'''
import json, os, shlex, subprocess, sys
settings=json.load(open(sys.argv[sys.argv.index('--settings')+1]))
payload={'session_id':'fixture-claude','cwd':os.getcwd()}
def hooks(event):
    results=[]
    for group in settings['hooks'][event]:
        if group.get('matcher'):continue
        for hook in group['hooks']:
            result=subprocess.run(shlex.split(hook['command']),input=json.dumps(payload),text=True,capture_output=True)
            if result.returncode:raise RuntimeError(result.stderr)
            results.append(json.loads(result.stdout))
    return results
print(json.dumps(hooks('SessionStart')),flush=True)
sys.stdin.readline()
print(json.dumps(hooks('PostToolUse')),flush=True)
sys.stdin.readline()
print(json.dumps(hooks('SessionEnd')),flush=True)
''')
    worker.chmod(0o700)
    env=dict(os.environ,AGENTSTACK_CLAUDE_BIN=str(worker),AGENTSTACK_CLIENT_CONFIG=str(client.path))
    helper=ROOT/'bin/lib/runtime_client.py'
    process=subprocess.Popen([sys.executable,str(helper),'--context',str(client.path),'launch','claude',str(client.isolation)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=client.isolation,env=env)
    try:
        started=json.loads(process.stdout.readline())
        assert started[0]['runtime_ready'] is True
        assert api['runtime_delivery'](client).launch()['phase']=='running'
        mid=send(server)
        process.stdin.write('post\n');process.stdin.flush()
        raw=process.stdout.readline()
        assert raw,process.stderr.read()
        post=json.loads(raw)
        assert post[0]['hookSpecificOutput']['hookEventName']=='PostToolUse'
        assert 'from Alpha [normal]: hello' in post[0]['hookSpecificOutput']['additionalContext']
        assert api['runtime_delivery'](client).pending_messages(mid)==[]
        process.stdin.write('end\n');process.stdin.flush()
        ended=json.loads(process.stdout.readline())
        assert ended[0]['end']=='unknown'  # SDK SessionEnd precedes native exit/wait
        assert api['runtime_delivery'](client).launch()['phase']=='running'
        out,err=process.communicate(timeout=15)
        assert process.returncode==0,err
        assert api['runtime_delivery'](client).launch()['phase']=='ended'
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.communicate()


def test_tmux_route_requires_same_provider_process_start_and_pane(launch,monkeypatch):
    import socket,tempfile
    import subprocess
    client,delivery,row=launch
    live={'kind':'orrery-global-live-session-v1','launch_id':row['launch_id'],'receipt_nonce':row['receipt_nonce'],'ended':False,
          'pane_id':'%4','pid':123,'process_start':'provider-start'}
    monkeypatch.setattr(delivery,'live_rows',lambda launch_id:[['session.json',live]])
    monkeypatch.setitem(delivery.api,'process_start',lambda pid,**kw:'provider-start')
    tmux_api=delivery.api['runtime_tmux_api']()
    with tempfile.TemporaryDirectory(prefix='r2route-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory:
        path=Path(directory).resolve()/'fixture.sock'
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind(str(path));path.chmod(0o600)
            def terminal(argv,**kw):
                text=('$1\torrery-'+row['launch_id']+'\t%4\t123\n' if '#{session_id}' in argv[-1] else 'python')
                return subprocess.CompletedProcess(argv,0,text,'')
            transport=tmux_api['Transport'](path,binary='fixture-tmux',run=terminal)
            monkeypatch.setitem(tmux_api['endpoint_candidates'].__globals__,'endpoint_candidates',lambda *args:[('legacy-default',transport)])
            actual=delivery.tmux_target()
            assert actual[0].path==path and actual[1]['pane_id']=='%4' and actual[1]['pid']==123
            monkeypatch.setitem(delivery.api,'process_start',lambda pid,**kw:None)
            assert delivery.tmux_target() is None
            monkeypatch.setitem(delivery.api,'process_start',lambda pid,**kw:'reused-pid-start')
            assert delivery.tmux_target() is None
            monkeypatch.setitem(delivery.api,'process_start',lambda pid,**kw:'provider-start')
            live['pid']=456
            assert delivery.tmux_target() is None


@pytest.mark.parametrize('emit_init',[False,True])
def test_gemini_child_uses_native_fixed_home_and_real_stream_receipt(server,monkeypatch,emit_init):
    import os
    import subprocess
    parent=context(server,'gemini-parent',window=False)
    prepared=parent.prepare_child({'child_client_name':'gemini-child','name':'StreamChild',
        'program':'antigravity','model':'fixture-model','task_description':'fixture','embed_task':True,'prepare_only':True})
    child=api['RuntimeClient'](Path(prepared['client_config']))
    task=child.outputs['task'];api['atomic_bytes'](task,b'Canonical embedded task')
    worker=child.isolation/'synthetic-stream-provider.py'
    worker.write_text('#!'+sys.executable+'\n'+r'''
import json,os,pathlib,sys
assert '--input-format' in sys.argv and '--output-format' in sys.argv
home=pathlib.Path(os.environ['HOME'])
assert 'GEMINI_CLI_HOME' not in os.environ
config=json.loads((home/'.gemini/config/mcp_config.json').read_text())
assert list(config['mcpServers'])==['orrery-mail']
event=json.loads(sys.stdin.readline())
assert event['message']['content']=='Canonical embedded task'
if os.environ['FIXTURE_EMIT_INIT']=='1':
    print(json.dumps({'event':'init','conversation_id':'fixture-conversation'}),flush=True)
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'fixture reply'}}),flush=True)
''')
    worker.chmod(0o700)
    env=dict(os.environ,AGENTSTACK_GEMINI_BIN=str(worker),FIXTURE_EMIT_INIT='1' if emit_init else '0')
    result=subprocess.run([sys.executable,str(ROOT/'bin/lib/runtime_client.py'),'--context',str(child.path),
        'launch','gemini',str(child.isolation)],cwd=child.isolation,env=env,capture_output=True,text=True,timeout=20)
    delivery=api['runtime_delivery'](child)
    if emit_init:
        assert result.returncode==0,result.stderr
        saved=json.loads((child.runtime_dir/'session_index/self.json').read_text())
        assert saved['session_id']=='fixture-conversation'
        assert delivery.launch()['phase']=='ended'
    else:
        assert result.returncode==2 and 'GEMINI_SESSION_RECEIPT_UNAVAILABLE' in result.stderr
        # Missing SDK init never proves readiness, but an actual wait result
        # does prove that this execution ended (and must not be relaunched).
        launch=delivery.launch()
        assert launch['phase']=='ended'
        assert launch['execution']['outcome']=={'state':'exited','returncode':0}
        assert launch['termination']['phase']=='ended'
        assert not (child.runtime_dir/'session_index/self.json').exists()
        assert delivery.status(launch['launch_id'])['runtime_ready'] is False
        assert not delivery.status(delivery.launch()['launch_id'])['runtime_ready']
    assert not (child.runtime_dir/'provider-home/gemini/settings.json').exists()
    assert not (child.runtime_dir/'provider-home/gemini/hooks.json').exists()


def test_gemini_interactive_root_refuses_before_launch_effects(server):
    client=context(server,'gemini-interactive',window=False)
    before={str(p):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}
    with pytest.raises(api['ClientError'],match='GEMINI_INTERACTIVE_RUNTIME_UNSUPPORTED'):
        api['runtime_delivery'](client).start('gemini',client.isolation)
    assert before=={str(p):p.read_bytes() for p in client.client_root.rglob('*') if p.is_file()}


def test_fenced_delivery_abandon_uses_current_digest_and_preserves_server_fact(launch,server):
    client,delivery,row=launch
    mid=send(server)
    prepared=delivery.prepare(mid,'tmux')
    with delivery.message(mid) as path:
        old=delivery.local(path,'delivery_intent')
        old['launch_id']=str(uuid.uuid4())
        delivery.save(path,old,'delivery_intent')
    status=delivery.delivery_status(mid)
    request={'message_id':mid,'expected_state_sha256':status['state_sha256'],'confirm':True,
             'resolution':{'action':'abandon-without-consuming','operator_evidence':'prior launch cannot be verified'}}
    result=delivery.operator('resolve-delivery',request)
    assert result['phase']=='blocked'
    assert result['captured']['launch_id']==old['launch_id']
    assert delivery.pending_messages(mid)
    assert delivery.operator('delivery-status',{'message_id':mid})['reason']=='OPERATOR_ABANDONED'
    with pytest.raises(api['ClientError'],match='RESOLUTION_DIGEST_MISMATCH'):
        delivery.operator('resolve-delivery',request)


def test_real_tmux_notification_uses_only_own_socket_and_completes_fact(launch,server,monkeypatch):
    import os
    import shlex
    import shutil
    import subprocess
    import time
    if not shutil.which('tmux'):
        pytest.skip('tmux is unavailable')
    client,delivery,row=launch
    socket_name='fixture-4c-'+uuid.uuid4().hex[:12]
    session='orrery-'+row['launch_id']
    worker=client.isolation/'synthetic-terminal-provider.py'
    output=client.isolation/'terminal-notification.txt'
    worker.write_text('import os,sys,termios,tty\n'
        'tty.setraw(0)\n'
        'raw=b""\n'
        'while b"\\r" not in raw and b"\\n" not in raw:\n'
        ' raw+=os.read(0,4096)\n'
        'open(sys.argv[1],"wb").write(raw)\n'
        'import time;time.sleep(30)\n')
    def tmux(*args):
        return subprocess.run(['tmux','-L',socket_name,*args],capture_output=True,text=True,timeout=10,check=True)
    tmux('new-session','-d','-s',session,shlex.join([sys.executable,str(worker),str(output)]))
    try:
        pane,pid,socket_path,server_pid=tmux('display-message','-p','-t',session,
            '#{pane_id} #{pane_pid} #{socket_path} #{pid}').stdout.strip().split()
        monkeypatch.setenv('TMUX',socket_path+','+server_pid+',0')
        start=api['process_start'](int(pid))
        assert start and start!='absent', 'provider process evidence requires sandbox-external ps'
        live={'kind':'orrery-global-live-session-v1','launch_id':row['launch_id'],'receipt_nonce':row['receipt_nonce'],'ended':False,
              'pane_id':pane,'pid':int(pid),'process_start':start}
        monkeypatch.setattr(delivery,'live_rows',lambda launch_id:[['session.json',live]])
        deadline=time.monotonic()+5
        while delivery.tmux_target() is None:
            assert time.monotonic()<deadline, 'synthetic provider did not become the pane process'
            time.sleep(.03)
        mid=send(server)
        result=delivery.deliver_tmux(mid)
        assert result['state']=='complete'
        while not output.exists():
            assert time.monotonic()<deadline, 'notification did not reach the synthetic provider'
            time.sleep(.03)
        assert b'hello' in output.read_bytes()
        assert delivery.pending_messages(mid)==[]
    finally:
        tmux('kill-server')


def test_child_tool_selection_uses_existing_policy_and_fixed_provider_home(server,monkeypatch):
    parent=context(server,'tool-parent',window=False)
    prepared=parent.prepare_child({'child_client_name':'tool-child','name':'ToolChild',
        'program':'claude-code','model':'fixture','task_description':'fixture',
        'base':'mail-only','parsed_tools':{'mcp':['fixture-server']},'prepare_only':True})
    child=api['RuntimeClient'](Path(prepared['client_config']))
    delivery=api['runtime_delivery'](child)
    home=delivery.directory(child.runtime_dir/'provider-home/claude')
    with pytest.raises(api['ClientError'],match='TOOL_SELECTION_UNAVAILABLE'):
        delivery.child_tool_plan('claude',home,str(child.isolation))
    definition={'command':sys.executable,'args':['fixture-mcp.py']}
    api['atomic_json'](home/'.claude.json',{'mcpServers':{'fixture-server':definition}})
    servers,flags,prompt=delivery.child_tool_plan('claude',home,str(child.isolation))
    assert servers=={'fixture-server':definition}
    assert '--no-chrome' in flags
    assert 'mail-only' in prompt
    with pytest.raises(api['ClientError'],match='TOOL_SELECTION_UNAVAILABLE'):
        delivery.child_tool_plan('gemini',home,str(child.isolation))
    state=api['read_json'](child.outputs['child'])
    state['tools_selection']={'base':'default','tools':{}}
    child.write_output(child.outputs['child'],state,'child')
    servers,_,_=delivery.child_tool_plan('claude',home,str(child.isolation))
    assert servers=={'fixture-server':definition}


def test_mail_task_replay_uses_single_parent_intent_and_never_embeds_body(server,monkeypatch):
    parent=context(server,'mail-task-parent',window=False)
    prepared=parent.prepare_child({'child_client_name':'mail-task-child','name':'MailTaskChild',
        'program':'claude-code','model':'fixture','task_description':'fixture','prepare_only':True})
    child=api['RuntimeClient'](Path(prepared['client_config']))
    parent.call('request_contact',{'to_agent_id':child.identity['agent_id']})
    child.call('respond_contact',{'from_agent_id':parent.identity['agent_id'],'accept':True})
    api['atomic_bytes'](child.outputs['task'],b'Private canonical task bytes')
    monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN','/usr/bin/true')
    delivery=api['runtime_delivery'](child)
    delivery.instruction_configure('claude')
    row=delivery.prepare_launch('claude',cwd=child.isolation)
    command=delivery.provider_command(row)
    assert 'Private canonical task bytes' not in ' '.join(command)
    assert 'Fetch your inbox' in command[-1]
    original=api['RuntimeClient'].rpc
    lost=False
    def rpc(client,method,params):
        nonlocal lost
        result=original(client,method,params)
        if not lost and method=='tools/call' and params.get('name')=='send_message':
            lost=True
            raise api['ClientError']('TRANSPORT_FAILED')
        return result
    monkeypatch.setattr(api['RuntimeClient'],'rpc',rpc)
    with pytest.raises(api['ClientError'],match='TRANSPORT_FAILED'):
        delivery.launch_task(row,send_mail=True)
    pending=parent.outputs['mutation'].read_bytes()
    assert b'Private canonical task bytes' in pending
    delivery.launch_task(row,send_mail=True)
    delivery.launch_task(row,send_mail=True)
    assert not parent.outputs['mutation'].exists()
    inbox=child.call('fetch_inbox',{'include_bodies':True,'limit':100})
    tasks=[item for item in inbox if item['subject']=='Delegated task']
    assert len(tasks)==1 and tasks[0]['body_md']=='Private canonical task bytes'
    assert parent.outputs['mutation'].exists() is False


def test_app_rate_admission_blocks_without_attempt_and_resumes_same_intent(launch,server):
    client,delivery,row=launch
    mid=send(server)
    blocked=delivery.prepare(mid,'codex-app',admission_reason='WAKE_RATE_LIMITED')
    assert blocked['state']=='blocked' and blocked['reason']=='WAKE_RATE_LIMITED'
    status=delivery.delivery_status(mid)
    assert status['attempt_count']==0 and status['phase']=='blocked'
    assert delivery.pending_messages(mid)
    resumed=delivery.prepare(mid,'codex-app')
    assert resumed['state']=='handoff' and not resumed['duplicate_possible']
    assert delivery.delivery_status(mid)['attempt_count']==1


def test_actual_await_sender_filter_and_after_id_cross_pages_without_consuming(server):
    import asyncio
    import os
    import subprocess
    client=context(server,'await-keyset',window=False)
    ids=[asyncio.run(messages.http(server,'send_message',{**messages.send(server),'subject':'page-'+str(i)}))['message_id'] for i in range(25)]
    env=dict(os.environ,AGENTSTACK_CLIENT_CONFIG=str(client.path),AGENTSTACK_HOME=str(ROOT))
    command=[sys.executable,str(ROOT/'bin/agentstack-await-reply'),'--from','Alpha','--timeout','10','--interval','.05']
    result=subprocess.run(command+['--after-id',str(ids[0]-1)],cwd=client.isolation,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['id']==ids[0]
    # No after_id retains the old one-page boundary (20 newest messages).
    page=subprocess.run(command,cwd=client.isolation,env=env,capture_output=True,text=True,timeout=20)
    assert page.returncode==0,page.stderr
    assert json.loads(page.stdout)['id']==ids[-20]
    pending=client.call('fetch_inbox',{'message_ids':[ids[0]],'notification_pending_only':True,'include_bodies':True,'limit':1})
    assert len(pending)==1 and pending[0]['id']==ids[0]


def test_app_cold_wake_serializes_root_and_rate_limit_keeps_server_fact(server,monkeypatch):
    import asyncio
    from collections import deque
    import time
    sys.path.insert(0,str(ROOT/'integrations/codex_app/src'))
    from agentstack_codex_app.global_bridge import GlobalBridge
    template=context(server,'cold-bridge',window=False)
    monkeypatch.setenv('AGENTSTACK_CODEX_BIN','/usr/bin/true')
    monkeypatch.setenv('CODEX_HOME',str(template.runtime_dir/'provider-home/codex'))
    bridge=GlobalBridge(api,template)
    root=bridge.delivery.directory(bridge.config.codex_sessions_root)
    sid='cold-desktop-session'
    transcript=root/('rollout-'+sid+'.jsonl')
    transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'originator':'Codex Desktop'}})+'\n')
    event={'schema_version':1,'session_id':sid,'agent_id':None,'cwd':str(template.isolation),
           'model':'fixture-model','hook_event_name':'SessionStart','turn_id':None}
    bridge.process_event(event)
    target=bridge.delivery.external_client(sid)
    delivery=api['runtime_delivery'](target)
    asyncio.run(messages.http(server,'request_contact',messages.owner(server,1,
        request_id=str(uuid.uuid4()),to_agent_id=target.identity['agent_id'])))
    target.call('respond_contact',{'from_agent_id':1,'accept':True})
    mids=[asyncio.run(messages.http(server,'send_message',{**messages.send(server),'to_agent_ids':[target.identity['agent_id']],'subject':'cold wake '+str(i)}))['message_id'] for i in range(2)]
    deadline=time.monotonic()+10
    while delivery.hints()!=mids:
        assert time.monotonic()<deadline, 'server signal worker did not materialize the fixture hints'
        time.sleep(.03)
    key=bridge.delivery.external_route(sid).parent.name
    for mid in mids:bridge._seen[(key,mid)]=time.monotonic()-3
    bridge._history[key]=deque([time.monotonic()]*12)
    bridge.wake_tick()
    assert not bridge._inflight
    for mid in mids:
        status=delivery.delivery_status(mid)
        assert status['phase']=='blocked' and status['reason']=='WAKE_RATE_LIMITED'
        assert status['attempt_count']==0 and delivery.pending_messages(mid)
    marker=target.isolation/'finish-fixture-wake'
    worker=target.isolation/'synthetic-cold-provider.py'
    worker.write_text('import pathlib,sys,time\n'
        'deadline=time.monotonic()+10\n'
        'while not pathlib.Path(sys.argv[1]).exists():\n'
        ' if time.monotonic()>deadline:sys.exit(1)\n'
        ' time.sleep(.02)\n')
    actual = bridge.resume_command(delivery,delivery.launch(),delivery.live_rows(delivery.launch()['launch_id'])[0][1])
    assert actual[-3:]==['exec','resume',sid]
    proxy_args=json.loads(next(value.split('=',1)[1] for value in actual if value.startswith('mcp_servers.orrery-mail.args=')))
    assert proxy_args[-3:]==['--context',str(target.path),'proxy']
    from agentstack_codex_app.wake import codex_provider_home, _resume_environment
    from agentstack_codex_app.daemon import BridgeConfig
    legacy=BridgeConfig.from_env({**os.environ,'AGENTSTACK_PROJECT_KEY':str(template.isolation),'AGENTSTACK_MCP_URL':template.endpoint})
    assert bridge.config.codex_sessions_root==legacy.codex_sessions_root==codex_provider_home()/'sessions'
    assert _resume_environment(actual[0])['CODEX_HOME']==str(bridge.config.codex_sessions_root.parent)
    before_transcript=transcript.read_bytes()
    def command(delivery,row,live):
        assert live['session_id']==sid
        return [sys.executable,str(worker),str(marker)]
    monkeypatch.setattr(bridge,'resume_command',command)
    bridge._history[key].clear()
    try:
        bridge.wake_tick()
        assert len(bridge._inflight)==1
        assert transcript.read_bytes()==before_transcript
        first=next(iter(bridge._inflight.values()))
        bridge.wake_tick()
        assert next(iter(bridge._inflight.values()))[0] is first[0]
        assert len(bridge._history[key])==1
        marker.write_text('finish')
        first[0].wait(timeout=10)
        bridge.wake_tick()
        assert delivery.pending_messages(first[2])==[]
        # Refreshing the one live slot retains App lifecycle state.
        row=delivery.launch()
        delivery.session_start({'session_id':sid,'transcript_path':str(transcript),'cwd':str(target.isolation)},
            trusted_receipt=(row['launch_id'],row['receipt_nonce']))
        live=api['read_json'](target.runtime_dir/'live-sessions'/(sid+'.json'))
        assert live['app_state']=='waiting' and live['app_subagent'] is False
    finally:
        bridge.stop()


def test_codex_operator_replan_recovers_fixed_market_and_invalidates_old_approval(server,monkeypatch):
    from types import SimpleNamespace
    client=context(server,'codex-replan',window=False)
    delivery=api['runtime_delivery'](client)
    monkeypatch.setenv('AGENTSTACK_CODEX_BIN','/usr/bin/true')
    trust,builder=delivery.plugin_helpers()
    home=delivery.directory(client.runtime_dir/'provider-home/codex')
    market=home/'marketplace'
    plan_path=home/'hook-plan.json'
    outside=client.isolation/'outside-market-sentinel'
    outside.write_bytes(b'unchanged')
    real_build=builder['build']
    def build(source,destination,name):
        assert destination==home/'marketplace'
        return real_build(source,destination,name)
    phase={'interrupt':True,'tag':'original','trust_calls':0}
    def run(args,**kwargs):
        assert args[0]=='/usr/bin/true' and kwargs['env']['CODEX_HOME']==str(home)
        if phase['interrupt']:
            phase['interrupt']=False
            return SimpleNamespace(returncode=1,stdout='')
        return SimpleNamespace(returncode=0,stdout=json.dumps({
            'installedPath':str(market/'plugins/agentstack-codex-app'),
            'pluginId':'agentstack-codex-app@agentstack-global'}))
    def capture(binary,provider_home,cwd,root,plugin_id):
        return {'schema_version':1,'approved':False,'enable_hooks':True,
            'shared_codex_home':str(provider_home),'cwd':str(cwd),'codex_binary':binary,
            'plugin_root':str(root),'plugin_id':plugin_id,'payload_digest':phase['tag'],
            'declared_hooks':{},'hooks':[]}
    monkeypatch.setattr(delivery,'plugin_helpers',lambda:({**trust,'capture_plan':capture},{**builder,'build':build}))
    monkeypatch.setattr(sys.modules['subprocess'],'run',run)
    with pytest.raises(api['ClientError'],match='CODEX_HOOK_CONFIGURE_FAILED'):
        delivery.configure_codex('plan')
    assert market.is_dir() and not plan_path.exists()
    first=delivery.configure_codex('plan')
    assert first['approved'] is False and delivery.configure_codex('plan')==first
    approval={**first,'approved':True}
    api['atomic_json'](plan_path,approval)
    phase['tag']='changed'
    second=delivery.configure_codex('plan')
    assert second['approved'] is False and second['payload_digest']=='changed'
    with pytest.raises(api['ClientError'],match='OPERATOR_CONFIRMATION_REQUIRED'):
        delivery.configure_codex('apply',approval)
    with pytest.raises(api['ClientError'],match='CODEX_HOOK_CONFIGURE_REQUIRED'):
        delivery.codex_trust()
    assert outside.read_bytes()==b'unchanged'


def test_hint_consumption_during_read_is_not_an_unsafe_file(server,launch,monkeypatch):
    client,delivery,_=launch
    directory=client.runtime/'signals/agents'/str(client.identity['agent_id'])
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=directory/'999.signal'
    api['atomic_json'](path,{'kind':'orrery-global-message-signal-v1','version':1,
        'server_instance_id':client.binding['expected_server_instance_id'],
        'candidate_generation':client.binding['candidate_generation'],'authority_epoch':client.binding['authority_epoch'],
        'recipient_agent_id':client.identity['agent_id'],'message_id':999})
    original=os.open
    def consumed(name,*args,**kwargs):
        fd=original(name,*args,**kwargs)
        if name=='999.signal':path.unlink()
        return fd
    monkeypatch.setattr(os,'open',consumed)
    assert delivery.hints()==[]
    monkeypatch.setattr(os,'open',original)
    api['atomic_json'](path,{'not':'a hint'})
    os.link(path,directory/'foreign-hardlink')
    with pytest.raises(api['ClientError'],match='PRIVATE_FILE_UNSAFE'):
        delivery.hints()


@pytest.mark.parametrize('channel',['tmux','codex-app'])
def test_channel_retry_bounds_survive_fresh_consumer_without_admission_attempts(server,launch,channel):
    client,delivery,_=launch
    mid=send(server)
    for _ in range(20):
        assert delivery.prepare(mid,channel,admissible=False)['attempt_id'] is None
    for attempt in range(1,7):
        delivery=api['runtime_delivery'](api['RuntimeClient'](client.path))
        result=delivery.prepare(mid,channel)
        if channel=='codex-app' and attempt==6:
            assert result['state']=='blocked'
            assert delivery.delivery_status(mid)['operator_required']
            break
        assert result['state']=='handoff'
        status=delivery.delivery_status(mid)
        assert status['attempt_count']==attempt
        assert result['duplicate_possible']==(attempt>1)
        assert delivery.prepare(mid,channel)['state']=='retry-wait'
        with delivery.message(mid) as path:
            row=delivery.local(path,'delivery_intent')
            row.update(next_attempt_at='2000-01-01T00:00:00+00:00',attempt_expires_at='2000-01-01T00:00:00+00:00')
            delivery.save(path,row,'delivery_intent')
    assert delivery.pending_messages(mid)


def test_operator_retry_keeps_duplicate_warning_and_evidence_limit(server,launch):
    _,delivery,_=launch
    mid=send(server);delivery.prepare(mid,'tmux')
    current=delivery.delivery_status(mid)
    request={'message_id':mid,'expected_state_sha256':current['state_sha256'],'confirm':True,
        'resolution':{'action':'retry-at-least-once','operator_evidence':'x'*4097}}
    with pytest.raises(api['ClientError'],match='ARGUMENTS_UNSUPPORTED'):
        delivery.operator('resolve-delivery',request)
    assert delivery.delivery_status(mid)['state_sha256']==current['state_sha256']
    request['resolution']['operator_evidence']='x'*4096
    delivery.operator('resolve-delivery',request)
    assert delivery.prepare(mid,'tmux')['duplicate_possible']


def test_codex_hook_flush_failure_keeps_pending_for_at_least_once_retry(server,launch,monkeypatch):
    import io,time
    sys.path.insert(0,str(ROOT/'integrations/codex_app/src'))
    from agentstack_codex_app import hook_entry,global_runtime
    client,delivery,_=launch
    mid=send(server)
    deadline=time.monotonic()+10
    while mid not in delivery.hints():
        assert time.monotonic()<deadline
        time.sleep(.03)
    monkeypatch.setattr(global_runtime,'selected_client',lambda:(api,client))
    monkeypatch.setattr(sys,'stdin',io.TextIOWrapper(io.BytesIO(json.dumps({'hook_event_name':'PostToolUse'}).encode())))
    class BrokenOutput(io.StringIO):
        def flush(self):raise BrokenPipeError('fixture disconnected')
    monkeypatch.setattr(sys,'stdout',BrokenOutput())
    assert hook_entry.main()==0
    status=delivery.delivery_status(mid)
    assert status['phase']=='external-unknown' and status['attempt_count']==1
    assert delivery.pending_messages(mid)


def test_actual_stdio_proxy_requires_receipt_and_never_accepts_caller_identity(server,launch):
    import subprocess
    client,delivery,row=launch
    delivery.session_start({'session_id':'stdio-fixture','cwd':str(client.isolation)},
        trusted_receipt=(row['launch_id'],row['receipt_nonce']))
    requests=[{'id':1,'method':'initialize'},
        {'id':2,'method':'tools/call','params':{'name':'fetch_inbox','arguments':{}}},
        {'id':3,'method':'tools/call','params':{'name':'bootstrap','arguments':{}}},
        {'id':4,'method':'tools/call','params':{'name':'fetch_inbox','arguments':{'agent_id':1}}},
        {'id':5,'method':'tools/call','params':{'name':'fetch_inbox','arguments':{}}}]
    result=subprocess.run([sys.executable,str(ROOT/'bin/lib/runtime_client.py'),'--context',str(client.path),'proxy'],
        input=''.join(json.dumps(value)+'\n' for value in requests),text=True,capture_output=True,timeout=30,
        env=dict(os.environ,AGENTSTACK_LAUNCH_ID=row['launch_id'],AGENTSTACK_LAUNCH_NONCE=row['receipt_nonce']))
    assert result.returncode==0,result.stderr
    responses=[json.loads(line) for line in result.stdout.splitlines()]
    assert [value['id'] for value in responses]==[1,2,3,4,5]
    assert responses[1]['error']['message']=='BOOTSTRAP_REQUIRED'
    assert json.loads(responses[2]['result']['content'][0]['text'])['runtime_ready']
    assert responses[3]['error']['message']=='ARGUMENTS_UNSUPPORTED'
    inbox=json.loads(responses[4]['result']['content'][0]['text'])
    assert isinstance(inbox,list) and inbox
    assert all(any(peer['agent_id']==client.identity['agent_id'] for key in ('to','cc','bcc') for peer in message[key]) for message in inbox)
    assert api['read_json'](client.credential)['registration_token'] not in result.stdout+result.stderr


@pytest.mark.parametrize('context_kind',['absent','explicit','implicit','broken'])
def test_app_partial_install_uses_existing_loader_admission(tmp_path,monkeypatch,context_kind):
    import runpy,shutil
    root=tmp_path/'installed'
    module=root/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py'
    module.parent.mkdir(parents=True)
    shutil.copy2(ROOT/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py',module)
    library=root/'bin/lib';library.mkdir(parents=True)
    shutil.copy2(ROOT/'bin/lib/global-client-loader.sh',library/'global-client-loader.sh')
    monkeypatch.setenv('AGENTSTACK_HOME',str(root))
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    if context_kind=='explicit':monkeypatch.setenv('AGENTSTACK_CLIENT_CONFIG',str(root/'missing-context.json'))
    if context_kind=='implicit':(root/'runtime-client.json').write_text('{}')
    if context_kind=='broken':(root/'runtime-client.json').symlink_to(root/'missing-context.json')
    entry=runpy.run_path(str(module))['selected_client']
    if context_kind=='absent':assert entry() is None
    else:
        with pytest.raises(RuntimeError,match='GLOBAL_ENTRY_UNAVAILABLE'):entry()


@pytest.mark.parametrize('stage',['before-context','after-context','before-clear'])
def test_window_projection_interruption_replays_same_uuid_and_clears_pending(server,monkeypatch,stage):
    client=context(server,'window-interruption',window=False)
    delivery=api['runtime_delivery'](client)
    write=client.write_output;clear=client.clear_mutation
    def interrupted(path,value,role):
        if role=='context' and stage=='before-context':raise OSError('fixture disk failure')
        result=write(path,value,role)
        if role=='context' and stage=='after-context':raise OSError('fixture stopped after context')
        return result
    def no_clear(value):raise OSError('fixture stopped before acknowledgement')
    monkeypatch.setattr(client,'write_output',interrupted)
    if stage=='before-clear':monkeypatch.setattr(client,'clear_mutation',no_clear)
    with pytest.raises(OSError):delivery.bind_window()
    pending=api['read_json'](client.outputs['mutation'])
    receipt=pending['receipt']
    assert receipt and pending['phase']=='committed'
    fresh=api['RuntimeClient'](client.path)
    api['runtime_delivery'](fresh).bind_window()
    assert fresh.identity['window_uuid']==receipt['window_uuid']
    assert fresh.identity['window_row_id']==receipt['window_row_id']
    assert not fresh.outputs['mutation'].exists()
    assert 'window_uuid' not in api['read_json'](fresh.credential)


def test_confirmed_completion_recovers_without_signal_or_second_injection(server,launch,monkeypatch):
    import time
    client,delivery,_=launch
    mid=send(server)
    attempt=delivery.prepare(mid,'tmux')
    path=delivery.consumer_agent()/'messages'/str(mid)/'intent.json'
    original=Path.unlink
    def interrupted(self,*args,**kwargs):
        if self==path:raise OSError('fixture stopped after server commit')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',interrupted)
    with pytest.raises(OSError):delivery.callback(mid,attempt['attempt_id'],confirmed=True)
    monkeypatch.setattr(Path,'unlink',original)
    assert api['read_json'](path)['phase']=='callback-pending'
    assert delivery.pending_messages(mid)==[]
    deadline=time.monotonic()+10
    while mid in delivery.hints():
        assert time.monotonic()<deadline
        time.sleep(.03)
    fresh=api['runtime_delivery'](api['RuntimeClient'](client.path))
    # A normal worker iteration discovers the existing intent even after the
    # server has removed the hint. No tmux or provider call is needed.
    monkeypatch.setattr(api['delivery_contract']()['RuntimeDelivery'],'tmux_target',lambda self: (_ for _ in ()).throw(AssertionError('second injection')))
    fresh.watch_once()
    assert not path.exists()
    assert fresh.intent_ids()==[]  # completion removes the empty candidate slot
    assert fresh.diagnostics()==[]


def test_child_codex_cli_spelling_selects_codex_without_changing_metadata(server,monkeypatch,capsys):
    parent=context(server,'alias-parent',window=False)
    prepared=parent.prepare_child({'child_client_name':'alias-child','name':'AliasChild',
        'program':'codex-cli','model':'fixture-model','task_description':'fixture','prepare_only':True})
    target=api['RuntimeClient'](Path(prepared['client_config']))
    before=target.registration_metadata()
    observed=[]
    def start(self,provider,cwd,**kwargs):
        observed.append((provider,cwd,kwargs))
        return {'ok':True,'runtime_ready':False,'prepared':True}
    monkeypatch.setattr(api['delivery_contract']()['RuntimeDelivery'],'start',start)
    assert api['entry_operation']('spawn-child',['--context',str(parent.path),'--child-client-name','alias-child'])==0
    assert observed[0][0]=='codex'
    assert api['RuntimeClient'](target.path).registration_metadata()==before
    assert before['program']=='codex-cli'


def test_parallel_consumers_share_one_handoff_slot(server, launch):
    import concurrent.futures
    import threading
    client, delivery, _ = launch
    mid = send(server)
    consumers = [api['runtime_delivery'](api['RuntimeClient'](client.path)) for _ in range(2)]
    barrier = threading.Barrier(2)
    def prepare(consumer):
        barrier.wait(timeout=5)
        return consumer.prepare(mid, 'tmux')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, consumers))
    assert sorted(result['state'] for result in results) == ['handoff', 'retry-wait']
    assert delivery.delivery_status(mid)['attempt_count'] == 1
    handoff = next(result for result in results if result['state'] == 'handoff')
    assert delivery.callback(mid, handoff['attempt_id'], confirmed=True)['state'] == 'complete'
    assert delivery.pending_messages(mid) == []


def test_delivery_status_serializes_current_writer_and_rejects_old_digest(server, launch, monkeypatch):
    import concurrent.futures
    import threading
    client, delivery, _ = launch
    mid = send(server)
    delivery.prepare(mid, 'tmux')
    other = api['RuntimeClient'](client.path)
    writer = api['runtime_delivery'](other)
    captured, release, writing = threading.Event(), threading.Event(), threading.Event()
    original = delivery.state_digest
    def paused(*args):
        value = original(*args)
        captured.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(delivery, 'state_digest', paused)
    def update():
        with other.operation_mutex():
            writing.set()
            row = writer.launch()
            row['error'] = 'synthetic-current-change'
            writer.save(other.outputs['launch'], row, 'launch_intent')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        status = pool.submit(delivery.delivery_status, mid)
        assert captured.wait(5)
        change = pool.submit(update)
        try:
            assert not writing.wait(.15)
        finally:
            release.set()
        first = status.result(timeout=5)
        change.result(timeout=5)
    monkeypatch.setattr(delivery, 'state_digest', original)
    with delivery.message(mid) as path:
        before = path.read_bytes()
    with pytest.raises(api['ClientError'], match='RESOLUTION_DIGEST_MISMATCH'):
        delivery.operator('resolve-delivery', {'message_id': mid,
            'expected_state_sha256': first['state_sha256'], 'confirm': True,
            'resolution': {'action': 'retry-at-least-once', 'operator_evidence': 'state changed'}})
    with delivery.message(mid) as path:
        assert path.read_bytes() == before
    assert delivery.delivery_status(mid)['state_sha256'] != first['state_sha256']


def test_notification_formatter_preserves_legacy_line_markers_and_complete_boundary():
    formatter = api['delivery_contract']()['format_notification']
    message = {'sender': {'name': 'Sender'}, 'subject': 'Topic', 'importance': 'normal',
               'body_md': 'first\nsecond\r\nthird'}
    complete = formatter(message)
    assert 'Body (complete; no inbox fetch needed): first ⏎ second ⏎ third' in complete
    message['body_md'] = 'x' * 2001
    preview = formatter(message)
    assert 'Body preview:' in preview and 'Fetch inbox to read the rest.' in preview
    assert 'x' * 2001 not in preview
    message.pop('body_md')
    assert formatter(message).endswith('Please call fetch_inbox to read it.')


def test_app_respects_shared_execution_switches_before_cold_handoff(launch, monkeypatch):
    sys.path.insert(0, str(ROOT/'integrations/codex_app/src'))
    from agentstack_codex_app.global_bridge import GlobalBridge
    from agentstack_codex_app.daemon import BridgeConfig
    client, delivery, row = launch
    home = delivery.directory(client.runtime_dir/'provider-home/codex')
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('AGENTSTACK_CODEX_APP_COLD_WAKE', '0')
    monkeypatch.setenv('AGENTSTACK_CODEX_BINARY', '/usr/bin/true')
    monkeypatch.setenv('AGENTSTACK_CODEX_APP_PLUGIN_ID', 'fixture-plugin@local')
    monkeypatch.setenv('AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK', '1')
    monkeypatch.delenv('AGENTSTACK_CODEX_BIN', raising=False)
    bridge = GlobalBridge(api, client)
    legacy = BridgeConfig.from_env({**os.environ, 'AGENTSTACK_PROJECT_KEY':str(client.isolation),
                                   'AGENTSTACK_MCP_URL':client.endpoint})
    for field in ('cold_wake_enabled', 'codex_binary', 'plugin_id', 'skip_git_repo_check'):
        assert getattr(bridge.config, field) == getattr(legacy, field)
    sessions = delivery.directory(bridge.config.codex_sessions_root)
    sid = 'fixture-configured-desktop'
    (sessions/('rollout-'+sid+'.jsonl')).write_text(json.dumps({
        'type':'session_meta', 'payload':{'id':sid,'originator':'Codex Desktop'}})+'\n')
    command = bridge.resume_command(delivery, row, {'session_id':sid})
    assert command[0] == '/usr/bin/true'
    assert command[command.index('exec'):] == ['exec','resume','--skip-git-repo-check',sid]
    assert 'plugins."fixture-plugin@local".mcp_servers.agentstack.enabled=false' in command
    def no_admission(*args, **kwargs):
        raise AssertionError('disabled cold wake must not read routes or prepare a handoff')
    monkeypatch.setattr(bridge.delivery, 'consumer_agent', no_admission)
    bridge.wake_tick()
    bridge.config = __import__('dataclasses').replace(bridge.config, cold_wake_enabled=True)
    monkeypatch.setenv('AGENTSTACK_MAIL_DISABLED', '1')
    bridge.wake_tick()

@pytest.mark.parametrize('provider',['claude','codex'])
def test_provider_instruction_configuration_is_explicit_and_stale_launch_is_readonly(server,monkeypatch,provider):
    monkeypatch.setenv('PATH','/usr/bin:/bin')
    monkeypatch.setenv(f'AGENTSTACK_{provider.upper()}_BIN','/usr/bin/true')
    client=unconfigured_context(server,window=False)
    delivery=api['runtime_delivery'](client)
    row=delivery.prepare_launch(provider,cwd=server['root'])
    home=client.runtime_dir/'provider-home'/provider
    name='CLAUDE.md' if provider=='claude' else 'AGENTS.md'
    with pytest.raises(api['ClientError'],match='INSTRUCTION_PROVIDER_CONFIG_REQUIRED'):
        delivery.provider_command(row)
    assert not (home/name).exists()
    home.mkdir(exist_ok=True,parents=True)
    target=home/name;target.write_text('outside note\n')
    delivery.instruction_configure(provider)
    before=target.read_bytes()
    assert before.startswith(b'outside note\n')
    assert not list((client.config['agentstack_home'] and Path(client.config['agentstack_home'])).glob('backups/instruction-migration/*/*/receipt.json'))
    target.write_bytes(before.replace(b'Applicability',b'Edited applicability',1))
    stale=target.read_bytes()
    with pytest.raises(api['ClientError'],match='INSTRUCTION_PROVIDER_CONFIG_REQUIRED'):
        delivery.provider_command(row)
    assert target.read_bytes()==stale
    delivery.instruction_configure(provider)
    assert target.read_bytes()==before
