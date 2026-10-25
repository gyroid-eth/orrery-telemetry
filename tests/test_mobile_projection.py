import copy
import pytest
from dashboard.mobile_projection import SnapshotStore, project_agents, project_mail


def test_content_store_cannot_resolve_a_never_issued_event():
    from dashboard import mobile_projection as projection
    class Source:
        def message(self,*args):pytest.fail('unissued event must not read content or revision')
    store=projection.MobileMailStore(Source(),clock=lambda:100)
    try:
        with pytest.raises(LookupError):store.message('day','a'*32)
        assert store.progress()['status']=='idle'
    finally:store.close()


def inputs():
    agent = {'name': 'Alpha', 'provider': 'openai', 'model': 'gpt-fixture',
             'last_active':90,
             'running': True, 'state': 'run', 'act_state': 'work', 'ctx_used': 20,
             'task': 'PRIVATE-TASK', 'live': 'PRIVATE-PANE', 'transcript_path': '/private/file',
             'annot': {'role': 'PRIVATE-ROLE'}, 'provider_stall': {'error': 'PRIVATE-ERROR'}}
    return {'agents': {'agents': [agent]},
            'graph': {'nodes': [agent], 'spawn': [], 'edges': [{'kind':'bcc','source':'Alpha','target':'Secret','count':10}]},
            'mail:day': {'status':'available','window':'day','scope':'b'*32,'next_cursor':None,'edges':[], 'events':[],
                         'events_truncated':False,'edges_truncated':False,'degraded':False},
            'mail:all': {'status':'available','window':'all','scope':'b'*32,'next_cursor':None,'edges':[], 'events':[],
                         'events_truncated':False,'edges_truncated':False,'degraded':False}}


def test_projection_omits_private_fields_and_unknown_is_not_zero():
    data = inputs()
    clock = [100.0]
    store = SnapshotStore(lambda key: data[key], clock=lambda: clock[0])
    first = store.snapshot()
    text = str(first)
    assert 'PRIVATE' not in text and '/private' not in text and 'bcc' not in text
    assert first['agents'][0]['ctx_used'] == 20
    assert first['agents'][0]['context_sampled_at'] is None
    assert first['quota'] == {'status':'unavailable'}
    assert first['timeline']['states'] == []
    data['agents']['agents'][0]['ctx_used'] = None
    data['agents']['agents'][0]['act_state'] = 'wait'
    clock[0] += 11
    second = store.snapshot()
    assert second['agents'][0]['ctx_used'] is None
    assert second['timeline']['states'][0]['from'] == 'work'
    assert second['timeline']['states'][0]['to'] == 'wait'
    assert second['timeline']['states'][0]['event_at'] is None


def test_failed_poll_retains_stale_values_without_state_events():
    data=inputs(); clock=[100.0]; fail=[False]; calls=[]
    def fetch(key):
        calls.append(key)
        if fail[0]:raise OSError('PRIVATE-ERROR')
        return data[key]
    store=SnapshotStore(fetch,clock=lambda:clock[0]); first=store.snapshot()
    store.snapshot(); assert len(calls)==3
    fail[0]=True;clock[0]+=11; second=store.snapshot()
    assert second['agents']==first['agents']
    assert second['sources']['agents']['status']=='stale'
    assert second['sources']['agents']['observed_at']==100
    assert second['timeline']['states']==[]
    assert 'PRIVATE' not in str(second)
    fresh=SnapshotStore(fetch,clock=lambda:clock[0]).snapshot()
    assert all(v['status']=='unavailable' for v in fresh['sources'].values())
    assert fresh['agents']==[]


def test_stopped_agent_cannot_keep_work_or_context():
    raw=inputs()['agents'];raw['agents'][0]['running']=False
    row=project_agents(raw)[0]
    assert row['act_state']=='' and row['ctx_used'] is None and row['state']=='gone'


def test_real_roster_category_without_graph_state_remains_running():
    raw=inputs()['agents'];del raw['agents'][0]['state']
    assert project_agents(raw)[0]['state']=='run'


