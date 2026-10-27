"""The service PATH must keep a Node-installed `codex` with its own runtime.

A Node wrapper resolves its platform package through the first `node` on PATH.
Under the service's minimal PATH that is a different Node than the one the
wrapper was installed under, so `codex app-server` dies and the dashboard
reports no Codex usage at all.
"""

from __future__ import annotations

import pathlib
import re
import shlex
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def test_dashboard_only_payload_copies_and_updates_internal_reader(tmp_path):
    text = INSTALLER.read_text()
    body = text.split('install_payload() {', 1)[1].split('\n}\n', 1)[0]
    target = tmp_path / 'install'
    (target / 'bin').mkdir(parents=True)
    variables = {
        'TIER': 'tier0', 'DRY_RUN': 'false', 'REPO_ROOT': str(ROOT),
        'SCRIPT_DIR': str(ROOT / 'scripts'), 'INSTALL_DIR': str(target),
        'HOOKS_DIR': str(target / 'hooks'), 'DASHBOARD_DIR': str(target / 'dashboard'),
        'BIN_DIR': str(target / 'bin'),
        'MERGE_SETTINGS_SCRIPT': str(ROOT / 'scripts/lib/merge_settings.py'),
        'MERGE_CLAUDE_MCP_SCRIPT': str(ROOT / 'scripts/lib/merge_claude_mcp.py'),
        'RUNTIME_SCHEMA_SOURCE': str(ROOT / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'),
        'RUNTIME_FIXTURE_SOURCE': str(ROOT / 'packages/agentstack_mail/fixtures/global-server-s2a.json'),
    }
    script = '\n'.join(['set -e', *[key + '=' + shlex.quote(value) for key, value in variables.items()],
        'RUNTIME_CLIENT_CONTRACT_SOURCES=()', 'RUNTIME_PUBLIC_ENTRY_SOURCES=()',
        'plan() { :; }', 'copy_core_tree() { :; }',
        'install_payload() {' + body + '\n}', 'install_payload'])
    private = target / 'private-config'
    private.write_bytes(b'keep configuration bytes')
    for _ in range(2):
        done = subprocess.run(['/bin/bash', '-c', script], cwd='/', stdin=subprocess.DEVNULL,
            env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
            text=True, capture_output=True, timeout=10)
        assert done.returncode == 0, done.stderr
        assert (target / 'hooks/llm_secret_reader.py').read_bytes() == (ROOT / 'hooks/llm_secret_reader.py').read_bytes()
        assert (target / 'hooks/native_connections.py').read_bytes() == (ROOT / 'hooks/native_connections.py').read_bytes()
        assert private.read_bytes() == b'keep configuration bytes'
        if _ == 0:
            (target / 'hooks/llm_secret_reader.py').write_bytes(b'old installed reader')
            (target / 'hooks/native_connections.py').write_bytes(b'old installed connection rules')


def _codex_path_block() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r'^if \[\[ -n "\$CODEX_BIN_SETTING" \]\]; then\n.*?^fi\n',
                      text, re.DOTALL | re.MULTILINE)
    assert match, "codex PATH block missing from the installer"
    return match.group(0)


def _resolve(codex_bin: str, path_value: str = DEFAULT_PATH) -> str:
    script = "\n".join([
        "set -euo pipefail",
        f'CODEX_BIN_SETTING={codex_bin!r}',
        f'PATH_VALUE={path_value!r}',
        _codex_path_block(),
        'printf "%s" "$PATH_VALUE"',
    ])
    result = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_codex_outside_the_service_path_brings_its_directory_along(tmp_path):
    codex = tmp_path / "nodebrew" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")

    resolved = _resolve(str(codex))

    assert resolved.split(":")[0] == str(codex.parent)
    assert resolved.endswith(DEFAULT_PATH)


def test_a_codex_already_on_the_service_path_does_not_repeat_it(tmp_path):
    resolved = _resolve("/usr/local/bin/codex")

    assert resolved == DEFAULT_PATH


def test_no_codex_leaves_the_service_path_alone():
    assert _resolve("") == DEFAULT_PATH


def _installer_function(name):
    text=INSTALLER.read_text()
    start=text.index(name+'() {')
    following=re.search(r'^\w+\(\) \{',text[start+len(name)+5:],re.M)
    block=text[start:] if following is None else text[start:start+len(name)+5+following.start()]
    return block[:block.rindex('\n}')+2]


def _fresh_render(tmp_path,*functions,commands='',overrides=None):
    import json,os,sys
    body='\n'.join(_installer_function(name) for name in functions)
    target=tmp_path/'installed';target.mkdir(exist_ok=True)
    for rel in ('runtime','dashboard','bin','hooks','mail/renders/run','candidate/bin'):(target/rel).mkdir(parents=True,exist_ok=True)
    values={key:'' for key in re.findall(r'\$(?:\{)?([A-Z][A-Z_0-9]*)',body)}
    values.update(HOME=str(tmp_path),PATH=os.environ['PATH'],PYTHON_BIN=sys.executable,DRY_RUN='false',
        INSTALL_DIR=str(target),REPO_ROOT=str(ROOT),SCRIPT_DIR=str(ROOT/'scripts'),BIN_DIR=str(target/'bin'),
        HOOKS_DIR=str(target/'hooks'),DASHBOARD_DIR=str(target/'dashboard'),RUNTIME_DIR=str(target/'runtime'),
        ENV_FILE=str(target/'env.sh'),MANIFEST=str(target/'manifest.json'),TIER='tier0',
        LABEL_PREFIX='org.orrery.test.normal',LABEL='org.orrery.test.normal.dashboard',PORT='19877',
        AGENTSTACK_RUNTIME_MODE='fresh',FRESH_CONTEXT_SETTING=str(tmp_path/'context.json'),
        FRESH_SOCKET_SETTING=str(tmp_path/'tmux.sock'),AGENTSTACK_FRESH_SERVER_CONFIG=str(tmp_path/'root/config.json'),
        ORRERY_FRESH_MAIL_INSTANCE_ID='a'*32,NATIVE_MAIL_RUNNER=str(target/'mail/renders/run/run-agentstack-mail.sh'),
        NATIVE_MAIL_ENV=str(target/'mail/renders/run/service.env'),NATIVE_MAIL_DEPLOYMENT=str(target/'mail/renders/run/deployment.json'),
        NATIVE_MAIL_VENV=str(target/'candidate'),NATIVE_MAIL_STATE_ROOT=str(tmp_path/'root'),
        NATIVE_MAIL_SERVICE_ROOT=str(target/'mail'),NATIVE_MAIL_SOURCE_ID='fixture',MCP_URL='http://127.0.0.1:19876/mcp',
        PATH_VALUE='/usr/bin:/bin',AGENT_MAIL_NAME_CAPABILITY_JSON='{}',MAIL_ENV=str(target/'mail/renders/run/service.env'))
    values.update(overrides or {})
    script='set -eu\n'+'\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n'
    script+='plan() { :; }\n'+body+'\nexport AGENTSTACK_INSTALL_FRESH_ENV="$(fresh_environment_json)"\n'+commands
    done=subprocess.run(['/bin/bash','-c',script],cwd=tmp_path,env={'HOME':str(tmp_path),'PATH':os.environ['PATH'],
        'PYTHONPATH':os.environ.get('PYTHONPATH','')},capture_output=True,text=True,timeout=20)
    assert done.returncode==0,done.stderr
    return target,values


