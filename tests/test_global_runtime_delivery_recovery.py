"""Interrupted launch resolution and isolated, bounded delivery recovery."""
import json
import os
import subprocess
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest
from test_global_runtime_delivery import server, launch, send, api, context, messages, ROOT


def expire(delivery,mid):
    with delivery.message(mid) as path:
        row=delivery.local(path,'delivery_intent')
        row.update(next_attempt_at='2000-01-01T00:00:00+00:00',attempt_expires_at='2000-01-01T00:00:00+00:00')
        delivery.save(path,row,'delivery_intent')


def relaunch(client,delivery,row,root):
    row.update(phase='ended');delivery.save(client.outputs['launch'],row,'launch_intent')
    replacement=delivery.prepare_launch('claude',cwd=root)
    replacement.update(phase='running');delivery.save(client.outputs['launch'],replacement,'launch_intent')
    return replacement


@pytest.fixture
def resolution_provider():
    # A real owned provider makes the live/dead boundary observable outside
    # the sandbox. EOF ends it cooperatively; every failing path also reaps it.
    with subprocess.Popen(
        [sys.executable,'-c',"import sys; print('ready',flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
    ) as provider:
        try:
            assert provider.stdout.readline().strip()=='ready'
            yield provider
        finally:
            if provider.poll() is None:
                provider.terminate()
                try:provider.wait(timeout=5)
                except subprocess.TimeoutExpired:provider.kill();provider.wait(timeout=5)


@pytest.mark.parametrize('saved_lives',[0,1])
def test_ended_resolution_partial_write_requires_same_resolution_before_new_launch(server,launch,monkeypatch,saved_lives,resolution_provider):
    client,delivery,row=launch
    row['phase']='exec-unknown';delivery.save(client.outputs['launch'],row,'launch_intent')
    provider=resolution_provider
    birth=delivery.api['process_start'](provider.pid)
    assert birth not in (None,'absent'), 'requires native process observation; run outside sandbox'
    save=delivery.save
    def fail_running(path,value,schema):
        if path==client.outputs['launch'] and value.get('phase')=='running':raise OSError('fixture running interruption')
        save(path,value,schema)
    monkeypatch.setattr(delivery,'save',fail_running)
    for sid in ('first','second'):
        with pytest.raises(OSError):delivery.session_start({'session_id':sid,'provider_pid':provider.pid,'cwd':str(server['root'])},trusted_receipt=(row['launch_id'],row['receipt_nonce']))
    monkeypatch.setattr(delivery,'save',save)
    resolution={'action':'confirmed-ended','operator_evidence':'fixture process end independently confirmed'}
    status=delivery.status(row['launch_id'])
    live_before={name:live for name,live in delivery.live_rows(row['launch_id'])}
    assert len(live_before)==2
    assert all(live['pid']==provider.pid and live['process_start']==birth for live in live_before.values())
    launch_before=client.outputs['launch'].read_bytes()
    with pytest.raises(api['ClientError'],match='LAUNCH_OUTCOME_UNKNOWN'):
        delivery.operator('launch-resolve',{'launch_id':row['launch_id'],'expected_state_sha256':status['state_sha256'],'confirm':True,'resolution':resolution})
    assert client.outputs['launch'].read_bytes()==launch_before
    assert dict(delivery.live_rows(row['launch_id']))==live_before
    provider.stdin.close()
    assert provider.wait(timeout=5)==0
    assert delivery.api['process_start'](provider.pid)=='absent'
    atomic=delivery.api['atomic_json'];count=0
    def fail_live(path,value,*args,**kwargs):
        nonlocal count
        if Path(path).parent.name=='live-sessions' and value.get('ended'):
            if count==saved_lives:raise OSError('fixture ended interruption')
            count+=1
        return atomic(path,value,*args,**kwargs)
    monkeypatch.setitem(delivery.api,'atomic_json',fail_live)
    status=delivery.status(row['launch_id'])
    with pytest.raises(OSError):delivery.operator('launch-resolve',{'launch_id':row['launch_id'],'expected_state_sha256':status['state_sha256'],'confirm':True,'resolution':resolution})
    monkeypatch.setitem(delivery.api,'atomic_json',atomic)
    fresh=api['runtime_delivery'](api['RuntimeClient'](client.path))
    status=fresh.status(row['launch_id'])
    assert status['operator_required'] and status['next_operations']==['launch-resolve']
    assert status['observation']=='unknown' and not status['runtime_ready']
    before=client.outputs['launch'].read_bytes()
    with pytest.raises(api['ClientError'],match='LAUNCH_PENDING_CONFLICT'):fresh.prepare_launch('claude',cwd=server['root'])
    assert client.outputs['launch'].read_bytes()==before
    completed=fresh.operator('launch-resolve',{'launch_id':row['launch_id'],'expected_state_sha256':status['state_sha256'],'confirm':True,'resolution':resolution})
    assert completed['observation']=='verified-ended' and not completed['operator_required']
    assert all(live['ended'] for _,live in fresh.live_rows(row['launch_id']))
    assert fresh.prepare_launch('claude',cwd=server['root'])['launch_id']!=row['launch_id']


@pytest.mark.parametrize('server_exists',[False,True])
def test_retry_initial_missing_tmux_server_and_missing_session(server,launch,monkeypatch,server_exists):
    import subprocess
    import tempfile
    client,delivery,row=launch
    with tempfile.TemporaryDirectory(prefix='r246tmux-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as temporary:
        monkeypatch.setenv('TMUX_TMPDIR',temporary);monkeypatch.delenv('TMUX',raising=False)
        row['phase']='planned';delivery.save(client.outputs['launch'],row,'launch_intent')
        tmux=api['runtime_tmux_api']()
        dedicated=tmux['global_socket'](Path(client.config['agentstack_home']),client.binding['expected_server_instance_id'],create=True)
        prefix=['tmux','-S',str(dedicated)]
        run=subprocess.run;calls=[]
        def fixture_run(args,**kw):
            if args[:4]==[*prefix,'new-session']:
                calls.append(args)
                args=[*args[:-1],'sleep 60']
            return run(args,**kw)
        try:
            if server_exists:
                # Both controls belong to this fixture. The new client must
                # select the dedicated one without disturbing the old one.
                run(['tmux','new-session','-d','-s','fixture-control','sleep 60'],check=True)
                run([*prefix,'new-session','-d','-s','fixture-control','sleep 60'],check=True)
                dedicated.chmod(0o600)
            monkeypatch.setattr(delivery.operator.__globals__['subprocess'],'run',fixture_run)
            status=delivery.status(row['launch_id'])
            result=delivery.operator('launch-retry',{'launch_id':row['launch_id'],'expected_state_sha256':status['state_sha256']})
            assert result['phase']=='gated' and len(calls)==1
            assert calls[0][:4]==[*prefix,'new-session']
            assert calls[0][calls[0].index('-s')+1]==client.identity['name'] and '-A' not in calls[0]
            assert run([*prefix,'has-session','-t','='+client.identity['name']],capture_output=True).returncode==0
            if server_exists:
                assert run(['tmux','has-session','-t','=fixture-control'],capture_output=True).returncode==0
                assert run([*prefix,'has-session','-t','=fixture-control'],capture_output=True).returncode==0
        finally:
            run([*prefix,'kill-session','-t','='+client.identity['name']],capture_output=True)
            if server_exists:
                run([*prefix,'kill-session','-t','=fixture-control'],capture_output=True)
                run(['tmux','kill-session','-t','=fixture-control'],capture_output=True)


def test_retry_unknown_socket_failure_does_not_exec(launch,monkeypatch):
    import subprocess
    client,delivery,row=launch
    row['phase']='planned';delivery.save(client.outputs['launch'],row,'launch_intent')
    monkeypatch.setattr(delivery.operator.__globals__['subprocess'],'run',lambda args,**kw:subprocess.CompletedProcess(args,1,stderr=b'unknown endpoint'))
    status=delivery.status(row['launch_id'])
    with pytest.raises(api['ClientError'],match='LAUNCH_OUTCOME_UNKNOWN'):
        delivery.operator('launch-retry',{'launch_id':row['launch_id'],'expected_state_sha256':status['state_sha256']})
    assert delivery.launch()['phase']=='gated'


def test_relaunch_old_unknown_does_not_block_hook_or_watcher(server,launch,monkeypatch):
    client,delivery,row=launch;first=send(server)
    prepared=delivery.prepare(first,'claude-hook');delivery.callback(first,prepared['attempt_id'])
    replacement=relaunch(client,delivery,row,server['root']);second=send(server)
    monkeypatch.setattr(delivery,'hints',lambda:[first,second])
    notices=delivery.hook_notifications('claude-hook')
    assert [notice['message_id'] for notice in notices]==[first,second]
    assert notices[0]['duplicate_possible'] and delivery.delivery_status(first)['captured']['launch_id']==replacement['launch_id']
    # A single malformed/unavailable message does not hide the rest of the hook.
    third=send(server);monkeypatch.setattr(delivery,'hints',lambda:[first,third])
    prepare=delivery.prepare
    monkeypatch.setattr(delivery,'prepare',lambda mid,*a,**kw:(_ for _ in ()).throw(OSError('fixture mid failure')) if mid==first else prepare(mid,*a,**kw))
    assert [notice['message_id'] for notice in delivery.hook_notifications('claude-hook')]==[third]
    # The watcher similarly catches errors inside, not outside, its mid loop.
    visited=[]
    cls=type(delivery)
    def deliver(self,mid):
        visited.append(mid)
        if mid==first:raise OSError('fixture watcher failure')
    monkeypatch.setattr(cls,'hints',lambda self:[first,third])
    monkeypatch.setattr(cls,'deliver_tmux',deliver)
    delivery.watch_once()
    assert visited==[first,third]


def test_watcher_invalid_route_directory_does_not_block_other_agent(server,launch,monkeypatch):
    client,delivery,row=launch
    agents=delivery.consumer_agent().parent
    foreign=agents/'999999'
    foreign.mkdir(mode=0o700)
    (foreign/'route.json').write_text('{}')
    foreign.chmod(0o755)
    cls=type(delivery);visited=[]
    monkeypatch.setattr(cls,'hints',lambda self:[123])
    monkeypatch.setattr(cls,'deliver_tmux',lambda self,mid:visited.append((self.client.identity['agent_id'],mid)))
    delivery.watch_once()
    assert visited==[(client.identity['agent_id'],123)]
    assert delivery._watch_errors['999999']=='PRIVATE_DIRECTORY_REQUIRED'


def test_changed_owner_blocked_and_explicit_rebind_or_abandon_recovers(server,launch):
    client,delivery,row=launch;mid=send(server)
    first=delivery.prepare(mid,'tmux')
    with delivery.message(mid) as path:
        saved=delivery.local(path,'delivery_intent');saved['credential_generation']+=1
        delivery.save(path,saved,'delivery_intent')
    assert delivery.prepare(mid,'tmux')['state']=='blocked'
    status=delivery.delivery_status(mid)
    result=delivery.operator('resolve-delivery',{'message_id':mid,'expected_state_sha256':status['state_sha256'],'confirm':True,
        'resolution':{'action':'retry-at-least-once','operator_evidence':'current owner verified'}})
    assert result['captured']['credential_generation']==client.identity['credential_generation']
    assert delivery.prepare(mid,'tmux')['state']=='handoff'
    status=delivery.delivery_status(mid)
    delivery.operator('resolve-delivery',{'message_id':mid,'expected_state_sha256':status['state_sha256'],'confirm':True,
        'resolution':{'action':'abandon-without-consuming','operator_evidence':'leave unread for operator'}})
    assert delivery.prepare(mid,'tmux')['state']=='blocked'


def test_confirmed_previous_launch_only_replays_completion(server,launch,monkeypatch):
    client,delivery,row=launch;mid=send(server);first=delivery.prepare(mid,'tmux')
    completion=delivery.completion
    monkeypatch.setattr(delivery,'completion',lambda *a,**kw:(_ for _ in ()).throw(OSError('fixture callback loss')))
    with pytest.raises(OSError):delivery.callback(mid,first['attempt_id'],confirmed=True)
    monkeypatch.setattr(delivery,'completion',completion)
    relaunch(client,delivery,row,server['root'])
    assert delivery.prepare(mid,'tmux')['state']=='noop' and not delivery.pending_messages(mid)


@pytest.mark.parametrize('mode',['waiting','below','exhausted'])
def test_local_admission_skips_twenty_fetches_and_threshold_change_rechecks(server,launch,monkeypatch,mode):
    client,delivery,row=launch;mid=send(server)
    if mode=='below':monkeypatch.setenv('AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE','high')
    first=delivery.prepare(mid,'codex-app' if mode=='exhausted' else 'tmux')
    if mode=='waiting':delivery.callback(mid,first['attempt_id'])
    if mode=='exhausted':
        with delivery.message(mid) as path:
            saved=delivery.local(path,'delivery_intent');saved.update(attempt_count=5,next_attempt_at=None,attempt_expires_at=None)
            delivery.save(path,saved,'delivery_intent')
        assert delivery.prepare(mid,'codex-app')['state']=='blocked'
    calls=[];pending=delivery.pending_messages
    monkeypatch.setattr(delivery,'pending_messages',lambda m:(calls.append(m),pending(m))[1])
    for _ in range(20):assert delivery.prepare(mid,'codex-app' if mode=='exhausted' else 'tmux')['state'] in ('blocked','retry-wait')
    assert calls==[]
    if mode=='below':
        monkeypatch.setenv('AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE','low')
        assert delivery.prepare(mid,'tmux')['state']=='handoff' and calls==[mid]


def test_unknown_duplicate_warning_is_monotonic(server,launch):
    client,delivery,row=launch;mid=send(server)
    first=delivery.prepare(mid,'tmux');delivery.callback(mid,first['attempt_id']);expire(delivery,mid)
    second=delivery.prepare(mid,'tmux');delivery.callback(mid,second['attempt_id'],known_failure=True)
    assert delivery.delivery_status(mid)['duplicate_possible']


def test_native_config_regenerated_after_interpreter_and_wrapper_change(launch,monkeypatch):
    client,delivery,row=launch;delivery.provider_command(row)
    config=client.runtime_dir/'provider-home/claude/mcp.json'
    settings=config.with_name('orrery-hooks.json')
    before=config.read_bytes()
    monkeypatch.setattr(delivery.provider_command.__globals__['sys'],'executable','/fixture/updated/python')
    monkeypatch.setitem(delivery.provider_command.__globals__,'__file__','/fixture/updated/bin/lib/runtime_delivery.py')
    delivery.provider_command(row)
    value=api['read_json'](config)
    assert value['mcpServers']['orrery-mail']['command']=='/fixture/updated/python' and config.read_bytes()!=before
    assert value['mcpServers']['orrery-mail']['args'][0]=='/fixture/updated/bin/lib/runtime_client.py'
    assert '/fixture/updated/python' in api['read_json'](settings)['hooks']['SessionStart'][0]['hooks'][0]['command']
    assert config.stat().st_mode&0o777==0o600


def test_app_timing_is_same_existing_reader_and_defaults(launch,monkeypatch):
    sys.path.insert(0,str(ROOT/'integrations/codex_app/src'))
    from agentstack_codex_app.daemon import BridgeConfig
    client,delivery,row=launch
    values={'POLL_SECONDS':'.5','COALESCE_SECONDS':'3','LEASE_SECONDS':'120','BASE_BACKOFF_SECONDS':'4',
            'MAX_BACKOFF_SECONDS':'64','MAX_ATTEMPTS':'7','LIMIT_PER_HOUR':'8','TIMEOUT_SECONDS':'240'}
    for name,value in values.items():monkeypatch.setenv('AGENTSTACK_CODEX_APP_WAKE_'+name,value)
    legacy=BridgeConfig.from_env({**os.environ,'AGENTSTACK_PROJECT_KEY':str(client.isolation),'AGENTSTACK_MCP_URL':client.endpoint})
    policy=delivery.prepare.__globals__['policy']('codex-app')
    assert (policy['poll'],policy['coalesce'],policy['lease'],policy['max_attempts'],policy['wakes_per_hour'],policy['process_timeout'])==(legacy.wake_poll_seconds,legacy.wake_coalesce_seconds,legacy.wake_lease_seconds,legacy.wake_max_attempts,legacy.wake_limit_per_hour,legacy.wake_timeout_seconds)
    assert [policy['backoff'](n) for n in (1,2,5,10)]==[4,8,64,64]


@pytest.mark.parametrize('events',[False,True])
def test_hint_delivery_reacts_to_event_or_two_second_poll(launch,monkeypatch,events):
    client,delivery,row=launch;stop=threading.Event();seen=[]
    module=delivery.watch.__globals__;actual_popen=module['subprocess'].Popen
    script=client.isolation/'fixture-fswatch.py'
    script.write_text('import time,sys\ntime.sleep(.2)\nsys.stdout.buffer.write(b"hint\\0");sys.stdout.buffer.flush()\ntime.sleep(10)\n')
    monkeypatch.setattr(module['shutil'],'which',lambda cmd:sys.executable if cmd=='fswatch' and events else None)
    if events:monkeypatch.setattr(module['subprocess'],'Popen',lambda args,**kw:actual_popen([sys.executable,str(script)],**kw))
    started=time.monotonic()
    def scan():
        seen.append(time.monotonic()-started)
        if len(seen)==2:stop.set()
    monkeypatch.setattr(delivery,'watch_once',scan)
    delivery.watch(stop)
    assert len(seen)==2
    assert seen[1] < (1.5 if events else 4)
    if not events:assert seen[1]>=2


def app_fixture(server,monkeypatch,count=1):
    import asyncio
    sys.path.insert(0,str(ROOT/'integrations/codex_app/src'))
    from agentstack_codex_app.global_bridge import GlobalBridge
    template=context(server,'recovery-app',window=False)
    monkeypatch.setenv('AGENTSTACK_CODEX_BIN','/usr/bin/true')
    monkeypatch.setenv('CODEX_HOME',str(template.runtime_dir/'provider-home/codex'))
    bridge=GlobalBridge(api,template)
    root=bridge.delivery.directory(bridge.config.codex_sessions_root)
    targets=[]
    for n in range(count):
        sid='recovery-desktop-'+str(n)
        (root/('rollout-'+sid+'.jsonl')).write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'originator':'Codex Desktop'}})+'\n')
        bridge.process_event({'schema_version':1,'session_id':sid,'agent_id':None,'cwd':str(template.isolation),
            'model':'fixture','hook_event_name':'SessionStart','turn_id':None})
        target=bridge.delivery.external_client(sid)
        asyncio.run(messages.http(server,'request_contact',messages.owner(server,1,request_id=str(uuid.uuid4()),to_agent_id=target.identity['agent_id'])))
        target.call('respond_contact',{'from_agent_id':1,'accept':True})
        mid=asyncio.run(messages.http(server,'send_message',{**messages.send(server),'to_agent_ids':[target.identity['agent_id']]}))['message_id']
        delivery=api['runtime_delivery'](target);key=bridge.delivery.external_route(sid).parent.name
        deadline=time.monotonic()+10
        while mid not in delivery.hints():
            assert time.monotonic()<deadline
            time.sleep(.02)
        bridge._seen[(key,mid)]=time.monotonic()-10
        targets.append((key,target,delivery,mid))
    return bridge,targets


