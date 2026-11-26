"""Local authority, durable deduplication and lost-response tests; no live services."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time

import pytest

from orrery_chatgpt_mcp import fresh_messages as messages
from orrery_chatgpt_mcp.fresh_server import FreshGateway, enabled_config
from orrery_chatgpt_mcp.model import ToolError
from orrery_chatgpt_mcp.server import Config, ConfigurationError, Gateway
from test_readonly import FakeDashboard, INSTANCE


class FakeMail:
    instance = INSTANCE
    sender_id = 1
    binding_digest = 'fixed-test-context'

    def __init__(self, context):
        self.calls = []
        self.fail = False
        self.response = None
        self.changed = False

    def check(self):
        if self.changed:
            raise ValueError('private credential changed')

    def call(self, tool, args, request_id=None):
        self.calls.append((tool, deepcopy(args), request_id))
        if tool == 'whois':
            return {'mail_instance_id': INSTANCE, 'target_agent_id': 7, 'name': 'QuietBohr', 'state': 'active'}
        if self.fail:
            raise TimeoutError('DO_NOT_EXPOSE_SECRET_OR_BODY')
        if self.response is not None:
            return self.response
        return {'mail_instance_id': INSTANCE, 'message_id': 15, 'sender_id': 1, 'thread_id': '15',
                'created_ts': '2026-11-15T00:00:00+00:00', 'decision_only': False,
                'delivery_status': 'notification_pending', 'recipients': [{'agent_id': 7, 'kind': 'to'}]}



@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    policy = {'kind': 'orrery-mcp-fresh-message-policy-v1', 'context_path': str(root/'context.json'),
              'mail_instance_id': INSTANCE, 'sender_agent_id': 1,
              'recipients': [{'target_agent_id': 7, 'expected_target_name': 'QuietBohr', 'kind': 'to'}]}
    path = root/'policy.json'
    path.write_text(json.dumps(policy));path.chmod(0o600)
    monkeypatch.setattr(messages, 'BoundMail', FakeMail)
    manager = messages.FreshMessages(path, root/'state')
    args = {'idempotency_key': 'example-1', 'mail_instance_id': INSTANCE, 'target_agent_id': 7,
            'expected_target_name': 'QuietBohr', 'subject': '追加指示', 'body_md': '変更内容を検証してください。\n結果を報告してください。'}
    return manager, args, path, policy


def preview(manager, args):
    return manager.invoke('orrery_message_preview', args)


def proof(args, result):
    return {'idempotency_key': args['idempotency_key'], 'preview_id': result['preview_id'],
            'content_digest': result['content_digest']}


def commit(manager, args, result):
    return manager.invoke('orrery_message_commit', proof(args, result))


def status(manager, args):
    return manager.invoke('orrery_message_status', {'idempotency_key': args['idempotency_key']})


def test_preview_send_repeated_commit_and_restart_are_exactly_once(setup):
    manager, args, path, _ = setup
    first = preview(manager, args)
    assert first == preview(manager, args)
    assert first['message']['body_md'] == args['body_md']
    assert [c[0] for c in manager.mail.calls] == ['whois']
    accepted = commit(manager, args, first)
    assert accepted['status'] == 'accepted' and accepted['execution_status'] == 'unobserved'
    assert commit(manager, args, first) == status(manager, args) == accepted
    assert [c[0] for c in manager.mail.calls] == ['whois', 'send_message']
    assert manager.mail.calls[-1][2] == first['request_id']
    restarted = messages.FreshMessages(path, manager.state.root.parent)
    assert commit(restarted, args, first) == accepted
    assert restarted.mail.calls == []
    assert 'body_md' not in json.dumps(accepted)
    assert manager.state.path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('field,value', [('context_path','/private'),('auth',{}),('project_key','/other'),
    ('sender_agent_id',2),('request_id','chosen'),('recipients',[]),('endpoint','http://other')])
def test_tools_cannot_select_authority(setup, field, value):
    manager,args,_,_=setup
    with pytest.raises(ToolError,match='documented'):
        preview(manager,{**args,field:value})
    assert manager.mail.calls == []


@pytest.mark.parametrize('changes', [{'mail_instance_id':'f'*32},{'target_agent_id':8},
    {'target_agent_id':True},{'expected_target_name':'Other'},{'body_md':''},
    {'body_md':'x'*65537},{'body_md':'\ud800'},{'body_md':'bad\x00body'},
    {'subject':'bad\nsubject'},{'subject':'あ'*100},{'idempotency_key':'../file'}, {'idempotency_key':True}])
def test_invalid_or_unallowed_inputs_never_touch_mail(setup,changes):
    manager,args,_,_=setup
    with pytest.raises(ToolError):preview(manager,{**args,**changes})
    assert manager.mail.calls == []


def test_same_key_different_content_and_wrong_commit_proof_refused(setup):
    manager,args,_,_=setup;first=preview(manager,args)
    with pytest.raises(ToolError,match='different content'):preview(manager,{**args,'body_md':'changed'})
    p=proof(args,first);p['content_digest']='0'*64
    with pytest.raises(ToolError,match='exact preview'):manager.invoke('orrery_message_commit',p)
    assert [c[0] for c in manager.mail.calls] == ['whois']


def test_expiration_and_clock_rollback_do_not_send(setup,monkeypatch):
    manager,args,_,_=setup;first=preview(manager,args)
    for now in (first['expires_at']+1,first['expires_at']-messages.PREVIEW_SECONDS-1):
        monkeypatch.setattr(messages.time,'time',lambda:now)
        with pytest.raises(ToolError,match='expired'):commit(manager,args,first)
    assert [c[0] for c in manager.mail.calls] == ['whois']


@pytest.mark.parametrize('change', ['policy','context'])
def test_authority_change_refuses_before_send(setup,change):
    manager,args,path,policy=setup;first=preview(manager,args)
    if change=='policy':
        policy['recipients'][0]['expected_target_name']='Changed'
        path.write_text(json.dumps(policy))
    else:manager.mail.changed=True
    with pytest.raises(ToolError,match='unavailable'):commit(manager,args,first)
    assert [c[0] for c in manager.mail.calls]==['whois']


def test_timeout_is_durable_unknown_and_never_replayed(setup):
    manager,args,path,_=setup;first=preview(manager,args);manager.mail.fail=True
    result=commit(manager,args,first)
    assert result['status']=='unknown' and result['receipt'] is None
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)
    restarted=messages.FreshMessages(path,manager.state.root.parent)
    checked=status(restarted,args)
    assert checked == result and checked['status'] == 'unknown'
    assert commit(restarted,args,first)==checked
    assert restarted.mail.calls == []


def test_crash_after_claim_never_replays(setup):
    manager,args,path,_=setup;first=preview(manager,args)
    def crash(*a,**kw):raise KeyboardInterrupt
    manager.mail.call=crash
    with pytest.raises(KeyboardInterrupt):commit(manager,args,first)
    restarted=messages.FreshMessages(path,manager.state.root.parent)
    assert status(restarted,args)['status']=='unknown'
    assert commit(restarted,args,first)['status']=='unknown'
    assert restarted.mail.calls == []


@pytest.mark.parametrize('changed', [{}, {'decision_only':True}, {'mail_instance_id':'f'*32},
    {'sender_id':2},{'message_id':True},{'recipients':[{'agent_id':8,'kind':'to'}]},
    {'recipients':[]},{'delivery_status':'executed'},{'thread_id':None}])
def test_incomplete_or_mismatched_receipts_never_mean_accepted(setup,changed):
    manager,args,_,_=setup;first=preview(manager,args)
    full=manager.mail.call('send_message',first['message'],first['request_id'])
    manager.mail.calls.pop()
    manager.mail.response={**full,**changed} if changed else {}
    assert commit(manager,args,first)['status']=='unknown'


def test_concurrent_gateway_instances_share_one_durable_claim(setup):
    manager,args,path,_=setup;first=preview(manager,args)
    other=messages.FreshMessages(path,manager.state.root.parent)
    entered=threading.Event();release=threading.Event();real=manager.mail.call
    def delayed(*a,**kw):
        entered.set();assert release.wait(5);return real(*a,**kw)
    manager.mail.call=delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(commit,manager,args,first)
        assert entered.wait(5)
        unknown = commit(other, args, first)
        assert unknown['status'] == 'unknown'
        before = other.state.path.read_bytes()
        assert status(other, args) == unknown
        assert other.state.path.read_bytes() == before
        release.set();assert future.result()['status']=='accepted'
    assert other.mail.calls==[]
    assert status(other,args)['status']=='accepted'


@pytest.mark.parametrize('mutation', ['readable','symlink','hardlink','duplicate','unknown','empty','boolean','relative'])
def test_policy_refuses_unsafe_or_invalid_configuration(setup,tmp_path,mutation):
    manager,_,path,policy=setup
    if mutation=='readable':path.chmod(0o644)
    elif mutation=='symlink':
        original=path.with_suffix('.original');path.rename(original);path.symlink_to(original)
    elif mutation=='hardlink':os.link(path,path.with_suffix('.linked'))
    elif mutation=='duplicate':path.write_text('{"kind":1,"kind":2}')
    elif mutation=='unknown':policy['token']='secret';path.write_text(json.dumps(policy))
    elif mutation=='empty':policy['recipients']=[];path.write_text(json.dumps(policy))
    elif mutation=='boolean':policy['sender_agent_id']=True;path.write_text(json.dumps(policy))
    else:policy['context_path']='relative';path.write_text(json.dumps(policy))
    with pytest.raises((ValueError,OSError)):
        messages.FreshMessages(path,tmp_path.resolve()/'other-state')


def test_disabled_gateway_keeps_original_tools_and_rejects_commit(tmp_path):
    cfg=Config('fresh','http://127.0.0.1:8770',8791,tmp_path/'state')
    gateway=Gateway(cfg,FakeDashboard())
    _,result=gateway.rpc({'jsonrpc':'2.0','id':1,'method':'tools/list'})
    assert len(result['result']['tools'])==3
    _,result=gateway.rpc({'jsonrpc':'2.0','id':2,'method':'tools/call',
        'params':{'name':'orrery_message_commit','arguments':{}}})
    assert result['result']['structuredContent']['error']['code']=='unknown_tool'
    assert enabled_config({'ORRERY_MCP_ENABLE_MESSAGE':'0'}) is None
    _, result = gateway.rpc({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
        'params': {'name': 'orrery_status', 'arguments': {}}})
    assert result['result']['structuredContent']['adapter'] == {
        'read_only': True, 'spawn_enabled_on_server': False, 'message_enabled_on_server': False,
    }


def test_enabled_gateway_reports_effective_authority_and_rejects_swapped_dashboard(setup):
    manager,args,path,_=setup
    cfg=Config('fresh','http://127.0.0.1:8770',8791,manager.state.root.parent)
    dashboard=FakeDashboard();gateway=FreshGateway(cfg,path,dashboard)
    _,listed=gateway.rpc({'jsonrpc':'2.0','id':1,'method':'tools/list'})
    assert len(listed['result']['tools'])==6
    _,result=gateway.rpc({'jsonrpc':'2.0','id':2,'method':'tools/call',
        'params':{'name':'orrery_status','arguments':{}}})
    value=result['result']['structuredContent']
    assert value['adapter']=={'read_only':False,'spawn_enabled_on_server':False,'message_enabled_on_server':True}
    assert str(path) not in json.dumps(value) and 'context_path' not in json.dumps(value)
    dashboard.version={**dashboard.version,'mail_instance_id':'f'*32}
    _,result=gateway.rpc({'jsonrpc':'2.0','id':3,'method':'tools/call',
        'params':{'name':'orrery_message_preview','arguments':args}})
    assert result['result']['isError'] and gateway.messages.mail.calls==[]


@pytest.mark.parametrize('env', [
    {'ORRERY_MCP_ENABLE_MESSAGE':'yes'},
    {'ORRERY_MCP_ENABLE_MESSAGE':'1','ORRERY_MCP_MODE':'legacy'},
    {'ORRERY_MCP_ENABLE_MESSAGE':'1','ORRERY_MCP_MODE':'fresh'},
    {'ORRERY_MCP_ENABLE_MESSAGE':'1','ORRERY_MCP_MODE':'fresh','ORRERY_MCP_STATE_DIR':'/fixture/state'},
])
def test_optin_requires_fresh_policy_and_explicit_state(env):
    with pytest.raises(ConfigurationError):enabled_config(env)


def test_receipt_persistence_failure_does_not_reopen_claim(setup,monkeypatch):
    manager,args,path,_=setup;first=preview(manager,args)
    original=manager.state.put
    def fail_receipt(db,key,record):
        if record['state']=='accepted':raise OSError('fixture disk failure')
        return original(db,key,record)
    monkeypatch.setattr(manager.state,'put',fail_receipt)
    with pytest.raises(OSError):commit(manager,args,first)
    restarted=messages.FreshMessages(path,manager.state.root.parent)
    assert status(restarted,args)['status']=='unknown'
    assert commit(restarted,args,first)['status']=='unknown'
    assert restarted.mail.calls == []
    assert [c[0] for c in manager.mail.calls]==['whois','send_message']


def test_new_state_preserves_uuid_and_authority_changes_do_not_rebind(setup,tmp_path):
    manager,args,path,policy=setup;first=preview(manager,args)
    other=messages.FreshMessages(path,tmp_path.resolve()/'other-state')
    assert preview(other,args)['request_id']==first['request_id']
    policy['recipients'].append({'target_agent_id':8,'expected_target_name':'Other','kind':'to'})
    path.write_text(json.dumps(policy))
    changed=messages.FreshMessages(path,manager.state.root.parent)
    with pytest.raises(ToolError,match='different fixed authority'):status(changed,args)
    with pytest.raises(ToolError,match='different fixed authority'):commit(changed,args,first)
    assert changed.mail.calls==[]


def test_ledger_links_and_missing_evidence_refuse(setup,tmp_path):
    manager,args,_,_=setup;first=preview(manager,args)
    original=manager.state.path.with_suffix('.original')
    manager.state.path.rename(original)
    manager.state.path.symlink_to(original)
    with pytest.raises(ValueError):commit(manager,args,first)
    manager.state.path.unlink()
    with pytest.raises(OSError):commit(manager,args,first)
    assert [c[0] for c in manager.mail.calls]==['whois']


@pytest.mark.parametrize('saved_state', ['previewed', 'sending', 'unknown', 'accepted'])
def test_status_only_reads_local_evidence_after_authority_expires(setup, saved_state):
    manager, args, _, _ = setup
    first = preview(manager, args)
    key_hash = messages.digest(args['idempotency_key'])
    if saved_state == 'accepted':
        expected = commit(manager, args, first)
    elif saved_state == 'unknown':
        manager.mail.fail = True
        expected = commit(manager, args, first)
    else:
        if saved_state == 'sending':
            with manager.state.transaction() as db:
                record = manager.state.get(db, key_hash)
                record['state'] = 'sending'
                manager.state.put(db, key_hash, record)
        expected = status(manager, args)
    before = manager.state.path.read_bytes()
    modified = manager.state.path.stat().st_mtime_ns
    manager.mail.changed = True
    calls = deepcopy(manager.mail.calls)
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError('status must not contact Mail or persist reconciliation')

    manager.mail.receipt = manager.mail.replay = forbidden
    manager.state.put = forbidden
    assert status(manager, args) == expected
    assert status(manager, args) == expected
    assert manager.mail.calls == calls and forbidden_calls == []
    assert manager.state.path.read_bytes() == before
    assert manager.state.path.stat().st_mtime_ns == modified
    assert expected['status'] == ('unknown' if saved_state == 'sending' else saved_state)


def test_fresh_optin_never_enables_spawn(setup):
    manager,_,path,_=setup
    env={'ORRERY_MCP_MODE':'fresh','ORRERY_MCP_ENABLE_MESSAGE':'1',
         'ORRERY_MCP_STATE_DIR':str(manager.state.root.parent),'ORRERY_MCP_FRESH_MESSAGE_POLICY':str(path),
         'ORRERY_MCP_ENABLE_SPAWN':'1'}
    with pytest.raises(ConfigurationError):enabled_config(env)
