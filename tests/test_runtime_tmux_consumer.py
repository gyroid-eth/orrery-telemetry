"""Effects after successful initial admission must recheck the actual target."""
import os
from pathlib import Path
import runpy
import socket
import subprocess
import tempfile
import pytest
ROOT=Path(__file__).resolve().parents[1]
API=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux.py'))

@pytest.fixture
def consumer(tmp_path):
    base=tempfile.TemporaryDirectory(prefix='r2tc-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None)
    path=Path(base.name).resolve()/'fixture.sock'
    with base, socket.socket(socket.AF_UNIX) as stream:
        stream.bind(str(path));path.chmod(0o600)
        calls=[];state={'global':False,'actual':'Alpha','pid':100}
        def run(argv,**kw):
            calls.append(argv)
            command=argv[3]
            if command == 'list-sessions':
                return subprocess.CompletedProcess(argv,0,'$1\t'+state['actual']+'\n','')
            if command in ('list-panes','display-message'):
                return subprocess.CompletedProcess(argv,0,'$1\t'+state['actual']+'\t%1\t'+str(state['pid'])+'\n','')
            return subprocess.CompletedProcess(argv,0,'','')
        transport=API['Transport'](path,binary='fixture-tmux',run=run);transport.server_pid=123
        c=API['LegacyConsumer'](tmp_path,transport=transport,api={'process_start':lambda *a,**kw:'start'},
             proof_reader=lambda *a,**kw:[{'live':{'pane_id':'%1','pid':100,'process_start':'start'}}] if state['global'] else [])
        yield c,calls,state


@pytest.mark.parametrize('effect',[['paste-buffer','-t','Alpha','-b','fixture'],['send-keys','-t','Alpha','C-m'],['send-keys','-t','Alpha','C-m'],['kill-session','-t','Alpha'],['attach-session','-t','Alpha']])
def test_global_replacement_after_initial_capture_blocks_each_effect(consumer,effect):
    c,calls,state=consumer
    c.command(['capture-pane','-p','-t','Alpha'])
    state['global']=True
    with pytest.raises(API['TargetError'],match='^LEGACY_TARGET_IS_GLOBAL$'):c.command(effect)
    assert not any(argv[3]==effect[0] for argv in calls)


@pytest.mark.parametrize('actual',['AlphaX','alpha'])
def test_prefix_or_case_alias_never_reaches_effect(consumer,actual):
    c,calls,state=consumer;state['actual']=actual
    with pytest.raises(API['TargetError'],match='^TMUX_TARGET_UNAVAILABLE$'):
        c.command(['send-keys','-t','Alpha','C-m'])
    assert not any(argv[3]=='send-keys' for argv in calls)
    assert calls[0][3:7]==['list-panes','-s','-t','=Alpha']


def test_normal_legacy_effect_uses_pane_id_and_explicit_socket(consumer):
    c,calls,_=consumer;c.command(['send-keys','-t','Alpha','C-m'])
    assert calls[-1][3:]==['send-keys','-t','%1','C-m']
    assert all(argv[1:3]==['-S',str(c.transport.path)] for argv in calls)


def test_initial_only_admission_mutant_is_detected_by_effect_counter(consumer,monkeypatch):
    c,calls,state=consumer;c.command(['capture-pane','-p','-t','Alpha'])
    state['global']=True
    # Deliberate bypass of the production command's admission, matching a
    # faulty implementation which validates once and reuses that permission.
    monkeypatch.setattr(c,'admitted_argv',lambda args:[*c.transport.argv,*args])
    c.command(['send-keys','-t','%1','C-m'])
    def no_foreign_effect():assert not any(argv[3]=='send-keys' for argv in calls)
    with pytest.raises(AssertionError):no_foreign_effect()


def test_fresh_legacy_creation_uses_its_explicit_endpoint_only():
    import shutil
    binary=shutil.which('tmux')
    if binary is None:pytest.skip('tmux unavailable')
    with tempfile.TemporaryDirectory(prefix='r2fresh-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as value:
        home=Path(value).resolve();path=home/('tmux-'+str(os.getuid()))/'default'
        transport=API['Transport'](path,binary=binary,allow_absent=True)
        c=API['LegacyConsumer'](home,transport=transport,proof_reader=lambda *a,**k:[],api={})
        try:
            result=c.command(['new-session','-d','-s','Fresh','sleep 60'])
            assert result.returncode==0,result.stderr
            assert transport.identity==API['validate_socket'](path)
            assert transport.command('has-session','-t','=Fresh').returncode==0
        finally:subprocess.run([binary,'-S',str(path),'kill-server'],capture_output=True,check=False)


def test_naturally_stale_owned_socket_can_start_a_legacy_session():
    import hashlib
    import json
    import shutil
    import time
    binary = shutil.which('tmux')
    if binary is None:
        pytest.skip('tmux unavailable')
    evidence, path = {'calls': []}, None

    def run(argv, **kwargs):
        reply = subprocess.run(argv, **kwargs)
        evidence['calls'].append(dict(argv=argv, returncode=reply.returncode,
            stdout=reply.stdout, stderr=reply.stderr))
        return reply

    def tmux(*args):
        return run([binary, '-S', str(path), *args], capture_output=True, text=True, timeout=5)

    try:
        with tempfile.TemporaryDirectory(prefix='r2stale-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as value:
            home = Path(value).resolve()
            parent = home / ('tmux-' + str(os.getuid()))
            parent.mkdir(mode=0o700)
            path = parent / 'default'
            evidence['socket'] = str(path)
            try:
                seed = tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'Seed', '/bin/sleep 60')
                assert seed.returncode == 0, seed.stderr
                pid = tmux('display-message', '-p', '#{pid}')
                assert pid.returncode == 0 and pid.stdout.strip().isdigit()
                evidence['seed_server_pid'] = int(pid.stdout.strip())
                stopped = tmux('kill-server')
                assert stopped.returncode == 0, stopped.stderr
                if not path.exists():
                    pytest.skip('normal server exit removed its socket; no natural stale fixture')
                for _ in range(100):
                    observed = tmux('list-sessions', '-F', '#{session_name}')
                    if API['module_api']('legacy_consumer')['server_absent'](observed):
                        break
                    time.sleep(.01)
                if not path.exists():
                    pytest.skip('the socket disappeared; no natural stale fixture')
                assert observed.returncode == 1 and observed.stdout == ''
                assert API['module_api']('legacy_consumer')['server_absent'](observed)
                info = path.lstat()
                evidence['stale_lstat'] = dict(uid=info.st_uid, mode=oct(info.st_mode & 0o777),
                    device=info.st_dev, inode=info.st_ino)
                transport = API['legacy_transport'](binary=binary, run=run,
                    environ={'TMUX_TMPDIR': str(home)})
                identity = transport.identity
                assert identity == API['validate_socket'](path)
                assert getattr(transport, 'server_pid', None) is None
                c = API['LegacyConsumer'](home, transport=transport, api={}, proof_reader=lambda *a, **kw: [])
                imported = Path(c.admitted_argv.__code__.co_filename).resolve()
                assert imported == ROOT / 'bin/lib/legacy_consumer.py'
                evidence['consumer_source'] = str(imported)
                evidence['consumer_sha256'] = hashlib.sha256(imported.read_bytes()).hexdigest()
                refusal = None
                try:
                    reply = c.command(['new-session', '-d', '-s', 'Fresh', '/bin/sleep 60'])
                except API['TargetError'] as error:
                    refusal = str(error)
                    evidence['consumer_refusal'] = refusal
                    assert refusal == 'TMUX_TARGET_UNAVAILABLE'
                    control = tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'Control', '/bin/sleep 60')
                    evidence['normal_recreation_returncode'] = control.returncode
                    assert control.returncode == 0, control.stderr
                    assert tmux('has-session', '-t', '=Control').returncode == 0
                else:
                    evidence['consumer_returncode'] = reply.returncode
                    assert reply.returncode == 0, reply.stderr
                    assert transport.identity == API['validate_socket'](path)
                    assert transport.command('has-session', '-t', '=Fresh').returncode == 0
                assert refusal is None, 'actual consumer refused a naturally stale owned socket: ' + str(refusal)
            finally:
                cleanup = tmux('kill-server')
                evidence['cleanup_returncode'] = cleanup.returncode
                evidence['pid_exit_verified'] = False
    finally:
        evidence['endpoint_removed_after_context'] = path is not None and not path.exists()
        print('STALE_TMUX_EVIDENCE=' + json.dumps(evidence))


@pytest.mark.parametrize('returncode,stdout,stderr', [
    (1, '', 'permission denied'),
    (1, '', 'unexpected tmux diagnostic'),
    (2, '', 'no sessions'),
    (1, 'Alpha\n', 'no sessions'),
    (1, '', 'no sessions\npermission denied'),
    (1, '', ''),
])
def test_new_session_unknown_inventory_never_reaches_creation(consumer, returncode, stdout, stderr):
    c, calls, _ = consumer
    original, identity = c.transport.run, c.transport.identity
    def run(argv, **kwargs):
        if argv[3] == 'list-sessions':
            calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        return original(argv, **kwargs)
    c.transport.run = run
    with pytest.raises(API['TargetError'], match='^TMUX_TARGET_UNAVAILABLE$'):
        c.command(['new-session', '-A', '-s', 'Alpha'])
    assert not any(argv[3] == 'new-session' for argv in calls)
    assert c.transport.identity == identity


@pytest.mark.parametrize('replacement', ['socket', 'file', 'mode'])
def test_empty_inventory_cannot_adopt_a_changed_or_unsafe_socket(consumer, replacement):
    c, calls, _ = consumer
    identity, path = c.transport.identity, c.transport.path
    with socket.socket(socket.AF_UNIX) as other:
        def run(argv, **kwargs):
            calls.append(argv)
            assert argv[3] == 'list-sessions', 'no creation may follow a changed socket'
            if replacement == 'socket':
                other_path = path.with_name('replacement.sock')
                other.bind(str(other_path))
                other_path.chmod(0o600)
                os.replace(other_path, path)
            elif replacement == 'file':
                path.unlink()
                path.write_text('not a socket')
            else:
                path.chmod(0o606)
            return subprocess.CompletedProcess(argv, 1, '', 'no sessions')
        c.transport.run = run
        reason = 'TMUX_SOCKET_CHANGED' if replacement == 'socket' else 'TMUX_SOCKET_UNSAFE'
        with pytest.raises(API['TargetError'], match='^' + reason + '$'):
            c.command(['new-session', '-A', '-s', 'Alpha'])
        assert c.transport.identity == identity
        assert not any(argv[3] == 'new-session' for argv in calls)


@pytest.mark.parametrize('success', [False, True])
def test_creation_failure_or_unsafe_success_does_not_repin(consumer, success):
    c, calls, _ = consumer
    identity, path = c.transport.identity, c.transport.path
    with socket.socket(socket.AF_UNIX) as other:
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[3] == 'list-sessions':
                return subprocess.CompletedProcess(argv, 1, '', 'no sessions')
            assert argv[3] == 'new-session'
            if success:
                path.unlink()
                path.write_text('unsafe replacement after ACK')
            else:
                other_path = path.with_name('replacement.sock')
                other.bind(str(other_path))
                other_path.chmod(0o600)
                os.replace(other_path, path)
            return subprocess.CompletedProcess(argv, 0 if success else 1, '', '')
        c.transport.run = run
        if success:
            with pytest.raises(API['TargetError'], match='^TMUX_SOCKET_UNSAFE$'):
                c.command(['new-session', '-d', '-s', 'Fresh'])
        else:
            assert c.command(['new-session', '-d', '-s', 'Fresh']).returncode == 1
            assert API['validate_socket'](path) != identity
        assert c.transport.identity == identity
        assert c.transport.server_pid == 123
        assert sum(argv[3] == 'new-session' for argv in calls) == 1


def test_legacy_dashboard_session_id_is_resolved_as_id_not_literal_name(consumer):
    c,calls,state=consumer
    c.command(['capture-pane','-t','$1','-p'])
    assert calls[0][3:7]==['list-panes','-s','-t','$1']
    assert calls[-1][3:]==['capture-pane','-t','%1','-p']
    state['global']=True
    with pytest.raises(API['TargetError'],match='^LEGACY_TARGET_IS_GLOBAL$'):
        c.command(['capture-pane','-t','$1','-p'])


def test_new_adapter_reuses_older_helper_primitives_without_new_class(consumer):
    c,calls,state=consumer
    api={k:v for k,v in API.items() if k not in ('LegacyConsumer','TARGET_SPEC','TARGET_REASONS','runtime_api_view','lstat_if_present')}
    factory=runpy.run_path(str(ROOT/'bin/lib/legacy_consumer.py'))['LegacyConsumer']
    adapter=factory(c.home,transport=c.transport,api=c.api,proof_reader=c.proof_reader,transport_api=api)
    adapter.command(['send-keys','-t','Alpha','C-m'])
    assert calls[-1][3:]==['send-keys','-t','%1','C-m']
    state['global']=True
    with pytest.raises(API['TargetError'],match='^LEGACY_TARGET_IS_GLOBAL$'):
        adapter.command(['send-keys','-t','Alpha','C-m'])


def test_pure_legacy_inventory_does_not_initialize_global_operator_contracts(tmp_path,monkeypatch):
    function=API['global_inventory'];namespace=function.__globals__;original=namespace['module_api'];loaded=[]
    def modules(name):
        loaded.append(name)
        assert name not in ('runtime_operator','runtime_observation')
        return original(name)
    monkeypatch.setitem(namespace,'module_api',modules)
    monkeypatch.setitem(namespace,'installed_hints',lambda *a:{})
    for key in ('AGENTSTACK_RUNTIME_MODE','AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENTSTACK_CLIENT_CONFIG'):
        monkeypatch.delenv(key,raising=False)
    value=function(tmp_path,api=original('runtime_client'))
    assert value=={'proofs':[],'diagnostics':[],'global_complete':True}
    assert loaded==[]


def test_legacy_session_rename_pins_the_session_id_not_its_first_pane(consumer):
    c,calls,state=consumer
    c.command(['rename-session','-t','$1','NewName'])
    assert calls[-1][3:]==['rename-session','-t','$1','NewName']
    state['global']=True
    with pytest.raises(API['TargetError'],match='^LEGACY_TARGET_IS_GLOBAL$'):
        c.command(['rename-session','-t','$1','NewName'])
    assert sum(argv[3]=='rename-session' for argv in calls)==1


def test_legacy_attach_or_create_cannot_adopt_an_existing_global_session(consumer):
    c,calls,state=consumer;original=c.transport.run
    def run(argv,**kw):
        if argv[3]=='list-sessions':
            calls.append(argv);return subprocess.CompletedProcess(argv,0,'Alpha\n','')
        return original(argv,**kw)
    c.transport.run=run
    c.command(['new-session','-A','-s','Alpha'])
    assert sum(argv[3]=='new-session' for argv in calls)==1
    state['global']=True
    with pytest.raises(API['TargetError'],match='^LEGACY_TARGET_IS_GLOBAL$'):
        c.command(['new-session','-A','-s','Alpha'])
    assert sum(argv[3]=='new-session' for argv in calls)==1


@pytest.mark.parametrize('adapter',['ghostty','wt'])
@pytest.mark.parametrize('operation',['attach-session','new-session'])
@pytest.mark.parametrize('foreign',[False,True])
def test_actual_terminal_caller_and_child_recheck_effect_and_endpoint(consumer,monkeypatch,operation,foreign,adapter):
    import ast,sys
    from types import SimpleNamespace
    c,calls,state=consumer;effects=[];launch=[];original=c.transport.run
    def run(argv,**kw):
        if argv[3]=='list-sessions':return subprocess.CompletedProcess(argv,0,'Alpha\n','')
        return original(argv,**kw)
    c.transport.run=run
    cli=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux_consumer.py'))
    def modules(path):return API if str(path).endswith('runtime_tmux.py') else {'LegacyConsumer':lambda *a,**kw:c}
    monkeypatch.setitem(cli['main'].__globals__,'runpy',SimpleNamespace(run_path=modules))
    monkeypatch.setattr(os,'execvpe',lambda binary,argv,env:effects.append(argv))
    def terminal(argv,**kw):
        if adapter=='wt':argv=launch[-1]
        index=argv.index(str(ROOT/'bin/lib/runtime_tmux_consumer.py'))
        assert 'TMUX='+str(c.transport.path)+',123,0' in argv
        state['global']=foreign
        rc=cli['main'](argv[index+1:])
        return subprocess.CompletedProcess(argv,rc,'','refused' if rc else '')
    source=ast.parse((ROOT/'dashboard/server.py').read_text())
    nodes=[node for node in source.body if isinstance(node,ast.FunctionDef) and node.name in ('_open_terminal_tmux','_legacy_terminal_argv')]
    ns=dict(Path=Path,sys=sys,HOOKS_DIR=str(ROOT/'hooks'),_terminal_adapter=lambda:adapter,_wsl_distro=lambda:'FixtureWSL',
        _write_wt_launcher=lambda argv,title:launch.append(argv) or '/fixture/launch.sh',
        _legacy_tmux_consumer=lambda:c,subprocess=SimpleNamespace(run=terminal))
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(ROOT/'dashboard/server.py'),'exec'),ns)
    args=['tmux','attach-session','-t','Alpha'] if operation=='attach-session' else ['tmux','new-session','-A','-s','Alpha']
    result=ns['_open_terminal_tmux'](args,'Alpha')
    assert result['ok'] is not foreign
    if foreign:assert effects==[]
    else:
        assert len(effects)==1
        assert effects[0][:3]==['fixture-tmux','-S',str(c.transport.path)]


