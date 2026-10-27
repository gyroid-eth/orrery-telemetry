"""Explicit empty fresh root, protected config reader and existing consumers.

All initialized roots are temporary. Port 8765 is policy-tested without bind;
the actual HTTP fixture always obtains its own dynamic port.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from agentstack_mail.fresh_cli import main
from agentstack_mail.fresh_client import FreshBoundClient, _endpoint, _http
from agentstack_mail.fresh_delivery import config as watcher_config, watch_once
from agentstack_mail.fresh_runtime import FRESH_KIND, KIND, FreshRuntime, _fresh_root, _isolated_root, mail_port_allowed, read_config
from agentstack_mail.fresh_server import build_fresh_server
from agentstack_mail.storage_origin_contract import B1Error, canonical
from test_fresh_mail import snapshot

ROOT=Path(__file__).resolve().parents[3]


@asynccontextmanager
async def state(tmp_path,mode='fresh'):
    runtime=await FreshRuntime.initialize(tmp_path/'store',mode=mode)
    people=[]
    try:
        for name in ('Alpha','Beta'):
            person=await runtime.register_local(request_id=str(uuid4()),name=name,program='fixture',model='fixture')
            people.append(person)
        yield runtime,people
    finally:await runtime.close()


async def prepared(runtime,people):
    server=build_fresh_server(core=runtime.core)
    async def dispatch(endpoint,tool,args):return await _http(server,tool,args)
    return await FreshBoundClient.prepare(runtime.root/'config.json',people[0]['binding_path'],
        'http://127.0.0.1:19999/mcp',client_id=str(uuid4()),dispatch=dispatch)


@pytest.mark.parametrize('mode',['isolated','fresh'])
def test_cli_init_mode_is_explicit_and_shared_reader_uses_same_identity(tmp_path,capsys,mode):
    root=tmp_path/'cli'
    args=['init','--root',str(root)]
    if mode=='fresh':args+=['--mode','fresh']
    main(args);result=json.loads(capsys.readouterr().out)
    assert result['mode']==mode
    path=root/'config.json';actual,value=read_config(path)
    assert (actual,value)==watcher_config(path)
    assert actual==root and value['instance_id']==result['mail_instance_id']
    assert value['kind']==(FRESH_KIND if mode=='fresh' else KIND)
    assert root.stat().st_mode&0o077==0 and path.stat().st_mode&0o077==0
    async def run():
        runtime=await FreshRuntime.open(path)
        try:assert (await runtime.core.health())['mail_instance_id']==value['instance_id']
        finally:await runtime.close()
    asyncio.run(run())


def test_fresh_path_validator_does_not_use_tmp_isolation_as_its_authority():
    # Pure path admission: no file is created or opened outside a temp root.
    path=Path('/orrery-synthetic-outside-temp')/'empty-root'
    assert _fresh_root(path)==path
    with pytest.raises(B1Error,match='FRESH_ISOLATION_REQUIRED'):_isolated_root(path)


def test_existing_root_is_never_adopted_or_reinitialized(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime,people):
            before=await snapshot(runtime)
            config_before=(runtime.root/'config.json').read_bytes()
            with pytest.raises(B1Error,match='FRESH_DESTINATION_EXISTS'):
                await FreshRuntime.initialize(runtime.root,mode='fresh')
            assert await snapshot(runtime)==before
            assert (runtime.root/'config.json').read_bytes()==config_before
        # Even an empty existing destination is not implicit adoption.
        empty=tmp_path/'already-there';empty.mkdir(mode=0o700)
        with pytest.raises(B1Error,match='FRESH_DESTINATION_EXISTS'):
            await FreshRuntime.initialize(empty,mode='fresh')
        assert list(empty.iterdir())==[]
    asyncio.run(run())


@pytest.mark.parametrize('style',['relative','root-symlink','parent-symlink'])
def test_explicit_fresh_init_refuses_noncanonical_roots(tmp_path,style):
    async def run():
        real=tmp_path/'real';real.mkdir(mode=0o700)
        alias=tmp_path/'alias';alias.symlink_to(real,target_is_directory=True)
        path={'relative':Path('relative-root'),'root-symlink':alias,'parent-symlink':alias/'new'}[style]
        with pytest.raises(B1Error,match='FRESH_ROOT_UNSAFE'):await FreshRuntime.initialize(path,mode='fresh')
        assert list(real.iterdir())==[]
    asyncio.run(run())


@pytest.mark.parametrize('damage',['mode','kind','root','database','instance-type','extra','permissions','symlink'])
def test_backend_client_and_watcher_share_config_rejection_without_db_changes(tmp_path,damage):
    async def run():
        async with state(tmp_path) as (runtime,people):
            client=await prepared(runtime,people)
            path=runtime.root/'config.json';original=path.read_bytes();value=json.loads(original)
            before=await snapshot(runtime)
            if damage=='mode':value['mode']='isolated'
            elif damage=='kind':value['kind']=KIND
            elif damage=='root':value['root']=str(tmp_path/'foreign')
            elif damage=='database':value['database']=str(tmp_path/'foreign.sqlite3')
            elif damage=='instance-type':value['instance_id']=123
            elif damage=='extra':value['project_key']='not-accepted'
            if damage=='permissions':path.chmod(0o644)
            elif damage=='symlink':
                saved=runtime.root/'saved-config';path.rename(saved);path.symlink_to(saved)
            else:path.write_text(canonical(value))
            try:
                with pytest.raises(B1Error):await FreshRuntime.open(path)
                with pytest.raises(B1Error):watcher_config(path)
                with pytest.raises(B1Error):client._load()
                assert await snapshot(runtime)==before
            finally:
                if damage=='symlink':path.unlink();saved.rename(path)
                path.chmod(0o600);path.write_bytes(original)
    asyncio.run(run())


def test_prepared_context_pins_mode_and_instance_and_missing_pin_is_not_guessed(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime,people):
            client=await prepared(runtime,people)
            assert client.context['server_mode']=='fresh'
            path=runtime.root/'config.json';original=path.read_bytes()
            value=json.loads(original)
            for changed in [dict(value,mode='isolated',kind=KIND),dict(value,instance_id='foreign-instance')]:
                path.write_text(canonical(changed))
                with pytest.raises(B1Error,match='FRESH_CLIENT_CONFIG_MISMATCH'):client._load()
            path.write_bytes(original)
            context=json.loads(client.path.read_text());del context['server_mode']
            client.path.write_text(canonical(context))
            with pytest.raises(B1Error,match='FRESH_CLIENT_CONTEXT_INVALID'):FreshBoundClient(client.path)
    asyncio.run(run())


@pytest.mark.parametrize('mode,port,accepted',[
    ('isolated',8765,False),('fresh',8765,True),('isolated',18765,True),('fresh',18765,True),
    ('fresh',8770,False),('fresh',8781,False),('fresh',8791,False),('unknown',19999,False),
    ('fresh',True,False),('fresh',80,False)])
def test_port_policy_is_config_mode_scoped_without_opening_any_socket(mode,port,accepted):
    assert mail_port_allowed(port,mode) is accepted
    if type(port) is not bool:
        url='http://127.0.0.1:'+str(port)+'/mcp'
        if accepted:assert _endpoint(url,server_mode=mode)==url
        else:
            with pytest.raises(B1Error,match='FRESH_ENDPOINT_INVALID'):_endpoint(url,server_mode=mode)


def test_real_fresh_config_http_client_and_existing_watcher_once_use_same_instance(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime,people):
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
            assert port not in {8765,8770,8781,8791}
            log_path=tmp_path/'layout-http.log'
            with log_path.open('w') as log:
                process=subprocess.Popen([sys.executable,'-m','agentstack_mail.fresh_cli','serve',
                    '--config',str(runtime.root/'config.json'),'--port',str(port)],stdout=log,stderr=subprocess.STDOUT)
                try:
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        assert process.poll() is None,log_path.read_text()
                        try:
                            with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                        except OSError:await asyncio.sleep(.1)
                    else:pytest.fail('private HTTP did not start')
                    clients=[]
                    for person in people:
                        clients.append(await FreshBoundClient.prepare(runtime.root/'config.json',person['binding_path'],
                            'http://127.0.0.1:'+str(port)+'/mcp',client_id=str(uuid4())))
                    owner=await clients[0].call('reconnect',{})
                    assert owner['mail_instance_id']==runtime.config['instance_id']
                    sent=await clients[0].call('send_message',{'recipients':[{'target_name':'Beta'}],
                        'subject':'fresh-layout','body_md':'real temporary fresh root'})
                    inbox=await clients[1].call('fetch_inbox',{})
                    assert inbox[0]['id']==sent['message_id'] and sent['mail_instance_id']==owner['mail_instance_id']
                    assert all(c.context['server_mode']=='fresh' for c in clients)
                    # Prepared contexts have no native launch yet; no ps/tmux is needed.
                    assert await watch_once(runtime.root/'config.json',ROOT)==dict(ok=True,routes=[])
                    env=dict(os.environ,AGENTSTACK_PYTHON=sys.executable,AGENTSTACK_RUNTIME_MODE='fresh')
                    for name in ('AGENTSTACK_FRESH_CLIENT_CONTEXT','AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG'):
                        env.pop(name,None)
                    result=await asyncio.to_thread(subprocess.run,['/bin/bash',str(ROOT/'hooks/watch_agent_mail_signals.sh'),
                        '--fresh-config',str(runtime.root/'config.json'),'--once'],env=env,capture_output=True,text=True,timeout=30)
                    assert result.returncode==0 and json.loads(result.stdout)==dict(ok=True,routes=[]),result.stderr
                    (tmp_path/'layout-watcher.json').write_text(canonical(dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)))
                finally:
                    if process.poll() is None:process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                    assert process.poll() is not None
    asyncio.run(run())
