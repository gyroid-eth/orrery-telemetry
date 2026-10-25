"""Explicit, bounded view-only projections; no filesystem or provider readers."""
from __future__ import annotations

import math
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
    return {'name': name(row.get('name')), 'provider': provider, 'model': model,
            'locality': locality, 'running': running, 'retired': retired,
            'state': state, 'act_state': act,
            'ctx_used': number(row.get('ctx_used'), maximum=100) if running is True else None,
            # HTTP observation time does not prove a context sampling time.
            'context_sampled_at': None, 'last_active': number(row.get('last_active'))}


def project_agents(raw):
    result = [project_agent(x) for x in rows(raw, 'agents', 5000)]
    if len({x['name'] for x in result}) != len(result):
        raise ValueError('duplicate_public_name')
    return result


def project_graph(raw):
    nodes = [project_agent(x) for x in rows(raw, 'nodes', 5000)]
    spawn = []
    for row in rows(raw, 'spawn', 10000):
        spawn.append({'source': name(row.get('source')), 'target': name(row.get('target'))})
    return {'nodes': nodes, 'spawn': spawn, 'degraded': raw.get('degraded') is True}


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
                      'count': int(count), 'last_ts': ts, 'kind': row['kind']})
    events = {}
    for row in rows(raw, 'events', 200):
        key = row.get('key')
        ts = number(row.get('event_at'))
        if not isinstance(key, str) or not EVENT_KEY.fullmatch(key) or ts is None or row.get('kind') not in {'to','cc'} or row.get('count') != 1:
            raise ValueError('mail_event_schema')
        event = {'key': key, 'source': name(row.get('source')), 'target': name(row.get('target')),
                 'kind': row['kind'], 'count': 1, 'event_at': ts}
        if key in events and events[key] != event:
            raise ValueError('conflicting_mail_event')
        events[key] = event
    return {'edges': edges, 'events': list(events.values()),
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
            value = projector(self.fetch(key))
        except (OSError, TimeoutError, ValueError, TypeError):
            part['error'] = 'upstream_unavailable'
            return
        part.update(value=value, observed=now, error=None)
        if key == 'agents':
            next_states = {}
            for agent in value:
                state = agent['act_state'] or agent['state']
                if state == 'unknown':
                    continue
                next_states[agent['name']] = state
                old = self._previous.get(agent['name'])
                if old is not None and old != state:
                    self._state_seq += 1
                    self._states.appendleft({'key': 'state-' + str(self._state_seq),
                                             'name': agent['name'], 'from': old, 'to': state,
                                             'observed_at': now, 'event_at': None})
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
        for a in [*graph['nodes'],*(value('agents') or [])]:
            roster[a['name']]=dict(a)
            stamp=a['last_active']
            if stamp is not None and stamp<=now:
                activity[a['name']]=max(activity.get(a['name'],stamp),stamp)
        if mail is not None:
            for edge in mail['edges']:
                stamp=edge['last_ts']
                if stamp<=now:
                    for endpoint in ('source','target'):
                        nm=edge[endpoint]
                        activity[nm]=max(activity.get(nm,stamp),stamp)
        unknown=sum(nm not in activity for nm in roster)
        lower=None if WINDOWS[window] is None else now-WINDOWS[window]
        roster={nm:{**a,'last_active':activity.get(nm)} for nm,a in roster.items()
                if lower is None or nm in activity and activity[nm]>=lower}
        names = set(roster)
        edges = [] if mail is None else [e for e in mail['edges'] if e['source'] in names and e['target'] in names]
        spawn = [e for e in graph['spawn'] if e['source'] in names and e['target'] in names]
        sources = {key: self._source(key if key != 'mail' else 'mail:' + window, now)
                   for key in ('agents','graph','mail')}
        payload = {'schema': 1, 'observed_at': now, 'window': window, 'sources': sources,
                   'period':{'seconds':WINDOWS[window],'activity_unknown':unknown},
                   'agents': list(roster.values()),
                   'graph': {'edges': edges, 'spawn': spawn,
                             'unrepresented_edges': 0 if mail is None else len(mail['edges']) - len(edges),
                             'degraded': graph['degraded'] or (bool(mail['degraded']) if mail else False)},
                   'timeline': {'mail': [] if mail is None else mail['events'],
                                'states': [s for s in self._states if s['name'] in names and
                                           (lower is None or s['observed_at']>=lower)],
                                'states_truncated': self._state_seq > self._states.maxlen,
                                'events_truncated': None if mail is None else mail['events_truncated'],
                                'edges_truncated': None if mail is None else mail['edges_truncated']},
                   'quota': {'status': 'unavailable'}}
        return deepcopy(payload)