@pytest.mark.parametrize('waiting',[False,True])
def test_app_tick_fetches_once_or_zero_when_waiting_and_prunes_seen(server,monkeypatch,waiting):
    bridge,targets=app_fixture(server,monkeypatch)
    key,target,delivery,mid=targets[0]
    if waiting:delivery.prepare(mid,'codex-app')
    calls=[];cls=type(delivery);pending=cls.pending_messages
    def counted(self,which=None):
        if self.client.identity['agent_id']==target.identity['agent_id']:calls.append(which)
        return pending(self,which)
    monkeypatch.setattr(cls,'pending_messages',counted)
    monkeypatch.setattr(bridge,'resume_command',lambda *a:[sys.executable,'-c','import time;time.sleep(20)'])
    bridge._seen[(key,99999)]=time.monotonic()-10
    try:
        bridge.wake_tick()
        assert calls==([] if waiting else [mid])
        assert (key,99999) not in bridge._seen
        if not waiting:assert len(bridge._inflight)==1
    finally:bridge.stop()


def test_app_tick_prunes_seen_when_route_directory_disappears(server,monkeypatch):
    bridge,targets=app_fixture(server,monkeypatch)
    root=bridge.delivery.consumer_agent().parent.parent/'external'
    moved=root.with_name('external-fixture-hidden')
    root.rename(moved)
    try:
        assert bridge._seen
        bridge.wake_tick()
        assert bridge._seen=={}
    finally:
        moved.rename(root)
        bridge.stop()