def test_prepared_fresh_normal_service_render_preserves_references_and_real_entrypoints(tmp_path):
    import json,plistlib
    from pathlib import Path
    target,values=_fresh_render(tmp_path,'fresh_environment_json','write_env_file','render_launchd_plist','render_systemd_unit','render_native_mail_runner',
        commands='write_env_file\nrender_launchd_plist\nrender_systemd_unit\nrender_native_mail_runner\n')
    def read_env(path):
        result={}
        for line in path.read_text().splitlines():
            words=shlex.split(line,comments=True)
            if len(words)==2 and words[0]=='export':
                key,value=words[1].split('=',1);result[key]=value
        return result
    saved=read_env(target/'env.sh')
    assert saved['AGENTSTACK_RUNTIME_MODE']=='fresh'
    for key,value in {'AGENTSTACK_HOME':str(target),'AGENTSTACK_FRESH_CLIENT_CONTEXT':values['FRESH_CONTEXT_SETTING'],
            'AGENTSTACK_FRESH_SERVER_CONFIG':values['AGENTSTACK_FRESH_SERVER_CONFIG'],
            'AGENTSTACK_FRESH_DASHBOARD_SOCKET':values['FRESH_SOCKET_SETTING'],
            'ORRERY_FRESH_MAIL_INSTANCE_ID':'a'*32,'AGENTSTACK_MAIL_RUNNER':values['NATIVE_MAIL_RUNNER']}.items():
        assert saved[key]==value
    plist=plistlib.loads((tmp_path/'Library/LaunchAgents/org.orrery.test.normal.dashboard.plist').read_bytes())
    systemd=(tmp_path/'.config/systemd/user/org.orrery.test.normal.dashboard.service').read_text()
    for key in ('AGENTSTACK_FRESH_CLIENT_CONTEXT','AGENTSTACK_FRESH_SERVER_CONFIG','ORRERY_FRESH_MAIL_INSTANCE_ID'):
        assert plist['EnvironmentVariables'][key]==saved[key]
        assert key in systemd and saved[key] in systemd
    argv=shlex.split(next(line.strip().removesuffix(' &') for line in Path(values['NATIVE_MAIL_RUNNER']).read_text().splitlines() if ' foreground ' in line))
    assert argv[:2]==[str(target/'candidate/bin/agentstack-mail-service'),'foreground']
    assert argv[argv.index('--server-executable')+1]==str(target/'candidate/bin/python')
    assert argv[argv.index('--fresh-config')+1]==values['AGENTSTACK_FRESH_SERVER_CONFIG']
    assert argv[argv.index('--fresh-port')+1]=='19876'
    assert 'registration_token' not in (target/'env.sh').read_text()
    # Background service_runner reads this exact generated env.sh, rather than
    # another translation of the settings.
    assert '. "$ENV_FILE"' in _installer_function('start_supervised_background')


