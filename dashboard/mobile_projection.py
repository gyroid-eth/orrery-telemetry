"""Explicit, bounded view-only projections; no filesystem or provider readers."""
from __future__ import annotations

import math
import json
import re
import threading
import time
from collections import OrderedDict, deque
from copy import deepcopy
from typing import Callable

WINDOWS = {'all': None, 'day': 86400, '15m': 900,
           **{'m'+str(n): n*60 for n in range(15, 43201, 15)}}
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,95}\Z')
MODEL = re.compile(r'[A-Za-z0-9_.:+-]+(?:/[A-Za-z0-9_.:+-]+)?\Z')
EVENT_KEY = re.compile(r'[a-f0-9]{32}\Z')
STATES = {'run', 'idle', 'finished', 'gone', 'retired', 'unknown'}
ACTS = {'work', 'wait', 'ask', 'question'}


def png_image(data):
    """Static PNG envelope check; actual decode may still fail to initials."""
    return (isinstance(data,bytes) and 33<=len(data)<=512*1024 and
            data[:8]==b'\x89PNG\r\n\x1a\n' and data[8:12]==b'\0\0\0\r' and
            data[12:16]==b'IHDR' and
            1<=int.from_bytes(data[16:20],'big')<=1024 and
            1<=int.from_bytes(data[20:24],'big')<=1024)


def number(value, *, minimum=0, maximum=10**12):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and minimum <= value <= maximum else None


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError('invalid_public_name')
    return value


def rows(raw, field, limit):
    if not isinstance(raw, dict) or raw.get('error') or not isinstance(raw.get(field), list):
        raise ValueError('upstream_schema')
    value = raw[field]
    if len(value) > limit or any(not isinstance(x, dict) for x in value):
        raise ValueError('upstream_bounds')
    return value


def project_agent(row):
    retired = row.get('retired') is True or row.get('category') == 'retired'
    running = False if retired else row.get('running') if type(row.get('running')) is bool else None
    state = ('retired' if retired else row.get('state') if row.get('state') in STATES
             else row.get('category') if row.get('category') in STATES
             else 'run' if running is True else 'gone' if running is False else 'unknown')
    # A stopped runtime cannot retain an old working/approval projection.
    act = row.get('act_state') if running is True and row.get('act_state') in ACTS else ''
    if state == 'run' and running is not True:
        state = 'gone' if running is False else 'unknown'
    provider = str(row.get('provider') or '').lower()
    aliases = {'claude': 'anthropic', 'claude-code': 'anthropic', 'codex': 'openai',
               'codex-cli': 'openai', 'dsh': 'deepseek', 'gemini': 'google'}
    provider = aliases.get(provider, provider)
    locality = 'local' if row.get('locality') == 'local' or provider in {'strata', 'local'} else None
    if locality:
        provider = 'local'
    if provider not in {'anthropic', 'openai', 'deepseek', 'google', 'local'}:
        program = aliases.get(str(row.get('program') or '').lower(), '')
        provider = program if program in {'anthropic','openai','google'} else ''
    model = row.get('model_raw') or row.get('model') or ''
    if not isinstance(model, str) or len(model) > 120 or not MODEL.fullmatch(model):
        model = ''
    result = {'name': name(row.get('name')), 'provider': provider, 'model': model,
            'locality': locality, 'running': running, 'retired': retired,
            'state': state, 'act_state': act,
            'ctx_used': number(row.get('ctx_used'), maximum=100) if running is True else None,
            # HTTP observation time does not prove a context sampling time.
            'context_sampled_at': None, 'last_active': number(row.get('last_active'))}
    if 'mail_instance_id' in row or 'mail_agent_id' in row:
        instance=row.get('mail_instance_id');agent=row.get('mail_agent_id')
        # FreshRuntime publishes uuid4().hex. Keep those opaque bytes unchanged;
        # canonical hyphenated UUIDs were only a development fixture, not a contract.
        if not isinstance(instance,str) or not re.fullmatch(r'[0-9a-f]{32}',instance) or type(agent) is not int or agent<=0:
            raise ValueError('invalid_mail_identity')
        result.update(mail_instance_id=instance,mail_agent_id=agent)
    return result


