"""Installed legacy operator configure with a fake loopback MCP peer.

The real core transport/owner branch and literal settings reader run. No
provider CLI/key/Strata/live/install/service is used; the peer is test-only.
"""
from copy import deepcopy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from runtime_intent_store import write_private_bytes
from test_runtime_legacy_configure import prepared as configure_fixture
from test_runtime_legacy_context import prepared as context_fixture
from test_runtime_configure_ownership import snapshot

TOKEN='fixture-parent-owner-00000000000000000000'
CANARY='fixture-auth-reply-token-never-publish-00000000'
CHILD_TOKEN='fixture-child-owner-000000000000000000000'


@contextmanager
def peer(target):
    calls=[];controls={'refuse':False,'raw_model':target['parent']['model'],'candidate':'absent',
        'child':False,'child_model':'fixture-wire-model','child_id':73,'child_refused':False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if request['method']=='tools/list':
                fields=('project_key','name','program','model','existing_agent_id','registration_token')
                result={'tools':[{'name':'register_agent','inputSchema':{'properties':{k:{} for k in fields}}}]}
            else:
                params=request['params'];calls.append(params)
                profile={**target['parent'],'project_id':target['project_id'],
                    'model':'display-only','model_raw':controls['raw_model']}
                parent=params['arguments'].get('name',params['arguments'].get('agent_name'))==target['parent']['name']
                if not parent:
                    profile={**profile,'id':controls['child_id'],'name':target['name'],
                        'program':'claude-code','model_raw':controls['child_model']}
                if params['name']=='register_agent':
                    args=params['arguments']
                    if ((controls['refuse'] if parent else controls['child_refused'])
                            or args.get('existing_agent_id')!=profile['id']
                            or args.get('registration_token')!=(TOKEN if parent else CHILD_TOKEN)):
                        result={'isError':True,'content':[{'type':'text','text':'owner refused '+CANARY}]}
                    else:result={'structuredContent':{**profile,'registration_token':CANARY}}
                elif params['name']=='whois':
                    if parent or controls['child']:
                        result={'structuredContent':profile}
                    elif controls['candidate']=='absent':
                        result={'isError':True,'content':[{'type':'text','text':
                            "Error calling tool 'whois': Agent '"+target['name']+"' not found in project '/fixture-project'"}]}
                    elif controls['candidate']=='existing':
                        result={'structuredContent':{**profile,'id':73,'name':target['name']}}
                    else:result={'isError':True,'content':[{'type':'text','text':'lookup unavailable'}]}
                else:result={'isError':True,'content':[{'type':'text','text':'unrelated tool refused'}]}
            payload=json.dumps({'result':result,'jsonrpc':'2.0','id':request['id']}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=HTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=False);worker.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port)+'/mcp/',calls,controls
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)
        assert not worker.is_alive(), 'test MCP peer did not stop'


@pytest.fixture
def prepared(configure_fixture):
    client,ws,resources,_,_,_=configure_fixture
    target=deepcopy(ws.context['legacy_context'])
    with peer(target) as (url,calls,controls):
        old=ws.context['legacy_context']['mail_url']
        ws.context['legacy_context']['mail_url']=url
        env=Path(client.config['wrapper_root'])/'env.sh';env.write_text(env.read_text().replace(old,url));env.chmod(0o600)
        runtime=Path(target['runtime_dir'])
        write_private_bytes(runtime/('agent_token_'+target['parent']['name']),TOKEN.encode())
        (ws.tmp/'command-tmp').mkdir(mode=0o700)
        allowed={'local_models_root','effective_scope','approved_settings_sha256',
            'approved_settings_digests','auth_context','legacy_context','execution_policy'}
        payload={'caller':deepcopy(ws.caller),'context':{k:v for k,v in ws.context.items() if k in allowed},
            'resources':deepcopy(resources)}
        yield client,ws,payload,calls,controls


def invoke(prepared,payload,*arguments,operator='FixtureParent',entry='runtime_client.py'):
    client,ws,_,_,_=prepared
    return subprocess.run([sys.executable,'-B',str(Path(client.config['wrapper_root'])/'bin/lib'/entry),
        *arguments],input=json.dumps(payload),capture_output=True,text=True,cwd=ws.caller['cwd'],timeout=20,
        env={'HOME':str(ws.tmp/'home'),'PATH':'/usr/bin:/bin','LANG':'C','TMPDIR':str(ws.tmp/'command-tmp'),
            'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.preview-configure-cli',
            'AGENTSTACK_RUNTIME_MODE':'legacy','AGENTSTACK_HOME':client.config['wrapper_root'],
            'AGENTSTACK_RUNTIME_DIR':str(ws.tmp/'runtime'),'AGENT_NAME':operator})


