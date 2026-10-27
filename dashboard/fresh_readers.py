"""Finite fresh ORM readers. No legacy DB discovery or Mail side effects.

Cursor mechanics and byte limits mirror graph_data's mobile readers. That
module performs legacy listener discovery on import, so this adapter imports
none of its database globals. IDs and opaque handles bind the selected I and
this reader epoch; a display name is never an arbitrary latest-row selector.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import secrets
import threading
import time

from sqlalchemy import LargeBinary, and_, cast, func, or_, select, tuple_
from agentstack_mail.models import Message as M, MessageRecipient as R
from agentstack_mail.storage_origin_contract import B1Error, name_keys
try:
    from dashboard.mobile_projection import WINDOWS, EVENT_KEY
except ModuleNotFoundError as exc:
    if exc.name != 'dashboard':raise
    from mobile_projection import WINDOWS, EVENT_KEY


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def epoch(value):
    return value.timestamp()


def iso(value):
    return value.isoformat() if value else None


def excerpt(value):
    first = value.strip().split('\n', 1)[0]
    return re.sub(r'^[#>*\\-\\s\x60]+', '', first).replace('\x60', '')[:120]


class Cursors:
    # Same bounded, query-bound, process-local contract as MobileMailCursors.
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def issue(self, scope, window, boundary, *, pair=None, direction='both', kind='mail', seen=0):
        with self.lock:
            key = secrets.token_hex(16)
            self.items[key] = (self.clock()+600, scope, window, boundary, pair, direction, kind, seen)
            while len(self.items)>256:self.items.popitem(last=False)
            return key

    def resolve(self, key, scope, window, *, pair=None, direction='both', kind='mail'):
        with self.lock:
            for old, value in list(self.items.items()):
                if value[0]<=self.clock():self.items.pop(old, None)
            value = self.items.get(key)
            if value is None or (value[1], value[2], value[4], value[5], value[6]) != (scope, window, pair, direction, kind):
                raise LookupError('mobile_cursor_expired')
            if kind=='cache':self.items.pop(key)
            return value[3], value[7]


class Reader:
    def __init__(self):
        self.nonce = secrets.token_hex(16)
        self.cursors = Cursors()

    def scope(self, instance):
        return hashed(self.nonce+':'+instance)

    @staticmethod
    def resolve(owner, instance, name=None, agent=None, expected=None):
        if expected is not None and expected!=instance:raise B1Error('FRESH_TARGET_INSTANCE_MISMATCH')
        if agent is not None:
            if expected is None or not str(agent).isdigit() or int(agent)<=0:
                raise ValueError('invalid_mail_identity')
            candidates=[a for a in owner.records.values() if a.id==int(agent)]
            if name and any(a.name!=name for a in candidates):raise B1Error('TARGET_NAME_STALE')
        else:
            keys=set(name_keys(name))
            candidates=[a for a in owner.records.values() if keys.intersection(name_keys(a.name))]
        if len(candidates)!=1:
            raise B1Error('TARGET_NAME_AMBIGUOUS' if candidates else 'TARGET_NAME_NOT_FOUND',
                candidates=[dict(mail_instance_id=instance,mail_agent_id=a.id,name=a.name,
                                 retired=a.retired_at is not None) for a in candidates])
        if candidates[0].retired_at is not None:raise B1Error('TARGET_RETIRED')
        return candidates[0].id

    def delivery(self, owner, window, now, *, public=True, pair=None, direction='both'):
        if window not in WINDOWS or direction not in {'both','forward','reverse'}:raise ValueError('invalid_mail_query')
        ids=list(owner.records)
        stmt=select(M.id, R.agent_id, M.sender_id, R.kind, M.created_ts).join(R,R.message_id==M.id).where(
            M.sender_id.in_(ids),R.agent_id.in_(ids),M.created_ts<=datetime.fromtimestamp(now,timezone.utc))
        if public:stmt=stmt.where(R.kind.in_(('to','cc')))
        if WINDOWS[window] is not None:
            stmt=stmt.where(M.created_ts>=datetime.fromtimestamp(now-WINDOWS[window],timezone.utc))
        if pair is not None:
            a,b=pair
            if a==b and direction!='both':raise ValueError('invalid_direction')
            forward=and_(M.sender_id==a,R.agent_id==b)
            reverse=and_(M.sender_id==b,R.agent_id==a)
            stmt=stmt.where(or_(forward,reverse) if direction=='both' else forward if direction=='forward' else reverse)
        elif direction!='both':raise ValueError('invalid_pair')
        return stmt

    def event(self, instance, owner, row):
        mid,rid,sid,kind,stamp=row
        return dict(key=hashed(instance+':'+str(mid)+':'+str(rid)),source=owner.records[sid].name,
            target=owner.records[rid].name,mail_instance_id=instance,source_agent_id=sid,target_agent_id=rid,
            kind=kind,event_at=epoch(stamp),count=1)

    async def content(self, owner, instance, mid):
        # Bound database materialization too, before Python decodes UTF-8.
        subject,raw,length=(await owner.session.execute(select(func.substr(M.subject,1,513),
            func.substr(cast(M.body_md,LargeBinary),1,65537),func.length(cast(M.body_md,LargeBinary))).where(M.id==mid))).one()
        body=raw[:65536].decode('utf-8',errors='ignore')
        value=dict(subject=subject[:512],body=body,subject_truncated=len(subject)>512,
                   body_truncated=length>len(body.encode()))
        revision=hashed(json.dumps([instance,value,length],ensure_ascii=False,separators=(',',':')))
        return value,revision

    async def mobile(self, owner, instance, route, q, now):
        scope=self.scope(instance)
        window=q.get('window','day')
        cursor=q.get('cursor')
        if cursor is not None and not EVENT_KEY.fullmatch(cursor):raise ValueError('invalid_cursor')
        pair=None;direction=q.get('direction','both')
        if set(q)&{'a','b','a_id','b_id','direction'}:
            pair=tuple(self.resolve(owner,instance,q.get(k),q.get(k+'_id'),q.get('mail_instance_id'))
                       for k in ('a','b'))
        if route=='/api/mobile-cache-page':window='day'
        stmt=self.delivery(owner,window,now,pair=pair,direction=direction)
        if route=='/api/mobile-message':
            key=q.get('event','');mode=q.get('mode','content')
            if not EVENT_KEY.fullmatch(key) or mode not in {'content','revision'}:raise ValueError('invalid_event')
            # Opaque event identifiers are computed in SQL; no unbounded rows or bodies.
            # The function receives non-secret I and numeric message/recipient IDs.
            connection=await owner.session.connection()
            raw=await connection.get_raw_connection()
            await raw.driver_connection.create_function('fresh_event_key',2,
                lambda mid,rid:hashed(instance+':'+str(mid)+':'+str(rid)),deterministic=True)
            found=(await owner.session.execute(stmt.where(func.fresh_event_key(M.id,R.agent_id)==key).limit(2))).all()
            if len(found)!=1:raise LookupError('mobile_event_unavailable')
            value,revision=await self.content(owner,instance,found[0][0])
            return dict(mail_instance_id=instance,scope=scope,event=self.event(instance,owner,found[0]),
                fingerprint=hashed(scope+':'+str(found[0][0])),revision=revision,**(value if mode=='content' else {}))
        cache=route=='/api/mobile-cache-page'
        seen=0
        if cursor:
            boundary,seen=self.cursors.resolve(cursor,scope,window,pair=pair,direction=direction,kind='cache' if cache else 'mail')
            mid,rid,stamp=boundary
            if cache:
                stmt=stmt.where(tuple_(M.created_ts,M.id)<tuple_(datetime.fromtimestamp(stamp,timezone.utc),mid))
            else:
                stmt=stmt.where(tuple_(M.created_ts,M.id,R.agent_id)<tuple_(datetime.fromtimestamp(stamp,timezone.utc),mid,rid))
        if cache:
            # One canonical recipient per message, same MAX(recipient_id) rule.
            sub=stmt.subquery()
            chosen=select(sub.c.id,func.max(sub.c.agent_id).label('rid')).group_by(sub.c.id).subquery()
            stmt=stmt.where(tuple_(M.id,R.agent_id).in_(select(chosen.c.id,chosen.c.rid)))
        size=min(32,2000-seen) if cache else 200
        found=(await owner.session.execute(stmt.order_by(M.created_ts.desc(),M.id.desc(),R.agent_id.desc()).limit(size+1))).all()
        items=[];last=None;reason=None
        for row in found[:size]:
            event=self.event(instance,owner,row)
            if cache:
                value,revision=await self.content(owner,instance,row[0])
                event=dict(mail_instance_id=instance,scope=scope,event=event,revision=revision,
                           fingerprint=hashed(scope+':'+str(row[0])),**value)
                if len(json.dumps(dict(scope=scope,items=[*items,event],next_cursor='f'*32,complete=False,reason='count_limit'),
                                  separators=(',',':'),allow_nan=False).encode())>4*1024*1024-512:
                    reason='serialized_batch_limit';break
            items.append(event);last=(row[0],row[1],epoch(row[4]))
        more=len(found)>len(items);seen+=len(items)
        if cache and more and seen>=2000:reason='count_limit'
        token=self.cursors.issue(scope,window,last,pair=pair,direction=direction,
            kind='cache' if cache else 'mail',seen=seen) if more and last and (not cache or seen<2000) else None
        if cache:
            return dict(mail_instance_id=instance,scope=scope,items=items,next_cursor=token,
                        complete=not more,reason=reason,observed_at=int(now))
        result=dict(mail_instance_id=instance,status='available',scope=scope,window=window,events=items,
                    next_cursor=token,observed_at=int(now),pair=[q.get('a'),q.get('b')] if pair else None,direction=direction)
        if route=='/api/mobile-mail':
            base=self.delivery(owner,window,now).subquery()
            counts=(await owner.session.execute(select(base.c.sender_id,base.c.agent_id,base.c.kind,
                func.count(),func.max(base.c.created_ts)).group_by(base.c.sender_id,base.c.agent_id,base.c.kind)
                .order_by(func.count().desc(),base.c.sender_id,base.c.agent_id,base.c.kind).limit(10001))).all()
            edges=[dict(source=owner.records[s].name,target=owner.records[r].name,kind=k,count=n,last_ts=epoch(t),
                        mail_instance_id=instance,source_agent_id=s,target_agent_id=r) for s,r,k,n,t in counts[:10000]]
            result.update(edges=edges,events_truncated=more,edges_truncated=len(counts)>10000,
                          invalid_timestamps=0,degraded=len(counts)>10000)
        return result

    async def pc_messages(self, owner, instance, route, q, now):
        try:limit=int(q.get('limit',50 if route.endswith('thread') else 40))
        except (TypeError,ValueError):raise ValueError('invalid_limit') from None
        if not 1<=limit<=100:raise ValueError('invalid_limit')
        ids=list(owner.records)
        picked=select(M.id).where(M.sender_id.in_(ids))
        ascending=route.endswith('thread')
        if route.endswith('message'):
            mid=int(q.get('id','0'))
            if mid<=0:raise ValueError('invalid_message')
            picked=picked.where(M.id==mid);limit=1
        elif ascending:
            thread=q.get('thread_id','')
            if not thread or len(thread)>128:raise ValueError('invalid_thread')
            picked=picked.where(M.thread_id==thread)
        if 'agent' in q or 'mail_agent_id' in q:
            a=self.resolve(owner,instance,q.get('agent'),q.get('mail_agent_id'),q.get('mail_instance_id'))
            picked=picked.where(or_(M.sender_id==a,M.id.in_(select(R.message_id).where(R.agent_id==a))))
        picked=picked.order_by(M.created_ts.asc() if ascending else M.created_ts.desc(),
                               M.id.asc() if ascending else M.id.desc()).limit(limit)
        cols=select(M.id,M.sender_id,M.created_ts,M.thread_id,M.importance).where(M.id.in_(picked)).order_by(
            M.created_ts.asc() if ascending else M.created_ts.desc(),M.id.asc() if ascending else M.id.desc())
        result=[]
        for mid,sid,stamp,thread,importance in (await owner.session.execute(cols)).all():
            recipients=(await owner.session.execute(select(R.agent_id,R.kind).where(
                R.message_id==mid,R.agent_id.in_(ids)).order_by(R.kind,R.agent_id).limit(5001))).all()
            if len(recipients)>5000:raise B1Error('FRESH_ROSTER_LIMIT')
            value,_=await self.content(owner,instance,mid)
            msg=dict(id=mid,mail_instance_id=instance,sender_agent_id=sid,sender=owner.records[sid].name,
                ts=int(epoch(stamp)),thread_id=thread,subject=value['subject'],importance=importance,
                recipients=[dict(name=owner.records[r].name,kind=k,mail_agent_id=r,mail_instance_id=instance) for r,k in recipients],
                subject_truncated=value['subject_truncated'])
            if route.endswith('message'):msg.update(body_md=value['body'],body_truncated=value['body_truncated'])
            else:msg['excerpt']=excerpt(value['body'])
            result.append(msg)
        if route.endswith('message'):
            if not result:raise LookupError('message_not_found')
            return dict(ok=True,mail_instance_id=instance,message=result[0])
        return dict(ok=True,mail_instance_id=instance,now=int(now),messages=result)

    async def read(self, owner, instance, route, q, now=None):
        now=time.time() if now is None else now
        if len(owner.records)>5000:raise B1Error('FRESH_ROSTER_LIMIT')
        if q.get('mail_instance_id',instance)!=instance:raise B1Error('FRESH_TARGET_INSTANCE_MISMATCH')
        if route.startswith('/api/mobile-'):return await self.mobile(owner,instance,route,q,now)
        if route.startswith('/api/mail/'):return await self.pc_messages(owner,instance,route,q,now)
        if route=='/api/graph':
            try:days=float(q.get('days','4'))
            except ValueError:days=4.0
            if not math.isfinite(days) or days<=0:days=4.0
            show_all=q.get('all') in ('1','true')
            base=self.delivery(owner,'all',now,public=False).subquery()
            counts=(await owner.session.execute(select(base.c.sender_id,base.c.agent_id,base.c.kind,
                func.count(),func.max(base.c.created_ts)).group_by(base.c.sender_id,base.c.agent_id,base.c.kind)
                .order_by(func.count().desc(),base.c.sender_id,base.c.agent_id,base.c.kind).limit(10001))).all()
            edges=[dict(source=owner.records[s].name,target=owner.records[r].name,kind=k,count=n,last_ts=epoch(t),
                        mail_instance_id=instance,source_agent_id=s,target_agent_id=r) for s,r,k,n,t in counts[:10000]]
            active={a:0 for a in owner.records}
            for e in edges:
                for a in (e['source_agent_id'],e['target_agent_id']):active[a]+=e['count']
            nodes=[dict(mail_instance_id=instance,mail_agent_id=a.id,name=a.name,program=a.program,
                model=a.model,running=None,state='retired' if a.retired_at else 'unknown',retired=a.retired_at is not None,
                last_active=epoch(a.last_active_ts),activity=active[a.id]) for a in owner.records.values()]
            selected={n['mail_agent_id'] for n in nodes if show_all or n['last_active']>=now-days*86400}
            nodes=[n for n in nodes if n['mail_agent_id'] in selected]
            edges=[e for e in edges if e['source_agent_id'] in selected and e['target_agent_id'] in selected]
            return dict(mail_instance_id=instance,nodes=[] if q.get('spawn_only') in ('1','true') else nodes,
                edges=[] if q.get('spawn_only') in ('1','true') else edges,spawn=[],total=len(nodes),
                _selected=selected,degraded=len(counts)>10000,timestamp_diagnostics=dict(invalid_count=0,fields={}),ts=int(now))
        pair=None
        if route=='/api/edge-messages':
            pair=tuple(self.resolve(owner,instance,q.get(k),q.get(k+'_id'),q.get('mail_instance_id')) for k in ('a','b'))
        stmt=self.delivery(owner,'all',now,public=False,pair=pair)
        limit=max(1,min(200,int(q.get('limit','60' if pair else '80'))))
        if route=='/api/messages-since':
            since=max(int(q.get('since','0')),int(now)-300)
            stmt=stmt.where(M.created_ts>datetime.fromtimestamp(since,timezone.utc))
        elif route=='/api/agent-history':
            names=q.get('names',q.get('name','')).split(',')
            if len(names)>100:raise ValueError('invalid_names')
            selected=[self.resolve(owner,instance,n,q.get('mail_agent_id'),q.get('mail_instance_id')) for n in names]
            hours=max(1,min(168,int(q.get('hours') or 168)))
            stmt=stmt.where(or_(M.sender_id.in_(selected),R.agent_id.in_(selected)),
                M.created_ts>=datetime.fromtimestamp(now-hours*3600,timezone.utc));limit=200
        found=(await owner.session.execute(stmt.order_by(M.created_ts.desc(),M.id.desc(),R.agent_id.desc()).limit(limit))).all()
        messages=[];seen=set()
        for row in found:
            mid,rid,sid,kind,stamp=row
            if pair and mid in seen:continue
            seen.add(mid)
            subject,thread,topic,importance,ack=(await owner.session.execute(select(
                func.substr(M.subject,1,512),M.thread_id,M.topic,M.importance,M.ack_required).where(M.id==mid))).one()
            value,_=await self.content(owner,instance,mid)
            msg=dict(id=mid,mail_instance_id=instance,sender_agent_id=sid,recipient_agent_id=rid,
                     ts=int(epoch(stamp)),ts_unix=epoch(stamp),sender=owner.records[sid].name,recipient=owner.records[rid].name,
                     subject=subject,importance=importance,kind=kind,thread_id=thread)
            if pair:
                mr=await owner.session.get(R,(mid,rid))
                msg.update(ts=iso(stamp),body=value['body'],body_truncated=value['body_truncated'],topic=topic,
                    ack_required=bool(ack),read_ts=iso(mr.read_ts),ack_ts=iso(mr.ack_ts))
            else:msg.update(subject=subject[:90],excerpt=excerpt(value['body']))
            messages.append(msg)
        if pair:return dict(ok=True,mail_instance_id=instance,a=owner.records[pair[0]].name,b=owner.records[pair[1]].name,
                            count=len(messages),messages=messages)
        if route=='/api/messages-since':
            return dict(ok=True,mail_instance_id=instance,now=int(now),since=since,messages=list(reversed(messages)))
        if route=='/api/agent-history':
            events=[dict(m,kind='mail_sent' if m['sender_agent_id'] in selected else 'mail_recv',
                ref=m['recipient'] if m['sender_agent_id'] in selected else m['sender'],rcpt_n=1,
                agent=m['sender'] if m['sender_agent_id'] in selected else m['recipient']) for m in reversed(messages)]
            since=now-hours*3600
            for aid in selected:
                a=owner.records[aid]
                for kind,stamp in (('spawn',a.inception_ts),('retire',a.retired_at)):
                    if stamp and since<=epoch(stamp)<=now:
                        events.append(dict(id=None,ts=epoch(stamp),kind=kind,agent=a.name,ref='',subject='agent '+kind,
                            importance='normal',sender=a.name,recipient='',mail_instance_id=instance,mail_agent_id=aid))
            events=sorted(events,key=lambda e:e['ts'])[-200:]
            return dict(ok=True,mail_instance_id=instance,name=q.get('name'),names=names,hours=hours,
                auto_range=not bool(q.get('hours')),since_ts=since,now_ts=now,range=dict(start_ts=since,end_ts=now),
                total_raw=len(events),events=events,include_pane_states=False,
                initial_state=dict(ts=since,alive_agents=[owner.records[a].name for a in selected]),
                inception_ts=epoch(owner.records[selected[0]].inception_ts),retired_ts=None)
        raise ValueError('invalid_route')


ROUTES = {
    '/api/graph': {'all','days','spawn_only'},
    '/api/edge-messages': {'a','b','a_id','b_id','limit'},
    '/api/messages-since': {'since','limit'},
    '/api/agent-history': {'name','names','mail_agent_id','hours','include_pane_states'},
    '/api/mail/recent': {'limit','agent','mail_agent_id'},
    '/api/mail/message': {'id'},
    '/api/mail/thread': {'thread_id','limit'},
    '/api/mobile-mail': {'window'},
    '/api/mobile-message': {'window','event','mode'},
    '/api/mobile-mail-page': {'window','cursor','a','b','a_id','b_id','direction'},
    '/api/mobile-cache-page': {'cursor'},
}

