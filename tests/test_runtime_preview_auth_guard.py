"""Unconnected preview must not use standalone auth's missing-row shortcut.

All profile/runner resources below are real temporary fixture bytes. A fake
credential executable is fingerprinted but NEVER executed, and no key is read.
This is refusal evidence only, not accepted ancestry/native/CLI enforcement.
"""
from copy import deepcopy
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
import local_model_auth as auth
import runtime_model_resources as resources
from runtime_intent_store import write_owner, write_private_bytes
from runtime_launch_intent import resolve_launch_intent
from test_runtime_legacy_context import prepared as context_fixture


@pytest.fixture
def auth_owner(context_fixture):
    ws=context_fixture
    credential=ws.tmp/'fixture-credential'
    credential.write_text('#!/bin/sh\nexit 91\n');credential.chmod(0o500)
    ws.profile.write_text(ws.profile.read_text().replace('[claude]',
        '[claude]\nauth_command = ["'+str(credential)+'"]'))
    # Resource-only interpreter stub: never executed. Do not depend on the
    # developer's Homebrew hardlink/layout or pretend to accept native Python.
    interpreter=ws.tmp/'fixture-interpreter'
    interpreter.write_text('#!/bin/sh\nexit 91\n');interpreter.chmod(0o500)
    output=resources.runner_output(ws.context['legacy_context']['install_root'],str(interpreter))
    runner=Path(output['runner']['path'])
    write_private_bytes(runner,output['output']['content'].encode());runner.chmod(0o500)
    ws.context['auth_runner']=output['runner']
    ws.context['auth_source']={'executable':resources.executable_evidence(str(credential)),
        'dependency_digests':{}}
    value=resolve_launch_intent(ws.caller,ws.context)
    owner=Path(ws.context['intent_owner'])
    write_owner(owner,{'launch_intent':value})
    return ws,owner,value,runner


def test_prepared_preview_with_real_validated_resources_cannot_release_auth_plan(auth_owner):
    ws,owner,value,runner=auth_owner
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):
        auth.read_auth_plan(owner,value['intent_digest'],'claude',runner_path=runner)
    assert not list(ws.tmp.rglob('credential-called'))


def test_standalone_runner_main_cannot_reach_credential_executor_without_preview_binding(auth_owner,monkeypatch,capsys):
    ws,owner,value,runner=auth_owner;calls=[]
    def forbidden(plan):
        calls.append('unexpected-key-request')
        raise AssertionError('preview reached credential executor before execution binding')
    monkeypatch.setattr(auth,'run_auth',forbidden)
    monkeypatch.setattr(auth,'__file__',str(runner))
    result=auth.main(['--intent',str(owner),'--digest',value['intent_digest'],'--provider','claude'])
    captured=capsys.readouterr()
    assert result==1 and captured.out=='' and captured.err=='LOCAL_MODEL_AUTH_FAILED\n'
    assert calls==[]


@pytest.mark.parametrize('field,value',[('execution_policy',None),('legacy_context',None),
    ('execution_policy',{'native_scope':'verified'}),('legacy_context',{})])
def test_present_preview_metadata_never_becomes_missing_global_launch_receipt(tmp_path,field,value):
    intent={'provider':'claude',field:value}
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):
        auth._read_cli_launch_auth(tmp_path/'Child.json',intent)


def test_ordinary_missing_receipt_keeps_its_existing_nonpreview_semantics(tmp_path):
    assert auth._read_cli_launch_auth(tmp_path/'profile.json',{'provider':'claude'})==(None,None)