def local_plan(prepared,payload=None):
    reply=invoke(prepared,prepared[2] if payload is None else payload,
        'instruction-configure','operator','claude','local-plan')
    assert reply.returncode==0, 'legacy explicit configure refused: '+reply.stderr
    return json.loads(reply.stdout)


def test_real_installed_operator_plan_authenticates_parent_readonly_without_outputs(prepared):
    client,ws,payload,calls,controls=prepared;before=snapshot(ws.tmp)
    value=local_plan(prepared)
    assert snapshot(ws.tmp)==before
    assert value['owner']==str(client.profile_path) and not client.profile_path.exists()
    assert value['intent']['execution_policy']==payload['context']['execution_policy']
    assert value['intent']['legacy_context']==payload['context']['legacy_context']
    assert [p['name'] for p in calls]==['whois','register_agent','whois']
    assert calls[1]['arguments']['existing_agent_id']==41
    assert calls[2]['arguments']['agent_name']=='FixtureChild'
    assert TOKEN not in json.dumps(value) and CANARY not in json.dumps(value)


def test_real_installed_apply_requires_same_plan_consent_and_keeps_it_unverified(prepared):
    client,ws,payload,calls,controls=prepared;value=local_plan(prepared)
    before=snapshot(ws.tmp);old=len(calls)
    rejected=invoke(prepared,value,'instruction-configure','operator','claude','local-apply',value['plan_id'])
    assert rejected.returncode==2 and 'OPERATOR_CONFIRMATION_REQUIRED' in rejected.stderr
    assert snapshot(ws.tmp)==before and len(calls)==old
    result=invoke(prepared,value,'--assume-yes','instruction-configure','operator','claude','local-apply',value['plan_id'])
    assert result.returncode==0,result.stderr
    observed=json.loads(result.stdout)
    assert observed['configured'] is True and observed['launch_admissible'] is False
    state=json.loads(client.profile_path.read_text())
    assert state=={'configuration_stage':'prepared','launch_intent':value['intent']}
    assert all(p['name']!='register_agent' or p['arguments'].get('existing_agent_id')==41 for p in calls)
    assert TOKEN not in result.stdout and CANARY not in result.stdout


@pytest.mark.parametrize('mutation',['unknown-context','owner-injection','uid-injection','wrong-provider',
    'caller-parent','policy-missing','parent-name'])
def test_bad_operator_input_refuses_before_credential_auth_or_outputs(prepared,mutation):
    client,ws,original,calls,controls=prepared;payload=deepcopy(original)
    if mutation=='unknown-context':payload['context']['native_verified']=True
    elif mutation=='owner-injection':payload['context']['intent_owner']=str(ws.tmp/'foreign.json')
    elif mutation=='uid-injection':payload['context']['operator_uid']=os.getuid()
    elif mutation=='wrong-provider':payload['caller']['provider']='codex'
    elif mutation=='caller-parent':payload['caller']['parent']='OtherParent'
    elif mutation=='policy-missing':payload['context'].pop('execution_policy')
    else:payload['context']['legacy_context']['parent']['name']='OtherParent'
    before=snapshot(ws.tmp)
    result=invoke(prepared,payload,'instruction-configure','operator','claude','local-plan')
    assert result.returncode==2 and not result.stdout and 'Traceback' not in result.stderr
    assert snapshot(ws.tmp)==before and calls==[]


@pytest.mark.parametrize('refusal',['operator-name','owner-refusal','raw-model'])
def test_operator_identity_mismatch_cannot_use_another_parent_or_return_secrets(prepared,refusal):
    client,ws,payload,calls,controls=prepared;before=snapshot(ws.tmp)
    if refusal=='owner-refusal':controls['refuse']=True
    elif refusal=='raw-model':controls['raw_model']='other-wire-model'
    result=invoke(prepared,payload,'instruction-configure','operator','claude','local-plan',
        operator='OtherParent' if refusal=='operator-name' else 'FixtureParent')
    assert result.returncode==2 and not result.stdout and 'Traceback' not in result.stderr
    assert TOKEN not in result.stderr and CANARY not in result.stderr and snapshot(ws.tmp)==before
    if refusal=='operator-name':assert calls==[]


