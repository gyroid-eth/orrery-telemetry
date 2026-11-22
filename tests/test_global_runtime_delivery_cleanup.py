"""Per-message admission, policy cleanup and provider-owned settings."""
import asyncio
import json
import os
import subprocess
import threading
import time

import pytest
from test_global_runtime_delivery import server, launch, send, api, messages, ROOT
from test_global_runtime_delivery_recovery import app_fixture


@pytest.mark.parametrize('consumer',['hook','watcher'])
def test_unsafe_intent_directory_does_not_hide_other_message(server,launch,monkeypatch,consumer):
    client,delivery,row=launch
    bad,good=send(server),send(server)
    delivery.prepare(bad,'tmux')
    directory=delivery.consumer_agent()/'messages'/str(bad)
    directory.chmod(0o755)
    monkeypatch.setattr(type(delivery),'hints',lambda self:[bad,good])
    delivered=[]
    def complete(self,mid):
        item=self.prepare(mid,'tmux')
        if item['state']=='handoff':
            self.callback(mid,item['attempt_id'],confirmed=True);delivered.append(mid)
    monkeypatch.setattr(type(delivery),'deliver_tmux',complete)
    try:
        if consumer=='hook':
            notices=delivery.hook_notifications('claude-hook')
            assert [n['message_id'] for n in notices]==[good]
            delivery.callback(good,notices[0]['attempt_id'],confirmed=True)
        else:
            delivery.watch_once();assert delivered==[good]
        warnings=delivery.diagnostics()
        assert any(w['message_id']==bad and w['reason']=='PRIVATE_DIRECTORY_REQUIRED' for w in warnings)
        assert all(w.get('message_id')!=good for w in warnings)
    finally:directory.chmod(0o700)


def test_app_unsafe_intent_directory_does_not_hide_other_message(server,monkeypatch):
    bridge,targets=app_fixture(server,monkeypatch)
    key,target,delivery,bad=targets[0]
    good=asyncio.run(messages.http(server,'send_message',{**messages.send(server),'to_agent_ids':[target.identity['agent_id']]}))['message_id']
    deadline=time.monotonic()+10
    while good not in delivery.hints():
        assert time.monotonic()<deadline;time.sleep(.02)
    delivery.prepare(bad,'codex-app')
    directory=delivery.consumer_agent()/'messages'/str(bad);directory.chmod(0o755)
    bridge._seen[(key,good)]=time.monotonic()-10
    monkeypatch.setattr(bridge,'resume_command',lambda *a:['/usr/bin/true'])
    try:
        bridge.wake_tick()
        assert [entry[2] for entry in bridge._inflight.values()]==[good]
        assert any(w['message_id']==bad for w in delivery.diagnostics())
    finally:directory.chmod(0o700);bridge.stop()


@pytest.mark.parametrize('reason',['BELOW_MIN_IMPORTANCE','SELF_MESSAGE'])
def test_policy_blocked_is_not_operator_incident_and_disappears_after_read(server,launch,monkeypatch,reason):
    client,delivery,row=launch
    if reason=='SELF_MESSAGE':
        mid=client.call('send_message',{'to_agent_ids':[client.identity['agent_id']],'subject':'self','body_md':'fixture'})['message_id']
    else:
        mid=send(server)
        monkeypatch.setenv('AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE','urgent')
    assert delivery.prepare(mid,'claude-hook',exclude_self=True)['state']=='blocked'
    with delivery.message(mid) as path:
        assert delivery.local(path,'delivery_intent')['error']==reason
    assert not delivery.delivery_status(mid)['operator_required']
    assert delivery.diagnostics()==[]
    delivery.reconcile_confirmed();assert path.exists()
    client.call('mark_message_read',{'message_id':mid})
    deadline=time.monotonic()+10
    while mid in delivery.hints():
        assert time.monotonic()<deadline;time.sleep(.02)
    delivery.reconcile_confirmed()
    assert not path.exists() and delivery.diagnostics()==[]


