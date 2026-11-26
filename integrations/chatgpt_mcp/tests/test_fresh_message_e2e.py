"""Real fresh Mail HTTP/storage, private configuration and actual MCP HTTP calls."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.storage_origin_contract import B1Error
from agentstack_mail.fresh_runtime import FreshRuntime
from agentstack_mail.models import Agent, Message, MessageRecipient
from orrery_chatgpt_mcp.fresh_messages import FreshMessages
from orrery_chatgpt_mcp.fresh_server import FreshGateway
from orrery_chatgpt_mcp.server import Config, Server, handler, pair, read_secret
from test_readonly import FakeDashboard, FRESH

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def live(tmp_path):
    root=tmp_path.resolve()
    env={k:v for k,v in os.environ.items() if not k.startswith(('AGENTSTACK_','MCP_AGENT_','ORRERY_MCP_'))
         and k not in {'TMUX','TMUX_PANE','AGENT_NAME','PARENT_AGENT','CODEX_HOME','CLAUDE_CONFIG_DIR'}}
    home=root/'home';home.mkdir(mode=0o700)
    env.update(HOME=str(home),PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=str(ROOT/'packages/agentstack_mail/src')+os.pathsep+str(ROOT/'integrations/chatgpt_mcp/src'),
               AGENTSTACK_LABEL_PREFIX='org.agentstack.test.mcp-fresh-message',AGENTSTACK_MAIL_TOOLS_LOG_ENABLED='false')

    async def initialize():
        runtime=await FreshRuntime.initialize(root/'mail',mode='fresh')
        try:
            people=[await runtime.register_local(request_id=str(uuid4()),name=name,program='fixture',model='fixture')
                    for name in ('Sender','Recipient')]
            return people,runtime.config['instance_id']
        finally:await runtime.close()

    people,instance=asyncio.run(initialize())
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    assert port not in {8765,8770,8781,8791,18765}
    log_path=root/'mail.log'
    process=None
    with log_path.open('w') as log:
        log_path.chmod(0o600)
        process=subprocess.Popen([sys.executable,'-m','agentstack_mail.fresh_cli','serve',
            '--config',str(root/'mail/config.json'),'--port',str(port)],env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                assert process.poll() is None,log_path.read_text()
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                except OSError:time.sleep(.05)
            else:pytest.fail('private fresh Mail startup timed out')
            async def prepare():
                return await FreshBoundClient.prepare(root/'mail/config.json',people[0]['binding_path'],
                    f'http://127.0.0.1:{port}/mcp',client_id=str(uuid4()))
            client=asyncio.run(prepare())
            policy={'kind':'orrery-mcp-fresh-message-policy-v1','context_path':str(client.path),
                    'mail_instance_id':instance,'sender_agent_id':people[0]['agent_id'],
                    'recipients':[{'target_agent_id':people[1]['agent_id'],'expected_target_name':'Recipient','kind':'to'}]}
            policy_path=root/'policy.json';policy_path.write_text(json.dumps(policy));policy_path.chmod(0o600)
            cfg=Config('fresh','http://127.0.0.1:8770',8791,root/'state')
            pair(cfg)
            dashboard=FakeDashboard(version={**FRESH,'mail_instance_id':instance},agents={
                'mail_instance_id':instance,'observation_truncated':False,'agents':[]})
            gateway=FreshGateway(cfg,policy_path,dashboard)
            with Server(('127.0.0.1',0),handler(gateway)) as endpoint:
                gateway.config=replace(cfg,port=endpoint.server_port)
                gateway.hosts={f'127.0.0.1:{endpoint.server_port}',f'localhost:{endpoint.server_port}'}
                thread=threading.Thread(target=endpoint.serve_forever,daemon=True);thread.start()
                try:
                    args={'idempotency_key':'real-1','mail_instance_id':instance,
                          'target_agent_id':people[1]['agent_id'],'expected_target_name':'Recipient',
                          'subject':'isolated fresh instruction','body_md':'fixture-private-body-実送信\n検証してください。'}
                    def rpc(name,args):
                        conn=http.client.HTTPConnection('127.0.0.1',endpoint.server_port,timeout=30)
                        try:
                            conn.request('POST','/mcp/'+read_secret(cfg),json.dumps({'jsonrpc':'2.0','id':1,
                                'method':'tools/call','params':{'name':name,'arguments':args}}),
                                {'Content-Type':'application/json','Accept':'application/json'})
                            response=conn.getresponse();raw=response.read();assert response.status==200
                            return json.loads(raw)['result']
                        finally:conn.close()
                    yield SimpleNamespace(root=root,people=people,instance=instance,client=client,policy=policy_path,
                        cfg=cfg,gateway=gateway,args=args,rpc=rpc,env=env,port=endpoint.server_port,log_path=log_path)
                finally:
                    endpoint.shutdown();thread.join(5)
        finally:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            assert process.poll() is not None
            assert 'fixture-private-body' not in log_path.read_text()
            assert all(json.loads(Path(p['binding_path']).read_text())['registration_token'] not in log_path.read_text()
                       for p in people)


def unwrap(value):
    assert value['isError'] is False,value
    return value['structuredContent']


def commit_args(case,preview):
    return {'idempotency_key':case.args['idempotency_key'],'preview_id':preview['preview_id'],
            'content_digest':preview['content_digest']}


def stored(case):
    # Inspect only this fixture's database, including deliberately corrupt identity state.
    database=Path(json.loads((case.root/'mail/config.json').read_text())['database'])
    assert database.parent==case.root/'mail'
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        return (db.execute('SELECT id,sender_id,subject,body_md FROM messages ORDER BY id').fetchall(),
                db.execute('SELECT message_id,agent_id FROM message_recipients ORDER BY message_id').fetchall())


def restart_process(case,tool,args):
    code=('import json,sys; from pathlib import Path; '
          'from orrery_chatgpt_mcp.fresh_messages import FreshMessages; '
          'm=FreshMessages(Path(sys.argv[1]),Path(sys.argv[2])); '
          'print(json.dumps(m.invoke(sys.argv[3],json.loads(sys.argv[4]))))')
    return subprocess.run([sys.executable,'-c',code,str(case.policy),str(case.cfg.state_dir),tool,json.dumps(args)],
                          env=case.env,capture_output=True,text=True,timeout=30)


def test_real_send_storage_request_uuid_and_restart(live):
    preview=unwrap(live.rpc('orrery_message_preview',live.args))
    assert stored(live)==([],[])
    accepted=unwrap(live.rpc('orrery_message_commit',commit_args(live,preview)))
    assert accepted['status']=='accepted' and accepted['execution_status']=='unobserved'
    rows,pairs=stored(live)
    assert rows==[(accepted['receipt']['message_id'],live.people[0]['agent_id'],live.args['subject'],live.args['body_md'])]
    assert pairs==[(rows[0][0],live.people[1]['agent_id'])]
    saved=json.loads((live.client.path.parent/'requests'/f"{preview['request_id']}.json").read_text())
    assert saved['arguments']==preview['message'] and saved['request_id']==preview['request_id']
    assert unwrap(live.rpc('orrery_message_commit',commit_args(live,preview)))==accepted
    restarted=restart_process(live,'orrery_message_commit',commit_args(live,preview))
    assert restarted.returncode==0 and json.loads(restarted.stdout)==accepted
    assert len(stored(live)[0])==1
    conn=http.client.HTTPConnection('127.0.0.1',live.port)
    try:
        conn.request('GET','/healthz');response=conn.getresponse()
        assert response.status==200 and json.loads(response.read())['read_only'] is False
    finally:conn.close()



def test_recipient_owner_fetch_read_ack_are_distinct_from_mcp_acceptance(live):
    async def recipient():
        return await FreshBoundClient.prepare(
            live.root / 'mail/config.json', live.people[1]['binding_path'],
            live.client.context['endpoint'], client_id=str(uuid4()),
        )

    client = asyncio.run(recipient())
    assert asyncio.run(client.call('fetch_inbox', {})) == []
    preview = unwrap(live.rpc('orrery_message_preview', live.args))
    accepted = unwrap(live.rpc('orrery_message_commit', commit_args(live, preview)))
    message_id = accepted['receipt']['message_id']
    assert accepted['execution_status'] == 'unobserved'
    inbox = asyncio.run(client.call('fetch_inbox', {}))
    assert len(inbox) == 1
    assert (inbox[0]['id'], inbox[0]['sender_id'], inbox[0]['subject'], inbox[0]['body_md']) == (
        message_id, live.people[0]['agent_id'], live.args['subject'], live.args['body_md'],
    )
    assert inbox[0]['read_ts'] is None and inbox[0]['ack_ts'] is None

    # An authenticated sender cannot mark the recipient's message as read.
    async def wrong_owner():
        with pytest.raises(B1Error, match='NOT_A_RECIPIENT'):
            await live.client.call('mark_message_read', {'message_id': message_id})

    asyncio.run(wrong_owner())
    read = asyncio.run(client.call('mark_message_read', {'message_id': message_id}))
    assert read['message_id'] == message_id and read['agent_id'] == live.people[1]['agent_id']
    assert read['read_ts'] and read['ack_ts'] is None
    acknowledged = asyncio.run(client.call('acknowledge_message', {'message_id': message_id}))
    assert acknowledged['read_ts'] == read['read_ts'] and acknowledged['ack_ts']
    again = asyncio.run(client.call('fetch_inbox', {}))
    assert len(again) == 1
    assert again[0]['read_ts'] == read['read_ts']
    assert again[0]['ack_ts'] == acknowledged['ack_ts']
    # Receipt does not claim that a real agent began processing the instruction.
    assert unwrap(live.rpc('orrery_message_status', {'idempotency_key': live.args['idempotency_key']})) == accepted
    assert len(stored(live)[0]) == 1

def test_real_response_loss_keeps_unknown_without_resend(live):
    preview = unwrap(live.rpc('orrery_message_preview', live.args))
    real = live.gateway.messages.mail.client.dispatch

    async def lose(endpoint, tool, args):
        await real(endpoint, tool, args)
        raise RuntimeError('fixture loss after actual server acceptance')

    live.gateway.messages.mail.client.dispatch = lose
    unknown = unwrap(live.rpc('orrery_message_commit', commit_args(live, preview)))
    assert unknown['status'] == 'unknown' and unknown['receipt'] is None
    assert len(stored(live)[0]) == 1
    restarted = restart_process(live, 'orrery_message_status', {'idempotency_key': 'real-1'})
    assert restarted.returncode == 0 and json.loads(restarted.stdout) == unknown
    dispatches = []

    async def forbidden(*args, **kwargs):
        dispatches.append((args, kwargs))
        raise AssertionError('status must not query or replay Mail')

    live.gateway.messages.mail.client.dispatch = forbidden
    before = live.gateway.messages.state.path.read_bytes()
    checked = unwrap(live.rpc('orrery_message_status', {'idempotency_key': 'real-1'}))
    assert checked == unknown and checked['execution_status'] == 'unobserved'
    assert unwrap(live.rpc('orrery_message_commit', commit_args(live, preview))) == unknown
    assert dispatches == [] and live.gateway.messages.state.path.read_bytes() == before
    assert len(stored(live)[0]) == 1


@pytest.mark.parametrize('change',['retire','identity_corruption','contact'])
def test_server_transaction_rejects_target_change_after_preview(live,change):
    preview=unwrap(live.rpc('orrery_message_preview',live.args))
    async def change_target():
        runtime=await FreshRuntime.open(live.root/'mail/config.json')
        try:
            if change=='retire':await runtime.core.retire(live.people[1]['agent_id'])
            else:
                async with runtime.sessions() as session:
                    target=await session.get(Agent,live.people[1]['agent_id'])
                    if change=='identity_corruption':target.name='Renamed'
                    else:target.contact_policy='block_all'
                    await session.commit()
        finally:await runtime.close()
    asyncio.run(change_target())
    result=unwrap(live.rpc('orrery_message_commit',commit_args(live,preview)))
    assert result['status']=='unknown' and result['receipt'] is None
    assert stored(live)==([],[])
    checked=unwrap(live.rpc('orrery_message_status',{'idempotency_key':'real-1'}))
    assert checked == result and checked['status'] == 'unknown'
    assert stored(live)==([],[])


def test_new_adapter_state_uses_same_upstream_id_for_same_intent(live):
    preview=unwrap(live.rpc('orrery_message_preview',live.args))
    first=unwrap(live.rpc('orrery_message_commit',commit_args(live,preview)))
    separate=FreshMessages(live.policy,live.root/'separate-state')
    again=separate.invoke('orrery_message_preview',live.args)
    assert again['request_id']==preview['request_id']
    second=separate.invoke('orrery_message_commit',commit_args(live,again))
    assert second['receipt']==first['receipt'] and len(stored(live)[0])==1


def test_response_loss_before_delivery_never_becomes_a_status_send(live):
    preview = unwrap(live.rpc('orrery_message_preview', live.args))
    dispatches = []

    async def lose_before_delivery(*args, **kwargs):
        dispatches.append((args, kwargs))
        raise RuntimeError('fixture loss before delivery')

    live.gateway.messages.mail.client.dispatch = lose_before_delivery
    unknown = unwrap(live.rpc('orrery_message_commit', commit_args(live, preview)))
    assert unknown['status'] == 'unknown' and len(dispatches) == 1
    checked = unwrap(live.rpc('orrery_message_status', {'idempotency_key': 'real-1'}))
    assert checked == unknown
    assert unwrap(live.rpc('orrery_message_commit', commit_args(live, preview))) == unknown
    assert len(dispatches) == 1 and stored(live) == ([], [])


def test_other_saved_body_under_same_uuid_cannot_be_claimed_accepted(live):
    preview=unwrap(live.rpc('orrery_message_preview',live.args))
    assert unwrap(live.rpc('orrery_message_commit',commit_args(live,preview)))['status']=='accepted'
    other=FreshMessages(live.policy,live.root/'other-state')
    changed={**live.args,'body_md':'different intent under same key'}
    next_preview=other.invoke('orrery_message_preview',changed)
    assert next_preview['request_id']==preview['request_id']
    assert other.invoke('orrery_message_commit',commit_args(live,next_preview))['status']=='unknown'
    checked=other.invoke('orrery_message_status',{'idempotency_key':'real-1'})
    assert checked['status']=='unknown' and checked['receipt'] is None
    assert len(stored(live)[0])==1 and stored(live)[0][0][-1]==live.args['body_md']