def test_concurrent_poll_does_not_repeat_or_iterate_partial_refresh():
    import threading
    started,release=threading.Event(),threading.Event();data=inputs();calls=[]
    def fetch(key):
        calls.append(key)
        if key=='agents':started.set();assert release.wait(2)
        return data[key]
    store=SnapshotStore(fetch);worker=threading.Thread(target=store.snapshot)
    worker.start()
    try:
        assert started.wait(2)
        pending=store.snapshot()
        assert pending['refreshing'] and pending['sources']['agents']['status']=='unavailable'
        assert calls==['agents']
    finally:
        release.set();worker.join(3)
    assert not worker.is_alive() and len(calls)==3


def test_metadata_keys_deduplicate_only_same_delivery_row():
    event={'key':'a'*32,'source':'Alpha','target':'Beta','kind':'to','count':1,'event_at':90}
    raw={**inputs()['mail:all'],'events':[event,copy.deepcopy(event)]}
    assert len(project_mail(raw,'all')['events'])==1
    raw['events'][1]['target']='Gamma'
    with pytest.raises(ValueError,match='conflicting'):
        project_mail(raw,'all')


def test_malformed_names_or_raw_paths_do_not_become_renderer_inputs():
    raw=inputs()['agents'];raw['agents'][0]['model']='/private/model'
    assert project_agents(raw)[0]['model']==''
    raw['agents'][0]['name']='<img-onerror>'
    with pytest.raises(ValueError):project_agents(raw)


@pytest.mark.parametrize('field',['events_truncated','edges_truncated','degraded'])
def test_missing_data_quality_flag_is_not_healthy_false(field):
    raw=inputs()['mail:all'];del raw[field]
    with pytest.raises(ValueError,match='mail_quality'):
        project_mail(raw,'all')


def test_observed_state_history_has_explicit_rolling_limit():
    data=inputs();clock=[100.0]
    store=SnapshotStore(lambda key:data[key],clock=lambda:clock[0])
    assert store.snapshot()['timeline']['states_truncated'] is False
    for i in range(201):
        data['agents']['agents'][0]['act_state']='wait' if i%2==0 else 'work'
        clock[0]+=11;result=store.snapshot()
    assert len(result['timeline']['states'])==200
    assert result['timeline']['states_truncated'] is True
    assert result['timeline']['states'][0]['key']=='state-201'


def test_default_wall_clock_period_filters_union_before_phone_payload():
    now=1792915200
    def agent(name,stamp,**extra):
        return {'name':name,'last_active':stamp,'running':False,**extra}
    roster=[agent('RecentRetired',now-5,retired=True),
            agent('Boundary',now-86400),agent('OldRunning',now-86401,running=True),
            agent('Unknown',None),agent('Future',now+1),agent('ByMail',None),
            agent('Rescued',now-3)]
    roster += [agent('Old'+str(i),now-100000) for i in range(3000)]
    def fetch(key):
        if key=='graph':return {'nodes':roster,'spawn':[{'source':'OldRunning','target':'Boundary'}]}
        if key=='agents':return {'agents':[agent('Rescued',None),agent('UnionOld',now-100000)]}
        return {'status':'available','window':key[5:],'scope':'b'*32,'next_cursor':None,
                'edges':[{'source':'ByMail','target':'Boundary','kind':'to','count':3,'last_ts':now-2}],
                'events':[],'events_truncated':False,'edges_truncated':False,'degraded':False}
    store=SnapshotStore(fetch,clock=lambda:now)
    result=store.snapshot()
    assert result['window']=='day'
    assert {a['name'] for a in result['agents']}=={'RecentRetired','Boundary','ByMail','Rescued'}
    assert result['graph']['spawn']==[] and len(result['graph']['edges'])==1
    assert result['period']['seconds']==86400 and result['period']['activity_unknown']==2
    assert next(a for a in result['agents'] if a['name']=='RecentRetired')['retired'] is True
    assert next(a for a in result['agents'] if a['name']=='Rescued')['last_active']==now-3
    assert len(store.snapshot('all')['agents'])==3008
    assert 'OldRunning' not in {a['name'] for a in store.snapshot('m60')['agents']}


