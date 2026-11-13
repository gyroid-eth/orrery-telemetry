"""Offline publication + real encrypted payloads; never contacts a push service."""
import base64
import copy
import http.client
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from dashboard import mobile_push as push, mobile_relay as relay
from test_mobile_relay import serving


def b64(value):return base64.urlsafe_b64encode(value).decode().rstrip('=')


def recipient(suffix='a'):
    key=ec.generate_private_key(ec.SECP256R1())
    return key,{'endpoint':'https://fcm.googleapis.com/fcm/send/'+suffix*40,'keys':{
        'auth':b64(b'a'*16),'p256dh':b64(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint))}}


@pytest.fixture
def env(tmp_path):
    agent=dict(name='SampleAgent',mail_instance_id='a'*32,mail_agent_id=1,running=True,act_state='wait',retired=False)
    state=dict(agents=[agent],events=[dict(id='old',kind='text',role='assistant',text='Previous result.')],demo=False,calls=[],now=100)
    def roster():return state['agents']
    def history(name):return {'events':state['events']}
    def sender(sub,payload,*_):state['calls'].append((sub,copy.deepcopy(payload)));return state.get('code',201)
    service=push.PushService(tmp_path/'push','owner@example.test','https://phone.example.ts.net:9443',
        roster,history,lambda:{'on':state['demo'],'supported':state.get('demo_supported',True)},enabled=True,sender=sender,ready=lambda:True,clock=lambda:state['now'])
    service.public_key()
    _,sub=recipient()
    def save(**changes):
        return service.call('save',{'subscription':sub,'agent':'SampleAgent','reply_needed':True,'finished':True,'previews':False,**changes})
    save()
    def tick(act=None,count=1):
        if act is not None:agent['act_state']=act
        for _ in range(count):state['now']+=5;service.tick()
    return SimpleNamespace(service=service,state=state,agent=agent,sub=sub,save=save,tick=tick)


def test_opt_out_has_no_optional_import_directory_or_thread(tmp_path,monkeypatch):
    monkeypatch.setattr(push,'dependency_ready',lambda:pytest.fail('dependency touched'))
    service=push.PushService(tmp_path/'absent','o','https://example.test',None,None,None)
    service.start();assert not service.capability()['available']
    assert not (tmp_path/'absent').exists() and service.thread is None


def test_save_status_remove_preserves_key_and_private_modes(env):
    service=env.service
    assert service.call('status',{'subscription':env.sub,'agent':'SampleAgent'})==dict(enabled=True,reply_needed=True,finished=True,previews=False)
    old=(service.root/'vapid.pem').read_bytes()
    assert service.public_key()==service.public_key()
    for path in [service.root/'vapid.pem',service.root/'subscriptions.json']:
        assert path.stat().st_mode&0o777==0o600
    assert service.root.stat().st_mode&0o777==0o700
    env.save(finished=False,previews=True)
    service.call('remove',{'subscription':env.sub})
    assert not service.devices and (service.root/'vapid.pem').read_bytes()==old
    assert not service.call('status',{'subscription':env.sub,'agent':'SampleAgent'})['enabled']


def test_lost_key_is_not_regenerated_for_existing_subscriptions(env):
    (env.service.root/'vapid.pem').unlink()
    with pytest.raises(push.PushError,match='key_unavailable'):env.service.public_key()
    loaded=push.PushService(env.service.root,env.service.owner,env.service.origin,None,None,None,enabled=True,ready=lambda:True)
    assert not loaded.capability()['available'] and not (env.service.root/'vapid.pem').exists()


@pytest.mark.parametrize('damage',['owner','mode','symlink','invalid_json','bad_device'])
def test_unsafe_persistence_is_fail_closed(env,damage,tmp_path):
    service=env.service;path=service.root/'subscriptions.json'
    if damage=='owner':
        value=json.loads(path.read_text());value['owner']='wrong';path.write_text(json.dumps(value))
    elif damage=='mode':path.chmod(0o644)
    elif damage=='symlink':
        old=path.read_bytes();path.unlink();other=tmp_path/'other';other.write_bytes(old);path.symlink_to(other)
    elif damage=='invalid_json':path.write_text('{')
    else:path.write_text(json.dumps({'owner':push.digest([service.owner,service.origin]),'devices':{'x':{}},'sent':[]}))
    loaded=push.PushService(service.root,service.owner,service.origin,None,None,None,enabled=True,ready=lambda:True)
    assert not loaded.capability()['available'] and not loaded.devices