def test_fresh_and_codex_default_settings_coexist_through_render_and_reload(tmp_path):
    import json,os,plistlib
    codex=dict(CODEX_DEFAULT_MODEL_SETTING='gpt-fixture-terra',CODEX_DEFAULT_EFFORT_SETTING='high',
        # As after `--codex-default-model ... --codex-default-effort ...`.
        CHOSEN_SETTINGS='AGENTSTACK_CODEX_DEFAULT_MODEL AGENTSTACK_CODEX_DEFAULT_EFFORT',
        # The same manifest inputs as the normal-startup layout below.
        MAIL_DB=str(tmp_path/'root/storage.sqlite3'),MAIL_HOME=str(tmp_path/'unused-mail-home'),
        SIGNALS_DIR=str(tmp_path/'unused-signals'),MAIL_WATCHER_LABEL='org.orrery.test.normal.watcher',
        MAIL_LAUNCHD_LABEL_SETTING='org.orrery.test.normal.mail',
        SAFE_MERGE_RESULT_FILE=str(tmp_path/'no-settings'),MCP_MERGE_RESULT_FILE=str(tmp_path/'no-mcp'))
    functions=('fresh_environment_json','write_env_file','render_launchd_plist','render_systemd_unit',
        'render_native_mail_runner','mail_watcher_environment','write_manifest')
    commands='write_env_file\nrender_launchd_plist\nrender_systemd_unit\nrender_native_mail_runner\nwrite_manifest manual "" "" ""\n'
    target,values=_fresh_render(tmp_path,*functions,commands=commands,overrides=codex)
    plist_path=tmp_path/'Library/LaunchAgents/org.orrery.test.normal.dashboard.plist'
    unit_path=tmp_path/'.config/systemd/user/org.orrery.test.normal.dashboard.service'
    outputs=(target/'env.sh',plist_path,unit_path)
    first={path:path.read_bytes() for path in outputs}
    saved=_normal_saved_env(target/'env.sh')
    fresh={'AGENTSTACK_RUNTIME_MODE':'fresh','AGENTSTACK_FRESH_CLIENT_CONTEXT':values['FRESH_CONTEXT_SETTING'],
        'AGENTSTACK_FRESH_SERVER_CONFIG':values['AGENTSTACK_FRESH_SERVER_CONFIG'],
        'AGENTSTACK_FRESH_DASHBOARD_SOCKET':values['FRESH_SOCKET_SETTING'],
        'ORRERY_FRESH_MAIL_INSTANCE_ID':values['ORRERY_FRESH_MAIL_INSTANCE_ID'],
        'AGENTSTACK_MAIL_RUNNER':values['NATIVE_MAIL_RUNNER']}
    chosen={'AGENTSTACK_CODEX_DEFAULT_MODEL':'gpt-fixture-terra','AGENTSTACK_CODEX_DEFAULT_EFFORT':'high'}
    for key,value in (fresh|chosen).items():
        assert saved[key]==value,key
    plist=plistlib.loads(plist_path.read_bytes())['EnvironmentVariables']
    unit=unit_path.read_text()
    manifest=json.loads((target/'manifest.json').read_text())
    for key in ('AGENTSTACK_FRESH_CLIENT_CONTEXT','AGENTSTACK_FRESH_SERVER_CONFIG','ORRERY_FRESH_MAIL_INSTANCE_ID',*chosen):
        assert plist[key]==saved[key],key
        assert key+'='+saved[key] in unit or key+'="'+saved[key]+'"' in unit,key
        assert manifest['env'][key]==saved[key],key
    # The normal update path reloads both groups from the saved env.sh alone.
    reload='\n'.join(_installer_function(n) for n in ('setting_listed','setting_previously_chosen','resolve_setting'))
    code='set -eu\n. '+shlex.quote(str(ROOT/'hooks/project-context.sh'))+'\n'+reload+'\n'+r'''
    SETTINGS_ENV_FILE="$1"; RESET_SETTINGS="$2"; OPTION_GIVEN=''; CHOSEN_SETTINGS=''
    # The installer's own reading of the saved record of choices.
    PREVIOUS_CHOSEN=''; PREVIOUS_CHOSEN_RECORDED=false
    if grep -q '^export AGENTSTACK_CHOSEN_SETTINGS=' "$SETTINGS_ENV_FILE"; then
      PREVIOUS_CHOSEN_RECORDED=true
      PREVIOUS_CHOSEN="$(agentstack_installed_env_value AGENTSTACK_CHOSEN_SETTINGS "$SETTINGS_ENV_FILE")"
    fi
    RUNTIME_MODE_SETTING=''; FRESH_CONTEXT_SETTING=''; FRESH_SOCKET_SETTING=''; CODEX_DEFAULT_MODEL_SETTING=''; CODEX_DEFAULT_EFFORT_SETTING=''
    resolve_setting RUNTIME_MODE_SETTING AGENTSTACK_RUNTIME_MODE legacy kept
    resolve_setting FRESH_CONTEXT_SETTING AGENTSTACK_FRESH_CLIENT_CONTEXT '' kept
    resolve_setting FRESH_SOCKET_SETTING AGENTSTACK_FRESH_DASHBOARD_SOCKET '' kept
    resolve_setting CODEX_DEFAULT_MODEL_SETTING AGENTSTACK_CODEX_DEFAULT_MODEL
    resolve_setting CODEX_DEFAULT_EFFORT_SETTING AGENTSTACK_CODEX_DEFAULT_EFFORT
    printf '%s\n' "$RUNTIME_MODE_SETTING" "$FRESH_CONTEXT_SETTING" "$FRESH_SOCKET_SETTING" "$CODEX_DEFAULT_MODEL_SETTING" "$CODEX_DEFAULT_EFFORT_SETTING" "$CHOSEN_SETTINGS"
    '''
    def reload(reset):
        done=subprocess.run(['/bin/bash','-c',code,'test',str(target/'env.sh'),reset],
            env={'HOME':str(tmp_path),'PATH':os.environ['PATH']},capture_output=True,text=True,timeout=10)
        assert done.returncode==0,done.stderr
        return done.stdout.splitlines()
    fresh_reloaded=['fresh',values['FRESH_CONTEXT_SETTING'],values['FRESH_SOCKET_SETTING']]
    assert reload('0')==fresh_reloaded+['gpt-fixture-terra','high',codex['CHOSEN_SETTINGS']]
    # --reset-settings drops the chosen Codex defaults but never the fresh selection.
    assert reload('1')==fresh_reloaded+['','','']
    # Rendering again from the reloaded values reproduces every output byte for byte.
    reloaded=dict(codex,FRESH_CONTEXT_SETTING=values['FRESH_CONTEXT_SETTING'],FRESH_SOCKET_SETTING=values['FRESH_SOCKET_SETTING'])
    _fresh_render(tmp_path,*functions,commands=commands,overrides=reloaded)
    for path in outputs:
        assert path.read_bytes()==first[path],path.name


def test_normal_update_keeps_fresh_selection_without_old_bootstrap_or_enrollment(tmp_path):
    import os
    target=tmp_path/'installed';target.mkdir()
    saved=target/'env.sh';saved.write_text("export AGENTSTACK_RUNTIME_MODE=fresh\nexport AGENTSTACK_FRESH_CLIENT_CONTEXT=/private/prepared/context.json\nexport AGENTSTACK_FRESH_DASHBOARD_SOCKET=/private/prepared/tmux.sock\n")
    functions='\n'.join(_installer_function(n) for n in ('setting_listed','setting_previously_chosen','resolve_setting','preflight_agent_mail_port','ensure_native_agentstack_mail'))
    code='set -eu\n. '+shlex.quote(str(ROOT/'hooks/project-context.sh'))+'\n'+functions+'\n'+r'''
    SETTINGS_ENV_FILE="$1"; OPTION_GIVEN=''; PREVIOUS_CHOSEN_RECORDED=false; PREVIOUS_CHOSEN=''; CHOSEN_SETTINGS=''; RESET_SETTINGS=1
    RUNTIME_MODE_SETTING=''; FRESH_CONTEXT_SETTING=''; FRESH_SOCKET_SETTING=''
    resolve_setting RUNTIME_MODE_SETTING AGENTSTACK_RUNTIME_MODE legacy kept
    resolve_setting FRESH_CONTEXT_SETTING AGENTSTACK_FRESH_CLIENT_CONTEXT '' kept
    resolve_setting FRESH_SOCKET_SETTING AGENTSTACK_FRESH_DASHBOARD_SOCKET '' kept
    [[ "$RUNTIME_MODE_SETTING" == fresh && "$FRESH_CONTEXT_SETTING" == /private/prepared/context.json && "$FRESH_SOCKET_SETTING" == /private/prepared/tmux.sock ]]
    AGENTSTACK_RUNTIME_MODE=fresh; NATIVE_MAIL_EXISTING=false; DRY_RUN=false
    preflight_agent_mail_port # Must not probe the old/default port.
    ensure_native_mail_candidate() { printf 'candidate\n'; }
    write_fresh_mail_runner() { printf 'fresh-runner\n'; }
    native_mailctl() { [[ "$3" == start ]]; printf 'owned-start\n'; }
    initialize_native_mail_state() { exit 90; }
    write_native_mail_env() { exit 91; }
    NATIVE_MAIL_ENV=private-env; NATIVE_MAIL_RUNNER=private-runner
    ensure_native_agentstack_mail
    '''
    done=subprocess.run(['/bin/bash','-c',code,'test',str(saved)],env={'HOME':str(tmp_path),'PATH':os.environ['PATH']},capture_output=True,text=True,timeout=10)
    assert done.returncode==0,done.stderr
    assert done.stdout.splitlines()==['candidate','fresh-runner','owned-start']


