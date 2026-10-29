"""Real fresh ORM -> private dashboard Handler -> mobile reader contracts."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from sqlalchemy import select

from agentstack_mail.models import Message, MessageRecipient
from agentstack_mail.fresh_child import prepare
from agentstack_mail.storage_origin_contract import B1Error
from dashboard.fresh_backend import Backend
from dashboard.fresh_readers import Reader
from dashboard.mobile_projection import project_mail, project_graph, checked_content, SnapshotStore
from dashboard.mobile_relay import Upstream
from test_fresh_guard import live
from test_fresh_mail import snapshot
from test_fresh_dashboard import dashboard

ROOT=Path(__file__).resolve().parents[3]


def test_reader_imports_module_and_direct_without_hiding_dependency_errors(tmp_path):
    import os
    import subprocess
    import sys
    from textwrap import dedent
    code=dedent("""
        import builtins,importlib,json,sys
        from pathlib import Path
        from types import SimpleNamespace
        root=Path(sys.argv[1]);mode=sys.argv[2]
        prefix='dashboard.' if mode=='module' else ''
        reader=importlib.import_module(prefix+'fresh_readers')
        backend=importlib.import_module(prefix+'fresh_backend')
        backend.FreshBoundClient=lambda path:SimpleNamespace(path=Path(path))
        obj=backend.Backend(Path.cwd()/'private-context.json',root)
        assert type(obj.reader) is reader.Reader
        original=builtins.__import__
        checked=[]
        for target in ('dashboard.mobile_projection','dashboard.fresh_readers'):
            def broken(name,*args,**kwargs):
                if name==target:
                    raise ModuleNotFoundError('fixture dependency missing',name='fixture_internal_dependency')
                if name in ('mobile_projection','fresh_readers'):
                    checked.append(name)
                return original(name,*args,**kwargs)
            builtins.__import__=broken
            try:
                try:
                    if target.endswith('mobile_projection'):importlib.reload(reader)
                    else:backend.Backend(Path.cwd()/'private-context.json',root)
                except ModuleNotFoundError as error:
                    assert error.name=='fixture_internal_dependency'
                else:raise AssertionError('internal dependency failure was hidden')
            finally:builtins.__import__=original
        assert not checked
        print(json.dumps(dict(mode=mode,reader=reader.__name__,backend=backend.__name__,
                              context_io=0,db=0,http=0,internal_error_preserved=True)))
    """)
    for mode,paths in (('module',[ROOT,ROOT/'packages/agentstack_mail/src']),
                       ('direct',[ROOT/'dashboard',ROOT/'packages/agentstack_mail/src'])):
        env=dict(os.environ,PYTHONPATH=os.pathsep.join(map(str,paths)),PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([sys.executable,'-P','-c',code,str(ROOT),mode],cwd=tmp_path,env=env,
                              capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stderr
        proof=json.loads(result.stdout)
        assert proof['mode']==mode and proof['internal_error_preserved'] and proof['db']==proof['http']==0
        (tmp_path/('api2a-import-'+mode+'.json')).write_text(json.dumps(proof)+'\n')



async def seed(runtime, people, count=1, *, body='fixture body'):
    # Actual tool handler for the first message; bulk ORM rows exercise paging.
    first=await runtime.core.write_message('send',expected_server_instance_id=runtime.config['instance_id'],
        auth=people[0]['auth'],request_id=str(uuid4()),recipients=[{'target_name':'Beta'}],
        subject='reader subject',body_md=body,thread_id='reader-thread')
    async with runtime.sessions() as s:
        original=(await s.execute(select(Message))).scalars().first()
        stamp=original.created_ts
        for n in range(count-1):
            message=Message(project_id=original.project_id,sender_id=people[0]['agent_id'],
                subject='bulk '+str(n),body_md=body,thread_id='reader-thread',created_ts=stamp)
            s.add(message);await s.flush()
            s.add(MessageRecipient(message_id=message.id,agent_id=people[1]['agent_id']))
            s.add(MessageRecipient(message_id=message.id,agent_id=people[2]['agent_id'],kind='cc'))
        await s.commit()
    return first


def test_actual_handler_metadata_body_page_cache_graph_and_zero_side_effects(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            await seed(runtime,people,105,body='日本語'*24000)
            backend=Backend(parent.path,ROOT)
            before=await snapshot(runtime)
            with dashboard(parent,backend,monkeypatch,tmp_path) as (module,request):
                code,raw=await asyncio.to_thread(request,'/api/mobile-mail?window=day')
                assert code==200,raw
                meta=project_mail(raw,'day')
                assert len(meta['events'])==200 and raw['events_truncated'] and len(meta['edges'])==2
                instance=runtime.config['instance_id']
                assert meta['mail_instance_id']==instance and meta['edges'][0]['source_agent_id']==people[0]['agent_id']
                code,page=await asyncio.to_thread(request,'/api/mobile-mail-page?'+urlencode(dict(window='day',cursor=meta['next_cursor'])))
                assert code==200 and len(page['events'])==9 and not page['next_cursor'],page
                keys=[e['key'] for e in meta['events']+page['events']]
                assert len(set(keys))==209
                code,_=await asyncio.to_thread(request,'/api/mobile-mail-page?'+urlencode(dict(window='all',cursor=meta['next_cursor'])))
                assert code==410
                event=meta['events'][0]
                path='/api/mobile-message?'+urlencode(dict(window='day',event=event['key']))
                code,body=await asyncio.to_thread(request,path)
                value=checked_content(body)
                assert code==200 and value['body_truncated'] and len(value['body'].encode())<=65536
                assert value['event']['mail_instance_id']==instance
                code,revision=await asyncio.to_thread(request,path+'&mode=revision')
                assert code==200 and revision['revision']==body['revision'] and 'body' not in revision
                code,cache=await asyncio.to_thread(request,'/api/mobile-cache-page')
                assert code==200 and 1<=len(cache['items'])<=32 and not cache['complete']
                assert cache['reason']=='serialized_batch_limit'
                for item in cache['items']:checked_content(item)
                cache_ids={x['fingerprint'] for x in cache['items']}
                code,next_cache=await asyncio.to_thread(request,'/api/mobile-cache-page?cursor='+cache['next_cursor'])
                assert code==200 and not cache_ids.intersection(x['fingerprint'] for x in next_cache['items'])
                code,_=await asyncio.to_thread(request,'/api/mobile-cache-page?cursor='+cache['next_cursor'])
                assert code==410
                code,graph=await asyncio.to_thread(request,'/api/graph?all=1')
                assert code==200 and len(project_graph(graph)['nodes'])==3
                assert len(graph['edges'])==2 and graph['mail_instance_id']==instance
                # Use the existing relay's real fixed-loopback transport too.
                server=module.ThreadingHTTPServer(('127.0.0.1',0),module.Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    upstream=Upstream(server.server_port)
                    relayed=await asyncio.to_thread(upstream,'mail:day')
                    assert project_mail(relayed,'day')['events']==meta['events']
                    relayed_body=await asyncio.to_thread(upstream.message,'day',event['key'],'content')
                    assert checked_content(relayed_body)==value
                finally:
                    server.shutdown();server.server_close();thread.join(timeout=5)
                    assert not thread.is_alive()
                code,edge=await asyncio.to_thread(request,'/api/edge-messages?a=Alpha&b=Beta&limit=2')
                assert code==200 and edge['count']==2 and edge['messages'][0]['body_truncated']
                code,comets=await asyncio.to_thread(request,'/api/messages-since?since=0&limit=2')
                assert code==200 and len(comets['messages'])==2 and comets['since']>=comets['now']-300
                assert comets['messages'][0]['sender_agent_id']==people[0]['agent_id']
                code,hist=await asyncio.to_thread(request,'/api/agent-history?name=Alpha&hours=24')
                assert code==200 and hist['events'] and hist['range']['end_ts']==hist['now_ts']
                assert all(e['kind'] in {'mail_sent','mail_recv','spawn','retire'} for e in hist['events'])
                code,recent=await asyncio.to_thread(request,'/api/mail/recent?limit=2&agent=Beta')
                assert code==200 and len(recent['messages'])==2 and 'excerpt' in recent['messages'][0]
                mid=recent['messages'][0]['id']
                code,pc=await asyncio.to_thread(request,'/api/mail/message?id='+str(mid))
                assert code==200 and pc['message']['body_truncated'] and pc['message']['body_md']
                code,thread=await asyncio.to_thread(request,'/api/mail/thread?thread_id=reader-thread&limit=2')
                assert code==200 and len(thread['messages'])==2
                assert thread['messages'][0]['id']<thread['messages'][1]['id']
                store=SnapshotStore(lambda k: raw if k.startswith('mail:') else graph if k=='graph' else asyncio.run(backend.roster()))
                view=await asyncio.to_thread(store.snapshot,'day')
                assert len(view['graph']['edges'])==2 and view['sources']['mail']['status']=='available'
                wire=json.dumps([raw,page,body,cache,graph,edge,comets,hist,recent,pc,thread,view])
                assert not any(secret in wire for secret in ('registration_token','credential_generation','context_path','database'))
                # Wrong I / invalid query never reaches legacy or grants another scope.
                for path,status in [
                    ('/api/mail/recent?mail_instance_id='+'0'*32,409),
                    ('/api/mobile-mail?window=invalid',400),
                    ('/api/mobile-message?window=day&event='+'0'*32,410),
                    ('/api/mobile-mail?window=day&window=all',400),
                    ('/api/mail/recent?project_key=old',400),
                ]:
                    code,_=await asyncio.to_thread(request,path);assert code==status,path
                assert await snapshot(runtime)==before
    asyncio.run(run())


def test_cursor_epoch_pair_binding_and_revoked_owner(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            await seed(runtime,people,202)
            backend=Backend(parent.path,ROOT)
            first=await backend.read('/api/mobile-mail-page',dict(window='all',a='Alpha',b='Beta',direction='forward'))
            assert len(first['events'])==200 and first['next_cursor']
            query=dict(window='all',a='Alpha',b='Beta',direction='reverse',cursor=first['next_cursor'])
            with pytest.raises(LookupError):await backend.read('/api/mobile-mail-page',query)
            other=Backend(parent.path,ROOT)
            query['direction']='forward'
            with pytest.raises(LookupError):await other.read('/api/mobile-mail-page',query)
            # Absolute I/A selector uses the same query identity and target rows.
            query.pop('cursor')
            query.update(mail_instance_id=runtime.config['instance_id'],a_id=str(people[0]['agent_id']),b_id=str(people[1]['agent_id']))
            page=await backend.read('/api/mobile-mail-page',query)
            assert page['events'][0]['key']==first['events'][0]['key']
            await runtime.core.recover(people[0]['agent_id'],expected_generation=people[0]['credential_generation'],
                                       new_token='private-reader-recovery-token')
            with pytest.raises(B1Error):await backend.read('/api/mobile-mail',dict(window='day'))
    asyncio.run(run())


def test_public_mobile_excludes_bcc_but_pc_preserves_existing_scope_and_period(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            await seed(runtime,people)
            async with runtime.sessions() as s:
                msg=(await s.execute(select(Message))).scalars().one()
                s.add(MessageRecipient(message_id=msg.id,agent_id=people[2]['agent_id'],kind='bcc'))
                await s.commit()
            backend=Backend(parent.path,ROOT)
            raw=await backend.read('/api/mobile-mail',dict(window='all'))
            assert [e['target'] for e in raw['events']]==['Beta']
            pc=await backend.read('/api/mail/message',dict(id=str(msg.id)))
            assert {r['kind'] for r in pc['message']['recipients']}=={'to','bcc'}
            graph=await backend.read('/api/graph',dict(all='1'))
            assert {e['kind'] for e in graph['edges']}=={'to','bcc'}
            async with runtime.sessions() as s:
                old=await s.get(Message,msg.id);old.created_ts=datetime.now(timezone.utc)-timedelta(days=2)
                await s.commit()
            assert not (await backend.read('/api/mobile-mail',dict(window='day')))['events']
            assert (await backend.read('/api/mobile-mail',dict(window='all')))['events']
            assert (await backend.read('/api/mobile-cache-page',{}))['items']==[]
    asyncio.run(run())


def test_name_selector_counts_retired_alias_and_explicit_ids_pin_active_target(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            await seed(runtime,people)
            alias=await runtime.register_local(request_id=str(uuid4()),name='ALPHA',program='fixture',model='fixture')
            await runtime.core.retire(alias['agent_id'])
            backend=Backend(parent.path,ROOT)
            before=await snapshot(runtime)
            with pytest.raises(B1Error,match='TARGET_NAME_AMBIGUOUS'):
                await backend.read('/api/edge-messages',dict(a='Alpha',b='Beta'))
            result=await backend.read('/api/edge-messages',dict(a_id=str(people[0]['agent_id']),
                b_id=str(people[1]['agent_id']),mail_instance_id=runtime.config['instance_id']))
            assert result['count']==1 and result['messages'][0]['sender_agent_id']==people[0]['agent_id']
            with pytest.raises(B1Error,match='TARGET_RETIRED'):
                await backend.read('/api/mail/recent',dict(mail_agent_id=str(alias['agent_id']),
                    mail_instance_id=runtime.config['instance_id']))
            assert await snapshot(runtime)==before
    asyncio.run(run())


def test_graph_logical_parent_excludes_standalone_preparation_root(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime,people,parent,_):
            child=await prepare(parent,'reader_child','ReaderChild','codex','fixture','fixture')
            solo=await prepare(parent,'reader_solo','ReaderSolo','codex','fixture','fixture',standalone=True)
            backend=Backend(parent.path,ROOT)
            before=await snapshot(runtime)
            graph=await backend.read('/api/graph',dict(all='1',spawn_only='1'))
            assert not graph['nodes'] and not graph['edges']
            assert len(graph['spawn'])==1 and graph['spawn'][0]['target_agent_id']==child['mail_agent_id']
            assert graph['spawn'][0]['source_agent_id']==people[0]['agent_id']
            assert solo['mail_agent_id']!=child['mail_agent_id'] and await snapshot(runtime)==before
    asyncio.run(run())

