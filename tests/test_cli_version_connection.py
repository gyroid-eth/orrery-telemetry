"""Comparison/real-R2/helper connection only; never native acceptance."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from r3_local_profiles.conftest import isolated_local_profile_home,workspace
from r3_local_profiles.caller_binding import make_delivery_python,_private_python_sites
from r3_local_profiles.caller_payload import ROOT,copy_installer_payload
from r3_local_profiles.caller_server import FreshServerLifetime
from cli_version_connection_fixture import digest

sys.path.insert(0,str(ROOT/'bin/lib'))
auth=importlib.import_module('local_model_auth')


@pytest.fixture
def connection_workspace(monkeypatch):
    base='/private/tmp' if Path('/private/tmp').is_dir() else None
    with tempfile.TemporaryDirectory(prefix='r3-caller-',dir=base) as raw:
        root=Path(raw).resolve(strict=True);root.chmod(0o700)
        for name in ('home','tmp','runtime','cwd','vault','task-dir','bin'):
            (root/name).mkdir(mode=0o700)
        ws=workspace.__wrapped__(root)
        for key,value in {'HOME':root/'home','TMPDIR':root/'tmp',
            'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r3-cli-version',
            'AGENTSTACK_HOME':root/'home/.agentstack','AGENTSTACK_RUNTIME_DIR':root/'runtime',
            'CODEX_HOME':root/'home/.codex','CLAUDE_CONFIG_DIR':root/'home/.claude'}.items():
            monkeypatch.setenv(key,str(value))
        yield ws


def require_seams():
    import runtime_native_scope
    assert callable(getattr(auth,'create_cli_acceptance',None)), 'R3_CLI_ACCEPTANCE_PRODUCER_UNIMPLEMENTED'
    assert hasattr(runtime_native_scope,'NativeConfigurationBinding'), 'R3_CLI_OBSERVATION_BINDING_UNIMPLEMENTED'


@contextmanager
def connection(ws,provider):
    require_seams()
    install=ws.tmp/'install'
    payload=copy_installer_payload(install)
    python,delivery_trace=make_delivery_python(ws,install,enabled=False)
    site=_private_python_sites(python)[0]
    bootstrap=ROOT/'tests/cli_version_connection_fixture.py'
    expected_bootstrap=hashlib.sha256(bootstrap.read_bytes()).hexdigest()
    copied=site/'_orrery_cli_version_fixture.py'
    shutil.copyfile(bootstrap,copied);copied.chmod(0o500)
    trace=ws.tmp/'comparison-binding.jsonl'
    settings=ws.tmp/('native.json' if provider=='claude' else 'native.toml')
    settings.write_text('{}' if provider=='claude' else '');settings.chmod(0o600)
    counter=ws.tmp/'credential-count'
    key=ws.tmp/'bin/fixture-credential'
    key.write_text('#!'+str(python)+' -I\nimport os,sys\nfrom pathlib import Path\n'
        +f'p=Path({str(counter)!r})\n'
        +'n=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n'
        +'assert os.environ["PATH"]=="/usr/bin:/bin"\n'
        +'assert not any(k in os.environ for k in ("OPENAI_API_KEY","ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN"))\n'
        +'sys.stdout.buffer.write(b"fixture-credential-"+str(n).encode())\n')
    key.chmod(0o500)
    # Explicit fake credential source for BOTH adapters; original #266 profile
    # and its authless caller tests remain untouched.
    ws.profile.write_text(ws.profile.read_text().replace('env = ',
        'auth_command = ['+json.dumps(str(key))+']\nenv = '))
    ws.profile.chmod(0o600)
    config={'install':str(install),'trace':str(trace),'bootstrap_sha256':expected_bootstrap,
        'observed_version':'fixture-pinned','enabled':True,'scope_change':False}
    config_path=site/'cli-version-fixture.json'
    def configure(**fields):
        config.update(fields)
        if config_path.exists():config_path.chmod(0o600)
        config_path.write_text(json.dumps(config,sort_keys=True));config_path.chmod(0o400)
    configure()
    pth=site/'zz-orrery-cli-version-fixture.pth'
    pth.write_text('import _orrery_cli_version_fixture; _orrery_cli_version_fixture.startup()\n')
    pth.chmod(0o400)
    datafile=ws.tmp/'entry.json'
    expectedfile=ws.tmp/'expected.json'
    resultfile=ws.tmp/'provider-result.json'
    binary=ws.tmp/'bin/fixture-provider'
    original_binary=('#!'+str(python)+'\nimport runpy,sys\n'
        +f'n=runpy.run_path({str(bootstrap)!r})\n'
        +f'sys.argv[1:1]=["provider",{str(datafile)!r}]\nn["main"]()\n'
        +'# fixture-v1\n').encode()
    binary.write_bytes(original_binary);binary.chmod(0o500)
    ws.caller['provider']=provider
    context=deepcopy(ws.context)
    context['effective_scope']['settings_sources']=[str(settings)]
    context['approved_settings_sha256']=hashlib.sha256(settings.read_bytes()).hexdigest()
    data={'provider':provider,'caller':deepcopy(ws.caller),'context':context,
        'settings':str(settings),'provider_binary':str(binary),'fixture_root':str(ROOT),
        'expected':str(expectedfile)}
    servers=FreshServerLifetime(ws)
    with servers.entry() as live:
        data['server_config']=str(live.config)
        def environment():
            return {'HOME':str(ws.tmp/'home'),'TMPDIR':str(ws.tmp/'tmp'),
                'PATH':'/usr/bin:/bin','LANG':'C','AGENTSTACK_RUNTIME_MODE':'global',
                'AGENTSTACK_GLOBAL_SERVER_CONFIG':str(live.config),'AGENTSTACK_MCP_URL':live.url,
                'AGENTSTACK_CONTACT_POLICY':'skip','AGENTSTACK_'+provider.upper()+'_BIN':str(binary)}
        def run(action):
            datafile.write_text(json.dumps(data,sort_keys=True));datafile.chmod(0o600)
            return subprocess.run([str(python),str(bootstrap),action,str(datafile)],
                env=environment(),capture_output=True,text=True,timeout=45)
        prepared=run('setup')
        assert prepared.returncode==0, 'R3_COMPARISON_SETUP_REJECTED: '+prepared.stderr
        data.update(json.loads(prepared.stdout))
        owner=ws.tmp/'isolation/agentstack/clients/child_01/profile.json'
        assert data['child_context']==str(owner.with_name('runtime-client.json'))
        owner_bytes=owner.read_bytes()
        intent=json.loads(owner_bytes)['launch_intent']
        assert intent['intent_digest']==digest({k:v for k,v in intent.items() if k!='intent_digest'})
        # Independent fixed-layout/bytes oracle, not runner_output/resolver.
        runner_bytes=('#!'+str(python)+' -I\n').encode()+(ROOT/'bin/lib/local_model_auth.py').read_bytes()
        runner=install/'libexec/local-model-auth'/hashlib.sha256(runner_bytes).hexdigest()/'runner'
        assert runner.read_bytes()==runner_bytes and runner.stat().st_mode&0o777==0o500
        expected={'auth_command':[str(runner),'--intent',str(owner),'--digest',intent['intent_digest'],
                '--provider',provider], 'intent_digest':intent['intent_digest'],
            'launch':str(owner.parent/'runtime/launch.json'),'result':str(resultfile),
            'version':'fixture-pinned','mutation':None}
        def expect(**fields):
            expected.update(fields);expectedfile.write_text(json.dumps(expected));expectedfile.chmod(0o600)
        expect()
        def change_bytes():
            binary.chmod(0o700);binary.write_bytes(original_binary+b'# fixture-v2\n');binary.chmod(0o500)
        yield SimpleNamespace(ws=ws,install=install,owner=owner,owner_bytes=owner_bytes,
            intent=intent,data=data,run=run,configure=configure,expect=expect,change_bytes=change_bytes,
            binary=binary,runner=runner,key=key,counter=counter,result=resultfile,trace=trace,delivery_trace=delivery_trace,
            payload=payload,bootstrap=copied,expected_bootstrap=expected_bootstrap)


def check_fixture_gates(f,*,enabled=True):
    rows=[json.loads(line) for line in f.trace.read_text().splitlines()]
    boots=[row for row in rows if row['event']=='comparison-bootstrap']
    expected=['runtime_native_scope.require_native_binding'] if enabled else []
    # Setup was explicitly enabled; only the final operation's new processes
    # use disabled gates. Inspect the final bootstrap for disabled controls.
    if enabled:assert all(row['replacements']==expected and row['replacement_count']==1 for row in boots)
    else:assert boots[-1]['replacements']==[] and boots[-1]['replacement_count']==0
    original=[json.loads(line) for line in f.delivery_trace.read_text().splitlines()]
    assert all(row['replacement_count']==0 for row in original if row['event']=='bootstrap')
    assert hashlib.sha256(f.bootstrap.read_bytes()).hexdigest()==f.expected_bootstrap
    for relative,source in f.payload['sources'].items():
        assert (f.install/relative).read_bytes()==source.read_bytes(), 'comparison fixture changed product payload'
    if enabled:
        calls={(row['file'],row['name']) for row in original if row['event']=='real-call'}
        required={'read_owner','checked_intent','materialize_launch_intent','check_configuration_evidence',
            'create_cli_acceptance','validate_cli_acceptance','execute','exec_provider'}
        observed={name for file,name in calls if file.startswith(str(f.install/'bin/lib')+os.sep)}
        assert required<=observed, 'real comparison/R2 gate was not reached'


@pytest.mark.parametrize('provider',['codex','claude'])
@pytest.mark.parametrize('version,bytes_changed', [('fixture-pinned',False),('fixture-v2',False),('fixture-v2',True)])
def test_real_r2_and_standalone_helper_share_comparison_without_reapproval(connection_workspace,provider,version,bytes_changed):
    with connection(connection_workspace,provider) as f:
        f.configure(observed_version=version);f.expect(version=version)
        if bytes_changed:f.change_bytes()
        result=f.run('launch')
        assert result.returncode==0, 'R3_REAL_COMPARISON_R2_REJECTED: '+result.stderr
        captured=json.loads(f.result.read_text())
        row=json.loads((f.owner.parent/'runtime/launch.json').read_text())
        proof=captured['cli_acceptance']
        assert row['phase']=='ended' and row['execution']['outcome']=={'state':'exited','returncode':0}
        assert row['launch_id']==captured['launch_id'] and row['receipt_nonce']==captured['receipt_nonce']
        assert row['cli_acceptance']==proof and row['intent_digest']==f.intent['intent_digest']
        assert proof['approved_cli']['version']=='fixture-pinned' and proof['observed_cli']['version']==version
        assert proof['version_changed'] is (version!='fixture-pinned') and proof['bytes_changed'] is bytes_changed
        assert proof['observed_cli']['sha256']==hashlib.sha256(f.binary.read_bytes()).hexdigest()
        assert [x['sha256'] for x in captured['auth_results']]==[
            hashlib.sha256(b'fixture-credential-0').hexdigest(),hashlib.sha256(b'fixture-credential-1').hexdigest()]
        assert f.counter.read_text()=='2' and f.owner.read_bytes()==f.owner_bytes
        check_fixture_gates(f)
        traces=[json.loads(line) for line in f.delivery_trace.read_text().splitlines()]
        shared=str(f.install/'bin/lib/process_observation.py')
        bindings=[entry for entry in traces if entry['event']=='real-call'
            and entry['file']==shared and entry['name']=='auth_process_binding']
        assert len(bindings)==2, 'each actual helper must invoke the pinned shared process gate'
        assert all(entry['pid'] not in (captured['pid'],row['execution']['runner']['pid']) for entry in bindings)
        for entry in bindings:
            names={item['name'] for item in traces if item['event']=='real-call'
                and item['file']==shared and item['pid']==entry['pid']}
            assert {'process_start','process_birth_utc','birth_not_before','ancestry'}<=names


@pytest.mark.parametrize('provider',['codex','claude'])
@pytest.mark.parametrize('mutation,reason', [('same-version-bytes','LOCAL_MODEL_CLI_CHANGED'),
    ('scope','LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE')])
def test_real_r2_refuses_comparison_difference_before_provider_or_key(connection_workspace,provider,mutation,reason):
    with connection(connection_workspace,provider) as f:
        if mutation=='same-version-bytes':f.change_bytes()
        else:f.configure(observed_version='fixture-v2',scope_change=True)
        result=f.run('launch')
        assert result.returncode!=0 and reason in result.stderr
        assert not f.counter.exists() and not f.result.exists()
        assert not (f.owner.parent/'runtime/launch.json').exists()
        assert f.owner.read_bytes()==f.owner_bytes


@pytest.mark.parametrize('provider',['codex','claude'])
@pytest.mark.parametrize('mutation',['nonce','launch-id','provider-pid','post-exec-bytes',
    'proof-sha','proof-scope','ended-receipt','gated-receipt'])
def test_actual_helper_refuses_unbound_or_changed_execution_before_key(connection_workspace,provider,mutation):
    with connection(connection_workspace,provider) as f:
        f.configure(observed_version='fixture-v2');f.expect(version='fixture-v2',mutation=mutation)
        f.change_bytes()
        result=f.run('launch')
        assert result.returncode==0, 'R3_REAL_AUTH_CONTROL_UNREACHED: '+result.stderr
        captured=json.loads(f.result.read_text())
        assert captured['auth_results']==[{'returncode':1,'sha256':hashlib.sha256(b'').hexdigest()}]
        assert not f.counter.exists() and f.owner.read_bytes()==f.owner_bytes
        check_fixture_gates(f)


@pytest.mark.parametrize('provider',['codex','claude'])
@pytest.mark.parametrize('resource',['renderer','runner','credential'])
def test_version_change_never_relaxes_real_non_cli_resource_checks(connection_workspace,provider,resource):
    with connection(connection_workspace,provider) as f:
        f.configure(observed_version='fixture-v2');f.expect(version='fixture-v2');f.change_bytes()
        target={'renderer':f.install/'bin/lib/process_observation.py','runner':f.runner,'credential':f.key}[resource]
        target.chmod(0o700);target.write_bytes(target.read_bytes()+b'\n# non-CLI resource drift\n');target.chmod(0o500)
        result=f.run('launch')
        assert result.returncode!=0 and 'LOCAL_MODEL_AUTH_FAILED' in result.stderr
        assert not f.counter.exists() and not f.result.exists()
        assert not (f.owner.parent/'runtime/launch.json').exists()
        assert f.owner.read_bytes()==f.owner_bytes


@pytest.mark.parametrize('provider',['codex','claude'])
def test_default_native_gate_still_refuses_without_comparison_binding(connection_workspace,provider):
    with connection(connection_workspace,provider) as f:
        f.configure(enabled=False)
        result=f.run('launch')
        assert result.returncode!=0 and 'LOCAL_MODEL_ADAPTER_UNSUPPORTED' in result.stderr
        assert not f.counter.exists() and not f.result.exists()
        assert not (f.owner.parent/'runtime/launch.json').exists()
        assert f.owner.read_bytes()==f.owner_bytes
        check_fixture_gates(f,enabled=False)