def test_fresh_keep_reuses_owned_render_and_records_its_actual_candidate(tmp_path):
    import sys,json
    from pathlib import Path
    target,values=_fresh_render(tmp_path,'fresh_environment_json','write_env_file','render_native_mail_runner',
        commands='write_env_file\nrender_native_mail_runner\n')
    (target/'candidate/bin/python').symlink_to(sys.executable)
    commands=r'''
    native_mail_render_id() { printf 'new-render-id'; }
    mcp_endpoint_listening() { return 0; }
    native_mailctl() { [[ "$1" == "$MAIL_ENV" && "$2" == "$NATIVE_MAIL_RUNNER" && "$3" == status ]]; }
    die() { printf '%s\n' "$*" >&2; exit 1; }
    . "$REPO_ROOT/hooks/project-context.sh"
    MAIL_UPDATE_MODE=keep
    NATIVE_MAIL_VENV=must-not-record-new-candidate
    resolve_fresh_mail_connection
    [[ "$NATIVE_MAIL_EXISTING" == true && "$PROVISION_NATIVE_MAIL" == false && "$NATIVE_MAIL_VENV" == "$INSTALL_DIR/candidate" ]]
    [[ "$NATIVE_MAIL_ENROLL_AVAILABLE" == false && -z "$NATIVE_MAIL_ENROLL_BIN" ]]
    MAIL_UPDATE_MODE=update
    if (resolve_fresh_mail_connection) 2> "$INSTALL_DIR/refusal"; then exit 99; fi
    grep -qx FRESH_MAIL_UPDATE_REQUIRES_OPERATOR_STOP "$INSTALL_DIR/refusal"
    '''
    _fresh_render(tmp_path,'fresh_environment_json','resolve_fresh_mail_connection',commands=commands)


async def _normal_startup_layout(tmp_path,private_socket,ports):
    """Shared setup: actual prepare/payload/render, no service or OS manager."""
    import json,os,sys
    from uuid import uuid4
    from agentstack_mail.fresh_client import FreshBoundClient,_http,startup_projection
    from agentstack_mail.fresh_runtime import FreshRuntime
    from agentstack_mail.fresh_server import build_fresh_server
    from test_gemini_core_install_boundary import _run_install_payload
    assert len(ports)==3 and len(set(ports))==3 and all(1024<=p<=65535 and p not in {8765,8770,8781,8791} for p in ports)
    tmp_path.chmod(0o700)
    runtime=await FreshRuntime.initialize(tmp_path/'prepared-state',mode='fresh')
    try:
        person=await runtime.register_local(request_id=str(uuid4()),name='NormalHost',program='fixture',model='fixture')
        server=build_fresh_server(core=runtime.core)
        async def wire(endpoint,tool,args):return await _http(server,tool,args)
        clients=[]
        for _ in range(2):
            clients.append(await FreshBoundClient.prepare(runtime.root/'config.json',person['binding_path'],
                'http://127.0.0.1:'+str(ports[0])+'/mcp',client_id=str(uuid4()),dispatch=wire))
        contexts=[startup_projection(client.path) for client in clients]
        instance=runtime.config['instance_id'];config=runtime.root/'config.json'
    finally:await runtime.close()
    target=tmp_path/'installed'
    for _ in range(2):
        result=_run_install_payload(target)
        assert result.returncode==0,'private payload failed'
    overrides=dict(PORT=str(ports[1]),FRESH_CONTEXT_SETTING=contexts[0]['context_path'],
        FRESH_SOCKET_SETTING=str(private_socket),AGENTSTACK_FRESH_SERVER_CONFIG=str(config),
        ORRERY_FRESH_MAIL_INSTANCE_ID=instance,AGENTSTACK_MCP_URL=contexts[0]['endpoint'],MCP_URL=contexts[0]['endpoint'],
        NATIVE_MAIL_STATE_ROOT=str(config.parent),MAIL_DB=str(config.parent/'storage.sqlite3'),
        MAIL_HOME=str(tmp_path/'unused-mail-home'),SIGNALS_DIR=str(tmp_path/'unused-signals'),
        DASHBOARD_LOG=str(target/'runtime/dashboard.log'),DASHBOARD_LOG_MAX_BYTES='1048576',
        DASHBOARD_LOG_BACKUPS='1',DASHBOARD_RESTART_DELAY='1',
        MAIL_WATCHER_LABEL='org.orrery.test.normal.watcher',MAIL_LAUNCHD_LABEL_SETTING='org.orrery.test.normal.mail',
        AGENT_MAIL_NAME_CAPABILITY_JSON='{}',SAFE_MERGE_RESULT_FILE=str(tmp_path/'no-settings'),
        MCP_MERGE_RESULT_FILE=str(tmp_path/'no-mcp'))
    functions=('fresh_environment_json','prepare_fresh_install','write_env_file','write_fresh_mail_runner',
        'render_native_mail_runner','render_launchd_plist','render_systemd_unit',
        'mail_watcher_environment','render_mail_watcher_unit','write_manifest')
    commands='prepare_fresh_install\nwrite_fresh_mail_runner\nwrite_env_file\nrender_launchd_plist\nrender_systemd_unit\nrender_mail_watcher_unit launchd\nrender_mail_watcher_unit systemd-user\nwrite_manifest manual "" "" ""\n'
    made,values=_fresh_render(tmp_path,*functions,commands=commands,overrides=overrides)
    assert made==target
    candidate=target/'candidate/bin'
    # This fixture borrows an existing approved dev venv; it does not build
    # a candidate venv. A lone Python symlink loses that venv's prefix.
    python=candidate/'python'
    python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');python.chmod(0o700)
    service=candidate/'agentstack-mail-service'
    service.write_text('#!/bin/bash\nexec '+shlex.quote(sys.executable)+' -m agentstack_mail.service "$@"\n');service.chmod(0o700)
    # A fake uname scopes the controller to its existing supervised process
    # path. No real launchd/systemd job is queried, loaded or stopped.
    fakebin=tmp_path/'private-bin';fakebin.mkdir()
    uname=fakebin/'uname';uname.write_text('#!/bin/sh\nprintf "Linux\\n"\n');uname.chmod(0o700)
    saved=_normal_saved_env(target/'env.sh')
    env={k:v for k,v in os.environ.items() if not k.startswith(('AGENTSTACK_','ORRERY_','TMUX'))}
    env.update(HOME=str(tmp_path),PATH=str(fakebin)+':'+os.environ['PATH'],AGENTSTACK_HOME=str(target),
        AGENTSTACK_MAIL_START_GRACE='20',AGENTSTACK_MAIL_HEALTH_GRACE='10')
    layout=dict(tmp=tmp_path,target=target,contexts=contexts,instance=instance,config=config,
        ports=ports,values=values,saved=saved,env=env,service=service,
        mailctl=target/'bin/agentstack-mailctl',mail_pidfile=target/'mail/runtime/agentstack-mail.pid')
    layout['candidate_python']=_normal_candidate_check(layout)
    return layout


