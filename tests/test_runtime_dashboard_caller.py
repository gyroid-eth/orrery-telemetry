"""Actual global HTTP/full caller admission; no native permission simulation."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import selectors
import subprocess
import sys
from types import SimpleNamespace

import pytest

from dashboard import global_backend, server as dashboard
from test_global_child_client import server
from test_global_dashboard_backend import ui, request
from test_runtime_shell_caller import prepared_caller
from test_runtime_local_configure import prepared as local_proposal
from r3_local_profiles.evidence import snapshot_tree, assert_tree_unchanged


def full(caller, **overrides):
    return {'client_name':'child_01', 'parent_client_name':'parent',
            'caller':deepcopy(caller), 'prepare_only':True, **overrides}


@pytest.fixture
def prepared_http(ui, prepared_caller, monkeypatch):
    parent, child, caller, intent = prepared_caller
    monkeypatch.setenv('AGENTSTACK_SPAWN_ROOTS', str(parent.isolation))
    # The original task source is not a private credential or runtime output.
    Path(caller['task']).chmod(0o644)
    return ui, parent, child, caller, intent


def no_rpc(monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('rejected HTTP caller reached authenticated RPC')
    monkeypatch.setattr(global_backend.OPERATOR_API['API']['RuntimeClient'], 'rpc', forbidden)
    return calls


@pytest.fixture
def approved_foreground(prepared_http):
    # Explicit fixture approval, not a request that merely disagrees with the
    # saved detached=True intent. Keep all ordinary admission gates real.
    from local_model_auth import intent_digest
    from runtime_intent_store import save_launch_intent
    http, parent, child, caller, intent = prepared_http
    caller = {**caller, 'detached':False}
    intent = {**intent, 'detached':False, 'revision':intent['revision'] + 1}
    intent['intent_digest'] = intent_digest(intent)
    with child.operation_mutex(), child.fence():
        save_launch_intent(child.profile_path, intent, mode='global', locked=True,
            writer=lambda path, value:child.write_output(path, value, 'profile'))
    return http, parent, child, caller, intent


def test_http_rejects_approved_foreground_execution_before_effects(approved_foreground, monkeypatch):
    http, parent, child, caller, intent = approved_foreground
    assert json.loads(child.profile_path.read_text())['launch_intent'] == intent
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST',
                        body=full(caller, prepare_only=False))
    assert code == 400 and json.loads(raw) == {
        'ok':False, 'reason':'HTTP_FOREGROUND_UNSUPPORTED'}, (raw, calls)
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)
    assert not child.outputs['task'].exists() and not child.outputs['launch'].exists()


def test_http_can_prepare_approved_foreground_without_rewriting_it(approved_foreground):
    http, parent, child, caller, intent = approved_foreground
    owner = child.profile_path.read_bytes()
    code, raw = request(http, '/api/global/spawn', method='POST', body=full(caller))
    assert code == 200 and json.loads(raw)['ok'] is True, raw
    assert child.profile_path.read_bytes() == owner
    assert json.loads(owner)['launch_intent'] == intent and intent['detached'] is False
    assert child.outputs['task'].read_bytes() == Path(caller['task']).read_bytes()
    assert json.loads(child.outputs['child'].read_text())['phase'] == 'prepared'
    assert not child.outputs['launch'].exists()


@pytest.mark.parametrize('field,value,reason', [
    ('name', 'OtherChild', 'LOCAL_MODEL_IDENTITY_MISMATCH'),
    ('model', 'gpt-other-fixture-9', 'LOCAL_MODEL_INTENT_CHANGED')])
def test_minimal_http_conflicts_use_common_409_without_effects(prepared_http, monkeypatch, field, value, reason):
    http, parent, child, caller, intent = prepared_http
    body = {'client_name':'child_01', 'name':caller['name'], 'program':caller['provider'],
            'model':caller['model'], 'cwd':caller['cwd'], field:value}
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST', body=body)
    assert code == 409 and json.loads(raw) == {'ok':False, 'reason':reason}, (raw, calls)
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)
    assert not child.outputs['task'].exists() and not child.outputs['launch'].exists()


@pytest.mark.parametrize('mode', [0o600, 0o644])
def test_full_http_calls_actual_do_spawn_and_publishes_task(prepared_http, monkeypatch, mode):
    http, parent, child, caller, intent = prepared_http
    Path(caller['task']).chmod(mode)
    owner = child.profile_path.read_bytes()
    observed = []
    original = dashboard.do_spawn
    def traced(payload):
        observed.append(deepcopy(payload))
        return original(payload)
    monkeypatch.setattr(dashboard, 'do_spawn', traced)
    body = full(caller)
    code, raw = request(http, '/api/global/spawn', method='POST', body=body)
    result = json.loads(raw)
    assert code == 200 and result['ok'] is True, raw
    assert observed == [body], 'HTTP bypassed the actual dashboard caller'
    assert result['client_name'] == 'child_01' and result['agent_id'] == child.identity['agent_id']
    assert child.profile_path.read_bytes() == owner
    assert json.loads(owner)['launch_intent'] == intent
    assert child.outputs['task'].read_bytes() == Path(caller['task']).read_bytes()
    assert json.loads(child.outputs['child'].read_text())['phase'] == 'prepared'
    assert not child.outputs['launch'].exists()


@pytest.mark.parametrize('field', [
    'provider', 'model', 'cwd', 'task', 'name', 'parent', 'tools', 'base',
    'clear_tools', 'detached', 'resources', 'worktree', 'chrome',
    'effort', 'strict_output_cap', 'task_delivery'])
def test_full_http_rejects_each_different_literal_without_RPC(prepared_http, monkeypatch, field):
    http, parent, child, caller, intent = prepared_http
    changed = {'provider':'claude', 'model':'gpt-other-fixture-9',
        'cwd':str(parent.isolation), 'task':str(parent.isolation/'other.md'),
        'name':'OtherChild', 'parent':'OtherParent', 'tools':{'mcp':['fixture_extra']},
        'base':'default', 'clear_tools':True, 'detached':False, 'resources':[],
        'worktree':True, 'chrome':True, 'effort':'', 'strict_output_cap':False,
        'task_delivery':'mail'}
    assert field not in caller or caller[field] != changed[field]
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST',
                        body=full({**caller, field:changed[field]}))
    reason = ('LOCAL_MODEL_IDENTITY_MISMATCH' if field in ('provider','cwd')
              else 'LOCAL_MODEL_INTENT_CHANGED')
    assert code == 409 and json.loads(raw) == {'ok':False, 'reason':reason}, raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)
    assert not child.outputs['task'].exists() and not child.outputs['launch'].exists()


def test_full_http_does_not_canonicalize_a_different_cwd_literal(prepared_http, monkeypatch):
    http, parent, child, caller, intent = prepared_http
    alias = parent.isolation/'cwd-alias'
    alias.symlink_to(caller['cwd'], target_is_directory=True)
    assert alias.resolve() == Path(caller['cwd']) and str(alias) != caller['cwd']
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST',
                        body=full({**caller, 'cwd':str(alias)}))
    assert code == 409 and json.loads(raw)['reason'] == 'LOCAL_MODEL_IDENTITY_MISMATCH', raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)


@pytest.mark.parametrize('field,value', [
    ('name','OtherChild'), ('program','codex'), ('model','gpt-fixture-future-9'),
    ('cwd','/other'), ('dir','/other'), ('server_config','/other/server-config.json'),
    ('prepare_only','false'), ('parent_client_name',None), ('expected_digest',43)])
def test_http_rejects_mixed_or_unknown_transport_fields_before_effects(prepared_http, monkeypatch, field, value):
    http, parent, child, caller, intent = prepared_http
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST', body=full(caller, **{field:value}))
    assert code == 400 and json.loads(raw)['reason'] == 'QUERY_INVALID', raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)


@pytest.mark.parametrize('field', [
    'provider', 'model', 'cwd', 'task', 'name', 'parent', 'tools', 'base',
    'clear_tools', 'detached', 'resources', 'worktree', 'chrome'])
def test_http_never_backfills_a_missing_literal_from_owner(prepared_http, monkeypatch, field):
    http, parent, child, caller, intent = prepared_http
    incoming = deepcopy(caller)
    del incoming[field]
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST', body=full(incoming))
    assert code == 400 and json.loads(raw)['reason'] == 'LOCAL_MODEL_CALLER_INVALID', raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)


@pytest.mark.parametrize('field,value', [
    ('root','/other'), ('owner','/other/profile.json'), ('effective_scope',{}),
    ('auth',{}), ('detached','false'), ('resources','not-a-list')])
def test_http_rejects_invalid_nested_caller_before_effects(prepared_http, monkeypatch, field, value):
    http, parent, child, caller, intent = prepared_http
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST', body=full({**caller, field:value}))
    assert code == 400 and json.loads(raw)['reason'] == 'LOCAL_MODEL_CALLER_INVALID', raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)


@pytest.mark.parametrize('name', ['mail', 'shell'])
def test_full_http_preserves_real_tools_grammar(prepared_http, monkeypatch, name):
    http, parent, child, caller, intent = prepared_http
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST',
                        body=full({**caller, 'tools':{name:True}}))
    assert code == 400 and json.loads(raw)['reason'] == 'CHILD_TOOLS_INVALID', raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)


def test_full_http_selects_explicit_parent_not_display_name(prepared_http, monkeypatch):
    http, parent, child, caller, intent = prepared_http
    operator = global_backend.OPERATOR_API['RuntimeOperator'](http[0]['config_path'])
    operator.create('otherparent', agent_name='OtherParent', program='daemon', model='deterministic')
    before = snapshot_tree(parent.client_root.parent)
    calls = no_rpc(monkeypatch)
    code, raw = request(http, '/api/global/spawn', method='POST',
                        body=full(caller, parent_client_name='otherparent'))
    assert code == 409 and json.loads(raw)['reason'] == 'CHILD_PARENT_MISMATCH', raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)


@pytest.mark.parametrize('kind', ['unauthenticated', 'foreign-origin', 'foreign-host'])
def test_full_http_admission_runs_before_actual_do_spawn(prepared_http, monkeypatch, kind):
    http, parent, child, caller, intent = prepared_http
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('unadmitted request reached dashboard caller')
    monkeypatch.setattr(dashboard, 'do_spawn', forbidden)
    before = snapshot_tree(parent.client_root.parent)
    extra = {'Origin':'https://foreign.example'} if kind == 'foreign-origin' else (
        {'Host':'foreign.example'} if kind == 'foreign-host' else {})
    code, raw = request(http, '/api/global/spawn', method='POST', body=full(caller),
                        auth=kind != 'unauthenticated', extra=extra)
    reason = {'unauthenticated':'HTTP_OPERATOR_REQUIRED', 'foreign-origin':'HTTP_ORIGIN_FORBIDDEN',
              'foreign-host':'HTTP_HOST_FORBIDDEN'}[kind]
    assert code == 403 and json.loads(raw)['reason'] == reason, raw
    assert calls == []
    assert_tree_unchanged(parent.client_root.parent, before)


@pytest.mark.parametrize('prepare_only', [False, True])
def test_direct_global_do_spawn_is_only_a_literal_operator_adapter(prepared_http, monkeypatch, prepare_only):
    # Argument handoff only: deliberately stop before operator admission/effects.
    # This does not prove R2/native execution or HTTP authentication.
    http, parent, child, caller, intent = prepared_http
    class Forwarded(Exception): pass
    observed = []
    def stop(self, name, **kwargs):
        observed.append((name, kwargs))
        raise Forwarded()
    monkeypatch.setattr(global_backend.OPERATOR_API['RuntimeOperator'], 'spawn', stop)
    with pytest.raises(Forwarded):
        dashboard.do_spawn(full(caller, prepare_only=prepare_only, task_delivery='mail'))
    assert observed == [('child_01', {'caller':caller, 'parent_client_name':'parent',
        'prepare_only':prepare_only, 'task_delivery':'mail', 'expected_digest':None})]
    assert not child.outputs['task'].exists() and not child.outputs['launch'].exists()


def test_global_full_caller_capability_is_not_a_native_acceptance_claim(ui):
    code, raw = request(ui, '/api/version')
    value = json.loads(raw)
    assert code == 200 and value['api'] == 21
    assert value['capabilities']['global_spawn_full_caller'] == 1
    assert 'native_local_models' not in value['capabilities']


def test_full_http_never_approves_an_unverified_local_proposal(ui, local_proposal, monkeypatch):
    # Real explicit configure, real native fail-closed gate, no binding patch.
    # All executables are inert owned sentinels; none may be executed.
    import runtime_local_configure
    from r3_local_profiles.caller_payload import copy_installer_payload
    client, workspace, args = local_proposal
    # The configure fixture owns a different private install, so a checkout
    # dashboard correctly rejects it with WRAPPER_ROOT_MISMATCH. Exercise the
    # matching real installed dashboard in a fresh process instead of patching
    # that protection or treating its earlier refusal as native-gate evidence.
    install = Path(client.isolation)/'http-install'
    (install.parent/'home').mkdir(mode=0o700, exist_ok=True)
    copied = copy_installer_payload(install)
    # runpy's fixed client adds its sibling library path. Own that temporary
    # addition through pytest so later files cannot inherit this install.
    monkeypatch.syspath_prepend(str(install/'bin/lib'))
    api = runpy.run_path(str(install/'bin/lib/runtime_client.py'))
    api['atomic_json'](client.path, {**client.config, 'wrapper_root':str(install)})
    client = api['RuntimeClient'](client.path)
    plan = runtime_local_configure.plan_local_configuration(client, workspace.caller,
        workspace.context, **args)
    runtime_local_configure.apply_local_configuration(client, plan, approved_id=plan['plan_id'])
    before = snapshot_tree(client.client_root.parent)
    operator = global_backend.OPERATOR_API['RuntimeOperator'](ui[0]['config_path'])
    revision = operator.read({'kind':'health'})['as_of_revision']
    home, temporary = install.parent/'http-home', install.parent/'http-tmp'
    home.mkdir(mode=0o700); temporary.mkdir(mode=0o700)
    env = {'HOME':str(home), 'TMPDIR':str(temporary), 'PATH':'/usr/bin:/bin', 'LANG':'C',
        'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r3-dashboard-caller',
        'CODEX_HOME':str(home/'.codex'), 'CLAUDE_CONFIG_DIR':str(home/'.claude'),
        'AGENTSTACK_RUNTIME_MODE':'global', 'AGENTSTACK_GLOBAL_SERVER_CONFIG':str(ui[0]['config_path']),
        'AGENTSTACK_MCP_URL':ui[0]['url'], 'AGENTSTACK_HOME':str(install.parent/'agentstack'),
        'AGENTSTACK_SPAWN_ROOTS':str(client.isolation), 'AGENTSTACK_CODEX_BIN':args['cli']}
    script = '''import sys
from pathlib import Path
from http.server import HTTPServer
sys.path.insert(0, sys.argv[1])
from dashboard import server
assert Path(server.__file__).resolve() == Path(sys.argv[1])/'dashboard/server.py'
assert server.GLOBAL_MODE is True
http = HTTPServer(('127.0.0.1', 0), server.Handler)
http.timeout = 15
try:
    print(http.server_port, flush=True)
    http.handle_request()
finally:
    http.server_close()
'''
    # Preserve copied installer bytes, including preexisting checkout pyc.
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', script, str(install)],
        cwd=install, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            # Cold installed imports also run under the CI worker load. EOF
            # still becomes readable immediately and fails the port assertion
            # below; only the test's upper readiness budget changes.
            assert selector.select(timeout=60), 'installed HTTP startup timed out'
            port = process.stdout.readline().strip()
        assert port.isascii() and port.isdigit(), 'installed HTTP did not start'
        installed_ui = (ui[0], SimpleNamespace(server_port=int(port)), ui[2])
        code, raw = request(installed_ui, '/api/global/spawn', method='POST',
            body=full(workspace.caller, client_name=client.config['client_name']))
        process.communicate(timeout=15)
        assert process.returncode == 0, 'installed HTTP process failed'
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate(timeout=5)
    assert code == 503 and json.loads(raw)['reason'] == 'LOCAL_MODEL_ADAPTER_UNSUPPORTED', raw
    assert operator.read({'kind':'health'})['as_of_revision'] == revision
    assert_tree_unchanged(client.client_root.parent, before)
    assert not client.identity and not client.outputs['registration'].exists()
    assert not client.outputs['launch'].exists()
    assert not list(Path(client.isolation).rglob('must-not-run'))
    for relative, source in copied['sources'].items():
        assert (install/relative).read_bytes() == source.read_bytes(), relative