@pytest.mark.parametrize('failure',['root','mid'])
def test_app_poisoned_root_or_mid_does_not_block_other_delivery(server,monkeypatch,failure):
    import asyncio
    bridge,targets=app_fixture(server,monkeypatch,2 if failure=='root' else 1)
    key,target,delivery,mid=targets[0]
    if failure=='root':
        run=bridge.wake_root
        def bad(folder,*args):
            if folder.name==key:raise api['ClientError']('fixture root unavailable')
            return run(folder,*args)
        monkeypatch.setattr(bridge,'wake_root',bad)
        expected=targets[1][3]
    else:
        expected=asyncio.run(messages.http(server,'send_message',{**messages.send(server),'to_agent_ids':[target.identity['agent_id']]}))['message_id']
        deadline=time.monotonic()+10
        while expected not in delivery.hints():
            assert time.monotonic()<deadline;time.sleep(.02)
        bridge._seen[(key,expected)]=time.monotonic()-10
        cls=type(delivery);prepare=cls.prepare
        def bad(self,which,*args,**kw):
            if which==mid:raise api['ClientError']('fixture mid unavailable')
            return prepare(self,which,*args,**kw)
        monkeypatch.setattr(cls,'prepare',bad)
    monkeypatch.setattr(bridge,'resume_command',lambda *a:[sys.executable,'-c','import time;time.sleep(20)'])
    try:
        bridge.wake_tick()
        assert [entry[2] for entry in bridge._inflight.values()]==[expected]
    finally:bridge.stop()


