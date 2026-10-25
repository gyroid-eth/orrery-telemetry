import copy
import pytest
from dashboard.mobile_projection import SnapshotStore, project_agents, project_mail


def inputs():
    agent = {'name': 'Alpha', 'provider': 'openai', 'model': 'gpt-fixture',
             'running': True, 'state': 'run', 'act_state': 'work', 'ctx_used': 20,
             'task': 'PRIVATE-TASK', 'live': 'PRIVATE-PANE', 'transcript_path': '/private/file',
             'annot': {'role': 'PRIVATE-ROLE'}, 'provider_stall': {'error': 'PRIVATE-ERROR'}}
    return {'agents': {'agents': [agent]},
            'graph': {'nodes': [agent], 'spawn': [], 'edges': [{'kind':'bcc','source':'Alpha','target':'Secret','count':10}]},
            'mail:all': {'status':'available','window':'all','edges':[], 'events':[],
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