def project_agents(raw):
    result = [project_agent(x) for x in rows(raw, 'agents', 5000)]
    identities=[(x['mail_instance_id'],x['mail_agent_id']) if 'mail_instance_id' in x else x['name'] for x in result]
    if len(set(identities)) != len(result):
        raise ValueError('duplicate_public_name')
    if raw.get('mail_instance_id') is not None and any(a.get('mail_instance_id')!=raw['mail_instance_id'] for a in result):
        raise ValueError('invalid_mail_identity')
    return result


def delivery_identity(row, instance=None):
    """Optional fresh endpoints are a complete I/A tuple, never names."""
    if instance is None and not any(k in row for k in ('mail_instance_id','source_agent_id','target_agent_id')):return {}
    value=row.get('mail_instance_id')
    if (not isinstance(value,str) or not EVENT_KEY.fullmatch(value) or
            instance is not None and value!=instance or
            any(type(row.get(k)) is not int or row[k]<=0 for k in ('source_agent_id','target_agent_id'))):
        raise ValueError('invalid_mail_identity')
    return {k:row[k] for k in ('mail_instance_id','source_agent_id','target_agent_id')}


def project_graph(raw):
    nodes = [project_agent(x) for x in rows(raw, 'nodes', 5000)]
    spawn = []
    for row in rows(raw, 'spawn', 10000):
        spawn.append({'source': name(row.get('source')), 'target': name(row.get('target')),
                      **delivery_identity(row,raw.get('mail_instance_id'))})
    instance=raw.get('mail_instance_id')
    if instance is not None and (not isinstance(instance,str) or not EVENT_KEY.fullmatch(instance) or
                                any(n.get('mail_instance_id')!=instance for n in nodes)):
        raise ValueError('invalid_mail_identity')
    return {'nodes': nodes, 'spawn': spawn, 'degraded': raw.get('degraded') is True,
            **({'mail_instance_id':instance} if instance is not None else {})}


def project_mail(raw, window):
    if not isinstance(raw, dict) or raw.get('status') != 'available' or raw.get('window') != window:
        raise ValueError('mail_unavailable')
    if any(type(raw.get(k)) is not bool for k in ('events_truncated','edges_truncated','degraded')):
        raise ValueError('mail_quality')
    edges = []
    for row in rows(raw, 'edges', 10000):
        count = number(row.get('count'), minimum=1)
        ts = number(row.get('last_ts'))
        if row.get('kind') not in {'to', 'cc'} or count is None or ts is None or int(count) != count:
            raise ValueError('mail_schema')
        edges.append({'source': name(row.get('source')), 'target': name(row.get('target')),
                      'count': int(count), 'last_ts': ts, 'kind': row['kind'],
                      **delivery_identity(row,raw.get('mail_instance_id'))})
    events = {}
    for row in rows(raw, 'events', 200):
        key = row.get('key')
        ts = number(row.get('event_at'))
        if not isinstance(key, str) or not EVENT_KEY.fullmatch(key) or ts is None or row.get('kind') not in {'to','cc'} or row.get('count') != 1:
            raise ValueError('mail_event_schema')
        event = {'key': key, 'source': name(row.get('source')), 'target': name(row.get('target')),
                 'kind': row['kind'], 'count': 1, 'event_at': ts,
                 **delivery_identity(row,raw.get('mail_instance_id'))}
        if key in events and events[key] != event:
            raise ValueError('conflicting_mail_event')
        events[key] = event
    scope=raw.get('scope');cursor=raw.get('next_cursor')
    if not isinstance(scope,str) or not EVENT_KEY.fullmatch(scope) or (cursor is not None and (not isinstance(cursor,str) or not EVENT_KEY.fullmatch(cursor))):
        raise ValueError('mail_scope')
    return {'edges': edges, 'events': list(events.values()), 'scope':scope,'next_cursor':cursor,
            **({'mail_instance_id':raw['mail_instance_id']} if 'mail_instance_id' in raw else {}),
            'events_truncated': raw.get('events_truncated') is True,
            'edges_truncated': raw.get('edges_truncated') is True,
            'degraded': raw.get('degraded') is True}


