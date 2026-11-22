"""Legacy consumer safety against the shared transport/admission implementation."""
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / 'bin/lib/runtime_tmux.py'))
ERROR = API['TargetError']


@pytest.fixture
def endpoint():
    # Short isolated socket, never the operator's default tmux server.
    with tempfile.TemporaryDirectory(prefix='r2t-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as value:
        path = Path(value) / 'fixture.sock'
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind(str(path))
            path.chmod(0o600)
            yield path


def fake_transport(path, rows, *, changed=None):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv[1:3] == ['-S', str(path)]
        assert 'TMUX' not in kwargs['env'] and 'TMUX_PANE' not in kwargs['env']
        output = rows if changed is None or len(calls) == 1 else changed
        return subprocess.CompletedProcess(argv, 0, output, '')
    return API['Transport'](path, binary='/fixture/tmux', run=run), calls


@pytest.mark.parametrize('operation', ['input', 'split', 'close', 'kill-session', 'capture', 'attach', 'signal'])
def test_legacy_action_rejects_global_standalone_without_name_prefix(endpoint, operation):
    transport, calls = fake_transport(endpoint, '$1\tAlpha\t%4\t400\n')
    proof = {'live': {'pane_id': '%4', 'pid': 400, 'process_start': 'start', 'ended': False}}
    with pytest.raises(ERROR, match='^LEGACY_TARGET_IS_GLOBAL$'):
        API['legacy_admission'](transport, endpoint.parent, session='Alpha', operation=operation,
            api={'process_start': lambda pid, **_: 'start'}, proof_reader=lambda *_, **__: [proof])
    assert len(calls) == 1  # No input/split/kill command was issued.


def test_normal_legacy_target_continues_beside_rejected_global_target(endpoint):
    proof = {'live': {'pane_id': '%4', 'pid': 400, 'process_start': 'start'}}
    transport, calls = fake_transport(endpoint, '$2\tGamma\t%5\t500\n')
    result = API['legacy_admission'](transport, endpoint.parent, session='Gamma', operation='input',
        api={'process_start': lambda pid, **_: 'start'}, proof_reader=lambda *_, **__: [proof])
    assert result['allowed'] is True and result['panes'][0]['session'] == 'Gamma'
    assert result['argv'] == ['/fixture/tmux', '-S', str(endpoint)]
    assert len(calls) == 2


def test_same_pane_number_on_other_server_is_not_owner_evidence(endpoint):
    transport, _ = fake_transport(endpoint, '$2\tGamma\t%4\t500\n')
    result = API['legacy_admission'](transport, endpoint.parent, pane='%4', operation='capture',
        api={'process_start': lambda pid, **_: 'start'},
        proof_reader=lambda *_, **__: [{'live': {'pane_id': '%4', 'pid': 400, 'process_start': 'start'}}])
    assert result['allowed'] is True


def test_session_kill_checks_global_standalone_in_another_pane(endpoint):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        output = ('$1\tAlpha\t%1\t100\n' if len(calls) == 1 else
                  '$1\tAlpha\t%1\t100\n$1\tAlpha\t%4\t400\n')
        return subprocess.CompletedProcess(argv, 0, output, '')
    transport = API['Transport'](endpoint, binary='/fixture/tmux', run=run)
    with pytest.raises(ERROR, match='^LEGACY_TARGET_IS_GLOBAL$'):
        API['legacy_admission'](transport, endpoint.parent, pane='%1', operation='kill-session',
            api={'process_start': lambda pid, **_: 'start'},
            proof_reader=lambda *_, **__: [{'live': {'pane_id': '%4', 'pid': 400, 'process_start': 'start'}}])
    assert all('kill-session' not in args for args in calls)


@pytest.mark.parametrize('changed', ['$1\tAlpha\t%4\t401\n', '$2\tAlpha\t%4\t400\n'])
def test_pane_or_session_replaced_during_proof_scan_is_refused(endpoint, changed):
    transport, _ = fake_transport(endpoint, '$1\tAlpha\t%4\t400\n', changed=changed)
    with pytest.raises(ERROR, match='^TMUX_TARGET_CHANGED$'):
        API['legacy_admission'](transport, endpoint.parent, session='Alpha', operation='input',
            api={'process_start': lambda pid, **_: 'start'}, proof_reader=lambda *_, **__: [])


def test_server_pid_must_match_inherited_custom_transport(endpoint):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, str(endpoint) + '|123\n', '')
    with pytest.raises(ERROR, match='^LEGACY_TMUX_TRANSPORT_UNAVAILABLE$'):
        API['legacy_transport'](run=run, binary='/fixture/tmux', environ={'TMUX': str(endpoint) + ',124,0'})
    result = API['legacy_transport'](run=run, binary='/fixture/tmux', environ={'TMUX': str(endpoint) + ',123,0'})
    assert result.public()['server_pid'] == 123


