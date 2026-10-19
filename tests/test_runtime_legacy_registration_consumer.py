"""Real configure/Store/core MCP client, fake HTTP only; no native admission.

The public launch front doors stay closed until execution/auth are connected.
These tests exercise the internal registration consumer, not native isolation.
"""
from copy import deepcopy
import io
import json
from pathlib import Path
import runpy
import socket
import sys
import urllib.request

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from local_model_auth import LocalModelError
from runtime_intent_store import read_owner, write_owner, write_private_bytes
from runtime_legacy_registration import Store
from test_runtime_legacy_configure import prepared as configure_fixture, plan
from test_runtime_legacy_context import prepared as context_fixture

CANDIDATE='fixture-child-candidate-000000000000000000'
PARENT_TOKEN='fixture-parent-owner-00000000000000000000'
ADOPTED='fixture-original-registration-000000000000'
AUTH_CANARY='fixture-auth-reply-never-save-000000000000'


@pytest.fixture
def prepared(configure_fixture,monkeypatch):
    client,ws,args,configure,_,_=configure_fixture
    value=plan(configure_fixture)
    configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    target=value['intent']['legacy_context']
    write_private_bytes(Path(target['runtime_dir'])/('agent_token_'+target['parent']['name']),PARENT_TOKEN.encode())
    api=runpy.run_path(str(Path(client.config['wrapper_root'])/'bin/lib/runtime_legacy_mail.py'))
    store=Store(target['runtime_dir'],target['name'])
    def forbidden(*args,**kwargs):pytest.fail('registration consumer attempted actual network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    return api,value['intent'],store,ws,client


class Peer:
    def __init__(self,prepared,**options):
        self.api,self.intent,self.store,self.ws,self.client=prepared
        self.options=options;self.calls=[];self.new=0;self.auth=0;self.child_created=False
    def profile(self,parent=False):
        target=self.intent['legacy_context']
        if parent:return {**target['parent'],'project_id':target['project_id']}
        return {'id':73,'name':target['name'],'program':'claude-code','project_id':target['project_id'],
            'model':'display-only','model_raw':self.intent['model']['resolved']['model']}
    def open(self,request,timeout):
        assert request.full_url==self.intent['legacy_context']['mail_url']
        envelope=json.loads(request.data)
        if envelope['method']=='tools/list':
            fields=('existing_agent_id','registration_token','project_key','name','program','model','task_description')
            if self.options.get('schema_missing'):fields=fields[:-1]
            reply={'tools':[{'name':'register_agent','inputSchema':{'properties':{k:{} for k in fields}}}]}
            return io.BytesIO(json.dumps({'result':reply}).encode())
        else:
            tool=envelope['params']['name'];args=envelope['params']['arguments'];self.calls.append((tool,args))
            parent=args.get('agent_name',args.get('name'))==self.intent['legacy_context']['parent']['name']
            reply=self.profile(parent)
            if tool=='whois':
                if not parent and not self.child_created and not self.options.get('existing'):
                    name=args['agent_name']
                    text=("Error calling tool 'whois': Agent '"+name+"' not found in project '/fixture-project'")
                    if self.options.get('lookup_unknown'):text='fixture-unavailable '+AUTH_CANARY
                    return io.BytesIO(json.dumps({'result':{'isError':True,
                        'content':[{'type':'text','text':text}]}}).encode())
                if parent and self.options.get('wrong_parent'):reply['id']+=1
            elif tool=='register_agent' and 'existing_agent_id' in args:
                self.auth+=1
                assert args['existing_agent_id']==self.profile(parent)['id']
                expected=PARENT_TOKEN if parent else ADOPTED if self.options.get('adopt') else CANDIDATE
                assert args['registration_token']==expected
                if not parent and self.options.get('reject_child_auth'):
                    raise OSError(AUTH_CANARY)
                reply['registration_token']=AUTH_CANARY
                if parent and self.options.get('change_owner'):
                    frame=read_owner(self.store.owner);frame['concurrent']='must-keep';write_owner(self.store.owner,frame)
            elif tool=='register_agent':
                self.new+=1;self.child_created=True
                assert self.store.status()['phase']=='sent-unknown'  # fsync before HTTP
                assert args=={'project_key':self.intent['legacy_context']['project_key'],
                    'name':self.intent['name'],'program':'claude-code',
                    'model':self.intent['model']['resolved']['model'],
                    'task_description':'Delegated local preview','registration_token':CANDIDATE}
                if self.options.get('drop_response'):raise OSError(AUTH_CANARY)
                if self.options.get('substitute'):reply['name']='OtherChild'
                if self.options.get('adopt'):reply['registration_token']=ADOPTED
            else:pytest.fail('unrelated Mail operation: '+tool)
        return io.BytesIO(json.dumps({'result':{'structuredContent':reply}}).encode())


def connect(prepared,monkeypatch,**options):
    peer=Peer(prepared,**options)
    monkeypatch.setattr(urllib.request,'build_opener',lambda *handlers:peer)
    return peer


def run(prepared,**changes):
    api,value,store,ws,client=prepared
    assert callable(api.get('register_configured_child')), 'LEGACY_REGISTRATION_CONSUMER_NOT_IMPLEMENTED'
    options={'caller':ws.caller,'expected_state_digest':store.status()['state_digest'],
        'candidate_token':CANDIDATE,'preview_requested':True}
    options.update(changes)
    return api['register_configured_child'](value,**options)


@pytest.mark.parametrize('adopt',[False,True])
def test_new_registration_uses_actual_core_wire_then_child_owner_auth(prepared,monkeypatch,adopt,capsys):
    peer=connect(prepared,monkeypatch,adopt=adopt)
    result=run(prepared);api,value,store,ws,client=prepared
    assert result['phase']=='registered' and result['remote_effect']=='confirmed'
    assert peer.new==1 and peer.auth==2
    frame=read_owner(store.owner)
    assert frame['launch_intent']==value and frame['configuration_stage']=='prepared'
    assert frame['agent_id']==73 and frame['agent_name']==value['name']
    assert frame['registration_token']==store.token.read_text()==(ADOPTED if adopt else CANDIDATE)
    assert not store.pending.exists() and 'local_execution' not in frame
    assert AUTH_CANARY not in repr(result)+repr(capsys.readouterr())
    assert all(AUTH_CANARY.encode() not in p.read_bytes() for p in store.runtime.rglob('*') if p.is_file())


@pytest.mark.parametrize('reason',['wrong_parent','schema_missing','change_owner','existing','lookup_unknown'])
def test_parent_schema_or_owner_drift_refuses_before_new_registration(prepared,monkeypatch,reason):
    peer=connect(prepared,monkeypatch,**{reason:True})
    with pytest.raises(LocalModelError):run(prepared)
    assert peer.new==0 and not prepared[2].pending.exists() and not prepared[2].token.exists()


@pytest.mark.parametrize('reason',['drop_response','reject_child_auth','substitute'])
def test_unknown_effect_never_retries_or_rescues(prepared,monkeypatch,reason):
    peer=connect(prepared,monkeypatch,**{reason:True});store=prepared[2]
    with pytest.raises(LocalModelError) as error:run(prepared)
    assert peer.new==1 and store.pending.exists()
    assert AUTH_CANARY not in str(error.value)
    assert 'local_registration' not in read_owner(store.owner)
    with pytest.raises(LocalModelError):run(prepared)
    assert peer.new==1


@pytest.mark.parametrize('change',['preview','caller','state','credential','parent-credential','task','settings'])
def test_bad_inputs_stop_before_any_http_or_registration_save(prepared,monkeypatch,change):
    peer=connect(prepared,monkeypatch);api,value,store,ws,client=prepared;options={}
    if change=='preview':options['preview_requested']=False
    elif change=='caller':
        caller=deepcopy(ws.caller);caller['name']='OtherChild';options['caller']=caller
    elif change=='state':options['expected_state_digest']='0'*64
    elif change=='credential':options['candidate_token']='short'
    elif change=='parent-credential':
        (store.runtime/('agent_token_'+value['legacy_context']['parent']['name'])).unlink()
    elif change=='task':Path(value['task']).write_text('changed task')
    else:(store.owner.with_suffix('.preview-home')/'local-model-settings.json').write_text('{}')
    with pytest.raises(LocalModelError):run(prepared,**options)
    assert peer.calls==[] and peer.new==0 and not store.pending.exists() and not store.token.exists()


def test_saved_success_cannot_be_used_as_automatic_new_register_replay(prepared,monkeypatch):
    peer=connect(prepared,monkeypatch);run(prepared)
    with pytest.raises(LocalModelError):run(prepared)
    assert peer.new==1
