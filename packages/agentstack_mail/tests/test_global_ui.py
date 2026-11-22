"""Operator reads reuse visibility and never consume notification facts."""
import json
import os

import pytest
from agentstack_mail.global_s2c import S2cRuntime
from agentstack_mail.global_server import GlobalError
from agentstack_mail.global_s2a import revision
from agentstack_mail.namespace_store import changed
from test_global_server_s2c import prepared


def request(runtime, query):
    return dict(version=1, action='ui_read', expected_server_instance_id=runtime.instance,
                candidate_generation=runtime.candidate_generation, authority_epoch=runtime.epoch, query=query)


def read(runtime, **query):
    return runtime._management_apply(request(runtime, query), os.getuid())


def test_read_snapshot_roster_health_resolve(prepared):
    r=S2cRuntime(prepared['config_path'])
    assert read(r,kind='resolve_name',name='Beta')['data']['agent_id']==2
    page=read(r,kind='roster',before_agent_id=None,limit=1)
    assert len(page['data']['items'])==1 and page['data']['next_before_agent_id']
    assert 'registration_token' not in json.dumps(page)
    assert read(r,kind='health')['data']['server_ready'] is True
    with pytest.raises(GlobalError,match='PEER_UID_REQUIRED'):
        r._management_apply(request(r,dict(kind='health')),os.getuid()+1)


def test_visible_projection_and_cursor_no_consumption(prepared):
    r=S2cRuntime(prepared['config_path'])
    with r.transaction() as db:
        before=revision(db)
        facts=[tuple(v) for v in db.execute('SELECT * FROM message_recipients')]
    out=read(r,kind='activity',view_as_agent_id=2,limit=1,cursor=None)
    assert out['data']['items']
    cursor=out['data']['next_cursor']
    if cursor:
        second=read(r,kind='activity',view_as_agent_id=2,limit=1,cursor=cursor)
        assert second['data']['items'][0]['id']<out['data']['items'][0]['id']
    mid=out['data']['items'][0]['id']
    assert read(r,kind='message',view_as_agent_id=2,message_id=mid)['data']['body_md'] is not None
    with r.transaction() as db:
        assert revision(db)==before
        assert [tuple(v) for v in db.execute('SELECT * FROM message_recipients')]==facts
    with pytest.raises(GlobalError,match='TARGET_UNAVAILABLE'):
        read(r,kind='activity',view_as_agent_id=999,limit=10,cursor=None)


def test_edge_visibility_before_max_and_pagination(prepared):
    r=S2cRuntime(prepared['config_path'])
    from test_global_server_s2b import owner
    import uuid
    first=r.apply('send_message', owner(prepared,1,request_id=str(uuid.uuid4()),to_agent_ids=[2],subject='pair',body_md='visible'))
    hidden=r.apply('send_message', owner(prepared,1,request_id=str(uuid.uuid4()),to_agent_ids=[1],subject='hidden',body_md='private'))
    out=read(r,kind='edge_messages',view_as_agent_id=2,a_agent_id=1,b_agent_id=2,limit=1,cursor=None)['data']
    assert out['items'][0]['id']==first['message_id']
    assert out['max_message_id']==first['message_id']<hidden['message_id']
    with pytest.raises(GlobalError,match='CURSOR_INVALID'):
        read(r,kind='edge_messages',view_as_agent_id=2,a_agent_id=1,b_agent_id=1,limit=1,cursor=out['next_cursor'])


def test_history_and_after_filters_precede_max_and_bind_cursor(prepared):
    r=S2cRuntime(prepared['config_path'])
    from test_global_server_s2b import owner
    import uuid
    visible=r.apply('send_message',owner(prepared,1,request_id=str(uuid.uuid4()),to_agent_ids=[2],subject='one',body_md='body'))
    hidden=r.apply('send_message',owner(prepared,1,request_id=str(uuid.uuid4()),to_agent_ids=[1],bcc_agent_ids=[2],subject='bcc',body_md='body'))
    page=read(r,kind='activity',view_as_agent_id=1,target_agent_id=2,after_id=visible['message_id']-1,limit=1,cursor=None)['data']
    assert page['items'][0]['id']==hidden['message_id']
    assert page['max_message_id']==hidden['message_id']
    empty=read(r,kind='activity',view_as_agent_id=1,target_agent_id=999,after_id=0,limit=1,cursor=None)['data']
    assert empty['max_message_id']==0 and empty['items']==[]
    # A recipient can see the message but cannot discover another hidden BCC.
    private=r.apply('send_message',owner(prepared,1,request_id=str(uuid.uuid4()),to_agent_ids=[2],bcc_agent_ids=[1],subject='private target',body_md='body'))
    page=read(r,kind='activity',view_as_agent_id=2,target_agent_id=1,after_id=private['message_id']-1,limit=1,cursor=None)['data']
    # Sender 1 is public, so this row belongs in their history.
    assert page['items'][0]['id']==private['message_id']
    r.apply('set_contact_policy',owner(prepared,3,request_id=str(uuid.uuid4()),policy='open'))
    bcc=r.apply('send_message',owner(prepared,1,request_id=str(uuid.uuid4()),to_agent_ids=[2],bcc_agent_ids=[3],subject='hidden bcc',body_md='body'))
    page=read(r,kind='activity',view_as_agent_id=2,target_agent_id=3,after_id=bcc['message_id']-1,limit=1,cursor=None)['data']
    assert page['items']==[] and page['max_message_id']==0
    edge=read(r,kind='edge_messages',view_as_agent_id=2,a_agent_id=1,b_agent_id=3,limit=1,cursor=None)['data']
    assert all(item['id']!=bcc['message_id'] for item in edge['items'])