class SnapshotStore:
    """Single-flight bounded snapshots, retaining failed observations as stale.

    fetch takes an internal route key, never a user URL. This layer has no
    knowledge of credentials, Mail file paths, panes, or provider helpers.
    """
    def __init__(self, fetch: Callable, *, clock=time.time, refresh_seconds=10):
        self.fetch, self.clock, self.refresh_seconds = fetch, clock, refresh_seconds
        self._lock = threading.Lock()
        self._parts = {}
        self._previous = {}
        self._states = deque(maxlen=200)
        self._state_seq = 0
        self._published = OrderedDict()

    def _refresh(self, key, projector, now):
        part = self._parts.setdefault(key, {'attempted': None, 'observed': None, 'value': None, 'error': None})
        if part['attempted'] is not None and now - part['attempted'] < self.refresh_seconds:
            return
        part['attempted'] = now
        try:
            raw = self.fetch(key)
            value = projector(raw)
        except (OSError, TimeoutError, ValueError, TypeError):
            part['error'] = 'upstream_unavailable'
            return
        part.update(value=value, observed=now, error=None, instance=raw.get('mail_instance_id'))
        if key == 'agents':
            previous_ids=set(self._previous)
            fresh_ids={(a.get('mail_instance_id'),a.get('mail_agent_id')) for a in value if 'mail_instance_id' in a}
            if fresh_ids and fresh_ids!=previous_ids:
                self._states.clear();self._state_seq=0
            next_states = {}
            for agent in value:
                state = agent['act_state'] or agent['state']
                if state == 'unknown':
                    continue
                key=(agent['mail_instance_id'],agent['mail_agent_id']) if 'mail_instance_id' in agent else agent['name']
                next_states[key] = state
                old = self._previous.get(key)
                if old is not None and old != state:
                    self._state_seq += 1
                    event={'key': 'state-' + str(self._state_seq),
                                             'name': agent['name'], 'from': old, 'to': state,
                                             'observed_at': now, 'event_at': None}
                    if 'mail_instance_id' in agent:event.update(mail_instance_id=agent['mail_instance_id'],mail_agent_id=agent['mail_agent_id'])
                    self._states.appendleft(event)
            self._previous = next_states

    def _source(self, key, now):
        part = self._parts.get(key, {})
        stamp = part.get('observed')
        stale = stamp is not None and (bool(part.get('error')) or now - stamp > 30)
        status = 'unavailable' if stamp is None else 'stale' if stale else 'available'
        return {'status': status, 'observed_at': stamp,
                'age_seconds': None if stamp is None else max(0, int(now - stamp)),
                'error': part.get('error')}

    def known_agent(self,window,nm):
        published=self._published.get(window)
        return bool(published and any(a['name']==nm for a in published['agents']))

    def snapshot(self, window='day'):
        if window not in WINDOWS:
            raise ValueError('invalid_window')
        # Concurrent browser polls share the prior snapshot rather than create
        # a queue of repeated expensive agents/graph observations.
        acquired = self._lock.acquire(blocking=False)
        now = self.clock()
        if not acquired:
            # Only published immutable values are visible to concurrent readers.
            # In particular do not iterate a state deque another refresh writes.
            cached = self._published.get(window)
            if cached is None:
                return {'schema':1, 'observed_at':now, 'window':window, 'refreshing':True,
                        'period':{'seconds':WINDOWS[window],'activity_unknown':None},
                        'sources':{k:{'status':'unavailable','observed_at':None,'age_seconds':None,'error':None} for k in ('agents','graph','mail')},
                        'agents':[], 'graph':{'edges':[],'spawn':[],'degraded':False,'unrepresented_edges':0},
                        'timeline':{'mail':[],'states':[],'events_truncated':None,'edges_truncated':None,'states_truncated':False},
                        'quota':{'status':'unavailable'}}
            result = deepcopy(cached)
            result['refreshing'] = True
            for source in result['sources'].values():
                stamp = source['observed_at']
                source['age_seconds'] = None if stamp is None else max(0,int(now-stamp))
                if stamp is not None and now-stamp>30:source['status']='stale'
            return result
        try:
            self._refresh('agents', project_agents, now)
            self._refresh('graph', project_graph, now)
            self._refresh('mail:' + window, lambda raw: project_mail(raw, window), now)
            result = self._compose(window, now)
            self._published[window] = result
            self._published.move_to_end(window)
            while len(self._published)>4:
                evicted,_=self._published.popitem(last=False)
                self._parts.pop('mail:'+evicted,None)
            return deepcopy(result)
        finally:
            if acquired:
                self._lock.release()

    def _compose(self, window, now):
        value = lambda key: self._parts.get(key, {}).get('value')
        graph = value('graph') or {'nodes': [], 'spawn': [], 'degraded': False}
        mail = value('mail:' + window)
        roster,activity={},{}
        # Both sources obey one wall-clock cutoff. State fields prefer agents,
        # but an absent/older timestamp cannot erase the graph observation.
        # Running alone is not recent activity; HTTP time is not activity time.
        agents=value('agents') or []
        instance=self._parts.get('agents',{}).get('instance')
        fresh=instance is not None or any('mail_instance_id' in a for a in agents)
        if fresh:
            instance=instance or agents[0]['mail_instance_id']
            if graph.get('mail_instance_id')!=instance:graph={'nodes':[],'spawn':[],'degraded':True}
            if mail is not None and mail.get('mail_instance_id')!=instance:mail=None
        for a in [*([] if fresh else graph['nodes']),*agents]:
            key=(a['mail_instance_id'],a['mail_agent_id']) if 'mail_instance_id' in a else a['name']
            roster[key]=dict(a)
            stamp=a['last_active']
            if stamp is not None and stamp<=now:
                activity[key]=max(activity.get(key,stamp),stamp)
        if mail is not None:
            for edge in mail['edges']:
                stamp=edge['last_ts']
                if stamp<=now:
                    for endpoint in ('source','target'):
                        nm=(instance,edge[endpoint+'_agent_id']) if fresh else edge[endpoint]
                        activity[nm]=max(activity.get(nm,stamp),stamp)
        unknown=sum(nm not in activity for nm in roster)
        lower=None if WINDOWS[window] is None else now-WINDOWS[window]
        roster={nm:{**a,'last_active':activity.get(nm)} for nm,a in roster.items()
                if lower is None or nm in activity and activity[nm]>=lower}
        names = {a['name'] for a in roster.values()}
        represented=lambda e: all((instance,e[k+'_agent_id']) in roster for k in ('source','target')) if fresh else e['source'] in names and e['target'] in names
        edges = [] if mail is None else [e for e in mail['edges'] if represented(e)]
        spawn = [e for e in graph['spawn'] if represented(e)]
        sources = {key: self._source(key if key != 'mail' else 'mail:' + window, now)
                   for key in ('agents','graph','mail')}
        if fresh:
            for key,available in (('graph',graph.get('mail_instance_id')==instance),('mail',mail is not None)):
                if available:continue
                sources[key]={'status':'unavailable','observed_at':None,'age_seconds':None,
                              'error':'fresh_instance_mismatch'}
        payload = {'schema': 1, 'observed_at': now, 'window': window, 'sources': sources,
                   'period':{'seconds':WINDOWS[window],'activity_unknown':unknown},
                   'agents': list(roster.values()),
                   'graph': {'edges': edges, 'spawn': spawn,
                             'unrepresented_edges': 0 if mail is None else len(mail['edges']) - len(edges),
                             'degraded': graph['degraded'] or (bool(mail['degraded']) if mail else False)},
                   'timeline': {'mail': [] if mail is None else mail['events'],
                                'scope':None if mail is None else mail['scope'],
                                'next_cursor':None if mail is None else mail['next_cursor'],
                                'states': [s for s in self._states if s['name'] in names and
                                           (lower is None or s['observed_at']>=lower)],
                                'states_truncated': self._state_seq > self._states.maxlen,
                                'events_truncated': None if mail is None else mail['events_truncated'],
                                'edges_truncated': None if mail is None else mail['edges_truncated']},
                   'quota': {'status': 'unavailable'}}
        return deepcopy(payload)


