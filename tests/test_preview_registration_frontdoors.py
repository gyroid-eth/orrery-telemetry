"""Real legacy entry refusal gates; stand-in transport, no native CLI or keys."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0,str(Path(__file__).parent))
from test_runtime_legacy_preview import intent
from test_preregister_child_naming import _FAKE_LIB

ROOT=Path(__file__).resolve().parents[1]


def owner(runtime):
    runtime.mkdir(mode=0o700,exist_ok=True)
    directory=runtime/'child-agents';directory.mkdir(mode=0o700,exist_ok=True)
    path=directory/'FixtureCurie.json'
    path.write_text(json.dumps({'launch_intent':intent(),'sentinel':'keep'}));path.chmod(0o600)
    return path


@pytest.mark.parametrize('configured,preview,model,reason',[
    (False,False,'local:example','LOCAL_MODEL_INTENT_OWNER_REQUIRED'),
    (True,False,'local:example','LOCAL_MODEL_PREVIEW_CONFIRMATION_REQUIRED'),
    (True,True,'local:example','LOCAL_MODEL_ADAPTER_UNSUPPORTED'),
    (True,False,'cloud-model','LOCAL_MODEL_INTENT_CHANGED'),
    (False,True,'cloud-model','LOCAL_MODEL_PREVIEW_POLICY_REQUIRED'),
])
def test_real_preregister_refuses_before_token_generation_or_network(tmp_path,configured,preview,model,reason):
    runtime=tmp_path/'runtime';path=owner(runtime) if configured else None
    before=path.read_bytes() if path else None
    calls=tmp_path/'effects'
    # Both credential loading/generation and all remote operations are effects.
    lib=tmp_path/'register.sh'
    lib.write_text(_FAKE_LIB+'''
ags_mail_load_token() { printf 'load-token\\n' >> "$EFFECTS"; }
ags_generate_registration_token() { printf 'generate-token\\n' >> "$EFFECTS"; printf 'fake-token\\n'; }
original_mcp_call=$(declare -f ags_mcp_call)
eval "${original_mcp_call/ags_mcp_call/standin_mcp_call}"
ags_mcp_call() { printf '%s\\n' "$1" >> "$EFFECTS"; standin_mcp_call "$@"; }
''')
    env={**os.environ,'HOME':str(tmp_path),'AGENTSTACK_HOME':str(tmp_path),
        'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.preview-frontdoors',
        'AGENTSTACK_RUNTIME_DIR':str(runtime),'AGENTSTACK_PROJECT_KEY':'/fixture',
        'AGENTSTACK_REGISTER_LIB':str(lib),'AGENTSTACK_ENV_FILE':'',
        'AGENTSTACK_PYTHON':sys.executable,'EFFECTS':str(calls)}
    args=['/bin/bash',str(ROOT/'bin/agentstack-preregister-child'),'--name','FixtureCurie',
        '--program','claude-code','--model',model,'--token-file-out',str(tmp_path/'token')]
    if preview:args.append('--preview')
    result=subprocess.run(args,env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode!=0
    assert reason in result.stderr
    assert not calls.exists(), calls.read_text() if calls.exists() else ''
    assert not (tmp_path/'token').exists()
    if path:assert path.read_bytes()==before


@pytest.fixture
def dashboard(monkeypatch,tmp_path):
    import dashboard.server as server
    monkeypatch.setattr(server,'GLOBAL_MODE',False)
    monkeypatch.setattr(server,'RUNTIME_DIR',str(tmp_path/'runtime'))
    monkeypatch.setattr(server,'_project_key',lambda:'/fixture')
    monkeypatch.setattr(server,'_spawn_name_status',lambda _: 'available')
    script=tmp_path/'spawn.sh';script.write_text('#!/bin/bash\n')
    monkeypatch.setattr(server,'SPAWN_SCRIPT',str(script))
    effects=[]
    def remote(*args,**kwargs):effects.append(args);return {'ok':False,'error':'standin called'}
    monkeypatch.setattr(server,'_mcp_call',remote)
    monkeypatch.setattr(server.subprocess,'Popen',lambda *a,**k:pytest.fail('exec attempted'))
    return server,effects


@pytest.mark.parametrize('configured,preview,model,reason',[
    (False,False,'local:example','LOCAL_MODEL_INTENT_OWNER_REQUIRED'),
    (True,False,'local:example','LOCAL_MODEL_PREVIEW_CONFIRMATION_REQUIRED'),
    (True,True,'local:example','LOCAL_MODEL_ADAPTER_UNSUPPORTED'),
    (True,False,'cloud-model','LOCAL_MODEL_INTENT_CHANGED'),
    (False,True,'cloud-model','LOCAL_MODEL_PREVIEW_POLICY_REQUIRED'),
])
@pytest.mark.parametrize('entry',['http-consumer','direct-register'])
def test_dashboard_both_frontdoors_refuse_before_catalog_or_registration(
        monkeypatch,tmp_path,dashboard,configured,preview,model,reason,entry):
    server,effects=dashboard
    path=owner(Path(server.RUNTIME_DIR)) if configured else None
    before=path.read_bytes() if path else None
    payload={'parent':'Parent','name':'FixtureCurie','task':'fixture',
        'dir':str(tmp_path),'provider':'claude','model':model}
    if preview:payload['preview']=True
    if entry=='http-consumer':
        monkeypatch.setattr(server,'_claude_catalog',lambda:pytest.fail('local entered cloud catalog'))
        result=server.do_spawn(payload)
    else:
        spec=server.SpawnLaunchSpec(provider='claude',program='claude-code',model=model,script=server.SPAWN_SCRIPT)
        result=server.spawn_with_launch_spec(payload,spec)
    assert result['ok'] is False
    assert result['error']==reason
    assert effects==[]
    if path:assert path.read_bytes()==before


@pytest.mark.parametrize('bad',[None,0,1,'true',{},[]])
def test_dashboard_preview_is_an_exact_boolean_before_any_effect(dashboard,bad):
    server,effects=dashboard
    result=server.do_spawn({'parent':'Parent','task':'fixture','model':'local:example','preview':bad})
    assert result=={'ok':False,'error':'preview must be boolean'}
    assert effects==[]