def test_nonascii_legacy_admission_digest_matches_literal_base_receipt(consumer,monkeypatch):
    c,calls,state=consumer;state['actual']='日本語'
    # Receipt computed by the base implementation; fixed public diagnostics
    # make it independent of checkout history, UID and temporary socket names.
    monkeypatch.setattr(c.transport,'public',lambda:dict(transport='legacy-default',
        socket_path='/fixture/socket',argv=['fixture-tmux','-S','/fixture/socket'],
        environment_unset=['TMUX','TMUX_PANE'],server_pid=123))
    current=c.admission('%1','capture')
    assert current['state_sha256']=='7b44f2f66606332f0464ab6b0fd5fe575534ba9141b7e847a0a0c93ba4ceb2ce'
    assert current['panes'][0]['session']=='日本語'



def test_actual_private_cli_retains_known_refusal_reason(consumer,monkeypatch,capsys):
    from types import SimpleNamespace
    c,calls,state=consumer;state['global']=True
    cli=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux_consumer.py'))
    monkeypatch.setitem(cli['main'].__globals__,'runpy',SimpleNamespace(run_path=lambda path:API if str(path).endswith('runtime_tmux.py') else {'LegacyConsumer':lambda *a,**kw:c}))
    assert cli['main'](['attach-session','-t','Alpha'])==2
    streams=capsys.readouterr()
    assert streams.out=='' and streams.err.strip()=='LEGACY_TARGET_IS_GLOBAL'



