"""Fixed core MCP transport from pinned bytes, entirely fake HTTP delivery.

This proves neither provider CLI redirect behavior nor model/key acceptance.
The fake opener makes ANY actual socket/network call a fixture failure.
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
from local_model_auth import LocalModelError, intent_digest
from test_runtime_legacy_configure import prepared as configure_fixture, plan
from test_runtime_legacy_context import prepared as context_fixture
from test_runtime_legacy_owner_auth import Transport,ADOPTED,RETURNED_TOKEN


@pytest.fixture
def prepared(configure_fixture,monkeypatch):
    client,ws,args,configure,_,_=configure_fixture
    value=plan(configure_fixture)
    configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    factory=runpy.run_path(str(Path(client.config['wrapper_root'])/'bin/lib/runtime_legacy_mail.py'))
    def forbidden(*args,**kwargs):pytest.fail('MCP factory attempted real network access')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    return factory,value['intent'],client,ws


def make(prepared,**kwargs):
    factory,value,_,_=prepared
    assert callable(factory.get('configured_mail_client')), 'PINNED_LEGACY_MAIL_FACTORY_NOT_IMPLEMENTED'
    return factory['configured_mail_client'](value,**kwargs)


def test_factory_constructs_only_from_fixed_shared_source_without_network(prepared):
    factory,value,client,ws=prepared
    observed=make(prepared)
    assert observed.transport.endpoint=='http://127.0.0.1:45679/mcp/'
    assert observed.transport.bearer_token is None
    assert value['execution_policy']['native_scope']=='unverified'
    assert not list(ws.tmp.rglob('local_execution'))


@pytest.mark.parametrize('mutation',['missing-pin','absent-pin','source-bytes','source-symlink',
    'directory-symlink','different-install','invalid-policy'])
def test_missing_or_changed_source_never_loads_or_falls_back(prepared,mutation):
    factory,value,client,ws=prepared
    script=Path(client.config['wrapper_root'])/'integrations/codex_app/src/agentstack_codex_app/agent_mail_client.py'
    if mutation=='missing-pin':value['configuration_evidence']['renderer_sources'].pop(str(script),None)
    elif mutation=='absent-pin':value['configuration_evidence']['renderer_sources'][str(script)]={'state':'absent'}
    elif mutation=='source-bytes':script.write_text('from pathlib import Path\nPath('+repr(str(ws.tmp/'factory-code-ran'))+").write_text('BAD')\n")
    elif mutation=='source-symlink':
        saved=script.with_name('saved.py');script.rename(saved);script.symlink_to(saved)
    elif mutation=='directory-symlink':
        directory=script.parent;saved=directory.with_name('saved-package');directory.rename(saved);directory.symlink_to(saved)
    elif mutation=='different-install':value['legacy_context']['install_root']=str(ws.tmp/'other-install')
    else:value['execution_policy']['native_scope']='verified'
    value['intent_digest']=intent_digest(value)
    with pytest.raises(LocalModelError):make(prepared)
    assert not (ws.tmp/'factory-code-ran').exists()


def test_source_swapped_during_read_is_rejected_before_compile_not_just_after_execution(prepared,monkeypatch):
    factory,value,client,ws=prepared
    assert callable(factory.get('configured_mail_client')), 'PINNED_LEGACY_MAIL_FACTORY_NOT_IMPLEMENTED'
    namespace=factory['configured_mail_client'].__globals__;original=namespace['secure_read']
    script=Path(client.config['wrapper_root'])/'integrations/codex_app/src/agentstack_codex_app/agent_mail_client.py'
    marker=ws.tmp/'swapped-source-ran'
    def swap(path,*args,**kwargs):
        if Path(path)==script:return ('from pathlib import Path\nPath('+repr(str(marker))+").write_text('BAD')\n").encode()
        return original(path,*args,**kwargs)
    monkeypatch.setitem(namespace,'secure_read',swap)
    with pytest.raises(LocalModelError):make(prepared)
    assert not marker.exists()


def test_shared_transport_and_parser_reach_only_existing_owner_branch_and_strip_auth_reply(prepared,monkeypatch):
    factory,value,client,ws=prepared;requests=[];builders=[];fake=Transport()
    class Opener:
        def open(self,request,timeout):
            assert request.full_url==value['legacy_context']['mail_url']
            assert request.get_header('Authorization') is None
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps(fake(requests[-1])).encode())
    def build(*handlers):builders.append(handlers);return Opener()
    monkeypatch.setattr(urllib.request,'build_opener',build)
    monkeypatch.setenv('HTTPS_PROXY','http://unapproved-proxy.invalid:1')
    observed=make(prepared)
    result=factory['authenticate_existing_owner'](observed,project_key='/fixture-project',project_id=7,
        name='FixtureCurie',program='claude-code',model='fixture-wire-model',registration_token=ADOPTED)
    assert result=={'id':41,'project_id':7,'name':'FixtureCurie','program':'claude-code','model':'fixture-wire-model'}
    assert RETURNED_TOKEN not in json.dumps(result)
    assert [r['method'] for r in requests]==['tools/list','tools/call','tools/call']
    assert fake.owner_auth_calls==1 and fake.new_register_calls==0
    assert len(builders[-1])==2 and isinstance(builders[-1][0],urllib.request.ProxyHandler)
    assert builders[-1][0].proxies=={} and builders[-1][1].__class__.__name__=='_NoRedirect'


def test_transport_bearer_must_be_supplied_internally_not_read_from_ambient_env(prepared,monkeypatch):
    monkeypatch.setenv('MCP_AGENT_MAIL_TOKEN','fixture-ambient-must-not-be-read')
    assert make(prepared).transport.bearer_token is None
    assert make(prepared,_bearer_token='fixture-explicit-transport-token').transport.bearer_token=='fixture-explicit-transport-token'
    with pytest.raises(LocalModelError):make(prepared,_bearer_token='bad\nheader')
