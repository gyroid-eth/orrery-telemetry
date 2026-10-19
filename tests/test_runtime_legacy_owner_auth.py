"""Read-only existing-agent owner auth, with the real shared MCP client parser."""
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integrations/codex_app/src'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from agentstack_codex_app.agent_mail_client import AgentMailClient
from local_model_auth import LocalModelError
from test_runtime_legacy_registration import store,send,reply,ADOPTED

RETURNED_TOKEN='AUTH_REPLY_CANARY_MUST_NOT_BE_PUBLISHED_123456789'


def module():
    try:return importlib.import_module('runtime_legacy_mail')
    except ModuleNotFoundError:pytest.fail('LEGACY_OWNER_AUTH_NOT_IMPLEMENTED')


class Transport:
    def __init__(self,*,schema=True,lookup=None,auth=None,refuse=False):
        self.schema=schema;self.lookup=lookup;self.auth=auth;self.refuse=refuse
        self.new_register_calls=0;self.owner_auth_calls=0;self.calls=[]
    def __call__(self,envelope):
        if envelope['method']=='tools/list':
            fields=('existing_agent_id','registration_token','project_key','name','program','model') if self.schema else ('name','model')
            return {'result':{'tools':[{'name':'register_agent','inputSchema':{'properties':{key:{} for key in fields}}}]}}
        tool=envelope['params']['name'];args=envelope['params']['arguments'];self.calls.append((tool,args))
        profile={key:value for key,value in reply().items() if key!='registration_token'}
        if tool=='whois':profile.update(self.lookup or {})
        elif tool=='register_agent':
            if 'existing_agent_id' not in args:self.new_register_calls+=1
            else:self.owner_auth_calls+=1
            assert args.get('existing_agent_id')==41
            assert args['registration_token']==ADOPTED
            if self.refuse:return {'result':{'isError':True,'content':[{'type':'text','text':'owner refused'}]}}
            profile.update(self.auth or {});profile['registration_token']=RETURNED_TOKEN
        else:pytest.fail('unrelated Mail operation')
        return {'result':{'structuredContent':profile}}


def query(transport):
    return module().authenticate_existing_owner(AgentMailClient(transport),
        project_key='/fixture-project',project_id=7,name='FixtureCurie',program='claude-code',
        model='fixture-wire-model',registration_token=ADOPTED)


def test_whois_is_only_lookup_then_exact_existing_owner_auth_sanitizes_returned_token():
    transport=Transport();result=query(transport)
    assert result=={key:value for key,value in reply().items() if key!='registration_token'}
    assert [name for name,args in transport.calls]==['whois','register_agent']
    assert transport.new_register_calls==0 and transport.owner_auth_calls==1
    assert RETURNED_TOKEN not in json.dumps(result)


def test_no_existing_owner_schema_never_falls_back_to_creation_or_unauthed_whois():
    transport=Transport(schema=False)
    with pytest.raises(LocalModelError):query(transport)
    assert transport.calls==[] and transport.new_register_calls==0


@pytest.mark.parametrize('field,bad',[('id',None),('id',True),('name','OtherCurie'),
    ('program','codex'),('model','cloud'),('project_id',88)])
def test_lookup_failure_or_other_identity_is_not_authentication(field,bad):
    transport=Transport(lookup={field:bad})
    with pytest.raises(LocalModelError):query(transport)
    assert transport.new_register_calls==transport.owner_auth_calls==0


@pytest.mark.parametrize('field,bad',[('id',99),('name','OtherCurie'),('program','codex'),
    ('model','cloud'),('project_id',88),('retired_at','2026-10-18T00:00:00Z')])
def test_successful_auth_response_must_still_match_exact_identity(field,bad):
    transport=Transport(auth={field:bad})
    with pytest.raises(LocalModelError) as error:query(transport)
    assert RETURNED_TOKEN not in str(error.value)
    assert transport.new_register_calls==0 and transport.owner_auth_calls==1


def test_owner_refusal_is_not_satisfied_by_a_successful_whois():
    transport=Transport(refuse=True)
    with pytest.raises(LocalModelError):query(transport)
    assert transport.owner_auth_calls==1 and transport.new_register_calls==0


def test_auth_reply_token_is_never_published_in_real_store_state_or_status(store,capsys):
    send(store);transport=Transport()
    store.finalize(lambda token:query(transport),expected_digest=store.status()['state_digest'])
    for path in store.runtime.rglob('*'):
        if path.is_file():assert RETURNED_TOKEN.encode() not in path.read_bytes()
    assert RETURNED_TOKEN not in json.dumps(store.status())
    assert RETURNED_TOKEN not in str(capsys.readouterr())
    assert store.token.read_text()==ADOPTED  # Original credential, never the auth reply.


def test_native_mail_display_normalization_does_not_replace_saved_wire_model():
    # app.py:_agent_to_dict returns both fields; model is for display while
    # model_raw retains the literal registration. No model resolver is called.
    transport=Transport(lookup={'model':'display-only','model_raw':'fixture-wire-model'},
        auth={'model':'display-only','model_raw':'fixture-wire-model'})
    result=query(transport)
    assert result['model']=='fixture-wire-model'
    assert transport.calls[-1][1]['model']=='fixture-wire-model'
    assert transport.owner_auth_calls==1 and transport.new_register_calls==0


@pytest.mark.parametrize('stage,bad',[('lookup','other-wire'),('auth','other-wire'),
    ('lookup',None),('auth',None),('auth','bad\nmodel')])
def test_matching_display_model_cannot_hide_wrong_or_invalid_native_raw_model(stage,bad):
    options={stage:{'model_raw':bad}}
    transport=Transport(**options)
    with pytest.raises(LocalModelError):query(transport)
    assert transport.new_register_calls==0