def test_actual_stop_group_cli_rechecks_after_pgid_before_signal(consumer,monkeypatch,capsys):
    from types import SimpleNamespace
    c,calls,state=consumer;effects=[]
    cli=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux_consumer.py'))
    monkeypatch.setitem(cli['main'].__globals__,'runpy',SimpleNamespace(run_path=lambda path:API if str(path).endswith('runtime_tmux.py') else {'LegacyConsumer':lambda *a,**kw:c}))
    def process(*args,**kw):
        state['global']=True
        return subprocess.CompletedProcess(args,0,'555\n','')
    monkeypatch.setattr(subprocess,'run',process)
    monkeypatch.setattr(os,'killpg',lambda *args:effects.append(args))
    assert cli['main'](['stop-group','Alpha'])==2
    assert effects==[]
    assert capsys.readouterr().err.strip()=='LEGACY_TARGET_IS_GLOBAL'


@pytest.mark.parametrize('command', ['list-panes', 'list-sessions'])
def test_absent_inventory_rechecks_endpoint_without_creating_it(tmp_path, command):
    path = tmp_path / 'default'
    transport = API['Transport'](path, binary='fixture-tmux', allow_absent=True,
                               run=lambda *a, **kw: pytest.fail('absence must not execute tmux'))
    c = API['LegacyConsumer'](tmp_path, transport=transport, api={}, proof_reader=lambda *a, **kw: [])
    result = c.command([command, '-F', '#{pane_id}'])
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')
    assert not path.exists()
    with pytest.raises(API['TargetError'], match='^TMUX_TARGET_UNAVAILABLE$'):
        c.command(['list-clients'])
    path.write_text('foreign file appeared')
    with pytest.raises(API['TargetError'], match='^TMUX_SOCKET_UNSAFE$'):
        c.command([command, '-F', '#{pane_id}'])
    assert path.read_text() == 'foreign file appeared'