@pytest.mark.parametrize('mismatch', [None, 'pid', 'path', 'returncode', 'malformed'])
def test_c_locale_transport_keeps_exact_socket_and_pid_proof(endpoint, monkeypatch, mismatch):
    monkeypatch.setenv('LC_ALL', 'C')
    path = endpoint.with_name('fixture|nested.sock')
    with socket.socket(socket.AF_UNIX) as stream:
        stream.bind(str(path));path.chmod(0o600)
        def run(argv, **kwargs):
            assert 'LC_ALL' not in kwargs['env'] and kwargs['env']['LC_CTYPE'] == 'C.UTF-8'
            assert os.environ['LC_ALL'] == 'C'
            assert argv[1:3] == ['-S', str(path)]
            value = argv[-1].replace('#{socket_path}', str(path) + ('other' if mismatch == 'path' else ''))
            value = value.replace('#{pid}', '124' if mismatch == 'pid' else '123')
            # Native tmux C clients replace a TAB delimiter with an underscore.
            value = value.replace('\t', '_')
            if mismatch == 'malformed':value = 'no-server-proof'
            return subprocess.CompletedProcess(argv, 1 if mismatch == 'returncode' else 0, value + '\n', '')
        if mismatch:
            with pytest.raises(ERROR, match='^LEGACY_TMUX_TRANSPORT_UNAVAILABLE$'):
                API['legacy_transport'](run=run, binary='/fixture/tmux', environ={'TMUX': str(path) + ',123,0'})
        else:
            result = API['legacy_transport'](run=run, binary='/fixture/tmux', environ={'TMUX': str(path) + ',123,0'})
            assert result.path == path and result.public()['server_pid'] == 123


def test_tmux_child_utf8_env_keeps_tab_panes_and_parent_locale(endpoint, monkeypatch):
    monkeypatch.setenv('LC_ALL', 'C');monkeypatch.setenv('LC_CTYPE', 'C')
    original = dict(os.environ)
    def run(argv, **kwargs):
        child = kwargs['env']
        assert 'LC_ALL' not in child and child['LC_CTYPE'] == 'C.UTF-8'
        output = argv[-1].replace('#{socket_path}', str(endpoint)).replace('#{pid}', '123')
        output = output.replace('#{session_id}', '$1').replace('#{session_name}', 'name|with separators')
        output = output.replace('#{pane_id}', '%4').replace('#{pane_pid}', '400')
        if child.get('LC_ALL', child.get('LC_CTYPE')) == 'C':output = output.replace('\t', '_')
        return subprocess.CompletedProcess(argv, 0, output + '\n', '')
    transport = API['legacy_transport'](run=run, binary='/fixture/tmux', environ={'TMUX': str(endpoint) + ',123,0'})
    assert API['panes'](transport, pane='%4') == [dict(session_id='$1', session='name|with separators', pane_id='%4', pid=400)]
    assert dict(os.environ) == original