@pytest.mark.parametrize('act',['ask','question','work','wait'])
def test_initial_snapshot_never_notifies(env,act):
    env.tick(act,5);assert env.state['calls']==[]


@pytest.mark.parametrize('act',['ask','question'])
def test_all_agent_reply_needed_debounced_and_once(env,act):
    env.save(finished=False);env.tick();env.tick(act)
    assert not env.state['calls']
    env.tick(count=8)
    assert len(env.state['calls'])==1
    body=env.state['calls'][0][1]
    assert body['title']=='SampleAgent — Reply needed'
    assert body['body']==('Question' if act=='question' else 'Approval requested')


def test_work_end_needs_new_assistant_response_and_no_user_after_it(env):
    env.tick();env.tick('work')
    env.state['events'].append(dict(id='new',role='assistant',kind='text',text='The checks passed.'))
    env.tick('wait',2)
    assert len(env.state['calls'])==1 and env.state['calls'][0][1]['title'].endswith('Work finished')
    assert env.state['calls'][0][1]['body']==''
    env.tick('work');env.tick('wait',2)
    assert len(env.state['calls'])==1
    env.tick('work');env.state['events'] += [dict(id='next',role='assistant',kind='text',text='Next result.'),dict(id='user',role='user',kind='text',text='Continue.')]
    env.tick('wait',2);assert len(env.state['calls'])==1


@pytest.mark.parametrize('end',['ask','question','unknown','finished','gone'])
def test_approval_disconnect_unknown_and_exit_are_not_work_completion(env,end):
    env.save(reply_needed=False);env.tick();env.tick('work')
    env.state['events'].append(dict(id='new',role='assistant',kind='text',text='A reply.'))
    env.tick(end,3);assert not env.state['calls']


def test_opted_out_agent_and_phone_are_not_sent_completion(env):
    env.save(finished=False,reply_needed=False);env.tick();env.tick('work')
    env.state['events'].append(dict(id='new',role='assistant',kind='text',text='Done.'))
    env.tick('wait',3);assert not env.state['calls']


def test_preview_opt_in_and_sensitive_filter(env):
    env.save(previews=True);env.tick();env.tick('work')
    env.state['events'].append(dict(id='new',role='assistant',kind='text',text='The checks passed.'))
    env.tick('wait',2);assert env.state['calls'][-1][1]['body']=='The checks passed.'
    env.tick('work');env.state['events'].append(dict(id='secret',role='assistant',kind='text',text='token: sensitive-value'))
    env.tick('wait',2);assert env.state['calls'][-1][1]['body']==''


@pytest.mark.parametrize('text',['password=abc','API_KEY abc','/Users/example/private.txt','C:\\Users\\private.txt','![image](file.png)','```command```','Bearer aaa','a'*40,'<secret>','to\u200bken: sensitive'])
def test_private_text_never_enters_preview(text):assert push.preview(text)==''


def test_plain_preview_is_bounded():assert len(push.preview('Result is ready. '*100))==600


@pytest.mark.parametrize('reason',['demo','unknown_demo','unsupported_demo','sleep','generation','missing_agent'])
def test_demo_resume_generation_and_lost_roster_reset_baseline(env,reason):
    env.tick();env.tick('work');env.state['events'].append(dict(id='new',role='assistant',kind='text',text='Done.'))
    if reason=='demo':env.state['demo']=True;env.tick();env.state['demo']=False
    elif reason=='unknown_demo':env.state['demo']=None;env.tick();env.state['demo']=False
    elif reason=='unsupported_demo':env.state['demo_supported']=False;env.tick();env.state['demo_supported']=True
    elif reason=='sleep':env.state['now']+=60
    elif reason=='generation':env.agent['_generation']=42
    else:env.state['agents']=[];env.tick();env.state['agents']=[env.agent]
    env.tick('wait',3);assert not env.state['calls']