def mail_events(raw):
    """Project the same fixed metadata event fields for a page and a snapshot."""
    window=raw.get('window')
    if window not in WINDOWS:raise ValueError('invalid_window')
    return project_mail({**raw,'edges':[],'events_truncated':False,
                         'edges_truncated':False,'degraded':False},window)


def checked_content(raw, *, content=True):
    if not isinstance(raw,dict):raise ValueError('content_schema')
    for field in ('scope','fingerprint','revision'):
        if not isinstance(raw.get(field),str) or not EVENT_KEY.fullmatch(raw[field]):raise ValueError('content_identity')
    event=mail_events({'status':'available','window':'all','scope':raw['scope'],'events':[raw.get('event')]})['events'][0]
    result={k:raw[k] for k in ('scope','fingerprint','revision')};result['event']=event
    if content:
        if (not isinstance(raw.get('subject'),str) or len(raw['subject'])>512 or
                not isinstance(raw.get('body'),str) or len(raw['body'].encode())>65536 or
                any(type(raw.get(k)) is not bool for k in ('subject_truncated','body_truncated'))):raise ValueError('content_bounds')
        result.update({k:raw[k] for k in ('subject','body','subject_truncated','body_truncated')})
    return result


class MobileMailStore:
    """Reader-memory-only bodies and issued metadata; neither authorizes a read.

    Every tap revalidates the current canonical delivery and content revision.
    Cache entries contain their own metadata and expire together. The issued
    key registry is only an additional disclosure bound, never an auth token.
    """
    def __init__(self,upstream,*,clock=time.time,capacity=2000,budget=32*1024*1024):
        self.upstream,self.clock,self.capacity,self.budget=upstream,clock,capacity,budget
        self._lock=threading.RLock();self._issued=OrderedDict();self._cache=OrderedDict()
        self._scope=None;self._generation=0;self._bytes=0;self._status='idle';self._reason=None
        self._stop=threading.Event();self._thread=None;self._next_sweep=0;self._closed=False

    def _clear(self):
        self._issued.clear();self._cache.clear();self._bytes=0;self._generation+=1

    def _expire(self):
        now=self.clock()
        for key,value in list(self._issued.items()):
            if value[0]<=now:self._issued.pop(key,None)
        for key,(expiry,size,_) in list(self._cache.items()):
            if expiry<=now:self._cache.pop(key,None);self._bytes-=size

    def _register(self,scope,window,events):
        with self._lock:
            if self._closed:raise LookupError('reader_stopped')
            if self._scope!=scope:
                self._clear();self._scope=scope;self._status='idle';self._next_sweep=0
            self._expire()
            for event in events:
                self._issued[(window,event['key'])]=(self.clock()+600,dict(event))
                self._issued.move_to_end((window,event['key']))
            while len(self._issued)>2000:self._issued.popitem(last=False)

    def observe(self,snapshot):
        if snapshot.get('sources',{}).get('mail',{}).get('status')!='available':return
        timeline=snapshot.get('timeline',{})
        try:
            raw={'status':'available','window':snapshot['window'],'scope':timeline.get('scope'),
                 'next_cursor':timeline.get('next_cursor'),'events':timeline.get('mail')}
            value=mail_events(raw)
        except (ValueError,TypeError,KeyError):return
        self._register(value['scope'],snapshot['window'],value['events'])

    def page(self,window,cursor=None,*,pair=None,direction='both'):
        raw=self.upstream.mail_page(window,cursor,pair=pair,direction=direction)
        value=mail_events(raw)
        if raw.get('pair')!=(list(pair) if pair else None) or raw.get('direction')!=direction:raise ValueError('page_context')
        self._register(value['scope'],window,value['events'])
        return {'window':window,'events':value['events'],'scope':value['scope'],
                'next_cursor':value['next_cursor'],'pair':raw['pair'],'direction':direction}

    def _put(self,item,generation):
        # Count serialized content+metadata bytes, not Python RSS.
        encoded=len(json.dumps(item,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())
        with self._lock:
            if self._closed or generation!=self._generation or item['scope']!=self._scope:return False
            self._expire();key=item['fingerprint']
            if key in self._cache:self._bytes-=self._cache.pop(key)[1]
            now=self.clock();expiry=min(now+86400,item['event']['event_at']+86400)
            if expiry<=now or encoded>self.budget:return False
            evicted=False
            while self._cache and (len(self._cache)>=self.capacity or self._bytes+encoded>self.budget):
                self._bytes-=self._cache.popitem(last=False)[1][1];evicted=True
            self._cache[key]=(expiry,encoded,deepcopy(item));self._bytes+=encoded
            return not evicted

    def message(self,window,key):
        with self._lock:
            self._expire();issued=self._issued.get((window,key))
            if self._closed or issued is None:raise LookupError('event_not_issued_or_expired')
            expected=dict(issued[1]);scope=self._scope;generation=self._generation
        try:
            revision=checked_content(self.upstream.message(window,key,'revision'),content=False)
            if revision['scope']!=scope:
                with self._lock:
                    if generation==self._generation:self._clear();self._scope=None
                raise LookupError('event_scope_changed')
            if revision['event']!=expected:raise LookupError('event_changed')
            with self._lock:
                cached=self._cache.get(revision['fingerprint'])
                item=deepcopy(cached[2]) if cached and cached[2]['revision']==revision['revision'] else None
                if item is not None:self._cache.move_to_end(revision['fingerprint'])
            if item is None:
                item=checked_content(self.upstream.message(window,key,'content'))
                if any(item[k]!=revision[k] for k in ('scope','event','fingerprint','revision')):raise LookupError('message_changed_during_read')
                # Older metadata events are valid on demand, outside the cache.
                if item['event']['event_at']>=self.clock()-86400:self._put(item,generation)
            else:item['event']=expected  # one cached message can have several to/cc rows
            with self._lock:
                if self._closed or generation!=self._generation:raise LookupError('reader_generation_changed')
            return {k:item[k] for k in ('event','subject','body','subject_truncated','body_truncated')}
        except (OSError,ValueError,LookupError,TypeError,KeyError):
            with self._lock:
                # A failed canonical check must never leave stale bodies usable.
                if generation==self._generation:
                    self._cache.clear();self._bytes=0;self._status='unavailable';self._reason='revalidation_failed'
            raise

    def progress(self):
        with self._lock:
            self._expire()
            return {'status':self._status,'messages':len(self._cache),'logical_bytes':self._bytes,
                    'max_messages':self.capacity,'max_logical_bytes':self.budget,'reason':self._reason}

    def start_warm(self):
        with self._lock:
            if (self._closed or self._scope is None or self.clock()<self._next_sweep or
                    self._thread is not None and self._thread.is_alive()):return
            self._status='warming';self._reason=None
            self._thread=threading.Thread(target=self._warm,args=(self._generation,self._scope),daemon=True)
            self._thread.start()

    def _warm(self,generation,scope):
        start=time.monotonic();cursor=None;seen=set();complete=False;reason=None
        try:
            for _ in range(63):
                if self._stop.is_set() or time.monotonic()-start>=120:reason='time_limit';break
                with self._lock:
                    if generation!=self._generation:return
                raw=self.upstream.cache_page(cursor)
                if not isinstance(raw,dict):raise ValueError('cache_scope')
                if raw.get('scope')!=scope:
                    with self._lock:
                        if generation==self._generation:
                            self._clear();self._scope=None;self._status='unavailable';self._reason='scope_changed'
                    return
                batch=rows(raw,'items',32)
                if type(raw.get('complete')) is not bool:raise ValueError('cache_schema')
                for row in batch:
                    item=checked_content(row)
                    if item['scope']!=scope or item['event']['event_at']>self.clock():raise ValueError('cache_identity')
                    if not self._put(item,generation):reason='capacity_or_expiry'
                    seen.add(item['fingerprint'])
                if len(seen)>2000:raise ValueError('cache_count')
                cursor=raw.get('next_cursor')
                if cursor is not None and (not isinstance(cursor,str) or not EVENT_KEY.fullmatch(cursor)):raise ValueError('cache_cursor')
                if raw.get('reason'):reason='upstream_partial'
                if raw['complete']:
                    if cursor is not None:raise ValueError('cache_cursor')
                    complete=True;break
                if cursor is None:reason=reason or 'upstream_partial';break
                if self._stop.wait(.025):return
            if not complete:reason=reason or 'page_or_time_limit'
            with self._lock:
                if generation!=self._generation or self._closed:return
                if complete:
                    for key in list(self._cache):
                        if key not in seen:self._bytes-=self._cache.pop(key)[1]
                self._status='ready' if complete and reason is None else 'partial';self._reason=reason
        except (OSError,ValueError,LookupError,TypeError,KeyError):
            with self._lock:
                if generation==self._generation:
                    self._cache.clear();self._bytes=0;self._status='unavailable';self._reason='upstream_unavailable'
        finally:
            with self._lock:self._next_sweep=self.clock()+60

    def close(self):
        with self._lock:self._closed=True;self._clear();self._scope=None;self._stop.set();thread=self._thread
        if thread is not None:thread.join(3)
