"""Config-only watcher shares actual HTTP completion and existing route truth."""
import asyncio
import json
import os
from pathlib import Path
import time

import pytest

from test_global_runtime_delivery import server, launch, api, messages, send, context


def wait_hint(delivery, mid):
    deadline=time.monotonic()+10
    while mid not in delivery.hints():
        assert time.monotonic()<deadline
        time.sleep(.03)


def test_config_only_coordinator_completes_two_agents_and_skips_bad_route(server, launch, monkeypatch):
    first, first_delivery, _ = launch
    second=context(server, 'second', identity=False, window=False)
    second.create('SecondOwner','claude-code','fixture')
    second_delivery=api['runtime_delivery'](second)
    row=second_delivery.prepare_launch('claude',cwd=server['root'])
    second_delivery.bind_window()
    row.update(phase='running')
    second_delivery.save(second.outputs['launch'],row,'launch_intent')
    second_delivery.publish_route()
    # Completion uses the same S2b management callback; only terminal handoff is
    # replaced with a deterministic confirmation, no real provider dependency.
    mids=[send(server)]
    request=messages.send(server); request['to_agent_ids']=[second.identity['agent_id']]
    mids.append(asyncio.run(messages.http(server,'send_message',request))['message_id'])
    wait_hint(first_delivery,mids[0]);wait_hint(second_delivery,mids[1])
    agents=first_delivery.consumer_agent().parent
    bad=agents/'999999';bad.mkdir(mode=0o700);(bad/'route.json').write_text('{}');bad.chmod(0o755)
    visited=[]
    delivery_class=api['delivery_contract']()['RuntimeDelivery']
    def confirm(delivery, mid):
        attempt=delivery.prepare(mid,'tmux')
        if attempt['state']=='handoff':
            delivery.callback(mid,attempt['attempt_id'],confirmed=True)
            visited.append((delivery.client.identity['agent_id'],mid))
    monkeypatch.setattr(delivery_class,'deliver_tmux',confirm)
    # No context, no owner credential for coordinator construction.
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    api['delivery_contract']()['watch_service'](server['config_path'],api,once=True)
    assert sorted(visited)==sorted([(2,mids[0]),(second.identity['agent_id'],mids[1])])
    assert first_delivery.pending_messages(mids[0])==[]
    assert second_delivery.pending_messages(mids[1])==[]
    assert not (Path(server['root'])/'must-not-open-legacy.sqlite3').exists()


def test_config_only_zero_routes_is_healthy_without_creating_client_or_consumer(server, monkeypatch):
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    api['delivery_contract']()['watch_service'](server['config_path'],api,once=True)
    assert not (server['root']/'agentstack/clients').exists()
    assert not (server['root']/'agentstack/global-consumer').exists()


def test_config_only_shared_binding_failure_is_not_a_legacy_fallback(server):
    value=json.loads(server['config_path'].read_text());value['authority_epoch']='wrong-epoch'
    server['config_path'].write_text(json.dumps(value))
    with pytest.raises(api['ClientError']):
        api['delivery_contract']()['watch_service'](server['config_path'],api,once=True)


def test_contextless_operator_cli_uses_actual_management_socket(server, monkeypatch, capsys):
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','global')
    monkeypatch.setenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',str(server['config_path']))
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    monkeypatch.delenv('AGENT_NAME',raising=False)
    monkeypatch.delenv('AGENTSTACK_SESSION_ID',raising=False)
    assert api['main'](['service-health']) == 0
    assert api['main'](['operator-enroll','inspect','--agent-id','2']) == 0
    reply=json.loads(capsys.readouterr().out)
    assert reply['ok'] is True and reply['agent_id']==2
    assert not (server['root']/'agentstack/clients').exists()


@pytest.mark.parametrize('previous', [None, 'coerce'])
def test_service_dispatch_uses_global_server_without_legacy_foreground(server, monkeypatch, previous):
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','global')
    monkeypatch.setenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',str(server['config_path']))
    monkeypatch.setenv('AGENTSTACK_MCP_URL','http://127.0.0.1:19876/mcp')
    monkeypatch.delenv('AGENT_NAME',raising=False)
    monkeypatch.delenv('AGENTSTACK_SESSION_ID',raising=False)
    key='AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE'
    if previous is None:monkeypatch.delenv(key, raising=False)
    else:monkeypatch.setenv(key, previous)
    before=dict(os.environ)
    class Executed(Exception): pass
    def execute(path, argv, env):
        assert path=='/fixture/server'
        assert argv==['/fixture/server','--global-config',str(server['config_path']),
                      '--host','127.0.0.1','--port','19876','--path','/mcp']
        assert env=={**before, key:'passthrough'}
        raise Executed
    monkeypatch.setattr(os,'execve',execute)
    with pytest.raises(Executed):
        api['main'](['service-exec','/must-not-execute-legacy-service','foreground',
                     '--server-executable','/fixture/server'])

    assert dict(os.environ)==before


