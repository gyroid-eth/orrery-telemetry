"""Standard launcher handoff and cleanup evidence; synthetic identities only."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hooks'))
import child_resume
import harness_child as child


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    path.chmod(0o600)


def staged(tmp_path, program='dsh'):
    runtime = tmp_path / 'runtime'
    token = tmp_path / 'handoff-token'
    private(token, 'dummy-owner')
    private(Path(str(token) + '.binding.json'), dict(agent_id=7, agent_name='FixtureChild',
        project_key='/fixture/project', program=program))
    generation = 'a' * 32
    child_resume.stage_registration(runtime, 'FixtureChild', project_key='/fixture/project',
        program=program, generation=generation, source=token, binding=Path(str(token)+'.binding.json'))
    child_resume.prepare_active_state(runtime, 'FixtureChild', project_key='/fixture/project',
        program=program, generation=generation, parent_agent='')
    mcp = tmp_path / 'mcp.json'
    private(mcp, {'mcpServers': {'orrery-mail': {'env': {
        'AGENTSTACK_PROXY_AGENT_NAME':'FixtureChild', 'AGENTSTACK_MCP_URL':'http://127.0.0.1:9/mcp',
        'AGENTSTACK_MAIL_HTTP_BEARER_MODE':'disabled'}}}})
    prompt = tmp_path / 'prompt.txt'; prompt.write_text('Literal `task` $(no shell evaluation)')
    return SimpleNamespace(runtime_dir=str(runtime),agent_name='FixtureChild',project_key='/fixture/project',
        generation=generation,harness=program,backend='strata',model='formal-model',cwd=str(tmp_path),
        parent_agent='',mcp_config=str(mcp),manifest=str(tmp_path/'manifest.json'),
        prompt_file=str(prompt),effort=None,tools='{"base":"default","tools":{}}')


@pytest.mark.parametrize('program', ['dsh','hermes'])
@pytest.mark.parametrize('helper', [None,'same','alternate'])
def test_native_generation_keeps_identity_prompt_and_shared_instructions(tmp_path, program, helper, monkeypatch):
    args = staged(tmp_path, program)
    args.helper_model = helper
    generator = tmp_path / 'generator.py'
    generator.write_text("""import argparse,json,sys,os
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--out');a=p.parse_args()
s=json.load(open(a.input));prompt=open(s['prompt_file']).read()
with open(a.out,'w') as f:json.dump({'argv':[sys.executable,'-c','import json,sys; print(json.dumps(sys.argv[1:]))',prompt], 'env':{'FIXTURE_REF':s['mcp_config']},'cwd':s['cwd']},f)
os.chmod(a.out,0o600)
""")
    monkeypatch.setattr(child, 'selection', lambda *a: {'python':sys.executable})
    monkeypatch.setattr(child, 'config_generator', lambda *a: generator)
    state = child.prepare(args)
    source = child.read(state / 'input.json')
    assert source['program'] == program and source['backend_id'] == 'strata'
    assert source['parent_agent'] is None
    assert not ({'effort','task_timeout_seconds','task','allowed_paths','expected'} & source.keys())
    assert Path(source['prompt_file']).read_text() == Path(args.prompt_file).read_text()
    spec=importlib.util.spec_from_file_location('expected_instruction_renderer', ROOT/'bin/lib/instruction_blocks.py')
    renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
    assert Path(source['instructions_file']).read_bytes() == renderer.render(
        'harness',(ROOT/'codex/AGENTS.md').read_bytes(),str(ROOT),args.project_key)
    instructions = Path(source['instructions_file']).read_text()
    assert not any(word in instructions.lower() for word in ('codex', 'claude-code'))
    import shlex
    invocation = child.command(state, 'FixtureChild').split('; native_rc=$?',1)[0]
    assert shlex.split(invocation)[-1] == Path(args.prompt_file).read_text()
    result=child.subprocess.run(['/bin/bash','-c',invocation],capture_output=True,text=True,cwd=tmp_path,check=True)
    assert json.loads(result.stdout)==[Path(args.prompt_file).read_text()]
    assert source.get('helper_model') == (helper if helper=='alternate' else None)
    assert 'pane_entry.py' not in invocation and '--import' not in invocation
    assert (state/'input.json').stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError): child.command(state,'OtherChild')


def test_old_generator_not_admitted_by_file_presence(tmp_path):
    entry=tmp_path/'integrations/dsh/thin_config.py';entry.parent.mkdir(parents=True)
    for code in ['# old generator','THIN_CONFIG_CONTRACT = 0','THIN_CONFIG_CONTRACT = True']:
        entry.write_text(code)
        with pytest.raises(ValueError,match='thin_config_unavailable'):child.config_generator(tmp_path,'dsh')
    entry.write_text('THIN_CONFIG_CONTRACT = 1')
    assert child.config_generator(tmp_path,'dsh')==entry


def test_real_dynamic_prepare_and_command_selected_endpoint_once(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from test_dsh_thin_config import ThinTests
    fixture = ThinTests(); fixture.setUp()
    calls, payload = [], {'data': [{'id': 'new-wire-id', 'meta': {'n_ctx': 131072}}]}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True); worker.start()
    try:
        base = 'http://127.0.0.1:' + str(http.server_port)
        manifest = child.read(fixture.manifest)
        manifest['backends']['strata'] = {'model_source': 'server', 'urls': ['https://unused.invalid/v1', base]}
        manifest['harnesses']['dsh']['python'] = sys.executable
        private(fixture.manifest, manifest)
        args = staged(tmp_path)
        args.manifest, args.model, args.url_index = str(fixture.manifest), 'new-wire-id', 1
        args.url_key = child.native_server_models.url_key(base)
        mcp = child.read(args.mcp_config)
        mcp['mcpServers']['orrery-mail']['command'] = '/synthetic/mail-proxy'
        private(Path(args.mcp_config), mcp)
        monkeypatch.setattr(child.native_server_models, 'read_key', lambda *a: 'synthetic-key')
        state = child.prepare(args)
        assert calls == []
        source = child.read(state / 'input.json')
        assert (source['backend_id'], source['url_index'], source['server_url'], source['model']) == ('strata', 1, base, 'new-wire-id')
        invocation = child.command(state, args.agent_name)
        assert calls == ['/v1/models'] and 'synthetic-key' not in invocation
        overlay = child.read(state / 'dsh-config/native.patch.json')
        route = next(r['config'] for r in overlay if r.get('id') == 'llm-deepseek')
        assert route['baseURL'] == base and route['models'][0]['id'] == 'new-wire-id'
        assert route['models'][0]['contextWindow'] == 131072
        runtime = Path(args.runtime_dir)
        child_resume.finish_registration(runtime, args.agent_name, generation=args.generation, rollback=False)
        private(state / 'native-session.json', {'harness': 'dsh', 'agent_name': args.agent_name,
            'native_session_id': 'exact-native-id', 'created_at': '2026-10-19T00:00:00Z'})
        assert child_resume.mark_retired(runtime, args.agent_name, retention_days=30)
        child_resume.begin_resume(runtime, args.agent_name, agent_id=7, project_key=args.project_key, program='dsh')
        assert child.prepare_resume(runtime, args.agent_name) == state
        assert len(calls) == 1
        invocation = child.resume_command(runtime, args.agent_name)
        assert '--resume exact-native-id' in invocation and len(calls) == 2
        retained = child.read(state / 'input.json')
        assert (retained['backend_id'], retained['url_index'], retained['server_url'], retained['model']) == ('strata', 1, base, 'new-wire-id')
        payload['data'][0]['id'] = 'replacement-model'
        with pytest.raises(ValueError, match='model_not_served'):
            child.command(state, args.agent_name)
        assert len(calls) == 3
        payload['data'][0] = {'id': 'new-wire-id', 'status': {'value': 'unloaded'}}
        with pytest.raises(ValueError, match='model_not_loaded'):
            child.command(state, args.agent_name)
        assert len(calls) == 4
        from dashboard import server
        captured = []
        monkeypatch.setenv('AGENTSTACK_MANAGED_SPAWN_CONFIG', str(fixture.manifest))
        monkeypatch.setattr(server, 'GLOBAL_MODE', False)
        monkeypatch.setattr(server, '_project_key', lambda: '/fixture/project')
        monkeypatch.setattr(server, 'spawn_with_launch_spec', lambda payload, spec: captured.append(spec) or {'ok': True})
        request = dict(standalone=True, task='Synthetic task', dir=str(tmp_path),
            provider='strata', harness='dsh', model='new-wire-id', url_index=1, url_key=args.url_key)
        assert server.do_spawn(request)['ok']
        assert captured[0].provider_args[:8] == ('--harness', 'dsh', '--backend', 'strata',
            '--url-index', '1', '--url-key', args.url_key)
        assert len(calls) == 4  # POST is static; command owns the one lookup.
        assert not server.do_spawn({**request, 'url_key': '0' * 64})['ok']
        assert len(captured) == 1 and len(calls) == 4
        manifest['backends']['strata']['urls'][1] = 'https://replacement.invalid/v1'
        private(fixture.manifest, manifest)
        with pytest.raises(ValueError, match='server_url_changed'):
            child.command(state, args.agent_name)
        assert len(calls) == 4
    finally:
        http.shutdown(); http.server_close(); worker.join(); fixture.doCleanups()


def test_hermes_ca_evidence_is_required_without_running_native_code(tmp_path, monkeypatch):
    monkeypatch.setattr(child, 'config_generator', lambda *args: tmp_path / 'generator.py')
    executable = tmp_path / 'hermes'
    executable.write_text('#!/bin/sh\n')
    executable.chmod(0o700)
    source = tmp_path / 'source'
    source.mkdir()
    managed = tmp_path / 'managed'
    managed.mkdir()
    details = dict(hermes_executable=str(executable), hermes_source=str(source), managed_root=str(managed))
    receipt = dict(harness='hermes', complete=True, variant='plain', thin_config_contract=1, details=details)
    monkeypatch.setattr(child.subprocess, 'run', lambda *args, **kwargs: pytest.fail('static check ran a subprocess'))
    assert child.static_check(receipt, details, harness='hermes')['available'] is False


def test_real_shell_passes_dynamic_url_selection_to_real_prepare(tmp_path):
    import subprocess
    from test_spawn_child_harness import fixture, SPAWN
    env, work, token = fixture(tmp_path)
    helper = tmp_path / 'harness-fixture.py'
    code = helper.read_text().replace('if sys.argv[1]=="validate":',
        'if sys.argv[1]=="command":print("/usr/bin/true");raise SystemExit(0)\nif sys.argv[1]=="validate":')
    code = code.replace('{"python":sys.executable}',
        '{"python":sys.executable,"connection_kind":"strata","url_index":a[5],"url_key":a[7],"server_url":"https://selected.invalid/v1"}')
    helper.write_text(code)
    env['AGENTSTACK_CHILD_RESUME_RETENTION_DAYS'] = '0'
    fingerprint = 'a' * 64
    result = subprocess.run(['/bin/bash', str(SPAWN), '--pre-registered', 'HarnessChild',
        '--child-token-file', str(token), '--harness', 'dsh', '--backend', 'strata',
        '--url-index', '2', '--url-key', fingerprint, '--model', 'same-model',
        '--standalone', '', str(work)], env=env, cwd=ROOT, text=True, capture_output=True, timeout=25)
    assert result.returncode == 0, result.stderr
    state = child.read(Path(env['AGENTSTACK_RUNTIME_DIR']) / 'child-agents/HarnessChild.json')
    document = child.read(Path(state['harness_state_dir']) / 'input.json')
    assert (document['backend_id'], document['url_index'], document['url_key'], document['model']) == ('strata', 2, fingerprint, 'same-model')


@pytest.mark.parametrize('patch', [
    {'argv':['relative-node','entry']}, {'argv':['/node','bad\0arg']},
    {'cwd':'/different'}, {'env':{'DEEPSEEK_API_KEY':'dummy'}},
    {'env':{'CHILD_REGISTRATION_TOKEN':'dummy'}}, {'env':{'BAD-NAME':'ref'}},
])
def test_invalid_native_plan_stops_before_launch(patch):
    plan={'argv':['/fixed/node','/native/entry'], 'cwd':'/fixture','env':{}}
    plan.update(patch)
    with pytest.raises(ValueError):child.validate_plan(plan,'/fixture')


def test_plain_generation_check_does_not_admit_old_receipts(tmp_path,monkeypatch):
    monkeypatch.setattr(child,'config_generator',lambda *args:tmp_path/'generator.py')
    executable=tmp_path/'hermes';executable.write_text('#!/bin/sh\n');executable.chmod(0o755)
    source=tmp_path/'source';source.mkdir()
    root=tmp_path/'managed';root.mkdir()
    config={'hermes_executable':str(executable),'hermes_source':str(source),'managed_root':str(root)}
    receipt={'harness':'hermes','complete':True,'details':config.copy()}
    assert child.static_check(receipt,config,harness='hermes')['reason']=='plain_generation_required'
    receipt.update(variant='plain',thin_config_contract=1)
    # Plain alone is no longer sufficient: it must bind the owned CA inputs.
    assert child.static_check(receipt,config,harness='hermes')['available'] is False
    config['hermes_source']=str(tmp_path/'missing')
    assert child.static_check(receipt,config,harness='hermes')['available'] is False


def selection_fixture(tmp_path, monkeypatch):
    from test_managed_harness_install import CAFixture, fixture_stamp
    with CAFixture(None, tmp_path) as ca:
        managed = tmp_path / 'managed'
        source = managed / 'source'
        ca.source(source)
        ca.ca.apply(ca.repo, source)
        stamp = fixture_stamp(source)
        key = __import__('hashlib').sha256(str(source.resolve()).encode()).hexdigest()[:16]
        generation = managed / 'installs' / key / 'environments/fixture'
        executable = generation / 'bin/hermes'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/sh\n')
        executable.chmod(0o700)
        (generation / 'pyvenv.cfg').write_text('fixture')
        paths = ca.material(managed, source, generation)
        details = dict(hermes_executable=str(executable), hermes_source=str(source), managed_root=str(managed),
                       ca_recipe=ca.ca.load(ca.repo)[2], ca_evidence=ca.ca.runtime_evidence(ca.repo, managed, source, generation, ca.target, paths), **stamp)
        product = ca.repo
    monkeypatch.setattr(child,'ROOT',product)
    child._VALIDATION_CACHE.clear()
    generator=product/'integrations/hermes/thin_config.py'
    generator.write_text("THIN_CONFIG_CONTRACT = 1\ndef validate_input(document):\n if document.get('effort') == 'bad': raise ValueError('invalid_effort')\n")
    receipt=tmp_path/'receipt.json';private(receipt,{'harness':'hermes','variant':'plain','thin_config_contract':1,'complete':True,'details':details})
    config=tmp_path/'template.json';private(config,{**details,'generation_receipt':str(receipt)})
    manifest=tmp_path/'manifest.json';private(manifest,{'enabled':True,'backends':{'strata':{'model':'main','models':['main','alternate']}},'harnesses':{'hermes':{'python':sys.executable,'config_file':str(config)}}})
    return manifest,generator


def test_static_hermes_ca_checks_bundle_without_native_code(tmp_path, monkeypatch):
    manifest, _ = selection_fixture(tmp_path, monkeypatch)
    template = child.read(child.read(manifest)['harnesses']['hermes']['config_file'])
    receipt = child.read(template['generation_receipt'])
    monkeypatch.setattr(child.subprocess, 'run', lambda *a, **k: pytest.fail('native code executed'))
    assert child.static_check(receipt, template, harness='hermes')['available'] is True
    Path(receipt['details']['ca_evidence']['bundle']).write_text('stale bundle')
    assert child.static_check(receipt, template, harness='hermes')['available'] is False


def test_selection_checks_alternate_model_and_fixed_integration_reason(tmp_path,monkeypatch):
    manifest,generator=selection_fixture(tmp_path,monkeypatch)
    assert child.selection(str(manifest),'hermes','strata','alternate')['python']==sys.executable
    with pytest.raises(ValueError,match='model_mismatch'):
        child.selection(str(manifest),'hermes','strata','unlisted')
    with pytest.raises(ValueError,match='invalid_effort'):
        child.selection(str(manifest),'hermes','strata','main','bad')


def test_static_validation_cache_covers_models_failure_and_file_changes(tmp_path,monkeypatch):
    manifest,generator=selection_fixture(tmp_path,monkeypatch)
    clock=[100.0]; calls=[]
    monkeypatch.setattr(child,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    def run(*args,**kwargs):
        calls.append(args[0]);assert kwargs['timeout']==5
        return SimpleNamespace(returncode=0,stdout='')
    monkeypatch.setattr(child.subprocess,'run',run)
    for model in ('main','alternate'):
        child.selection(str(manifest),'hermes','strata',model)
        child.selection(str(manifest),'hermes','strata',model)
    assert len(calls)==2
    generator.write_text(generator.read_text()+'# changed\n')
    child.selection(str(manifest),'hermes','strata','main')
    assert len(calls)==3
    clock[0]+=16
    child.selection(str(manifest),'hermes','strata','main')
    assert len(calls)==4
    def timeout(*args,**kwargs):
        calls.append(args[0]);raise child.subprocess.TimeoutExpired('fixture',5)
    monkeypatch.setattr(child.subprocess,'run',timeout)
    child._VALIDATION_CACHE.clear()
    for _ in range(2):
        with pytest.raises(ValueError,match='validate_timeout'):
            child.selection(str(manifest),'hermes','strata','main')
    assert len(calls)==5
    generator.write_text(generator.read_text()+'# next\n')
    def invalid(*args,**kwargs):
        calls.append(args[0]);return SimpleNamespace(returncode=2,stdout='invalid_effort\n')
    monkeypatch.setattr(child.subprocess,'run',invalid)
    for _ in range(2):
        with pytest.raises(ValueError,match='invalid_effort'):
            child.selection(str(manifest),'hermes','strata','main')
    assert len(calls)==6


@pytest.mark.parametrize('cleanup_exit', [0, 7])
def test_failed_exit_marker_write_still_runs_cleanup_and_returns_failure(tmp_path, monkeypatch, cleanup_exit):
    import subprocess
    product = tmp_path / 'product'; (product / 'hooks').mkdir(parents=True)
    called = tmp_path / 'cleanup-called'
    cleanup = product / 'hooks/cleanup-child-agent.sh'
    cleanup.write_text('#!/bin/bash\ntouch ' + str(called) + '\nexit ' + str(cleanup_exit) + '\n')
    state = tmp_path / 'state'; state.mkdir()
    private(state / 'input.json', {'agent_name': 'FixtureChild', 'cwd': str(tmp_path)})
    # Native exits successfully after making its state directory read-only.
    # Obstructing the marker itself also makes the failure deterministic as root.
    (state / 'native-exit.status').mkdir()
    private(state / 'launch.json', {'argv': ['/bin/chmod', '500', str(state)],
                                   'env': {}, 'cwd': str(tmp_path)})
    monkeypatch.setattr(child, 'ROOT', product)
    try:
        result = subprocess.run(['/bin/bash', '-c', child.command(state, 'FixtureChild')],
                                cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert called.exists()
        assert result.returncode == (cleanup_exit or 1)
        assert 'native-exit.status' in result.stderr
    finally:
        state.chmod(0o700)