def test_retry_is_bounded_and_expired_subscription_removed(env):
    env.state['code']=503;env.tick();env.tick('ask',2);env.tick(count=20)
    assert len(env.state['calls'])==3 and not env.service.pending
    env.state['code']=410;env.tick('wait');env.tick('ask',2)
    assert not env.service.devices


def test_device_and_queue_limits(env):
    for n in range(1,push.MAX_DEVICES):
        _,sub=recipient(str(n));env.service.call('save',dict(subscription=sub,agent='SampleAgent',reply_needed=True,finished=False,previews=False))
    _,sub=recipient('z')
    with pytest.raises(push.PushError,match='device_limit'):env.service.call('save',dict(subscription=sub,agent='SampleAgent',reply_needed=True,finished=False,previews=False))
    env.tick();env.tick('ask',2)
    assert len(env.state['calls'])==4 and len(env.service.pending)==4


@pytest.mark.parametrize('url',['http://fcm.googleapis.com/fcm/send/'+'a'*40,'https://fcm.googleapis.com.evil.test/fcm/send/'+'a'*40,'https://127.0.0.1/x','https://fcm.googleapis.com:444/fcm/send/'+'a'*40,'https://fcm.googleapis.com/fcm/send/'+'a'*40+'?q=x','https://user@fcm.googleapis.com/fcm/send/'+'a'*40])
def test_endpoint_cannot_choose_host_port_scheme_query_or_user(url):
    _,sub=recipient();sub['endpoint']=url
    with pytest.raises(push.PushError):push.subscription(sub)


@pytest.mark.parametrize('addresses',[['127.0.0.1'],['10.0.0.1'],['169.254.169.254'],['::1'],['224.0.0.1'],['8.8.8.8','192.168.1.1']])
def test_even_allowed_provider_dns_must_resolve_only_public(monkeypatch,addresses):
    monkeypatch.setattr(push.socket,'getaddrinfo',lambda *_args,**_kw:[(2,1,6,'',(a,443)) for a in addresses])
    monkeypatch.setattr(push.socket,'create_connection',lambda *_args,**_kw:pytest.fail('socket opened'))
    with pytest.raises(push.PushError):push.SafeTransport().post(recipient()[1]['endpoint'],headers={},data=b'')