def test_many_slider_periods_have_a_bounded_coherent_snapshot_cache():
    def fetch(key):
        if key=='agents':return {'agents':[]}
        if key=='graph':return {'nodes':[],'spawn':[]}
        return {'status':'available','window':key[5:],'scope':'b'*32,'next_cursor':None,'edges':[],'events':[],
                'events_truncated':False,'edges_truncated':False,'degraded':False}
    store=SnapshotStore(fetch,clock=lambda:1792915200)
    for minutes in range(15,1501,15):
        result=store.snapshot('m'+str(minutes))
        assert result['period']['seconds']==minutes*60
        assert len(store._published)<=4 and len(store._parts)<=6
    assert store.snapshot('m15')['sources']['mail']['status']=='available'


def content_fixture(index=1,*,now=100000,scope='b'*32,body='LITERAL <img src=https://invalid.test/>',revision='c'*32):
    return {'scope':scope,'fingerprint':f'{index:032x}','revision':revision,
            'event':{'key':f'{index:032x}','source':'Alpha','target':'Beta','kind':'to','count':1,'event_at':now-1},
            'subject':'SUBJECT','body':body,'subject_truncated':False,'body_truncated':False}


def issued_snapshot(items,window='day'):
    return {'window':window,'sources':{'mail':{'status':'available'}},
            'timeline':{'scope':'b'*32,'mail':[x['event'] for x in items],'next_cursor':None}}


def test_body_cache_revalidates_each_tap_deduplicates_recipients_and_ages_out():
    from dashboard.mobile_projection import MobileMailStore
    clock=[100000];first=content_fixture();second=copy.deepcopy(first);second['event']['key']='d'*32;second['event']['target']='Gamma';second['event']['kind']='cc'
    class Source:
        failed=False
        def __init__(self):self.calls=[];self.items={x['event']['key']:x for x in (first,second)}
        def message(self,window,key,mode):
            self.calls.append((key,mode))
            if self.failed:raise LookupError('deleted')
            row=copy.deepcopy(self.items[key])
            if mode=='revision':
                for k in ('subject','body','subject_truncated','body_truncated'):row.pop(k)
            return row
    source=Source();store=MobileMailStore(source,clock=lambda:clock[0]);store.observe(issued_snapshot([first,second]))
    try:
        assert store.message('day',first['event']['key'])['body']==first['body']
        assert store.message('day',second['event']['key'])['event']==second['event']
        assert len(store._cache)==1 and [mode for _,mode in source.calls]==['revision','content','revision']
        source.items[first['event']['key']]['body']='UPDATED';source.items[first['event']['key']]['revision']='e'*32
        assert store.message('day',first['event']['key'])['body']=='UPDATED'
        source.failed=True
        with pytest.raises(LookupError):store.message('day',first['event']['key'])
        assert store.progress()['messages']==0
        source.failed=False;clock[0]+=600
        before=list(source.calls)
        with pytest.raises(LookupError):store.message('day',first['event']['key'])
        assert source.calls==before
    finally:store.close()


def test_older_than_cache_detail_and_scope_change_invalidate_all():
    from dashboard.mobile_projection import MobileMailStore
    row=content_fixture(now=100000-86400)
    class Source:
        def message(self,*args):return copy.deepcopy(row)
    store=MobileMailStore(Source(),clock=lambda:100000)
    try:
        store.observe(issued_snapshot([row],'all'))
        assert store.message('all',row['event']['key'])['body']==row['body'] and not store._cache
        changed=issued_snapshot([]);changed['timeline']['scope']='f'*32;store.observe(changed)
        with pytest.raises(LookupError):store.message('all',row['event']['key'])
        assert not store._issued and not store._cache
    finally:store.close()


