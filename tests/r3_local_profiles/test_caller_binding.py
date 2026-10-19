"""TEST harness controls; never evidence of native CLI capabilities."""
from pathlib import Path
import runpy
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from .caller_payload import ROOT, copy_installer_payload, installer_manifest_inventory
from .caller_binding import make_delivery_python, read_binding_trace, assert_single_replacement
from .evidence import CALLERS, prepare_caller_invocation


def test_prepare_child_delivers_through_real_R2_receipt_and_provider(caller_probe, workspace):
    from .evidence import (read_fresh_capture, assert_runtime_capture,
                           assert_caller_tool_configuration, assert_single_owner,
                           caller_client_root, CALLER_STARTUP_PROMPT)
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'prepare-child')
    script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
    caller_probe('prepare-child', workspace=workspace, real_entry=CALLERS['prepare-child'],
                 provider_binary=binary, owner=owner, prepare_only=False,
                 provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    observed = read_fresh_capture(capture, nonce)
    assert_runtime_capture(observed, owner.parent/'runtime/launch.json', capture=capture,
                           binary=binary, expected_script_sha256=script_hash)
    assert observed['cwd'] == workspace.caller['cwd']
    assert observed['env']['HOME'] == str(owner.parent/'runtime/provider-home/codex')
    assert observed['argv'][-1] == CALLER_STARTUP_PROMPT
    assert_caller_tool_configuration(observed, owner, {})
    saved = json.loads(owner.read_text())['launch_intent']
    for field in ('cwd','task','name','parent','base','tools','clear_tools','resources','detached','worktree','chrome'):
        assert saved[field] == workspace.caller[field]
    assert_single_owner(caller_client_root(workspace), owner)


def test_fresh_server_resume_uses_real_spawn_end_and_same_saved_intent(caller_probe, workspace):
    from .evidence import (read_fresh_capture, assert_runtime_capture,
                           assert_resume_capture, assert_intent_digest,
                           CALLER_STARTUP_PROMPT)
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'prepare-child')
    script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
    caller_probe('prepare-child', workspace=workspace, real_entry=CALLERS['prepare-child'],
        provider_binary=binary, owner=owner, prepare_only=False,
        provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    startup = read_fresh_capture(capture, nonce)
    assert_runtime_capture(startup, owner.parent/'runtime/launch.json', capture=capture,
                           binary=binary, expected_script_sha256=script_hash)
    approved_owner = owner.read_bytes()
    approved = json.loads(approved_owner)['launch_intent']
    assert_intent_digest(approved, approved['intent_digest'])
    owner, binary, capture, nonce = prepare_caller_invocation(
        workspace, 'resume-child', resume_owner=approved_owner)
    script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
    caller_probe('resume-child', workspace=workspace, real_entry=CALLERS['resume-child'],
        provider_binary=binary, owner=owner, prepare_only=False,
        provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    resumed = read_fresh_capture(capture, nonce)
    assert_runtime_capture(resumed, owner.parent/'runtime/launch.json', capture=capture,
                           binary=binary, expected_script_sha256=script_hash)
    assert_resume_capture(resumed, startup, startup_prompt=CALLER_STARTUP_PROMPT,
                          envelope_index=10, session_id='native-fixture-session')
    final = json.loads(owner.read_text())['launch_intent']
    assert final == approved == caller_probe.resume_precondition['intent']
    assert_intent_digest(final, final['intent_digest'])
    history = caller_probe.servers.history
    assert len(history) == 2 and all(row['returncode'] is not None for row in history)
    assert history[0]['binding']['authority_epoch'] != history[1]['binding']['authority_epoch']
    assert caller_probe.resume_precondition['capture'] != capture


@pytest.mark.parametrize('with_mcp', [False, True])
@pytest.mark.parametrize('mutation', [None, 'payload-byte', 'plan-digest'])
def test_explicit_fixture_codex_trust_uses_real_verify_and_rejects_drift(workspace, with_mcp, mutation):
    # Explicit fixture trust, not evidence of native discovery/configure.
    from .caller_trust import prepare_codex_trust
    from .evidence import snapshot_tree
    api = runpy.run_path(str(ROOT/'bin/lib/runtime_client.py'))
    client = SimpleNamespace(isolation=workspace.tmp, runtime_dir=workspace.tmp/'runtime',
        outputs={'launch':workspace.tmp/'runtime/launch.json'}, config={'wrapper_root':str(ROOT)})
    delivery = api['runtime_delivery'](client)
    plan = prepare_codex_trust(delivery, fixture_mcp=with_mcp)
    trust = delivery.plugin_helpers()[0]
    if mutation == 'payload-byte':
        payload = Path(plan['plugin_root'])/'hooks/hooks.json'
        payload.write_bytes(payload.read_bytes()+b' ')
    elif mutation == 'plan-digest':
        plan['payload_digest'] = '0'*64
        api['atomic_json'](client.runtime_dir/'provider-home/codex/hook-plan.json', plan)
    before = snapshot_tree(workspace.tmp)
    calls = []
    previous = sys.getprofile()
    def observe(frame, event, arg):
        if (event == 'call' and frame.f_code.co_name == 'verify_approved_plan'
                and frame.f_code.co_filename == str(ROOT/'scripts/codex_plugin_trust.py')):
            calls.append(frame.f_code.co_name)
    sys.setprofile(observe)
    try:
        if mutation is None:
            assert delivery.codex_trust() == plan
        else:
            with pytest.raises(api['ClientError']) as refused:
                delivery.codex_trust()
            assert str(refused.value) == 'CODEX_HOOK_CONFIGURE_REQUIRED'
    finally:
        sys.setprofile(previous)
    assert calls == ['verify_approved_plan'], 'real launch trust verification was not called once'
    if mutation is not None:
        with pytest.raises(trust['TrustUnavailable']) as refused:
            trust['verify_approved_plan'](plan)
        assert str(refused.value) == 'approved plugin payload changed'
    config = client.runtime_dir/'provider-home/codex/config.toml'
    import tomllib
    values = tomllib.loads(config.read_text())
    assert ('fixture_extra' in values.get('mcp_servers', {})) is with_mcp
    assert snapshot_tree(workspace.tmp) == before, 'launch trust check rewrote fixture resources'


@pytest.mark.parametrize('mutation',[None,'missing-payload','extra-payload'])
def test_installer_declarations_and_real_manifest_inventory_agree(workspace,mutation):
    checkout_manifest=ROOT/'install-state.json'
    before=checkout_manifest.stat() if checkout_manifest.exists() else None
    install=workspace.tmp/'install'
    payload=copy_installer_payload(install)
    assert payload['sources'], 'installer copied no payload'
    expected={str(install/rel) for rel in payload['sources']}
    expected|={str(install/'env.sh'),str(install/'install-state.json')}
    assert set(payload['manifest']['owned_files'])==expected
    if mutation=='missing-payload':
        # Only this owned fixture file is removed; source stays untouched.
        target=install/'bin/lib/runtime_client.py'
        target.unlink()
    elif mutation=='extra-payload':
        (install/'bin/undeclared-fixture-file').write_text('unrecorded\n')
    actual,_=installer_manifest_inventory(install)
    if mutation is None:
        assert set(actual)==expected
    else:
        assert set(actual)!=expected, 'inventory failed to expose a copy/manifest mismatch'
    assert (checkout_manifest.stat() if checkout_manifest.exists() else None)==before
    assert not any(Path(path).is_symlink() for path in actual)


@pytest.mark.parametrize('enabled,child', [(True, False), (False, False), (True, True)])
def test_private_python_patches_exactly_one_gate_across_minimal_env_exec(workspace, enabled, child):
    install = workspace.tmp / 'install'
    copy_installer_payload(install)
    original = Path(sys.executable).resolve(strict=True)
    def fingerprint():
        info = original.stat()
        # Reading/executing may update atime; it is not a host modification.
        return tuple(getattr(info, field) for field in (
            'st_dev','st_ino','st_nlink','st_mode','st_uid','st_gid','st_size',
            'st_mtime_ns','st_ctime_ns')), original.read_bytes()
    before = fingerprint()
    python, trace = make_delivery_python(workspace, install, enabled=enabled)
    assert python == workspace.tmp / 'delivery-python/bin/python'
    assert python.resolve() == python and not python.is_symlink()
    assert python.read_bytes() == before[1]
    code = '''
import sys
assert sys.dont_write_bytecode is True
import runtime_native_scope
from runtime_intent_store import checked_intent
from local_model_auth import LocalModelError
try:
    checked_intent({})
except LocalModelError:
    pass
try:
    runtime_native_scope.require_native_binding({
        'declared_cli_version':'fixture-pinned',
        'requires_native_validation':True,'launch_admissible':False})
except LocalModelError as error:
    print(str(error))
else:
    print('DELIVERY_ONLY_NOT_NATIVE_ACCEPTANCE')
'''
    if child:
        # R2 invokes sys.executable after dropping PYTHONPATH. Do not preserve
        # fixture env keys as a second, product-visible way to enable this.
        code = ('import os,sys; os.execve(sys.executable,[sys.executable,"-c",'
                + repr(code) + '],{"HOME":os.environ["HOME"],"PATH":"/usr/bin:/bin"})')
    result = subprocess.run([str(python), '-c', code], env={
        'HOME': str(workspace.tmp / 'home'), 'PATH': '/usr/bin:/bin'},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('DELIVERY_ONLY_NOT_NATIVE_ACCEPTANCE' if enabled
                                    else 'LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    rows = read_binding_trace(trace)
    assert_single_replacement(rows, enabled=enabled)
    assert len([row for row in rows if row['event'] == 'bootstrap']) == (2 if child else 1)
    calls = {(row['file'], row['name']) for row in rows if row['event'] == 'real-call'}
    assert (str(install / 'bin/lib/runtime_intent_store.py'), 'checked_intent') in calls
    assert fingerprint() == before
    assert not any(key in row for row in rows for key in ('env', 'args', 'locals', 'token'))


def test_delivery_binding_names_never_appear_in_product_sources():
    for directory in ('bin', 'hooks', 'dashboard'):
        for path in (ROOT / directory).rglob('*'):
            if path.suffix not in ('.py', '.sh') or not path.is_file():
                continue
            text = path.read_text()
            assert '_orrery_delivery_fixture' not in text
            assert 'delivery-fixture.json' not in text
            assert 'DELIVERY_ONLY_NOT_NATIVE_ACCEPTANCE' not in text


@pytest.mark.parametrize('case',['transient-connect','exited','expired','wrong-profile'])
def test_fixture_health_retries_httpx_startup_race_but_never_admits_failed_server(workspace,case):
    # Unit control for TEST server readiness only; no product gate or native
    # capability function is replaced, and no model success is claimed here.
    import httpx
    from .caller_acceptance import wait_fixture_health
    management = workspace.tmp/'fixture-control.sock'
    management.touch()
    process = SimpleNamespace(poll=lambda:9 if case=='exited' else None)
    calls = []
    async def health():
        calls.append(None)
        if case=='transient-connect' and len(calls)<3:
            raise httpx.ConnectError('fixture HTTP listen is not ready')
        return {'status':'ok','runtime_profile':'legacy-v1' if case=='wrong-profile' else 's2c-v1'}
    if case=='transient-connect':
        wait_fixture_health(process,management,health,timeout=3)
        assert len(calls)==3
    else:
        reason = 'stopped' if case=='exited' else 'health timeout'
        with pytest.raises(AssertionError,match=reason):
            wait_fixture_health(process,management,health,timeout=.02 if case=='wrong-profile' else 0)
        assert bool(calls) is (case=='wrong-profile')


@pytest.mark.parametrize('entry', CALLERS)
@pytest.mark.parametrize('caller_probe', [False], indirect=True)
def test_each_real_entry_without_delivery_binding_is_native_unsupported(caller_probe, workspace, entry):
    # This is an acceptance gate, not a control that treats ANY rejection as
    # proof of native admission. Unconnected fields/receipts remain honestly red.
    initial = 'shell' if entry == 'resume-child' else entry
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, initial)
    with pytest.raises(RuntimeError, match='LOCAL_MODEL_ADAPTER_UNSUPPORTED'):
        caller_probe(entry, workspace=workspace, real_entry=CALLERS[entry],
                     provider_binary=binary, owner=owner, prepare_only=(entry=='operator'),
                     provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    # Resume provisioning has a real enabled spawn before disabling the gate.
    # Only the actual invocation interval proves the disabled resume refused.
    rows = caller_probe.last_entry_rows
    assert_single_replacement(rows, enabled=False)
    assert any(row['event']=='real-call'
               and row['file']==str(caller_probe.install/'bin/lib/runtime_native_scope.py')
               and row['name']=='require_native_binding' for row in rows), 'native gate was not exercised'
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    if entry == 'dashboard':
        assert caller_probe.last_dashboard_http == {'status':503,
            'body':{'ok':False, 'reason':'LOCAL_MODEL_ADAPTER_UNSUPPORTED'}}
        pids = {row['pid'] for row in rows if row['event']=='real-call'
                and row['file']==str(caller_probe.install/'bin/lib/runtime_native_scope.py')
                and row['name']=='require_native_binding'}
        assert len(pids)==1
        calls = {(row.get('file'),row.get('name'),row['pid']) for row in rows if row['event']=='real-call'}
        for file, names in [('dashboard/http_operator.py',('http','network','authenticate')),
                            ('dashboard/server.py',('parse_request','do_POST','do_spawn')),
                            ('dashboard/global_backend.py',('spawn',))]:
            for name in names:
                assert (str(caller_probe.install/file),name,next(iter(pids))) in calls