@pytest.mark.parametrize('transition', ['appeared', 'disappeared'])
def test_inventory_rechecks_socket_presence_after_bootstrap(tmp_path, transition):
    with tempfile.TemporaryDirectory(prefix='inventory-race-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory:
        path = Path(directory).resolve() / 's'
        with socket.socket(socket.AF_UNIX) as stream:
            if transition == 'disappeared':
                stream.bind(str(path))
                path.chmod(0o600)
            transport = API['Transport'](path, binary='fixture-tmux', allow_absent=True,
                                       run=lambda *a, **kw: pytest.fail('presence observation must not execute tmux'))
            c = API['LegacyConsumer'](tmp_path, transport=transport, api={}, proof_reader=lambda *a, **kw: [])
            if transition == 'appeared':
                stream.bind(str(path))
                path.chmod(0o600)
                with pytest.raises(API['TargetError'], match='^TMUX_SOCKET_CHANGED$'):
                    c.command(['list-panes', '-a'])
            else:
                path.unlink()
                reply = c.command(['list-panes', '-a'])
                assert (reply.returncode, reply.stdout) == (0, '')
                assert not path.exists()


@pytest.mark.parametrize('failure', [PermissionError(13, 'fixture denied'), OSError(5, 'fixture IO error')])
def test_inventory_stat_errors_are_unknown_after_bootstrap(tmp_path, monkeypatch, failure):
    with tempfile.TemporaryDirectory(prefix='inventory-error-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory:
        path = Path(directory).resolve() / 's'
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind(str(path))
            path.chmod(0o600)
            transport = API['Transport'](path, binary='fixture-tmux')
            c = API['LegacyConsumer'](tmp_path, transport=transport, api={}, proof_reader=lambda *a, **kw: [])
            stat_call, lstat_call = os.stat, os.lstat
            def deny(original):
                def observe(value, *a, **kw):
                    if os.fspath(value) == str(path):
                        raise failure
                    return original(value, *a, **kw)
                return observe
            with monkeypatch.context() as patch:
                patch.setattr(os, 'stat', deny(stat_call))
                patch.setattr(os, 'lstat', deny(lstat_call))
                with pytest.raises((API['TargetError'], OSError)):
                    c.command(['list-panes', '-a'])
            assert path.lstat().st_mode & 0o777 == 0o600


def test_fresh_admission_keeps_older_transport_api_compatible(tmp_path):
    api = {key: value for key, value in API.items() if key not in ('LegacyConsumer', 'lstat_if_present')}
    path = tmp_path / 'default'
    transport = API['Transport'](path, binary='fixture-tmux', allow_absent=True)
    factory = runpy.run_path(str(ROOT / 'bin/lib/legacy_consumer.py'))['LegacyConsumer']
    adapter = factory(tmp_path, transport=transport, api={}, proof_reader=lambda *a, **kw: [], transport_api=api)
    assert adapter.admitted_argv(['new-session', '-A', '-s', 'Fresh']) == [
        'fixture-tmux', '-S', str(path), 'new-session', '-A', '-s', 'Fresh']
    assert not path.exists()


def test_missing_endpoint_parent_is_absent_without_creating_directories(tmp_path):
    path = tmp_path / 'missing-parent' / 'default'
    transport = API['Transport'](path, binary='fixture-tmux', allow_absent=True)
    assert transport.identity is None
    assert not path.parent.exists()


@pytest.mark.parametrize('failure',[PermissionError(1,'Operation not permitted'),subprocess.TimeoutExpired('ps',3)])
def test_sandbox_process_probe_refusal_remains_unknown_and_has_no_target_effect(consumer,monkeypatch,failure):
    c,calls,_=consumer
    observations=runpy.run_path(str(ROOT/'bin/lib/process_observation.py'))
    def denied(*a,**kw):raise failure
    c.api['process_start']=observations['process_start']
    with monkeypatch.context() as m:
        m.setattr(observations['subprocess'],'run',denied)
        with pytest.raises(API['TargetError'],match='^TMUX_TARGET_UNAVAILABLE$'):
            c.command(['has-session','-t','=Alpha'])
    assert not any(argv[3]=='has-session' for argv in calls)
    assert not any(argv[3] in ('send-keys','kill-session','kill-pane') for argv in calls)


def test_real_adapter_proves_a_missing_session_without_process_access(tmp_path):
    import shutil, sys
    binary=shutil.which('tmux')
    if binary is None:pytest.skip('tmux unavailable')
    with tempfile.TemporaryDirectory(prefix='r2absence-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as value:
        home=Path(value).resolve();path=home/('tmux-'+str(os.getuid()))/'default'
        transport=API['Transport'](path,binary=binary,allow_absent=True)
        c=API['LegacyConsumer'](home,transport=transport,api={},proof_reader=lambda *a,**kw:[])
        try:
            assert c.command(['new-session','-d','-s','Hold','/bin/sleep 60']).returncode==0
            assert transport.command('new-session','-d','-s','Victim','/bin/sleep 60').returncode==0
            assert transport.command('kill-session','-t','=Victim').returncode==0
            env={k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE')}
            env.update(TMUX_TMPDIR=str(home),AGENTSTACK_HOME=str(home))
            result=subprocess.run([sys.executable,str(ROOT/'bin/lib/runtime_tmux_consumer.py'),'has-session','-t','=Victim'],env=env,capture_output=True,text=True,timeout=10)
            assert result.returncode==1,result.stderr
            assert result.stderr=="can't find session: Victim\n"
            # Compose the real adapter with the actual launcher classifier,
            # rather than relying only on a stubbed tmux diagnostic.
            text=(ROOT/'hooks/spawn_child.sh').read_text()
            start=text.index('codex_session_alive() {')
            function=text[start:text.index('\n}',start)+2]
            script='tmux() { "$PYTHON" "$ADAPTER" "$@"; }\n'+function+'\ncodex_session_alive Victim\n'
            checked=subprocess.run(['/bin/bash','-c',script],env={**env,'PYTHON':sys.executable,'ADAPTER':str(ROOT/'bin/lib/runtime_tmux_consumer.py')},capture_output=True,text=True,timeout=10)
            assert checked.returncode==1,checked.stderr
            # Existing session remains: absence did not kill or replace a server.
            assert transport.command('has-session','-t','=Hold').returncode==0
        finally:
            subprocess.run([binary,'-S',str(path),'kill-server'],capture_output=True,check=False)


def test_has_session_inventory_failure_is_unknown_not_absent(consumer):
    c,calls,_=consumer
    original=c.transport.run
    def unavailable(argv,**kw):
        if argv[3]=='list-sessions':return subprocess.CompletedProcess(argv,1,'','Permission denied')
        return original(argv,**kw)
    c.transport.run=unavailable
    with pytest.raises(API['TargetError'],match='^TMUX_TARGET_UNAVAILABLE$'):
        c.command(['has-session','-t','=Missing'])


def test_has_session_present_still_rejects_a_global_replacement(consumer):
    c,calls,state=consumer;state['global']=True
    with pytest.raises(API['TargetError'],match='^LEGACY_TARGET_IS_GLOBAL$'):
        c.command(['has-session','-t','=Alpha'])
    assert not any(argv[3]=='has-session' for argv in calls)


def test_has_session_changing_inventory_cannot_prove_absence(consumer):
    c,calls,_=consumer
    original=c.transport.run;count=0
    def changing(argv,**kw):
        nonlocal count
        if argv[3]=='list-sessions':
            count+=1
            return subprocess.CompletedProcess(argv,0,'$1\tAlpha\n'+('$2\tMissing\n' if count>1 else ''),'')
        return original(argv,**kw)
    c.transport.run=changing
    with pytest.raises(API['TargetError'],match='^TMUX_TARGET_CHANGED$'):
        c.command(['has-session','-t','=Missing'])