@pytest.mark.parametrize('action',['plan','apply'])
@pytest.mark.parametrize('candidate',['existing','unknown'])
def test_configure_rechecks_candidate_availability_before_any_output(prepared,action,candidate):
    client,ws,payload,calls,controls=prepared
    value=local_plan(prepared) if action=='apply' else payload
    controls['candidate']=candidate;before=snapshot(ws.tmp)
    arguments=(['--assume-yes','instruction-configure','operator','claude','local-apply',value['plan_id']]
        if action=='apply' else ['instruction-configure','operator','claude','local-plan'])
    reply=invoke(prepared,value,*arguments)
    assert reply.returncode==2 and not reply.stdout
    assert snapshot(ws.tmp)==before
    assert TOKEN not in reply.stderr and CANARY not in reply.stderr
    assert all(p['name']!='register_agent' or p['arguments'].get('existing_agent_id')==41 for p in calls)


def pending_registration(prepared,*,lost_response=False):
    from runtime_legacy_registration import Store
    from local_model_auth import LocalModelError
    client,ws,payload,calls,controls=prepared;value=local_plan(prepared)
    applied=invoke(prepared,value,'--assume-yes','instruction-configure','operator','claude','local-apply',value['plan_id'])
    assert applied.returncode==0,applied.stderr
    target=value['intent']['legacy_context'];store=Store(target['runtime_dir'],target['name'])
    store.begin(expected_digest=store.status()['state_digest'],candidate_token=CHILD_TOKEN)
    controls['child']=True;controls['child_model']=value['intent']['model']['resolved']['model']
    identity={'id':73,'project_id':target['project_id'],'name':target['name'],'program':'claude-code',
        'model':controls['child_model'],'registration_token':CHILD_TOKEN}
    if lost_response:
        def dropped(*args):raise OSError('fixture lost registration response')
        with pytest.raises(LocalModelError):store.send_once(dropped,expected_digest=store.status()['state_digest'])
    else:store.send_once(lambda *args:identity,expected_digest=store.status()['state_digest'])
    return store


@pytest.mark.parametrize('lost_response',[False,True])
def test_installed_resolve_uses_parent_and_child_owner_auth_without_new_registration(prepared,lost_response):
    client,ws,payload,calls,controls=prepared;store=pending_registration(prepared,lost_response=lost_response)
    calls.clear();before_intent=json.loads(store.owner.read_text())['launch_intent']
    reply=invoke(prepared,{},'resolve',store.name,'--action=finalize-registration',
        '--expected-state-digest='+store.status()['state_digest'],entry='runtime_legacy_preview_cli.py')
    assert reply.returncode==0,reply.stderr
    assert json.loads(reply.stdout)['phase']=='registered' and not store.pending.exists()
    assert json.loads(store.owner.read_text())['launch_intent']==before_intent
    assert [(p['name'],p['arguments'].get('existing_agent_id')) for p in calls]==[
        ('whois',None),('register_agent',41),('whois',None),('register_agent',73)]
    assert TOKEN not in reply.stdout+reply.stderr and CHILD_TOKEN not in reply.stdout+reply.stderr
    assert CANARY not in store.owner.read_text() and CANARY not in store.token.read_text()


@pytest.mark.parametrize('failure',['parent-auth','child-auth','child-model','child-id','operator','stale-digest'])
def test_installed_resolve_never_rescues_an_unproven_identity(prepared,failure):
    client,ws,payload,calls,controls=prepared;store=pending_registration(prepared)
    digest=store.status()['state_digest'];before=snapshot(ws.tmp);calls.clear()
    if failure=='parent-auth':controls['refuse']=True
    elif failure=='child-auth':controls['child_refused']=True
    elif failure=='child-model':controls['child_model']='different-wire-model'
    elif failure=='child-id':controls['child_id']=74
    elif failure=='stale-digest':digest='0'*64
    reply=invoke(prepared,{},'resolve',store.name,'--action=finalize-registration',
        '--expected-state-digest='+digest,entry='runtime_legacy_preview_cli.py',
        operator='OtherParent' if failure=='operator' else 'FixtureParent')
    assert reply.returncode==2 and not reply.stdout and 'Traceback' not in reply.stderr
    assert snapshot(ws.tmp)==before and store.status()['phase']=='response-saved'
    assert all(p['name']!='register_agent' or 'existing_agent_id' in p['arguments'] for p in calls)
    assert TOKEN not in reply.stderr and CHILD_TOKEN not in reply.stderr and CANARY not in reply.stderr
    expected=([] if failure in ('operator','stale-digest') else
        ['whois','register_agent'] if failure=='parent-auth' else
        ['whois','register_agent','whois'] if failure=='child-model' else
        ['whois','register_agent','whois','register_agent'])
    assert [p['name'] for p in calls]==expected