@pytest.mark.parametrize('count,budget,expected',[(2000,32*1024*1024,'ready'),(2001,32*1024*1024,'partial'),(40,1000,'partial')])
def test_single_worker_2000_ready_vs_count_and_logical_byte_partial(count,budget,expected):
    from dashboard.mobile_projection import MobileMailStore
    class Source:
        def __init__(self):self.calls=[]
        def cache_page(self,cursor):
            start=int(cursor,16) if cursor else 0;self.calls.append(start)
            end=min(start+32,count,2000)
            return {'scope':'b'*32,'items':[content_fixture(i+1) for i in range(start,end)],
                    'complete':end==count,'next_cursor':f'{end:032x}' if end<min(count,2000) else None,
                    'reason':'count_limit' if end==2000 and count>2000 else None}
    source=Source();store=MobileMailStore(source,clock=lambda:100000,budget=budget)
    assert source.calls==[] and store.progress()['status']=='idle'
    store.observe(issued_snapshot([]));assert source.calls==[]
    try:
        store.start_warm();thread=store._thread;store.start_warm();assert store._thread is thread
        # The product sweep budget is120s, not an8s performance contract.
        thread.join(30);assert not thread.is_alive(),(source.calls[-3:],store.progress())
        progress=store.progress()
        assert progress['status']==expected and progress['messages']<=2000 and progress['logical_bytes']<=budget
        actual=sum(len(__import__('json').dumps(x[2],ensure_ascii=False,separators=(',',':')).encode()) for x in store._cache.values())
        assert progress['logical_bytes']==actual
        if count==2000:assert progress['messages']==2000 and len(source.calls)==63
        calls=list(source.calls);store.start_warm();assert source.calls==calls
    finally:store.close()
    assert not store._cache and not store._issued and store._bytes==0


def test_warm_stop_and_scope_generation_cannot_repopulate_late():
    import threading
    from dashboard.mobile_projection import MobileMailStore
    started,release=threading.Event(),threading.Event()
    class Source:
        def cache_page(self,cursor):
            started.set();assert release.wait(2)
            return {'scope':'b'*32,'items':[content_fixture()],'complete':True,'next_cursor':None}
    store=MobileMailStore(Source(),clock=lambda:100000);store.observe(issued_snapshot([]));store.start_warm()
    assert started.wait(1)
    changed=issued_snapshot([]);changed['timeline']['scope']='f'*32;store.observe(changed)
    release.set();store._thread.join(3)
    assert not store._cache and store._scope=='f'*32
    store.close();assert not store._thread.is_alive() and store._closed


def test_sweep_timeout_and_unavailable_are_not_ready(monkeypatch):
    import types,time
    from dashboard import mobile_projection as p
    ticks=iter([0,121]);monkeypatch.setattr(p,'time',types.SimpleNamespace(monotonic=lambda:next(ticks)))
    class Source:
        def cache_page(self,*args):pytest.fail('expired budget must not fetch')
    store=p.MobileMailStore(Source(),clock=lambda:100000);store.observe(issued_snapshot([]));store.start_warm();store._thread.join(3)
    assert store.progress()['status']=='partial' and store.progress()['reason']=='time_limit';store.close()
    monkeypatch.setattr(p,'time',time)
    class Failed:
        def cache_page(self,*args):raise TimeoutError('PRIVATE-ERROR')
    store=p.MobileMailStore(Failed(),clock=lambda:100000);store.observe(issued_snapshot([]));store.start_warm();store._thread.join(3)
    assert store.progress()['status']=='unavailable' and 'PRIVATE' not in str(store.progress());store.close()


def test_current_scope_change_on_tap_expires_issued_keys_and_cached_content():
    from dashboard.mobile_projection import MobileMailStore
    row=content_fixture()
    class Source:
        def message(self,*args):return {**row,'scope':'f'*32}
    store=MobileMailStore(Source(),clock=lambda:100000);store.observe(issued_snapshot([row]))
    try:
        with pytest.raises(LookupError):store.message('day',row['event']['key'])
        assert not store._issued and not store._cache and store._scope is None
    finally:store.close()