def _normal_candidate_check(layout):
    """Exercise the exact server-executable entry, without starting a service."""
    import json,sys
    entry=layout['target']/'candidate/bin/python'
    code='''import importlib,json,sys
deps={}
for name in ('sqlmodel','sqlalchemy','fastmcp','agentstack_mail.fresh_cli'):
    try:importlib.import_module(name);deps[name]=True
    except Exception:deps[name]=False
print(json.dumps(dict(sys_executable=sys.executable,sys_prefix=sys.prefix,dependencies=deps,
    arguments_preserved=sys.argv[1:]==['fixture argument with spaces','--literal=$value'])))
'''
    done=subprocess.run([str(entry),'-c',code,'fixture argument with spaces','--literal=$value'],
        env=layout['env'],cwd=layout['tmp'],capture_output=True,text=True,timeout=15)
    proof=dict(entry_regular=entry.is_file() and not entry.is_symlink(),import_returncode=done.returncode)
    try:
        assert done.returncode==0,'candidate Python probe failed'
        proof.update(json.loads(done.stdout))
        assert proof['sys_executable']==sys.executable and proof['sys_prefix']==sys.prefix
        assert proof['entry_regular'] and proof['arguments_preserved'] and all(proof['dependencies'].values())
        exited=subprocess.run([str(entry),'-c','raise SystemExit(23)'],env=layout['env'],cwd=layout['tmp'],
            capture_output=True,text=True,timeout=5)
        proof['exit_returncode']=exited.returncode
        assert exited.returncode==23,'candidate Python exit code changed'
    finally:(layout['tmp']/'candidate-python-proof.json').write_text(json.dumps(proof,indent=2)+'\n')
    return proof


def _normal_saved_env(path):
    result={}
    for line in path.read_text().splitlines():
        words=shlex.split(line,comments=True)
        if len(words)==2 and words[0]=='export':
            key,value=words[1].split('=',1);result[key]=value
    return result


def _normal_assert_layout(layout):
    """The same assertions are used before fixing and during actual acceptance."""
    import json,plistlib
    target=layout['target'];saved=layout['saved'];context=layout['contexts'][0]
    assert saved['AGENTSTACK_RUNTIME_MODE']=='fresh'
    for key,value in dict(AGENTSTACK_HOME=str(target),AGENTSTACK_FRESH_CLIENT_CONTEXT=context['context_path'],
        AGENTSTACK_FRESH_SERVER_CONFIG=str(layout['config']),ORRERY_FRESH_MAIL_INSTANCE_ID=layout['instance'],
        AGENTSTACK_MCP_URL=context['endpoint'],AGENTSTACK_PORT=str(layout['ports'][1])).items():assert saved[key]==value
    manifest=json.loads((target/'manifest.json').read_text())
    for key in ('AGENTSTACK_HOME','AGENTSTACK_RUNTIME_MODE','AGENTSTACK_FRESH_CLIENT_CONTEXT',
                'AGENTSTACK_FRESH_DASHBOARD_SOCKET','AGENTSTACK_FRESH_SERVER_CONFIG','ORRERY_FRESH_MAIL_INSTANCE_ID'):
        assert manifest['env'][key]==saved[key]
    plist=plistlib.loads((layout['tmp']/'Library/LaunchAgents/org.orrery.test.normal.dashboard.plist').read_bytes())
    for key in ('AGENTSTACK_FRESH_CLIENT_CONTEXT','AGENTSTACK_FRESH_DASHBOARD_SOCKET','ORRERY_FRESH_MAIL_INSTANCE_ID'):
        assert plist['EnvironmentVariables'][key]==saved[key]
    watcher=plistlib.loads((layout['tmp']/'Library/LaunchAgents/org.orrery.test.normal.watcher.plist').read_bytes())
    assert watcher['ProgramArguments'][:3]==['/usr/bin/env','-u','AGENTSTACK_FRESH_CLIENT_CONTEXT']
    assert watcher['ProgramArguments'][-2:]==['--fresh-config',str(layout['config'])]
    assert 'AGENTSTACK_FRESH_CLIENT_CONTEXT' not in watcher['EnvironmentVariables']
    assert not (layout['config'].parent/'archive').exists()
    assert layout['contexts'][0]['mail_instance_id']==layout['contexts'][1]['mail_instance_id']
    assert layout['contexts'][0]['context_digest']!=layout['contexts'][1]['context_digest']
    assert (target/'bin/agentstack-mailctl').read_bytes()==(ROOT/'bin/agentstack-mailctl').read_bytes()
    assert (target/'dashboard/agentctl.sh').read_bytes()==(ROOT/'dashboard/agentctl.sh').read_bytes()
    assert saved['AGENTSTACK_DASHBOARD_LOG']==str(target/'runtime/dashboard.log')
    assert layout['candidate_python']['exit_returncode']==23
    line=next(line.strip().removesuffix(' &') for line in (target/'mail/renders/run/run-agentstack-mail.sh').read_text().splitlines() if ' foreground ' in line)
    argv=shlex.split(line)
    assert argv[argv.index('--server-executable')+1]==str(target/'candidate/bin/python')
    for key in ('AGENTSTACK_DASHBOARD_LOG_MAX_BYTES','AGENTSTACK_DASHBOARD_LOG_BACKUPS','AGENTSTACK_DASHBOARD_RESTART_DELAY'):
        assert int(saved[key])>0


