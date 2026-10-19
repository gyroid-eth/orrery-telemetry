"""Installed, operator-only instruction-configure entry; no native processes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_runtime_local_configure import prepared, server
from test_runtime_configure_ownership import snapshot


def request(prepared):
    _,ws,args = prepared
    allowed = {'local_models_root','effective_scope','approved_settings_sha256',
               'approved_settings_digests','auth_context'}
    return {'caller':deepcopy(ws.caller),
            'context':{key:value for key,value in ws.context.items() if key in allowed},
            'resources':deepcopy(args)}


def invoke(prepared, payload, *arguments):
    c,ws,_ = prepared
    script = Path(c.config['wrapper_root'])/'bin/lib/runtime_client.py'
    return subprocess.run([sys.executable,str(script),'--context',str(c.path),*arguments],
        input=json.dumps(payload),text=True,capture_output=True,cwd=ws.caller['cwd'],timeout=20,
        env={'HOME':str(ws.tmp/'home'),'PATH':'/usr/bin:/bin','LANG':'C',
             'TMPDIR':str(ws.tmp/'tmp'),'AGENTSTACK_RUNTIME_MODE':'global'})


def local_plan(prepared,payload=None):
    reply = invoke(prepared,payload if payload is not None else request(prepared),
                   'instruction-configure','operator','codex','local-plan')
    assert reply.returncode == 0,reply.stderr
    return json.loads(reply.stdout)


def test_installed_operator_plan_creates_no_owner_mutex_outputs_or_bytecode(prepared):
    c,ws,_ = prepared
    lock = c.runtime_dir/'child.lock'
    if lock.exists():lock.unlink()  # Only this fixture's inactive constructor lock.
    before = snapshot(Path(c.isolation))
    value = local_plan(prepared)
    assert value['owner'] == str(c.profile_path)
    assert value['intent']['effective_scope']['native_proposal']['launch_admissible'] is False
    assert snapshot(Path(c.isolation)) == before
    assert not lock.exists() and not c.profile_path.exists()
    renderers = value['intent']['configuration_evidence']['renderer_sources']
    library=Path(c.config['wrapper_root'])/'bin/lib'
    expected={str(library/name) for name in (
        'runtime_local_configure.py','runtime_native_scope.py','runtime_launch_intent.py','runtime_provider_projection.py',
        'runtime_delivery.py','runtime_client.py','runtime_local_launch.py',
        'runtime_model_resources.py','local_model_auth.py','local_model_profiles.py',
        'runtime_caller_schema.py','runtime_intent_store.py','runtime_model_catalog.py',
        'instruction_blocks.py','process_observation.py')}
    expected.add(str(Path(c.config['wrapper_root'])/'hooks/child_tools.py'))
    assert set(renderers)==expected


def test_apply_without_explicit_advance_consent_is_rejected_before_mutex(prepared):
    c,_,_ = prepared
    value = local_plan(prepared)
    lock = c.runtime_dir/'child.lock'
    if lock.exists():lock.unlink()
    before = snapshot(Path(c.isolation))
    reply = invoke(prepared,value,'instruction-configure','operator','codex','local-apply',value['plan_id'])
    assert reply.returncode == 2 and reply.stderr.strip() == 'runtime-client: OPERATOR_CONFIRMATION_REQUIRED'
    assert not reply.stdout and snapshot(Path(c.isolation)) == before


def test_explicit_apply_and_lost_return_use_installed_same_owner_and_plan(prepared):
    c,_,_ = prepared
    value = local_plan(prepared)
    args = ['--assume-yes','instruction-configure','operator','codex','local-apply',value['plan_id']]
    reply = invoke(prepared,value,*args)
    assert reply.returncode == 0,reply.stderr
    assert json.loads(reply.stdout)['launch_admissible'] is False
    assert json.loads(c.profile_path.read_text())['launch_intent'] == value['intent']
    assert not c.credential.exists() and not c.outputs['registration'].exists()
    before = snapshot(Path(c.isolation))
    again = invoke(prepared,value,*args)
    assert again.returncode == 0,again.stderr
    assert json.loads(again.stdout)['changed'] is False
    assert snapshot(Path(c.isolation)) == before


@pytest.mark.parametrize('mutation',['unknown-context','owner-injection','uid-injection','extra-resources',
                                     'wrong-provider','extra-frame'])
def test_cli_plan_rejects_untrusted_operator_input_without_publishing(prepared,mutation):
    c,_,_ = prepared
    payload = request(prepared)
    if mutation == 'unknown-context':payload['context']['native_verified'] = True
    elif mutation == 'owner-injection':payload['context']['intent_owner'] = str(c.isolation/'foreign')
    elif mutation == 'uid-injection':payload['context']['operator_uid'] = os.getuid()
    elif mutation == 'extra-resources':payload['resources']['unknown_resource'] = True
    elif mutation == 'wrong-provider':payload['caller']['provider'] = 'claude'
    else:payload['unexpected'] = True
    before = snapshot(Path(c.isolation))
    reply = invoke(prepared,payload,'instruction-configure','operator','codex','local-plan')
    assert reply.returncode == 2 and 'Traceback' not in reply.stderr
    assert not reply.stdout and snapshot(Path(c.isolation)) == before


@pytest.mark.parametrize('intent',[None,[], 'wrong'])
def test_malformed_apply_does_not_leak_an_internal_exception(prepared,intent):
    c,_,_ = prepared
    before = snapshot(Path(c.isolation))
    reply = invoke(prepared,{'intent':intent},'--assume-yes','instruction-configure',
                   'operator','codex','local-apply','0'*64)
    assert reply.returncode == 2 and reply.stderr.strip() == 'runtime-client: CLIENT_INPUT_INVALID'
    assert not reply.stdout and snapshot(Path(c.isolation)) == before


def test_advance_consent_flag_is_not_silently_accepted_on_other_operations(prepared):
    c,_,_ = prepared
    before = snapshot(Path(c.isolation))
    reply = invoke(prepared,{},'--assume-yes','instruction-configure','operator','codex')
    assert reply.returncode == 2 and reply.stderr.strip() == 'runtime-client: ARGUMENTS_UNSUPPORTED'
    assert snapshot(Path(c.isolation)) == before
