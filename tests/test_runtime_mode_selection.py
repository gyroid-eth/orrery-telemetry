"""Mode-only admission and ownerless service routing over existing truth."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def selector(tmp_path, monkeypatch):
    api = runpy.run_path(str(ROOT / 'bin/lib/runtime_client.py'))
    monkeypatch.setitem(api['selected'].__globals__, 'ROOT', tmp_path)
    for key in ('AGENTSTACK_RUNTIME_MODE','AGENTSTACK_CLIENT_CONFIG',
                'AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENT_NAME','AGENTSTACK_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AGENTSTACK_HOME', str(tmp_path))
    # Selection must not construct a client or issue a remote call.
    def forbidden(*args, **kwargs): pytest.fail('mode selection reached owner/network IO')
    monkeypatch.setitem(api['selected'].__globals__, 'RuntimeClient', forbidden)
    monkeypatch.setitem(api['selected'].__globals__, 'management_exchange', forbidden)
    return api


def private(path, value):
    path.write_text(json.dumps(value)); path.chmod(0o600)

@pytest.mark.parametrize('mode,expected', [(None,'legacy'),('legacy','legacy'),('global','global')])
def test_mode_without_context_never_constructs_an_owner(selector, monkeypatch, mode, expected):
    if mode is not None: monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE', mode)
    choice = selector['selected']([])
    assert choice['mode'] == expected and choice['context'] is None
    if mode == 'global':
        with pytest.raises(selector['ClientError'], match='^CONTEXT_UNAVAILABLE$'):
            selector['configured'](selection=choice)

@pytest.mark.parametrize('mode', ['GLOBAL','invalid',''])
def test_unknown_or_empty_mode_is_not_legacy(selector, monkeypatch, mode):
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE', mode)
    with pytest.raises(selector['ClientError'], match='^RUNTIME_MODE_INVALID$'):
        selector['selected']([])


def test_explicit_mode_and_context_conflict_before_owner_io(selector, tmp_path):
    path = tmp_path/'runtime-client.json'
    private(path, {'kind':'orrery-runtime-client-s1','mode':'global'})
    assert selector['selected']([])['mode'] == 'global'
    with pytest.raises(selector['ClientError'], match='^RUNTIME_MODE_CONTEXT_CONFLICT$'):
        selector['selected'](['--runtime-mode','legacy'])
    path.unlink(); path.symlink_to(tmp_path/'absent')
    with pytest.raises(selector['ClientError'], match='^PRIVATE_FILE_UNAVAILABLE$'):
        selector['selected']([])


def test_bootstrap_default_is_read_once_but_inherited_mode_stays_pinned(selector, tmp_path, monkeypatch):
    env = tmp_path/'env.sh'
    env.write_text('export AGENTSTACK_RUNTIME_MODE=global\n')
    assert selector['selected'](bootstrap=True)['mode'] == 'global'
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','legacy')
    assert selector['selected'](bootstrap=True)['mode'] == 'legacy'
    assert selector['selected'](bootstrap=False)['mode'] == 'legacy'
    monkeypatch.delenv('AGENTSTACK_RUNTIME_MODE')
    assert selector['selected'](bootstrap=False)['mode'] == 'legacy'


def test_existing_legacy_session_requires_self_proof_not_project_hint(selector, tmp_path, monkeypatch):
    (tmp_path/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=global\n')
    monkeypatch.setenv('AGENT_NAME','LegacyOwner')
    monkeypatch.setenv('AGENTSTACK_SESSION_ID','old-session')
    monkeypatch.setenv('AGENTSTACK_PROJECT_KEY','/irrelevant/project')
    with pytest.raises(selector['ClientError'], match='^LEGACY_SESSION_MODE_UNRESOLVED$'):
        selector['selected'](bootstrap=True)
    directory=tmp_path/'runtime/session_index'; directory.mkdir(parents=True)
    private(directory/'7.json', {'schema_version':2,'binding_kind':'self',
        'agent_id':7,'agent_name':'LegacyOwner','registered_by':'LegacyOwner','session_id':'old-session','project_key':'/irrelevant/project'})
    choice=selector['selected'](bootstrap=True)
    assert choice['mode']=='legacy' and choice['source']=='legacy-session-proof'
    assert selector['selected'](['--runtime-mode','global'])['mode']=='global'

@pytest.mark.parametrize('hint', [('AGENTSTACK_RUNTIME_MODE','global'),('AGENTSTACK_RUNTIME_MODE','unknown'),('AGENTSTACK_RUNTIME_MODE',''),
                                 ('AGENTSTACK_GLOBAL_SERVER_CONFIG','/missing/server-config.json')])
def test_partial_loader_global_hint_cannot_continue_legacy(tmp_path, hint):
    import shutil
    lib=tmp_path/'bin/lib';lib.mkdir(parents=True)
    shutil.copyfile(ROOT/'bin/lib/global-client-loader.sh',lib/'global-client-loader.sh')
    env={**os.environ,hint[0]:hint[1]}
    result=subprocess.run(['/bin/bash',str(lib/'global-client-loader.sh'),'select'],env=env,capture_output=True,text=True)
    assert result.returncode==2 and 'GLOBAL_ENTRY_UNAVAILABLE' in result.stderr


def test_contextless_global_mode_cli_does_not_open_legacy_database(tmp_path):
    env={**os.environ,'AGENTSTACK_RUNTIME_MODE':'global','AGENTSTACK_HOME':str(tmp_path),'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-mode',
         'AGENTSTACK_MAIL_DB':str(tmp_path/'must-not-exist.sqlite3')}
    for key in ('AGENT_NAME','AGENTSTACK_SESSION_ID','AGENTSTACK_CLIENT_CONFIG'):
        env.pop(key,None)
    result=subprocess.run([sys.executable,str(ROOT/'bin/lib/runtime_client.py'),'mode'],env=env,capture_output=True,text=True)
    assert result.returncode==0 and result.stdout=='global\n'
    result=subprocess.run([sys.executable,str(ROOT/'bin/lib/runtime_client.py'),'observe'],env=env,capture_output=True,text=True)
    assert result.returncode==2 and 'CONTEXT_UNAVAILABLE' in result.stderr
    assert not (tmp_path/'must-not-exist.sqlite3').exists()


def test_config_only_selection_is_global_and_unknown_kind_never_falls_back(selector, tmp_path):
    path=tmp_path/'server-config.json'
    private(path,{'kind':'orrery-global-server-s2c-v1'})
    result=selector['selected'](['--server-config',str(path)])
    assert result['mode']=='global' and result['context'] is None
    with pytest.raises(selector['ClientError'],match='^RUNTIME_MODE_CONTEXT_CONFLICT$'):
        selector['selected'](['--runtime-mode','legacy','--server-config',str(path)])
    private(path,{'kind':'foreign'})
    with pytest.raises(selector['ClientError'],match='^ISOLATED_CONFIG_REQUIRED$'):
        selector['selected'](['--server-config',str(path)])


def test_installed_default_does_not_override_explicit_global_context(selector,tmp_path):
    (tmp_path/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=legacy\n')
    private(tmp_path/'runtime-client.json',{'kind':'orrery-runtime-client-s1','mode':'global'})
    assert selector['selected'](bootstrap=True)['mode']=='global'


def test_bootstrap_pinned_legacy_ignores_changed_global_defaults_without_python(tmp_path):
    import shutil
    lib=tmp_path/'bin/lib';lib.mkdir(parents=True)
    shutil.copy2(ROOT/'bin/lib/global-client-loader.sh',lib/'global-client-loader.sh')
    fake=tmp_path/'fake';fake.mkdir();marker=tmp_path/'python-ran'
    executable=fake/'python3'
    executable.write_text('#!/bin/sh\ntouch "'+str(marker)+'"\nexit 1\n');executable.chmod(0o755)
    default=tmp_path/'env.sh'
    default.write_text('export AGENTSTACK_RUNTIME_MODE=global\nexport AGENTSTACK_GLOBAL_SERVER_CONFIG=/must-not-read.json\n')
    script='. "$1"; ags_client_bootstrap "$2"; printf "%s:%s" "$AGENTSTACK_RUNTIME_MODE" "${AGENTSTACK_GLOBAL_SERVER_CONFIG-unset}"'
    env={**os.environ,'PATH':str(fake)+':/usr/bin:/bin','AGENTSTACK_RUNTIME_MODE':'legacy'}
    env.pop('AGENTSTACK_GLOBAL_SERVER_CONFIG',None);env.pop('AGENTSTACK_CLIENT_CONFIG',None)
    result=subprocess.run(['/bin/bash','-c',script,'test',str(lib/'global-client-loader.sh'),str(default)],
                          env=env,capture_output=True,text=True)
    assert result.returncode==0 and result.stdout=='legacy:unset'
    assert not marker.exists()


@pytest.mark.parametrize('entry',['bin/agentstack-enroll','bin/agentstack-mailctl','dashboard/agentctl.sh'])
def test_new_operator_controller_missing_loader_never_invokes_legacy(tmp_path,entry):
    import shutil
    destination=tmp_path/entry;destination.parent.mkdir(parents=True)
    shutil.copy2(ROOT/entry,destination)
    result=subprocess.run(['/bin/bash',str(destination),'status'],
                          env={**os.environ,'AGENTSTACK_HOME':str(tmp_path),'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-mode','AGENTSTACK_RUNTIME_MODE':'global'},
                          capture_output=True,text=True)
    assert result.returncode==2 and 'GLOBAL_ENTRY_UNAVAILABLE' in result.stderr
    assert not (tmp_path/'mail').exists()


def test_global_installer_is_rejected_before_legacy_service_or_database(tmp_path):
    env={**os.environ,'HOME':str(tmp_path),'AGENTSTACK_HOME':str(tmp_path/'stack'),
         'AGENTSTACK_PROJECT_KEY':str(tmp_path/'project'),'AGENTSTACK_RUNTIME_MODE':'global',
         'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-mode',
         'AGENTSTACK_MAIL_DB':str(tmp_path/'must-not-create.sqlite3')}
    for key in ('AGENT_NAME','AGENTSTACK_SESSION_ID','AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG'):
        env.pop(key,None)
    result=subprocess.run(['/bin/bash',str(ROOT/'scripts/install.sh'),'--dashboard-only','--dry-run'],
                          env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==2 and 'GLOBAL_INSTALL_REQUIRES_PR7' in result.stderr
    assert not (tmp_path/'must-not-create.sqlite3').exists() and not (tmp_path/'stack').exists()


@pytest.mark.parametrize('assignment,mode,reason',[
    ('export AGENTSTACK_RUNTIME_MODE=legacy\n','legacy',None),
    ('export AGENTSTACK_RUNTIME_MODE=global\n','global',None),
    ("export AGENTSTACK_RUNTIME_MODE=''\n",None,'RUNTIME_MODE_INVALID'),
    ('export AGENTSTACK_RUNTIME_MODE=unknown\n',None,'RUNTIME_MODE_INVALID'),
    ('export AGENTSTACK_RUNTIME_MODE="$(touch ATTACK_MARKER)"\n',None,'RUNTIME_MODE_INVALID'),
    ('export AGENTSTACK_RUNTIME_MODE="unterminated\n',None,'RUNTIME_DEFAULT_UNAVAILABLE'),
])
def test_installer_bootstrap_reads_only_literal_settings(tmp_path,assignment,mode,reason):
    # Other assignments are data, never shell evidence or code to execute.
    env_file=tmp_path/'env.sh'
    marker=tmp_path/'must-not-execute'
    env_file.write_text(assignment.replace('ATTACK_MARKER',str(marker))+
        'export AGENTSTACK_MAIL_ENV="$MISSING_RENDER/service.env"\n')
    script='. "$1"; . "$2"; ags_client_bootstrap "$3" --literal || exit $?; printf "%s" "$AGENTSTACK_RUNTIME_MODE"'
    env={k:v for k,v in os.environ.items() if not k.startswith('AGENTSTACK_') and k not in ('AGENT_NAME','PARENT_AGENT')}
    env.update(HOME=str(tmp_path),AGENTSTACK_LABEL_PREFIX='org.agentstack.test.r1-literal')
    result=subprocess.run(['/bin/bash','-uc',script,'fixture',str(ROOT/'hooks/project-context.sh'),
        str(ROOT/'bin/lib/global-client-loader.sh'),str(env_file)],env=env,capture_output=True,text=True)
    if reason:
        assert result.returncode==2 and reason in result.stderr,result.stderr
    else:
        assert result.returncode==0 and result.stdout==mode,result.stderr
    assert not marker.exists()


def test_literal_default_reader_failure_never_becomes_legacy(tmp_path):
    fake=tmp_path/'fake';fake.mkdir()
    python=fake/'python3';python.write_text('#!/bin/sh\nexit 1\n');python.chmod(0o755)
    env_file=tmp_path/'env.sh';env_file.write_text('export AGENTSTACK_RUNTIME_MODE=global\n')
    script='. "$1"; . "$2"; ags_client_bootstrap "$3" --literal'
    env={k:v for k,v in os.environ.items() if not k.startswith('AGENTSTACK_') and k not in ('AGENT_NAME','PARENT_AGENT')}
    env.update(HOME=str(tmp_path),PATH=str(fake)+':/usr/bin:/bin',AGENTSTACK_LABEL_PREFIX='org.agentstack.test.r1-reader')
    result=subprocess.run(['/bin/bash','-c',script,'fixture',str(ROOT/'hooks/project-context.sh'),
        str(ROOT/'bin/lib/global-client-loader.sh'),str(env_file)],env=env,capture_output=True,text=True)
    assert result.returncode==2 and 'RUNTIME_DEFAULT_UNAVAILABLE' in result.stderr


def test_operator_global_with_ambient_agent_name_does_not_require_session_proof(selector,tmp_path,monkeypatch):
    monkeypatch.setenv('AGENT_NAME','AmbientLegacy')
    config=tmp_path/'server-config.json';private(config,{'kind':'orrery-global-server-s2c-v1'})
    for args in (['--runtime-mode','global','--server-config',str(config)],[]):
        if not args:
            monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','global')
            monkeypatch.setenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',str(config))
        choice=selector['selected'](args)
        assert choice['mode']=='global' and choice['context'] is None


def test_legacy_session_proof_distinguishes_same_name_in_two_projects(selector,tmp_path,monkeypatch):
    directory=tmp_path/'runtime/session_index';directory.mkdir(parents=True)
    for aid,project in [(7,'/first'),(8,'/second')]:
        private(directory/f'{aid}.json',{'schema_version':2,'binding_kind':'self',
            'agent_id':aid,'agent_name':'SameOwner','registered_by':'SameOwner',
            'session_id':f'session-{aid}','project_key':project})
    monkeypatch.setenv('AGENT_NAME','SameOwner');monkeypatch.setenv('AGENTSTACK_PROJECT_KEY','/second')
    (tmp_path/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=global\n')
    assert selector['selected'](bootstrap=True)['source']=='legacy-session-proof'
    monkeypatch.setenv('AGENTSTACK_PROJECT_KEY','/third')
    with pytest.raises(selector['ClientError'],match='^LEGACY_SESSION_MODE_UNRESOLVED$'):
        selector['selected'](bootstrap=True)


def test_installed_legacy_with_leftover_global_config_is_a_conflict(selector,tmp_path):
    config=tmp_path/'server-config.json';private(config,{'kind':'orrery-global-server-s2c-v1'})
    (tmp_path/'env.sh').write_text(f'export AGENTSTACK_RUNTIME_MODE=legacy\nexport AGENTSTACK_GLOBAL_SERVER_CONFIG={config}\n')
    with pytest.raises(selector['ClientError'],match='^RUNTIME_MODE_CONTEXT_CONFLICT$'):
        selector['selected'](bootstrap=True)


@pytest.mark.parametrize('entry',['bin/agentstack-enroll','bin/agentstack-mailctl','dashboard/agentctl.sh',
                                 'scripts/lib/agentstack-persistent-launcher.sh','scripts/install.sh'])
def test_system_controller_never_rewrites_global_defaults_from_legacy_shell(tmp_path,entry):
    import shutil
    home=tmp_path/'home';home.mkdir();stack=home/'.agentstack';stack.mkdir()
    env_file=stack/'env.sh';raw='export AGENTSTACK_RUNTIME_MODE=global\nexport AGENTSTACK_GLOBAL_SERVER_CONFIG=/unused/config.json\n'
    env_file.write_text(raw)
    if entry!='scripts/install.sh':
        for sub in ('bin/lib','hooks','dashboard'):
            shutil.copytree(ROOT/sub,stack/sub)
        shutil.copy2(ROOT/'scripts/lib/agentstack-persistent-launcher.sh',stack/'bin/agentstack-persistent')
        if entry.startswith('bin/'):shutil.copy2(ROOT/entry,stack/entry)
        command=stack/('bin/agentstack-persistent' if entry.startswith('scripts/') else entry)
    else:command=ROOT/entry
    env={**os.environ,'HOME':str(home),'AGENTSTACK_HOME':str(stack),'AGENTSTACK_RUNTIME_MODE':'legacy',
         'AGENTSTACK_PROJECT_KEY':str(tmp_path),'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-system'}
    for key in ('AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG'):env.pop(key,None)
    argv=['--dry-run','--dashboard-only'] if entry=='scripts/install.sh' else ['status']
    result=subprocess.run(['/bin/bash',str(command),*argv],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode!=0 and 'RUNTIME_MODE_INHERITED_CONFLICT' in result.stderr
    assert env_file.read_text()==raw
    assert not (home/'Library/LaunchAgents').exists()
    assert not (stack/'mail').exists()


@pytest.mark.parametrize('provider',['codex','gemini'])
@pytest.mark.parametrize('project',[False,True])
def test_direct_bootstrap_preserves_pinned_legacy_before_existing_flow(tmp_path,provider,project):
    import shutil
    stack=tmp_path/'stack';(stack/'bin').mkdir(parents=True)
    shutil.copytree(ROOT/'bin/lib',stack/'bin/lib')
    # An early return from the existing legacy registration library avoids any
    # provider/network work and samples the real bootstrap's selected values.
    (stack/'bin/lib/agentstack-register.sh').write_text('return 0\n')
    shutil.copytree(ROOT/'hooks', stack/'hooks')
    source=ROOT/f'bin/agentstack-{provider}-bootstrap'
    shutil.copy2(source,stack/'bin'/source.name)
    (stack/'env.sh').write_text('touch "'+str(tmp_path/'env-read')+'"\nexport AGENTSTACK_RUNTIME_MODE=global\nexport AGENTSTACK_GLOBAL_SERVER_CONFIG=/unused/new-config.json\n')
    script='. "$1"; printf "\\nMODE=%s CONFIG=%s\\n" "$AGENTSTACK_RUNTIME_MODE" "${AGENTSTACK_GLOBAL_SERVER_CONFIG-unset}"'
    env={**os.environ,'HOME':str(tmp_path),'AGENTSTACK_HOME':str(stack),'AGENTSTACK_RUNTIME_MODE':'legacy',
         'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-direct','AGENTSTACK_MCP_URL':'http://127.0.0.1:1/mcp'}
    for key in ('PROJECT_KEY','AGENTSTACK_PROJECT_KEY','AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENTSTACK_CLIENT_CONFIG','TMUX','TMUX_PANE'):env.pop(key,None)
    if project:env['AGENTSTACK_PROJECT_KEY']=str(tmp_path)
    result=subprocess.run(['/bin/bash','-c',script,'fixture',str(stack/'bin'/source.name)],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0 and 'MODE=legacy CONFIG=unset' in result.stdout
    assert (tmp_path/'env-read').exists() is (not project)


@pytest.mark.parametrize('mode',['global','unknown','',None])
@pytest.mark.parametrize('helpers',['missing','old'])
def test_app_partial_admission_never_builds_legacy_proxy(tmp_path,monkeypatch,mode,helpers):
    stack=tmp_path/'stack';lib=stack/'bin/lib';lib.mkdir(parents=True)
    if helpers=='old':
        loader=lib/'global-client-loader.sh';loader.write_text('#!/bin/bash\nexit 125\n')
    entry=runpy.run_path(str(ROOT/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py'))['selected_client']
    monkeypatch.setitem(entry.__globals__,'__file__',str(stack/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py'))
    monkeypatch.setenv('AGENTSTACK_HOME',str(stack))
    for key in ('AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_RUNTIME_MODE','AGENTSTACK_GLOBAL_SERVER_CONFIG'):monkeypatch.delenv(key,raising=False)
    if mode is not None:monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE',mode)
    with pytest.raises(RuntimeError,match='^GLOBAL_ENTRY_UNAVAILABLE$'):entry()


def test_normal_global_installer_never_probes_explicit_codex_candidate(tmp_path):
    marker=tmp_path/'candidate-ran';candidate=tmp_path/'codex'
    candidate.write_text('#!/bin/sh\ntouch "'+str(marker)+'"\nexit 1\n');candidate.chmod(0o755)
    stack=tmp_path/'stack'
    env={**os.environ,'HOME':str(tmp_path),'AGENTSTACK_HOME':str(stack),'AGENTSTACK_RUNTIME_MODE':'global',
        'AGENTSTACK_PROJECT_KEY':str(tmp_path),'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-probe'}
    result=subprocess.run(['/bin/bash',str(ROOT/'scripts/install.sh'),'--dry-run','--codex-bin',str(candidate)],
                          env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==2 and 'GLOBAL_INSTALL_REQUIRES_PR7' in result.stderr
    assert not marker.exists() and not stack.exists()


def test_rendered_legacy_service_runner_executes_original_argv_without_selector(tmp_path):
    source=(ROOT/'scripts/install.sh').read_text().split('render_native_mail_runner() {',1)[1]
    program=source.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
    runner=tmp_path/'runner.sh'
    args=[str(runner),'/fixture/service','/fixture/server','/fixture/env','/fixture/state','source',
          '/fixture/venv','http://127.0.0.1:18765/mcp','/fixture/enroll',str(tmp_path/'deployment'),
          '/must-not-run/runtime_client.py',sys.executable,'legacy']
    result=subprocess.run([sys.executable,'-c',program,*args],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    text=runner.read_text()
    assert '/fixture/service foreground --server-executable /fixture/server --env-file /fixture/env --state-root /fixture/state' in text
    assert 'runtime_client.py' not in text and 'service-exec' not in text


@pytest.mark.parametrize('hint',['config','implicit','broken','legacy'])
def test_app_current_loader_is_the_only_partial_admission(tmp_path,monkeypatch,hint):
    import shutil
    stack=tmp_path/'stack';lib=stack/'bin/lib';lib.mkdir(parents=True)
    shutil.copy2(ROOT/'bin/lib/global-client-loader.sh',lib/'global-client-loader.sh')
    entry=runpy.run_path(str(ROOT/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py'))['selected_client']
    monkeypatch.setitem(entry.__globals__,'__file__',str(stack/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py'))
    monkeypatch.setenv('AGENTSTACK_HOME',str(stack))
    for key in ('AGENTSTACK_RUNTIME_MODE','AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENTSTACK_CLIENT_CONFIG'):monkeypatch.delenv(key,raising=False)
    if hint=='config':monkeypatch.setenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',str(stack/'server-config.json'))
    elif hint=='implicit':private(stack/'runtime-client.json',{'kind':'orrery-runtime-client-s1','mode':'global'})
    elif hint=='broken':(stack/'runtime-client.json').symlink_to(stack/'absent')
    if hint=='legacy':assert entry() is None
    else:
        with pytest.raises(RuntimeError,match='^GLOBAL_ENTRY_UNAVAILABLE$'):entry()


def test_system_bootstrap_explicit_mode_can_override_stale_inheritance(tmp_path):
    env_file=tmp_path/'env.sh';env_file.write_text('export AGENTSTACK_RUNTIME_MODE=legacy\n')
    env={**os.environ,'HOME':str(tmp_path),'AGENTSTACK_HOME':str(tmp_path),'AGENTSTACK_RUNTIME_MODE':'global',
         'AGENT_NAME':'AmbientAgent','AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r1-explicit'}
    for key in ('AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG'):env.pop(key,None)
    script='. "$1"; ags_client_bootstrap "$2" --system --runtime-mode legacy || exit $?; printf "%s" "$AGENTSTACK_RUNTIME_MODE"'
    result=subprocess.run(['/bin/bash','-c',script,'fixture',str(ROOT/'bin/lib/global-client-loader.sh'),str(env_file)],
                          env=env,capture_output=True,text=True)
    assert result.returncode==0 and result.stdout=='legacy'


@pytest.mark.parametrize('entry', ['agentstack-preregister-child', 'agentstack-reregister'])
def test_legacy_registration_helper_cannot_reload_global_mode(tmp_path, entry):
    import shutil
    root=tmp_path/'install'; lib=root/'bin/lib';lib.mkdir(parents=True)
    for name in ('global-client-loader.sh', 'runtime_client.py'):
        shutil.copyfile(ROOT/'bin/lib'/name, lib/name)
    shutil.copyfile(ROOT/'bin'/entry, root/'bin'/entry)
    # Stop at the legacy register boundary, before credentials/network access.
    (lib/'agentstack-register.sh').write_text('printf "%s/%s" "$AGENTSTACK_RUNTIME_MODE" "${AGENTSTACK_GLOBAL_SERVER_CONFIG-unset}"; exit 0\n')
    (root/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=global\nexport AGENTSTACK_GLOBAL_SERVER_CONFIG=/must-not-load\n')
    env={**os.environ, 'AGENTSTACK_HOME':str(root), 'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.registration-mode', 'AGENTSTACK_RUNTIME_MODE':'legacy'}
    for key in ('AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENTSTACK_ENV_FILE'):
        env.pop(key,None)
    result=subprocess.run(['/bin/bash',str(root/'bin'/entry)],env=env,capture_output=True,text=True)
    assert result.returncode==0 and result.stdout=='legacy/unset', result.stderr


@pytest.mark.parametrize('hint', ['legacy','global','unknown','empty','broken'])
def test_bundled_app_loader_admits_only_proven_legacy(tmp_path,monkeypatch,hint):
    import shutil
    package=tmp_path/'app/agentstack_codex_app';package.mkdir(parents=True)
    shutil.copyfile(ROOT/'integrations/codex_app/src/agentstack_codex_app/global_runtime.py',package/'global_runtime.py')
    shutil.copyfile(ROOT/'bin/lib/global-client-loader.sh',package/'_global-client-loader.sh')
    core=tmp_path/'core';core.mkdir()
    monkeypatch.setenv('AGENTSTACK_HOME',str(core))
    for key in ('AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENTSTACK_RUNTIME_MODE'):
        monkeypatch.delenv(key,raising=False)
    if hint=='broken':(core/'runtime-client.json').symlink_to(core/'missing')
    elif hint!='legacy':monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','' if hint=='empty' else hint)
    api=runpy.run_path(str(package/'global_runtime.py'))
    if hint=='legacy':assert api['selected_client']() is None
    else:
        with pytest.raises(RuntimeError,match='^GLOBAL_ENTRY_UNAVAILABLE$'):api['selected_client']()


@pytest.mark.parametrize('entry', ['agentstack-preregister-child','agentstack-reregister'])
def test_registration_helper_reads_installed_default_before_legacy_library(tmp_path,entry):
    import shutil
    from _installed_client_payload import copy_client_library
    root=tmp_path/'install';lib=root/'bin/lib';lib.mkdir(parents=True)
    for name in ('global-client-loader.sh','runtime_client.py'):
        shutil.copyfile(ROOT/'bin/lib'/name,lib/name)
    copy_client_library(root)
    shutil.copyfile(ROOT/'bin'/entry,root/'bin'/entry)
    (lib/'agentstack-register.sh').write_text('printf "%s" "$AGENTSTACK_RUNTIME_MODE"; exit 0\n')
    (root/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=global\n')
    env={**os.environ,'AGENTSTACK_HOME':str(root),'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.registration-default'}
    for key in ('AGENTSTACK_RUNTIME_MODE','AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENTSTACK_ENV_FILE','AGENT_NAME','AGENTSTACK_SESSION_ID'):
        env.pop(key,None)
    result=subprocess.run(['/bin/bash',str(root/'bin'/entry)],env=env,capture_output=True,text=True)
    if entry=='agentstack-preregister-child':
        assert result.returncode==0 and result.stdout=='global',result.stderr
    else:
        assert result.returncode!=0 and 'CONTEXT_UNAVAILABLE' in result.stderr
        assert result.stdout==''


@pytest.mark.parametrize('mode', ['global',None])
def test_disabled_global_hook_selection_never_reads_context_owner(selector,tmp_path,monkeypatch,mode):
    context=tmp_path/'broken.json';context.write_text('not JSON')
    monkeypatch.setenv('AGENTSTACK_CLIENT_CONFIG',str(context))
    monkeypatch.setenv('AGENTSTACK_MAIL_DISABLED','1')
    if mode:monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE',mode)
    def forbidden(*args,**kwargs):pytest.fail('disabled hook read owner bytes')
    monkeypatch.setitem(selector['selected'].__globals__,'read_json',forbidden)
    assert selector['selected'](['--disabled-hook'])['mode']=='global'


@pytest.mark.parametrize('installed', ['legacy','global'])
@pytest.mark.parametrize('project_variable', ['AGENTSTACK_PROJECT_KEY','PROJECT_KEY'])
def test_unpinned_continuation_keeps_live_project_and_endpoint(tmp_path,installed,project_variable):
    env_file=tmp_path/'env.sh'
    env_file.write_text('export AGENTSTACK_RUNTIME_MODE='+installed+'\nexport AGENTSTACK_PROJECT_KEY=/installed/project\nexport AGENTSTACK_MCP_URL=http://127.0.0.1:1/wrong\n')
    env={**os.environ,'HOME':str(tmp_path),'AGENTSTACK_HOME':str(tmp_path),
         project_variable:'/live/project','AGENTSTACK_MCP_URL':'http://127.0.0.1:2/live',
         'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r254-continuation'}
    for key in ('AGENTSTACK_RUNTIME_MODE','AGENTSTACK_GLOBAL_SERVER_CONFIG','AGENTSTACK_CLIENT_CONFIG','AGENT_NAME','AGENTSTACK_SESSION_ID'):
        env.pop(key,None)
    if project_variable=='PROJECT_KEY':env.pop('AGENTSTACK_PROJECT_KEY',None)
    command='. "$1"; ags_client_bootstrap "$2" --continue || exit $?; printf "%s|%s|%s" "$AGENTSTACK_RUNTIME_MODE" "${AGENTSTACK_PROJECT_KEY:-$PROJECT_KEY}" "$AGENTSTACK_MCP_URL"'
    result=subprocess.run(['/bin/bash','-c',command,'fixture',str(ROOT/'bin/lib/global-client-loader.sh'),str(env_file)],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout==installed+'|/live/project|http://127.0.0.1:2/live'


@pytest.mark.parametrize('provider',['codex','gemini'])
def test_legacy_launcher_passes_pinned_selection_to_new_tmux_pane(tmp_path,provider):
    import shutil
    stack=tmp_path/'stack';(stack/'bin').mkdir(parents=True)
    shutil.copytree(ROOT/'bin/lib',stack/'bin/lib')
    shutil.copytree(ROOT/'hooks',stack/'hooks')
    launcher=stack/'bin'/('agent-start-'+provider)
    shutil.copyfile(ROOT/'bin'/launcher.name,launcher)
    (stack/'bin'/('agentstack-'+provider+'-bootstrap')).write_text('return 0\n')
    capture=tmp_path/'argv.json';fake=tmp_path/'fake-tmux'
    fake.write_text('#!'+sys.executable+'\nimport json,os,sys\nopen(os.environ["CAPTURE"],"w").write(json.dumps(sys.argv[1:]))\n');fake.chmod(0o755)
    library=stack/'bin/lib/agentstack-launch.sh'
    library.write_text(library.read_text()+'\nags_resolve_tmux() { printf "%s" "$FAKE_TMUX"; }\n')
    (stack/'env.sh').write_text('export AGENTSTACK_RUNTIME_MODE=global\n')
    env={**os.environ,'HOME':str(tmp_path),'AGENTSTACK_HOME':str(stack),'AGENTSTACK_RUNTIME_MODE':'legacy',
         'AGENTSTACK_CODEX_BIN':'/usr/bin/true','AGENTSTACK_GEMINI_BIN':'/usr/bin/true',
         'AGENTSTACK_PROJECT_KEY':str(tmp_path),'AGENTSTACK_LABEL_PREFIX':'org.agentstack.test.r254-pane',
         'FAKE_TMUX':str(fake),'CAPTURE':str(capture),'SHELL':'/usr/bin/true'}
    for key in ('TMUX','TMUX_PANE','AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_GLOBAL_SERVER_CONFIG'):env.pop(key,None)
    result=subprocess.run(['/bin/bash',str(launcher),str(tmp_path)],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    args=json.loads(capture.read_text())
    values=[args[i+1] for i,value in enumerate(args[:-1]) if value=='-e']
    assert 'AGENTSTACK_RUNTIME_MODE=legacy' in values
    assert 'AGENTSTACK_GLOBAL_SERVER_CONFIG=' in values
    assert 'AGENTSTACK_CLIENT_CONFIG=' in values


@pytest.mark.parametrize('provider', ['codex', 'gemini'])
@pytest.mark.parametrize('helper', ['current', 'missing', 'old'])
@pytest.mark.parametrize('project_variable', ['AGENTSTACK_PROJECT_KEY', 'PROJECT_KEY'])
@pytest.mark.parametrize('default', ['legacy', 'global', 'invalid', 'unreadable', 'dangling', 'absent'])
@pytest.mark.parametrize('pinned', [False, True])
def test_actual_continuation_never_admits_an_unverified_default(
    tmp_path, provider, helper, project_variable, default, pinned
):
    import shutil
    stack = tmp_path / 'install'
    lib = stack / 'bin/lib'
    lib.mkdir(parents=True)
    shutil.copyfile(ROOT / 'bin/lib/global-client-loader.sh', lib / 'global-client-loader.sh')
    if helper == 'current':
        shutil.copyfile(ROOT / 'bin/lib/runtime_client.py', lib / 'runtime_client.py')
        from _installed_client_payload import copy_client_library
        copy_client_library(stack)
    elif helper == 'old':
        (lib / 'runtime_client.py').write_text('raise SystemExit(2)\n')
    entry = f'agentstack-{provider}-bootstrap'
    shutil.copyfile(ROOT / 'bin' / entry, stack / 'bin' / entry)
    # Observe the actual legacy registration boundary, never credentials/network.
    (lib / 'agentstack-register.sh').write_text(
        'printf "legacy_register_reached mode=%s url=%s\\n" '
        '"$AGENTSTACK_RUNTIME_MODE" "$AGENTSTACK_MCP_URL"; exit 0\n'
    )
    (stack/'hooks').mkdir(exist_ok=True)
    for name in ('project-context.sh', 'installed-env.py'):
        shutil.copy2(ROOT/'hooks'/name, stack/'hooks'/name)
    env_file = stack / 'env.sh'
    if default == 'dangling':
        env_file.symlink_to(stack / 'missing-env.sh')
    elif default != 'absent':
        value = 'invalid' if default == 'invalid' else ('global' if default == 'unreadable' else default)
        env_file.write_text(
            f'export AGENTSTACK_RUNTIME_MODE={value}\n'
            'export AGENTSTACK_PROJECT_KEY=/must-not-replace-live\n'
            'export PROJECT_KEY=/must-not-replace-live\n'
            'export AGENTSTACK_MCP_URL=http://must-not-replace-live.invalid\n'
        )
        if default == 'unreadable':
            env_file.chmod(0)
    env = {**os.environ, 'HOME': str(tmp_path), 'AGENTSTACK_HOME': str(stack),
           'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test.continuation-default',
           'AGENTSTACK_MCP_URL': 'http://127.0.0.1:1/live',
           'AGENTSTACK_EXTRA_PROTECTED_ROOTS': ''}
    for key in ('AGENTSTACK_RUNTIME_MODE', 'AGENTSTACK_CLIENT_CONFIG',
                'AGENTSTACK_GLOBAL_SERVER_CONFIG', 'AGENTSTACK_ENV_FILE',
                'AGENTSTACK_PROJECT_KEY', 'PROJECT_KEY', 'AGENT_NAME',
                'AGENTSTACK_SESSION_ID', 'TMUX', 'TMUX_PANE'):
        env.pop(key, None)
    env[project_variable] = '/fixture/live'
    if pinned:
        env['AGENTSTACK_RUNTIME_MODE'] = 'legacy'
    result = subprocess.run(['/bin/bash', str(stack / 'bin' / entry)],
                            env=env, capture_output=True, text=True, timeout=20)
    allowed = pinned or default == 'absent' or (helper == 'current' and default == 'legacy')
    if allowed:
        assert result.returncode == 0, result.stderr
        assert result.stdout == ('legacy_register_reached mode=legacy '
                                 'url=http://127.0.0.1:1/live\n')
    else:
        assert result.returncode != 0
        assert not result.stdout
        reason = ('RUNTIME_DEFAULT_UNAVAILABLE' if helper != 'current' or default in ('unreadable', 'dangling')
                  else 'RUNTIME_MODE_INVALID' if default == 'invalid' else 'CONTEXT_UNAVAILABLE')
        assert reason in result.stderr, result.stderr


@pytest.mark.parametrize('arguments, literal', [
    (['--task-description', 'use --literal here'], False),
    (['--task-description', ' --literal '], False),
    (['--name', '--literal-like'], False),
    (['--literal=value'], False),
    (['--literal'], True),
])
def test_literal_bootstrap_marker_is_one_complete_argument(tmp_path, arguments, literal):
    env_file = tmp_path / 'env.sh'
    env_file.write_text('export AGENTSTACK_RUNTIME_MODE=legacy\n')
    env = {**os.environ, 'HOME': str(tmp_path), 'AGENTSTACK_HOME': str(tmp_path),
           'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test.literal-argument'}
    for key in ('AGENTSTACK_RUNTIME_MODE', 'AGENTSTACK_CLIENT_CONFIG',
                'AGENTSTACK_GLOBAL_SERVER_CONFIG'):
        env.pop(key, None)
    script = '. "$1"; env_file="$2"; shift 2; ags_client_bootstrap "$env_file" "$@"'
    result = subprocess.run(['/bin/bash', '-c', script, 'fixture',
                             str(ROOT / 'bin/lib/global-client-loader.sh'), str(env_file), *arguments],
                            env=env, capture_output=True, text=True)
    # The ordinary entry has no installer literal reader. Only the exact
    # internal marker may require it; description values must stay ordinary.
    assert result.returncode == (2 if literal else 0), result.stderr
    assert ('RUNTIME_DEFAULT_UNAVAILABLE' in result.stderr) is literal