def test_service_dispatch_legacy_preserves_exact_argv(monkeypatch):
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','legacy')
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    class Executed(Exception): pass
    args=['/fixture/service','foreground','--env-file','/fixture/env file']
    def execute(path, argv):
        assert path==args[0] and argv==args
        raise Executed
    monkeypatch.setattr(os,'execv',execute)
    with pytest.raises(Executed):api['main'](['service-exec',*args])


def test_service_dispatch_reaches_actual_http_health_without_legacy_database(monkeypatch):
    """Reuse the server fixture's HTTP readiness and cleanup through service-exec."""
    import shlex
    import subprocess
    import sys
    from test_global_runtime_delivery import ROOT, s2
    prep=s2.prepared.__wrapped__(monkeypatch);state=next(prep)
    facade=state['root']/'mail-server'
    facade.write_text('#!/bin/sh\nexec '+shlex.join([sys.executable,'-m','agentstack_mail.cli'])+' "$@"\n')
    facade.chmod(0o700)
    original=subprocess.Popen;calls=[]
    def start(argv, **kwargs):
        if argv[:3]==[sys.executable,'-m','agentstack_mail.cli']:
            calls.append(argv)
            port=argv[argv.index('--port')+1]
            env=dict(kwargs['env'])
            env.pop('AGENT_NAME',None)  # service has no session owner
            env.update(AGENTSTACK_RUNTIME_MODE='global',
                       AGENTSTACK_GLOBAL_SERVER_CONFIG=str(state['config_path']),
                       AGENTSTACK_MCP_URL='http://127.0.0.1:'+port+'/mcp',
                       AGENTSTACK_MAIL_DB=str(state['root']/'never-legacy.sqlite3'))
            kwargs['env']=env
            argv=[sys.executable,str(ROOT/'bin/lib/runtime_client.py'),'service-exec',
                  '/must-not-call-legacy','foreground','--server-executable',str(facade)]
        return original(argv,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',start)
    service=s2.server.__wrapped__(state)
    try:
        next(service)
        health=asyncio.run(messages.http(state,'health_check',{}))
        assert health['status']=='ok' and health['runtime_profile']=='s2c-v1'
        assert len(calls)==1
        assert not (state['root']/'never-legacy.sqlite3').exists()
    finally:
        service.close();prep.close()


def test_app_adapter_uses_common_selection_for_legacy_and_actual_global(server,monkeypatch):
    import runpy
    from test_global_runtime_delivery import ROOT
    entry=runpy.run_path(str(ROOT/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py'))['selected_client']
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','legacy')
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    assert entry() is None
    client=context(server,window=False)
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','global')
    monkeypatch.setenv('AGENTSTACK_CLIENT_CONFIG',str(client.path))
    selected_api,selected_owner=entry()
    assert selected_owner.observe()['agent_id']==client.identity['agent_id']


def test_legacy_gemini_child_keeps_inherited_mode_when_default_changes(tmp_path):
    import shutil
    import subprocess
    from test_global_runtime_delivery import ROOT
    root=tmp_path/'installed';(root/'hooks').mkdir(parents=True)
    (root/'bin/lib').mkdir(parents=True)
    shutil.copy2(ROOT/'hooks/spawn_gemini_child.sh',root/'hooks/spawn_gemini_child.sh')
    for helper in ('project-context.sh', 'installed-env.py'):
        shutil.copy2(ROOT / 'hooks' / helper, root / 'hooks' / helper)
    shutil.copy2(ROOT/'bin/lib/global-client-loader.sh',root/'bin/lib/global-client-loader.sh')
    # Installed env.sh contains export data only. Observe the final mode in
    # the calling shell rather than adding executable logic to that file.
    (root/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=global\n')
    observe='trap \'printf "%s" "$AGENTSTACK_RUNTIME_MODE" > "$AGENTSTACK_HOME/mode.txt"\' EXIT\n. "$1" --help'
    result=subprocess.run(['/bin/bash','-c',observe,'fixture',str(root/'hooks/spawn_gemini_child.sh')],
        env={**os.environ,'AGENTSTACK_HOME':str(root),'AGENTSTACK_RUNTIME_MODE':'legacy',
             'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-gemini'},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (root/'mode.txt').read_text()=='legacy'


def test_bootstrap_propagates_installed_env_failure_through_shared_settings(tmp_path):
    import subprocess
    from test_global_runtime_delivery import ROOT
    env_file=tmp_path/'env.sh';env_file.write_text('false\n')
    script='. "$1"; . "$2"; ags_client_bootstrap "$3"'
    result=subprocess.run(['/bin/bash','-c',script,'fixture',str(ROOT/'hooks/project-context.sh'),
        str(ROOT/'bin/lib/global-client-loader.sh'),str(env_file)],capture_output=True,text=True)
    assert result.returncode==2 and 'RUNTIME_DEFAULT_UNAVAILABLE' in result.stderr


@pytest.mark.parametrize('outage',[ConnectionRefusedError,FileNotFoundError,TimeoutError])
def test_service_waits_through_transport_outage_at_start_and_tick(server,monkeypatch,outage):
    contract=api['delivery_contract']()
    service=contract['watch_service']; calls=[]; waits=[]
    class Stop:
        def is_set(self):return False
        def wait(self,seconds):waits.append(seconds)
    def read(*args,**kwargs):
        calls.append(1)
        if len(calls) in (1,3):raise outage('fixture outage')
        return {}
    monkeypatch.setitem(api,'operator_exchange',read)
    def loop(directory,received_api,tick,stop):
        assert tick() is False
        assert tick() is None
    monkeypatch.setitem(service.__globals__,'watch_loop',loop)
    service(server['config_path'],api,stop=Stop())
    assert len(calls)==4 and waits==[30]


@pytest.mark.parametrize('reason',['MANAGEMENT_BINDING_MISMATCH','OWNER_REQUIRED','WRITER_FENCED','RESPONSE_INVALID'])
def test_service_auth_or_binding_failure_is_fatal_not_transport_retry(server,monkeypatch,reason):
    service=api['delivery_contract']()['watch_service']
    def read(*args,**kwargs):raise api['ClientError'](reason)
    monkeypatch.setitem(api,'operator_exchange',read)
    with pytest.raises(api['ClientError'],match='^'+reason+'$'):
        service(server['config_path'],api,once=True)


def test_watcher_transport_outage_waits_rescue_interval_without_event_spin(tmp_path,monkeypatch):
    contract=api['delivery_contract']();loop=contract['watch_loop'];waits=[]
    class Stop:
        stopped=False
        def is_set(self):return self.stopped
        def wait(self,seconds):waits.append(seconds);self.stopped=True
    monkeypatch.setattr(loop.__globals__['shutil'],'which',lambda name:None)
    tmp_path.chmod(0o700)
    loop(tmp_path,api,lambda:False,Stop())
    assert waits==[30]


@pytest.mark.parametrize('source',['env','argument','equals'])
def test_watcher_auto_dispatch_uses_selected_config(server,monkeypatch,source):
    config=str(server['config_path']);calls=[]
    monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False)
    monkeypatch.delenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',raising=False)
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','global')
    args=['watch-auto']
    if source=='env':monkeypatch.setenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',config)
    elif source=='argument':args=['--server-config',config,*args]
    else:args=['--server-config='+config,*args]
    monkeypatch.setitem(api['delivery_contract'](),'watch_service',lambda path,namespace:calls.append(str(path)))
    # The operation must not construct an owner client for a config-only watch.
    monkeypatch.setitem(api['main'].__globals__,'configured',lambda *a,**k:pytest.fail('owner construction'))
    assert api['main'](args)==0
    assert calls==[config]


@pytest.mark.parametrize('source',['env','argument','equals'])
def test_actual_watcher_wrapper_forwards_one_auto_entry(server,tmp_path,monkeypatch,source):
    import shutil,subprocess,sys
    from _installed_client_payload import copy_client_library
    from test_global_runtime_delivery import ROOT
    root=tmp_path/'install';(root/'hooks').mkdir(parents=True);(root/'bin/lib').mkdir(parents=True)
    for name in ('runtime_client.py','global-client-loader.sh','agentstack-register.sh'):
        shutil.copyfile(ROOT/'bin/lib'/name,root/'bin/lib'/name)
    copy_client_library(root)
    wrapper=root/'hooks/watch_agent_mail_signals.sh'
    shutil.copyfile(ROOT/'hooks/watch_agent_mail_signals.sh',wrapper)
    fake=tmp_path/'fake';fake.mkdir();python=fake/'python3'
    python.write_text('#!'+sys.executable+'\nimport json,subprocess,sys\n'
        'if sys.argv[2]=="select":sys.exit(subprocess.call([sys.executable,*sys.argv[1:]]))\n'
        'print(json.dumps(sys.argv[2:]))\n');python.chmod(0o755)
    env={**os.environ,'PATH':str(fake)+os.pathsep+os.environ['PATH'],'AGENTSTACK_RUNTIME_MODE':'global'}
    for key in ('AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG'):env.pop(key,None)
    args=[];config=str(server['config_path'])
    if source=='env':env['AGENTSTACK_GLOBAL_SERVER_CONFIG']=config
    elif source=='argument':args=['--server-config',config]
    else:args=['--server-config='+config]
    result=subprocess.run(['/bin/bash',str(wrapper),*args],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==['watch-auto',*args]