def test_global_endpoint_is_stable_across_epoch_and_deep_home(tmp_path):
    home = tmp_path / ('a' * 100) / ('b' * 100)
    base = Path('/private/tmp' if Path('/private/tmp').is_dir() else tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix='r2g-', dir=base) as value:
        path = API['global_socket'](home, 'instance', base=Path(value))
        assert path == API['global_socket'](home, 'instance', base=Path(value))
        assert len(os.fsencode(path)) < 104
        assert path != API['global_socket'](home, 'other-instance', base=Path(value))


def test_legacy_bootstrap_with_no_server_is_readonly_and_target_is_unavailable(endpoint):
    base=endpoint.parent
    def never_run(*args, **kwargs):
        raise AssertionError('No server should be started or queried')
    transport=API['legacy_transport'](run=never_run,binary='/fixture/tmux',environ={'TMUX_TMPDIR':str(base)})
    assert transport.public()['server_pid'] is None
    assert transport.argv==['/fixture/tmux','-S',str(base/('tmux-'+str(os.getuid()))/'default')]
    assert not (base/('tmux-'+str(os.getuid()))).exists()
    with pytest.raises(ERROR,match='^TMUX_TARGET_UNAVAILABLE$'):
        transport.command('list-sessions')


def test_socket_symlink_and_replacement_are_refused(endpoint):
    link = endpoint.with_name('alias.sock')
    link.symlink_to(endpoint)
    with pytest.raises(ERROR, match='^TMUX_SOCKET_UNSAFE$'):
        API['Transport'](link, binary='/fixture/tmux')
    transport, _ = fake_transport(endpoint, '')
    endpoint.unlink()
    with socket.socket(socket.AF_UNIX) as replacement:
        replacement.bind(str(endpoint)); endpoint.chmod(0o600)
        with pytest.raises(ERROR, match='^TMUX_SOCKET_CHANGED$'):
            transport.command('list-sessions')


def test_pure_legacy_home_needs_no_global_config_or_mutex(tmp_path, monkeypatch):
    for key in ('AGENTSTACK_CLIENT_CONFIG', 'AGENTSTACK_GLOBAL_SERVER_CONFIG'):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / 'absent'
    assert API['global_proofs'](home) == []
    assert not home.exists()
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    assert API['global_proofs'](home) == []
    assert not list(home.iterdir())