def test_public_ip_is_pinned_tls_uses_provider_name_and_redirect_is_not_followed(monkeypatch):
    calls=[]
    monkeypatch.setenv('HTTPS_PROXY','https://evil.test')
    monkeypatch.setattr(push.socket,'getaddrinfo',lambda *_a,**_k:[(2,1,6,'',('8.8.8.8',443))])
    monkeypatch.setattr(push.socket,'create_connection',lambda address,**kw:calls.append(address) or SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(push.ssl,'create_default_context',lambda:SimpleNamespace(wrap_socket=lambda raw,server_hostname:calls.append(server_hostname) or raw))
    monkeypatch.setattr(push.http.client.HTTPSConnection,'request',lambda self,*a,**k:self.connect())
    monkeypatch.setattr(push.http.client.HTTPSConnection,'getresponse',lambda self:SimpleNamespace(status=302,read=lambda n:b''))
    code=push.SafeTransport().post(recipient()[1]['endpoint'],headers={},data=b'').status_code
    assert code==302 and calls==[('8.8.8.8',443),'fcm.googleapis.com']


def test_real_encryption_and_vapid_with_no_network(env,monkeypatch):
    import http_ece
    receiver,sub=recipient();captured={}
    def post(self,url,**kwargs):captured.update(kwargs);return SimpleNamespace(status_code=201,text='',headers={})
    monkeypatch.setattr(push.SafeTransport,'post',post)
    payload=dict(title='Fixture',body='A result.',agent='SampleAgent',tag='a'*32)
    assert push.publish(sub,payload,env.service.root/'vapid.pem','mailto:'+env.service.owner)==201
    assert b'A result.' not in captured['data']
    decrypted=http_ece.decrypt(captured['data'],private_key=receiver,auth_secret=b'a'*16,version='aes128gcm')
    assert json.loads(decrypted)==payload
    assert any(k.lower()=='authorization' and v.startswith('vapid ') for k,v in captured['headers'].items())


@pytest.mark.parametrize('case',['ok','wrong_owner','wrong_origin','no_origin','oversize','duplicate'])
def test_real_http_subscription_boundary(env,case):
    env.service.call('remove',{'subscription':env.sub})
    config=relay.Config(env.service.owner,env.service.origin)
    server=relay.make_server(config,push_service=env.service,test_port=0)
    headers={'Host':'phone.example.ts.net:9443','Tailscale-User-Login':env.service.owner,'Origin':env.service.origin,'Content-Type':'application/json'}
    if case=='wrong_owner':headers['Tailscale-User-Login']='other@example.test'
    if case=='wrong_origin':headers['Origin']='https://other.test'
    if case=='no_origin':headers.pop('Origin')
    data=json.dumps(dict(subscription=env.sub,agent='SampleAgent',reply_needed=True,finished=True,previews=False)).encode()
    if case=='oversize':data=b'x'*16385
    if case=='duplicate':data=b'{"a":1,"a":2}'
    with serving(server) as port:
        con=http.client.HTTPConnection('127.0.0.1',port,timeout=4)
        try:
            con.request('POST','/view/push/save',body=data,headers=headers);response=con.getresponse();response.read()
            assert response.status==(200 if case=='ok' else 400 if case=='duplicate' else 403)
            assert bool(env.service.devices)==(case=='ok')
        finally:con.close()


def test_legacy_roster_choices_use_reserved_name_and_reset_on_session_recreation():
    raw=dict(agents=[dict(name='LegacyAgent',project_key='/workspace/one',running=True,act_state='wait',created=1)])
    first=push.project_roster(raw)[0];raw['agents'][0]['created']=2;second=push.project_roster(raw)[0]
    assert push.identity(first)==push.identity(second) and first['_generation']!=second['_generation']
    raw['agents'][0]['project_key']='/workspace/two'
    assert push.identity(first)!=push.identity(push.project_roster(raw)[0])


def test_record_lag_can_settle_within_the_bounded_completion_window(env):
    env.tick();env.tick('work',30);env.tick('wait',3)
    assert not env.state['calls']
    env.state['events'].append(dict(id='late',role='assistant',kind='text',text='A delayed result.'))
    env.tick();assert len(env.state['calls'])==1


def test_same_reply_wait_after_a_state_glitch_is_not_sent_twice(env):
    env.tick();env.tick('work');env.tick('question',2)
    env.tick('wait',2);env.tick('question',2)
    assert len(env.state['calls'])==1
    env.tick('work');env.tick('question',2)
    assert len(env.state['calls'])==2


def test_queue_flood_is_coalesced_and_bounded(env):
    env.state['agents']=[dict(env.agent,name='Agent'+str(n),mail_agent_id=n+1) for n in range(80)]
    env.tick()
    for agent in env.state['agents']:agent['act_state']='ask'
    env.tick(count=2)
    assert len(env.service.pending)<=push.MAX_PENDING and len(env.state['calls'])==4


def test_slow_publication_does_not_block_phone_unsubscribe(env):
    entered=threading.Event();release=threading.Event();removed=threading.Event()
    def slow(*_):entered.set();release.wait(3);return 201
    env.service.sender=slow;env.tick();env.tick('ask')
    worker=threading.Thread(target=env.tick)
    remove=threading.Thread(target=lambda:(env.service.call('remove',{'subscription':env.sub}),removed.set()))
    worker.start()
    try:
        assert entered.wait(2);remove.start();assert removed.wait(1)
    finally:
        release.set();worker.join(4)
        if remove.ident:remove.join(4)
    assert not worker.is_alive() and not remove.is_alive() and not env.service.devices


def test_observer_runs_with_no_browser_and_closes_its_thread(env,monkeypatch):
    monkeypatch.setattr(push,'INTERVAL',.01)
    env.service.start()
    try:
        deadline=time.monotonic()+2
        while not env.service.previous and time.monotonic()<deadline:time.sleep(.01)
        assert env.service.previous
        env.agent['act_state']='ask'
        while not env.state['calls'] and time.monotonic()<deadline:time.sleep(.01)
        assert len(env.state['calls'])==1
    finally:env.service.close()
    assert not env.service.thread.is_alive()