def test_claude_native_settings_survive_hook_regeneration(launch):
    client,delivery,row=launch
    args=delivery.provider_command(row)
    home=client.runtime_dir/'provider-home/claude'
    settings=home/'settings.json';value={'model':'fixture-model','permissions':{'allow':['Read']}}
    api['atomic_json'](settings,value);before=settings.read_bytes()
    args=delivery.provider_command(row)
    assert settings.read_bytes()==before
    assert args[args.index('--settings')+1]==str(home/'orrery-hooks.json')
    assert 'SessionStart' in api['read_json'](home/'orrery-hooks.json')['hooks']


def test_gemini_native_config_preserves_other_entries_and_rejects_invalid_json(launch,monkeypatch):
    client,delivery,row=launch;row={**row,'provider':'gemini'}
    monkeypatch.setenv('AGENTSTACK_GEMINI_BIN','/usr/bin/true')
    delivery.provider_command(row)
    config=client.runtime_dir/'provider-home/gemini/.gemini/config/mcp_config.json'
    expected=api['read_json'](config)['mcpServers']['orrery-mail']
    value={'preferences':{'model':'fixture'},'mcpServers':{'other':{'command':'fixture-tool'},'orrery-mail':{'command':'stale'}}}
    api['atomic_json'](config,value)
    delivery.provider_command(row)
    saved=api['read_json'](config)
    assert saved['preferences']==value['preferences'] and saved['mcpServers']['other']==value['mcpServers']['other']
    assert saved['mcpServers']['orrery-mail']==expected
    for raw in (b'{invalid',b'[]',b'{"mcpServers":[]}'):
        config.write_bytes(raw)
        with pytest.raises(api['ClientError'],match='GENERATED_CONFIG_JSON_INVALID'):delivery.provider_command(row)
        assert config.read_bytes()==raw


@pytest.mark.parametrize('value',['0','1','300','301','nonnumeric'])
def test_legacy_numeric_policy_matches_actual_shell_validation(launch,monkeypatch,value):
    delivery=launch[1];source=(ROOT/'hooks/watch_agent_mail_signals.sh').read_text()
    # Execute only the existing numeric declarations, not watcher startup.
    scan=source[source.index('case "$(uname -s'):source.index('RETRY_COOLDOWN=')]
    timeout=source[source.index('HEADLESS_REPLY_TIMEOUT='):source.index('LEASE_TTL=')].split('# A worker retains')[0]
    lease=next(line for line in source.splitlines() if line.startswith('LEASE_TTL='))
    monkeypatch.setenv('AGENTSTACK_HEADLESS_REPLY_TIMEOUT',value)
    monkeypatch.setenv('AGENTSTACK_MAIL_WATCHER_SCAN_INTERVAL',value)
    result=subprocess.run(['/bin/bash','-c',scan+'\n'+timeout+'\n'+lease+'\nprintf "%s %s %s" "$SCAN_INTERVAL" "$HEADLESS_REPLY_TIMEOUT" "$LEASE_TTL"'],env=os.environ,capture_output=True,text=True,check=True)
    policy=delivery.watch.__globals__['legacy_policy']()
    assert tuple(map(int,result.stdout.split()))==(policy['scan'],policy['process_timeout'],policy['lease'])


def test_missing_signals_directory_waits_without_creating_it_then_delivers(launch,monkeypatch):
    client,delivery,row=launch
    # The running fixture server must not recreate the tested absent directory.
    runtime=client.isolation/'not-yet-running-server';runtime.mkdir(mode=0o700)
    monkeypatch.setattr(client,'runtime',runtime)
    directory=runtime/'signals'
    stop=threading.Event();seen=[]
    monkeypatch.setattr(delivery.watch.__globals__['shutil'],'which',lambda _:None)
    def scan():seen.append(True);stop.set()
    monkeypatch.setattr(delivery,'watch_once',scan)
    errors=[]
    def run():
        try:delivery.watch(stop)
        except Exception as exc:errors.append(exc)
    worker=threading.Thread(target=run);worker.start()
    try:
        time.sleep(.15)
        assert worker.is_alive() and not directory.exists() and seen==[] and errors==[]
        directory.mkdir(mode=0o700)
        worker.join(timeout=4)
        assert not worker.is_alive() and seen==[True] and errors==[]
    finally:stop.set();worker.join(timeout=4)