def _normal_dashboard_environment(layout,context,port,label):
    import sys
    runtime=layout['target']/'runtime'/label
    env=layout['env']|layout['saved']|dict(AGENTSTACK_PYTHON=sys.executable,
        AGENTSTACK_DASHBOARD_SELF_RESTART='1',AGENTSTACK_FRESH_CLIENT_CONTEXT=context['context_path'],
        AGENTSTACK_PORT=str(port),AGENTSTACK_RUNTIME_DIR=str(runtime),
        AGENTSTACK_DASHBOARD_LOG=str(runtime/'dashboard.log'),
        AGENTSTACK_DASHBOARD_RUN_STATE=str(runtime/'service.json'))
    env.pop('TMUX',None);env.pop('TMUX_PANE',None)
    return env


def test_normal_startup_setup_uses_actual_payload_render_and_separates_contexts(tmp_path):
    """Telemetry alone: always runs, without a cockpit checkout."""
    import asyncio,socket
    async def scenario():
        # This is a stat-only socket witness for the mock. It is not a tmux
        # server and is never reported as actual transport acceptance.
        witness=socket.socket(socket.AF_UNIX)
        path=tmp_path/'mock-socket';witness.bind(str(path));path.chmod(0o600)
        try:
            layout=await _normal_startup_layout(tmp_path,path,[19876,19877,19879])
            _normal_assert_layout(layout)
            a,b=(_normal_dashboard_environment(layout,row,19877+i,'dashboard-'+str(i))
                for i,row in enumerate(layout['contexts']))
            for key in ('AGENTSTACK_RUNTIME_DIR','AGENTSTACK_DASHBOARD_LOG','AGENTSTACK_DASHBOARD_RUN_STATE'):
                assert a[key]!=b[key] and a[key].startswith(str(layout['target']/'runtime')+'/')
            assert a['AGENTSTACK_FRESH_CLIENT_CONTEXT']==layout['contexts'][0]['context_path']
            assert b['AGENTSTACK_FRESH_CLIENT_CONTEXT']==layout['contexts'][1]['context_path']
            # The actual shared assertion rejects missing references rather than
            # accepting a hand-made proof of an unrelated setup.
            altered=dict(layout,saved=layout['saved']|{'ORRERY_FRESH_MAIL_INSTANCE_ID':'b'*32})
            import pytest
            with pytest.raises(AssertionError):_normal_assert_layout(altered)
        finally:witness.close();path.unlink(missing_ok=True)
    asyncio.run(scenario())