def test_partial_global_root_is_not_proof_of_absence(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir(mode=0o700)
    clients = home / 'clients'; clients.mkdir(mode=0o700)
    (clients / 'partial').mkdir(mode=0o700)
    with pytest.raises(RuntimeError):
        API['global_proofs'](home)
    assert not list((clients / 'partial').iterdir())  # No new mutex or recovery journal.


def test_capability_cli_does_not_select_an_owner_or_echo_secrets(monkeypatch, capsys):
    client = runpy.run_path(str(ROOT / 'bin/lib/runtime_client.py'))
    monkeypatch.setenv('CHILD_REGISTRATION_TOKEN', 'not-public')
    assert client['main'](['tmux-capabilities']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value == {'contract': 'orrery-tmux-target-v1',
                     'capabilities': ['legacy-transport', 'legacy-target-admission', 'legacy-target-diagnostics', 'legacy-window-target-admission', 'global-target-read', 'global-target-attach', 'global-target-compact']}
    assert 'not-public' not in json.dumps(value)


def test_actual_isolated_label_resolves_to_explicit_socket():
    binary = __import__('shutil').which('tmux')
    if binary is None: pytest.skip('tmux unavailable')
    with tempfile.TemporaryDirectory(prefix='r2l-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as value:
        env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE')}
        env['TMUX_TMPDIR'] = value
        command = [binary, '-L', 'fixture', '-f', '/dev/null']
        subprocess.run([*command, 'new-session', '-d', '-s', 'Alpha', 'sleep 60'], env=env, check=True, capture_output=True)
        try:
            proof = subprocess.run([*command, 'display-message', '-p', '#{socket_path},#{pid},0'], env=env, check=True, capture_output=True, text=True).stdout.strip()
            transport = API['legacy_transport'](environ={'TMUX': proof}, binary=binary)
            assert transport.command('has-session', '-t', '=Alpha').returncode == 0
            assert transport.argv[1] == '-S'
        finally:
            subprocess.run([*command, 'kill-server'], env=env, capture_output=True, check=False)


@pytest.fixture
def cli_fixture():
    binary=shutil.which('tmux')
    if binary is None: pytest.skip('tmux unavailable')
    with tempfile.TemporaryDirectory(prefix='r257c-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as value:
        base=Path(value);home=base/'home';home.mkdir(mode=0o700)
        env={k:v for k,v in os.environ.items() if not k.startswith(('AGENTSTACK_','MCP_AGENT_'))
             and k not in ('TMUX','TMUX_PANE','AGENT_NAME','PARENT_AGENT','CHILD_REGISTRATION_TOKEN','PROJECT_KEY')}
        env.update(HOME=str(home),TMUX_TMPDIR=str(base),AGENTSTACK_HOME=str(home/'.agentstack'),
                   AGENTSTACK_LABEL_PREFIX='org.agentstack.test.r257contract')
        command=[binary,'-L','contract','-f','/dev/null']
        fixture={'base':base,'home':home,'env':env,'binary':binary,'command':command,'started':False}
        try:
            yield fixture
        finally:
            if fixture['started']:
                subprocess.run([*command,'kill-server'],env=env,capture_output=True,check=False)


def start_cli_server(fixture):
    env=fixture['env'];command=fixture['command']
    created=subprocess.run([*command,'new-session','-d','-s','Control','sleep 60'],env=env,capture_output=True,text=True)
    assert created.returncode==0,created.stderr
    fixture['started']=True
    raw=subprocess.check_output([*command,'display-message','-p',
        '#{socket_path}\t#{pid}\t#{session_id}\t#{pane_id}\t#{pane_pid}'],env=env,text=True).strip().split('\t')
    fixture.update(socket=raw[0],server_pid=int(raw[1]),session_id=raw[2],pane_id=raw[3],pid=int(raw[4]))
    env['TMUX']=raw[0]+','+raw[1]+',0'
    # Exercise the actual CLI/schema/tmux boundary independently of sandbox ps
    # restrictions. Actual OS PID/start coverage stays a separate outer node.
    commands=fixture['base']/'commands';commands.mkdir(mode=0o700)
    ps=commands/'ps'
    ps.write_text('#!/bin/sh\n[ "$*" = "-p '+raw[4]+' -o lstart=" ] || exit 2\nprintf "%s\\n" "fixture-start"\n')
    ps.chmod(0o700)
    env['PATH']=str(commands)+os.pathsep+env.get('PATH','')


def run_cli(fixture,*arguments):
    return subprocess.run([sys.executable,str(fixture.get('client_entry',ROOT/'bin/lib/runtime_client.py')),*arguments],
        env=fixture['env'],text=True,capture_output=True,timeout=15)


def expected_transport(fixture,*,present):
    path=fixture['socket'] if present else str(fixture['base']/('tmux-'+str(os.getuid()))/'default')
    return {'contract':'orrery-tmux-target-v1','ok':True,'transport':'legacy-default',
            'socket_path':path,'argv':[fixture['binary'],'-S',path],
            'environment_unset':['TMUX','TMUX_PANE'],'server_pid':fixture['server_pid'] if present else None}


@pytest.mark.parametrize('present',[False,True])
def test_actual_transport_cli_returns_literal_success_after_schema_validation(cli_fixture,present):
    if present:start_cli_server(cli_fixture)
    result=run_cli(cli_fixture,'tmux-legacy-transport')
    assert result.returncode==0,result.stderr+result.stdout
    assert json.loads(result.stdout)==expected_transport(cli_fixture,present=present)


@pytest.mark.parametrize('operation',['input','signal'])
def test_actual_target_cli_returns_literal_allowed_after_schema_validation(cli_fixture,operation):
    start_cli_server(cli_fixture)
    result=run_cli(cli_fixture,'tmux-legacy-target','--session','Control','--operation',operation)
    assert result.returncode==0,result.stderr+result.stdout
    value=json.loads(result.stdout)
    assert {k:value[k] for k in expected_transport(cli_fixture,present=True)}==expected_transport(cli_fixture,present=True)
    assert value['allowed'] is True and value['operation']==operation
    assert value['panes']==[{'session_id':cli_fixture['session_id'],'session':'Control',
        'pane_id':cli_fixture['pane_id'],'pid':cli_fixture['pid'],'process_start':'fixture-start'}]
    assert __import__('re').fullmatch('[a-f0-9]{64}',value['state_sha256'])


def test_installer_payload_actual_cli_keeps_transport_and_target_success(cli_fixture):
    from _installed_client_payload import copy_client_library
    installed=cli_fixture['base']/'installed'
    copy_client_library(installed)
    cli_fixture['client_entry']=installed/'bin/lib/runtime_client.py'
    start_cli_server(cli_fixture)
    result=run_cli(cli_fixture,'tmux-legacy-transport')
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)==expected_transport(cli_fixture,present=True)
    result=run_cli(cli_fixture,'tmux-legacy-target','--session','Control','--operation','input')
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['allowed'] is True


@pytest.mark.parametrize('error',[KeyError('private'),TypeError('private'),ValueError('private'),RuntimeError('private'),AttributeError('private')])
def test_helper_internal_fault_is_distinct_from_domain_refusal(monkeypatch,capsys,error):
    def fail(**kwargs):raise error
    monkeypatch.setitem(API['main'].__globals__,'legacy_transport',fail)
    assert API['main'](['legacy-transport'])==70
    assert json.loads(capsys.readouterr().out)=={'contract':'orrery-tmux-target-v1','ok':False,
        'allowed':False,'reason':'TMUX_HELPER_FAILED','failure_kind':'helper'}


@pytest.mark.parametrize('reason,kind',[('LEGACY_TARGET_IS_GLOBAL','target'),('TMUX_SOCKET_UNSAFE','transport')])
def test_domain_refusal_retains_fixed_reason_and_classification(monkeypatch,capsys,reason,kind):
    def refuse(**kwargs):raise ERROR(reason)
    monkeypatch.setitem(API['main'].__globals__,'legacy_transport',refuse)
    assert API['main'](['legacy-transport'])==2
    assert json.loads(capsys.readouterr().out)=={'contract':'orrery-tmux-target-v1','ok':False,
        'allowed':False,'reason':reason,'failure_kind':kind}


def test_output_schema_fault_is_a_helper_failure_not_a_target_refusal(monkeypatch,capsys):
    class BrokenTransport:
        def public(self):
            return {'transport':'legacy-default','socket_path':'/fixture/socket',
                    'argv':['tmux','-S','/fixture/socket'],'server_pid':100,
                    'environment_unset':['TMUX','TMUX_PANE']}
    monkeypatch.setitem(API['main'].__globals__,'legacy_transport',lambda:BrokenTransport())
    monkeypatch.setitem(API['SPEC']['result_properties'],'socket_path',{'type':'string','pattern':'^/'})
    assert API['main'](['legacy-transport'])==70
    assert json.loads(capsys.readouterr().out)=={'contract':'orrery-tmux-target-v1','ok':False,
        'allowed':False,'reason':'TMUX_HELPER_FAILED','failure_kind':'helper'}


def test_actual_cli_target_uses_os_process_start(cli_fixture):
    start_cli_server(cli_fixture)
    cli_fixture['env']['PATH']=cli_fixture['env']['PATH'].split(os.pathsep,1)[1]
    process=subprocess.run(['ps','-p',str(cli_fixture['pid']),'-o','lstart='],
        env=cli_fixture['env'],text=True,capture_output=True)
    assert process.returncode==0,('OS ps proof unavailable: '+process.stderr)
    result=run_cli(cli_fixture,'tmux-legacy-target','--session','Control','--operation','input')
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['panes'][0]['process_start']==process.stdout.strip()


def test_unknown_operation_is_an_unavailable_helper_answer(cli_fixture):
    result=run_cli(cli_fixture,'tmux-legacy-target','--session','Control','--operation','not-an-operation')
    assert result.returncode==2 and result.stdout=='' and 'invalid choice' in result.stderr


def test_initial_null_pin_accepts_one_verified_server_then_refuses_change():
    initial={'transport':'legacy-default','socket_path':'/fixture/socket',
             'argv':['tmux','-S','/fixture/socket'],'server_pid':None}
    first={**initial,'server_pid':100}
    pin=API['check_pinned_transport'](initial,first)
    assert pin==first and initial['server_pid'] is None
    assert API['check_pinned_transport'](pin,first)==first
    with pytest.raises(ERROR,match='^TMUX_SOCKET_CHANGED$'):
        API['check_pinned_transport'](pin,{**first,'server_pid':101})
    with pytest.raises(ERROR,match='^TMUX_SOCKET_CHANGED$'):
        API['check_pinned_transport'](pin,{**first,'socket_path':'/fixture/other'})


def test_cli_partial_root_diagnostics_identifies_repair_without_returning_credentials(cli_fixture):
    home=Path(cli_fixture['env']['AGENTSTACK_HOME']);home.mkdir(mode=0o700)
    clients=home/'clients';clients.mkdir(mode=0o700)
    (clients/'partial').mkdir(mode=0o700)
    result=run_cli(cli_fixture,'tmux-diagnostics')
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)=={'contract':'orrery-tmux-target-v1','ok':True,'global_complete':False,
        'diagnostics':[{'client_name':'partial','mail_instance_id':None,'check':'context','reason':'GLOBAL_ROOT_UNAVAILABLE'}]}
    start_cli_server(cli_fixture)
    result=run_cli(cli_fixture,'tmux-legacy-target','--session','Control','--operation','input')
    assert result.returncode==2 and json.loads(result.stdout)=={'contract':'orrery-tmux-target-v1',
        'ok':False,'allowed':False,'reason':'GLOBAL_TARGET_OWNERSHIP_UNKNOWN','failure_kind':'target'}


@pytest.mark.parametrize('failure',[subprocess.TimeoutExpired('settings',5),KeyError('fixture'),TypeError('fixture'),RuntimeError('OBSERVATION_INCOMPLETE')])
def test_inventory_read_fault_is_helper_unavailable_not_root_diagnostic(monkeypatch,capsys,tmp_path,failure):
    def broken(*args,**kwargs):raise failure
    monkeypatch.setenv('AGENTSTACK_HOME',str(tmp_path))
    monkeypatch.setitem(API['global_inventory'].__globals__,'installed_hints',broken)
    assert API['main'](['diagnostics'])==70
    assert json.loads(capsys.readouterr().out)=={
        'contract':'orrery-tmux-target-v1','ok':False,'allowed':False,
        'reason':'TMUX_HELPER_FAILED','failure_kind':'helper'}


def test_busy_observation_is_transient_helper_unavailable(monkeypatch,capsys,tmp_path):
    def busy(*args,**kwargs):raise RuntimeError('OBSERVATION_BUSY')
    monkeypatch.setenv('AGENTSTACK_HOME',str(tmp_path))
    monkeypatch.setitem(API['global_inventory'].__globals__,'installed_hints',busy)
    assert API['main'](['diagnostics'])==70
    assert json.loads(capsys.readouterr().out)=={
        'contract':'orrery-tmux-target-v1','ok':False,'allowed':False,
        'reason':'TMUX_HELPER_FAILED','failure_kind':'helper'}



def test_repeated_inventory_loads_code_once_but_observes_new_roots(tmp_path,monkeypatch):
    api=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux.py'))
    original=runpy.run_path;loads=[]
    def load(path,*args,**kwargs):
        loads.append(Path(path).name)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(runpy,'run_path',load)
    home=tmp_path/'home';home.mkdir(mode=0o700)
    assert api['global_inventory'](home)['global_complete'] is True
    parent=home/'clients';parent.mkdir(mode=0o700)
    (parent/'partial').mkdir(mode=0o700)
    assert api['global_inventory'](home)['global_complete'] is False
    assert loads.count('runtime_client.py')==1
    assert loads.count('runtime_operator.py')==0
    assert loads.count('runtime_observation.py')==0
    for name in ('runtime_operator','runtime_observation'):
        api['module_api'](name);api['module_api'](name)
        assert loads.count(name+'.py')==1


def test_installed_hint_cache_rechecks_file_metadata_and_content(tmp_path,monkeypatch):
    from types import SimpleNamespace
    api=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux.py'))
    home=tmp_path/'home';home.mkdir(mode=0o700)
    path=home/'env.sh';path.write_text('legacy');calls=[]
    def read(*args,**kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=0,stdout='export AGENTSTACK_RUNTIME_MODE='+path.read_text()+'\n')
    monkeypatch.setattr(subprocess,'run',read)
    import time
    assert api['installed_hints'](home,time.monotonic()+5)['AGENTSTACK_RUNTIME_MODE']=='legacy'
    assert api['installed_hints'](home,time.monotonic()+5)['AGENTSTACK_RUNTIME_MODE']=='legacy'
    assert len(calls)==1
    path.write_text('global')
    assert api['installed_hints'](home,time.monotonic()+5)['AGENTSTACK_RUNTIME_MODE']=='global'
    assert len(calls)==2
    path.chmod(0o666)
    with pytest.raises(api['TargetError'],match='GLOBAL_TARGET_OWNERSHIP_UNKNOWN'):
        api['installed_hints'](home,time.monotonic()+5)



def test_window_selector_checks_every_current_pane_without_session_fallback(endpoint):
    transport,calls=fake_transport(endpoint,'$7\tMoved\t%1\t100\n$7\tMoved\t%4\t400\n')
    with pytest.raises(ERROR,match='LEGACY_TARGET_IS_GLOBAL'):
        API['legacy_admission'](transport,endpoint.parent,window='@8',operation='input',
            api={'process_start':lambda *a,**kw:'start'},
            proof_reader=lambda *a,**kw:[{'live':{'pane_id':'%4','pid':400,'process_start':'start'}}])
    assert calls[0][0][3:6]==['list-panes','-t','@8']


def test_window_selector_cli_success_has_literal_current_pane(cli_fixture):
    start_cli_server(cli_fixture)
    window=subprocess.check_output([*cli_fixture['command'],'display-message','-p','-t',cli_fixture['pane_id'],'#{window_id}'],
        env=cli_fixture['env'],text=True).strip()
    assert window=='@0'
    result=run_cli(cli_fixture,'tmux-legacy-target','--window',window,'--operation','input')
    assert result.returncode==0,result.stdout+result.stderr
    value=json.loads(result.stdout)
    assert value['allowed'] is True and value['operation']=='input'
    assert value['panes']==[{'session_id':'$0','session':'Control','pane_id':'%0',
        'pid':cli_fixture['pid'],'process_start':'fixture-start'}]


@pytest.mark.parametrize('provider_state',['start-provider','absent'])
def test_legacy_admission_rejects_the_global_runner_even_after_provider_exit(endpoint,provider_state):
    transport,calls=fake_transport(endpoint,'$1\tAlpha\t%4\t310\n')
    proof={'live':{'pane_id':'%4','pid':400,'process_start':'start-provider','launch_id':'launch','receipt_nonce':'nonce'},
           'launch':{'launch_id':'launch','receipt_nonce':'nonce','execution':{'runner':{'pid':310,'process_start':'start-runner'}}}}
    with pytest.raises(ERROR,match='^LEGACY_TARGET_IS_GLOBAL$'):
        API['legacy_admission'](transport,endpoint.parent,session='Alpha',operation='input',
            api={'process_start':lambda pid,**kw:'start-runner' if pid==310 else provider_state},
            proof_reader=lambda *a,**kw:[proof])
    assert len(calls)==1