def test_documented_operator_json_matches_source_schema(launch):
    import re
    delivery=launch[1]
    fixture,validator=delivery.operator.__globals__['contract']()
    for name in ('global-delivery-client.md','global-delivery-client.en.md'):
        text=(ROOT/'docs'/name).read_text()
        samples=re.findall(r'operator-json ([a-z-]+) <<EOF_JSON[^\n]*\n(\{[^\n]+\})',text)
        assert {operation for operation,_ in samples}=={'launch-status','launch-resolve','launch-cancel','launch-retry','delivery-status'}
        for operation,body in samples:
            body=body.replace('$launch_id',str(uuid.uuid4())).replace('$state_sha256','a'*64)
            validator(json.loads(body),fixture['local_operator_wire']['operations'][operation]['request_schema'],'ARGUMENTS_UNSUPPORTED',error_type=api['ClientError'])


def test_hook_callback_failure_keeps_exit_zero_and_completes_other_message(server,launch,monkeypatch,capsys):
    import io
    client,delivery,row=launch
    first,second=send(server),send(server)
    monkeypatch.setattr(type(delivery),'hints',lambda self:[first,second])
    cls=type(delivery);completion=cls.completion
    def unavailable(self,path,intent,**kw):
        if intent['message_id']==first:raise OSError('fixture callback unavailable')
        return completion(self,path,intent,**kw)
    monkeypatch.setattr(cls,'completion',unavailable)
    monkeypatch.setattr(sys,'stdin',io.StringIO('{}'))
    assert api['entry_operation']('notifications',['--context',str(client.path)])==0
    output=capsys.readouterr()
    assert json.loads(output.out)['hookSpecificOutput']['additionalContext'].count('ORRERY Mail notification:')==2
    assert output.err.count('global-notification ')==1
    assert delivery.pending_messages(first) and not delivery.pending_messages(second)
    assert delivery.delivery_status(first)['phase']=='external-confirmed'
    monkeypatch.setattr(cls,'completion',completion)
    delivery.reconcile_confirmed()
    assert not delivery.pending_messages(first)


@pytest.mark.skipif(__import__('shutil').which('fswatch') is None,reason='actual filesystem events require fswatch')
def test_real_hint_event_reaches_consumer_before_rescue_scan(server,launch,monkeypatch):
    client,delivery,row=launch
    stop=threading.Event();received=[];errors=[]
    def consume(self,mid):
        received.append((mid,time.monotonic()));stop.set()
    monkeypatch.setattr(type(delivery),'deliver_tmux',consume)
    module=delivery.watch.__globals__;reader=module['legacy_policy']
    monkeypatch.setitem(module,'legacy_policy',lambda:{**reader(),'scan':30})
    def run():
        try:delivery.watch(stop)
        except Exception as exc:errors.append(exc)
    worker=threading.Thread(target=run);worker.start()
    try:
        time.sleep(.2)
        started=time.monotonic();mid=send(server)
        worker.join(timeout=5)
        assert not worker.is_alive() and not errors
        assert received and received[0][0]==mid and received[0][1]-started<5
    finally:
        stop.set()
        # Wake a blocked select even on the assertion failure path.
        (client.runtime/'signals/fixture-stop').write_text('wake')
        worker.join(timeout=35)
        assert not worker.is_alive()