def test_normal_startup_external_setup_uses_actual_payload_render_and_saved_loader(tmp_path,monkeypatch):
    """The cockpit startup loader against the same setup; needs a cockpit checkout."""
    import asyncio,importlib.util,os,socket
    from pathlib import Path
    import pytest
    c=Path(os.environ.get('ORRERY_TEST_COCKPIT_ROOT',''))
    if not os.environ.get('ORRERY_TEST_COCKPIT_ROOT') or not (c/'bridge/runtime_target.py').is_file():
        pytest.skip('ORRERY_TEST_COCKPIT_ROOT must name a cockpit checkout')
    async def scenario():
        witness=socket.socket(socket.AF_UNIX)
        path=tmp_path/'mock-socket';witness.bind(str(path));path.chmod(0o600)
        try:
            layout=await _normal_startup_layout(tmp_path,path,[19876,19877,19879])
            _normal_assert_layout(layout)
            spec=importlib.util.spec_from_file_location('normal_startup_mock_boundary',c/'bridge/runtime_target.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            class Host:
                def __init__(self,*args,**kw):
                    context=next(row for row in layout['contexts'] if row['context_path']==kw['environ']['AGENTSTACK_FRESH_CLIENT_CONTEXT'])
                    self.instance=layout['instance'];self.host_pin=dict(context_digest=context['context_digest'],epoch='e'*64)
                def call(self,value):assert value=={'operation':'catalog'}
            monkeypatch.setattr(module,'FreshTargetBoundary',Host)
            env={k:v for k,v in layout['env'].items() if not k.startswith(('AGENTSTACK_','ORRERY_'))}
            env['HOME']=str(tmp_path)
            config=dict(telemetry_home=str(layout['target']))
            original=module.startup_projection(environ=env,config=config)
            assert original['identity']['mail_instance_id']==layout['instance']
            assert original['identity']['context_digest']==layout['contexts'][0]['context_digest']
            changed=module.startup_projection(environ=env|{'AGENTSTACK_FRESH_CLIENT_CONTEXT':layout['contexts'][1]['context_path']},config=config)
            assert changed['identity']!=original['identity'] and changed['identity']['host_epoch']==original['identity']['host_epoch']
        finally:witness.close();path.unlink(missing_ok=True)
    asyncio.run(scenario())


class _NormalOwnedProcesses:
    def __init__(self,folder,*,start=None):
        import subprocess
        self.folder=folder;self.start=start or subprocess.Popen;self.entries=[]
    def spawn(self,label,argv,env):
        import subprocess
        logfile=self.folder/(label+'-'+str(len(self.entries))+'.log');stream=logfile.open('w')
        try:
            process=self.start(argv,env=env,cwd=self.folder,stdin=subprocess.DEVNULL,
                stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        except BaseException:stream.close();raise
        entry=dict(label=label,process=process,stream=stream,stopped=False)
        self.entries.append(entry);return entry
    def stop(self,entry):
        import os,signal,subprocess
        if entry['stopped']:return
        p=entry['process']
        if p.poll() is None:p.terminate()
        try:p.wait(timeout=12)
        except subprocess.TimeoutExpired:
            # Only a session leader created by this fixture may be group-killed.
            assert os.getpgid(p.pid)==p.pid,'owned process group changed'
            os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=5)
        assert p.poll() is not None,'owned process did not stop'
        entry['stopped']=True;entry['stream'].close()
    def close(self):
        failures=[]
        for entry in reversed(self.entries):
            try:self.stop(entry)
            except BaseException as exc:failures.append(type(exc).__name__)
        assert not failures,'owned cleanup failed'


def _normal_cleanup(proof,steps,save):
    """Attempt every owned cleanup even after a preceding cleanup fails."""
    failures=[]
    try:
        for stage,step in steps:
            try:step()
            except BaseException as exc:failures.append(dict(stage=stage,type=type(exc).__name__))
    finally:
        proof['cleanup_failures']=failures
        save()
    assert not failures,'private normal-startup cleanup failed'


def _normal_runtime_identity(body,instance,context_digest,epoch=None):
    value=body['runtime']
    assert set(value)=={'runtime_mode','mail_instance_id','context_digest','host_epoch'}
    assert value['runtime_mode']=='fresh' and value['mail_instance_id']==instance
    assert value['context_digest']==context_digest
    assert isinstance(value['host_epoch'],str) and re.fullmatch('[a-f0-9]{64}',value['host_epoch'])
    if epoch is not None:assert value['host_epoch']==epoch
    return value


def test_normal_startup_owned_process_cleanup_and_exact_health_checks(tmp_path):
    import pytest
    class Process:
        def __init__(self):self.code=None;self.terminated=0;self.waited=0
        def poll(self):return self.code
        def terminate(self):self.terminated+=1;self.code=-15
        def wait(self,**kwargs):self.waited+=1;return self.code
    made=[]
    def start(argv,**kwargs):
        assert kwargs['start_new_session'] is True
        process=Process();made.append(process);return process
    owned=_NormalOwnedProcesses(tmp_path,start=start)
    owned.spawn('one',['private'],{});owned.spawn('two',['private'],{})
    owned.close();owned.close()
    assert [p.terminated for p in made]==[1,1] and all(e['stopped'] for e in owned.entries)
    body={'runtime':dict(runtime_mode='fresh',mail_instance_id='a'*32,context_digest='d'*64,host_epoch='e'*64)}
    assert _normal_runtime_identity(body,'a'*32,'d'*64,'e'*64)==body['runtime']
    for key in ('runtime_mode','mail_instance_id','context_digest','host_epoch'):
        changed={'runtime':body['runtime']|{key:'other'}}
        with pytest.raises(AssertionError):_normal_runtime_identity(changed,'a'*32,'d'*64,'e'*64)
    for omitted in body['runtime']:
        with pytest.raises(AssertionError):_normal_runtime_identity({'runtime':{k:v for k,v in body['runtime'].items() if k!=omitted}},'a'*32,'d'*64)
    proof={};calls=[]
    def failed():calls.append('first');raise RuntimeError('not copied into proof')
    def last():calls.append('last')
    with pytest.raises(AssertionError):
        _normal_cleanup(proof,[('first',failed),('last',last)],lambda:calls.append('save'))
    assert calls==['first','last','save']
    assert proof['cleanup_failures']==[dict(stage='first',type='RuntimeError')]


def test_external_normal_startup_private_mail_dashboard_and_saved_cockpit(tmp_path):
    """External operator only: regular entrypoints, no OS manager/live install."""
    import asyncio,json,os,plistlib,runpy,shutil,socket,sys,tempfile,threading,time
    import http.server,urllib.request
    from pathlib import Path
    import pytest
    if os.environ.get('ORRERY_EXTERNAL_NORMAL_STARTUP')!='1':pytest.skip('external normal-startup acceptance only')
    cockpit=Path(os.environ.get('ORRERY_TEST_COCKPIT_ROOT',''))
    if not os.environ.get('ORRERY_TEST_COCKPIT_ROOT') or not (cockpit/'bridge').is_dir():
        pytest.skip('ORRERY_TEST_COCKPIT_ROOT must name a cockpit checkout')
    tmp_path.chmod(0o700)
    private=Path(tempfile.mkdtemp(prefix='o213-normal-tmux-',dir='/private/tmp'));private.chmod(0o700)
    socket_path=private/'tmux.sock';tmux=shutil.which('tmux');assert tmux,'tmux missing'
    # Every query/stop below names this exact -S socket. No default/live server.
    prefix=[tmux,'-u','-S',str(socket_path)]
    reserved=[]
    for _ in range(4):
        s=socket.socket();s.bind(('127.0.0.1',0));reserved.append(s)
    ports=[s.getsockname()[1] for s in reserved]
    for s in reserved:s.close()
    assert len(set(ports))==4 and not set(ports)&{8765,8770,8781,8791}
    proof=dict(last_stage='private-start',real_os_managers=False,finder_gui=False,
        wheel_or_live_install=False,real_provider=False,mail_control={},processes_stopped=False,private_socket_stopped=False)
    owned=_NormalOwnedProcesses(tmp_path);layout=None;foreign=None;foreign_thread=None;env_bytes=None
    def command(argv,env,timeout=35):
        return subprocess.run(argv,env=env,cwd=tmp_path,stdin=subprocess.DEVNULL,
            capture_output=True,text=True,timeout=timeout,start_new_session=True)
    def rc(stage,argv,env,expected=0,timeout=35):
        proof['last_stage']=stage
        started=time.monotonic()
        result=command(argv,env,timeout)
        proof.setdefault('commands',[]).append(dict(stage=stage,returncode=result.returncode,elapsed_seconds=time.monotonic()-started))
        assert result.returncode==expected,stage+' returncode mismatch'
        return result
    def wait_json(url,entry,check,timeout=20):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            assert entry['process'].poll() is None,'owned startup process exited'
            try:
                with urllib.request.urlopen(url,timeout=.5) as response:value=json.loads(response.read(1<<18))
                if check(value):return value
            except (OSError,ValueError):pass
            time.sleep(.05)
        raise AssertionError('private startup response timed out')
    def rewrite_context(context):
        data=layout['saved']|{'AGENTSTACK_FRESH_CLIENT_CONTEXT':context['context_path']}
        (layout['target']/'env.sh').write_text('\n'.join('export '+k+'='+shlex.quote(v) for k,v in data.items())+'\n')
    try:
        env={k:v for k,v in os.environ.items() if k not in {'TMUX','TMUX_PANE'}}
        rc('private-tmux',[*prefix,'new-session','-d','-s','orrery-normal-fixture','/bin/sleep','300'],env)
        layout=asyncio.run(_normal_startup_layout(tmp_path,socket_path,ports[:3]));_normal_assert_layout(layout)
        env_bytes=(layout['target']/'env.sh').read_bytes()
        proof.update(real_payload_twice=True,real_render_and_manifest=True,prepared_identity_preserved=True,
            candidate_python=layout['candidate_python'])
        ctl=['/bin/bash',str(layout['mailctl'])]
        for action in ('start','status','restart','status'):
            rc('mail-'+action,[*ctl,action],layout['env'],timeout=60)
            proof['mail_control'][action]=True
        service_argv=[str(layout['service']),'foreground','--server-executable',str(layout['target']/'candidate/bin/python'),
            '--env-file',layout['values']['NATIVE_MAIL_ENV'],'--state-root',str(layout['config'].parent),
            '--fresh-config',str(layout['config']),'--fresh-port',str(ports[0])]
        duplicate=rc('mail-lock-refusal',service_argv,layout['env'],expected=1)
        assert 'another service process owns this state root' in duplicate.stderr
        proof['same_authority_lock_refused_duplicate']=True
        watcher=plistlib.loads((tmp_path/'Library/LaunchAgents/org.orrery.test.normal.watcher.plist').read_bytes())
        watcher_env=layout['env']|watcher['EnvironmentVariables']|{'AGENTSTACK_FRESH_CLIENT_CONTEXT':'must-remove-key'}
        rc('watcher-config-only',[*watcher['ProgramArguments'],'--once'],watcher_env)
        proof['actual_watcher_once_without_client_context']=True
        def start_dashboard(label,context,port):
            env=_normal_dashboard_environment(layout,context,port,label)
            entry=owned.spawn(label,[sys.executable,str(layout['target']/'dashboard/service_runner.py')],env)
            wait_json('http://127.0.0.1:'+str(port)+'/api/version',entry,
                lambda value:value.get('api')==34 and value.get('runtime_mode')=='fresh' and value.get('mail_instance_id')==layout['instance'])
            return entry
        proof['last_stage']='dashboard-and-host'
        dash=start_dashboard('dashboard-a',layout['contexts'][0],ports[1])
        dash_b=start_dashboard('dashboard-b',layout['contexts'][1],ports[3])
        settings=tmp_path/'.orrery';settings.mkdir(mode=0o700)
        (settings/'config.json').write_text(json.dumps(dict(orrery_root=str(cockpit),telemetry_home=str(layout['target']))))
        cenv={k:v for k,v in layout['env'].items() if not k.startswith(('AGENTSTACK_','ORRERY_'))}
        cenv.update(HOME=str(tmp_path),PORT=str(ports[2]),ORRERY_NO_UPDATE_CHECK='1',ORRERY_PYTHON=sys.executable,
            ORRERY_VENV=str(tmp_path/'must-not-create-venv'),PIP_NO_INDEX='1')
        assert not any(k.startswith('AGENTSTACK_') for k in cenv)
        def startup_identity():
            value=rc('saved-loader',[sys.executable,str(cockpit/'bridge/runtime_target.py'),'--startup-json'],cenv)
            return json.loads(value.stdout)['identity']
        expected=startup_identity()
        assert expected['mail_instance_id']==layout['instance'] and expected['context_digest']==layout['contexts'][0]['context_digest']
        def start_cockpit():
            entry=owned.spawn('cockpit',[sys.executable,str(cockpit/'bridge/orrery_backend.py'),'--host','127.0.0.1','--port',str(ports[2])],cenv)
            body=wait_json('http://127.0.0.1:'+str(ports[2])+'/telemetry/health',entry,lambda v:v.get('runtime')==expected)
            _normal_runtime_identity(body,layout['instance'],expected['context_digest'],expected['host_epoch']);return entry
        cbackend=start_cockpit()
        script=['/bin/bash',str(cockpit/'scripts/start-cockpit.sh')]
        same=rc('same-backend-reuse',script,cenv)
        assert 'already running' in same.stdout
        proof['saved_only_cockpit_and_same_pin_reuse']=True
        rewrite_context(layout['contexts'][1]);changed=startup_identity()
        assert changed['mail_instance_id']==expected['mail_instance_id'] and changed['context_digest']!=expected['context_digest']
        rc('same-instance-other-context-refused',script,cenv,expected=1)
        assert cbackend['process'].poll() is None
        proof['same_instance_other_context_no_spawn_no_stop']=True
        (layout['target']/'env.sh').write_bytes(env_bytes)
        owned.stop(dash)
        dash=start_dashboard('dashboard-a-restarted',layout['contexts'][0],ports[1])
        renewed=startup_identity()
        assert renewed['context_digest']==expected['context_digest'] and renewed['host_epoch']!=expected['host_epoch']
        rc('old-epoch-refused',script,cenv,expected=1)
        assert cbackend['process'].poll() is None
        proof['dashboard_restart_old_epoch_no_spawn_no_stop']=True
        owned.stop(cbackend);expected=renewed;cbackend=start_cockpit()
        rc('same-new-epoch-reuse',script,cenv)
        owned.stop(cbackend)
        class Foreign(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body=json.dumps(dict(backend='ok',boot='foreign',runtime={'runtime_mode':'legacy'})).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        foreign=http.server.ThreadingHTTPServer(('127.0.0.1',ports[2]),Foreign);foreign.daemon_threads=True
        foreign_thread=threading.Thread(target=foreign.serve_forever,daemon=True);foreign_thread.start()
        rc('foreign-listener-refused',script,cenv,expected=1)
        with urllib.request.urlopen('http://127.0.0.1:'+str(ports[2])+'/telemetry/health',timeout=2) as response:
            assert json.loads(response.read())['boot']=='foreign'
        proof['foreign_listener_no_spawn_no_stop']=True
        assert not (tmp_path/'must-not-create-venv').exists()
        proof['last_stage']='accepted'
    finally:
        def close_foreign():
            if foreign is not None:foreign.shutdown();foreign.server_close()
            if foreign_thread is not None:foreign_thread.join(3);assert not foreign_thread.is_alive()
        def restore_settings():
            if layout is not None and env_bytes is not None:(layout['target']/'env.sh').write_bytes(env_bytes)
        def close_owned():owned.close();proof['processes_stopped']=True
        def stop_mail():
            if layout is not None:
                stopped=command(['/bin/bash',str(layout['mailctl']),'stop'],layout['env'],timeout=60)
                proof['mail_control']['stop']=stopped.returncode==0
                proof['mail_pidfile_absent']=not layout['mail_pidfile'].exists()
                log=layout['target']/'mail/runtime/agentstack-mail.log'
                text=log.read_text() if log.exists() else ''
                started=re.findall(r'Started server process \[(\d+)\]',text)
                finished=re.findall(r'Finished server process \[(\d+)\]',text)
                proof['owned_server_started_finished_match']=bool(started) and sorted(started)==sorted(finished)
        def stop_tmux():
            command([*prefix,'kill-server'],{k:v for k,v in os.environ.items() if k not in {'TMUX','TMUX_PANE'}})
            remains=command([*prefix,'list-sessions'],{k:v for k,v in os.environ.items() if k not in {'TMUX','TMUX_PANE'}})
            proof['private_socket_stopped']=remains.returncode!=0
        _normal_cleanup(proof,[('foreign',close_foreign),('settings',restore_settings),
            ('processes',close_owned),('mail',stop_mail),('tmux',stop_tmux)],
            lambda:(tmp_path/'normal-startup-proof.json').write_text(json.dumps(proof,indent=2)+'\n'))
    assert proof['last_stage']=='accepted'
    assert proof['processes_stopped'] and proof['private_socket_stopped']
    assert proof['mail_control']['stop'] and proof['mail_pidfile_absent'] and proof['owned_server_started_finished_match']
