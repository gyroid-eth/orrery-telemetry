"""Real compatibility Mail HTTP -> both editing guards, with isolated state."""
import json
import hashlib
import os
from pathlib import Path
import socket
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
import subprocess
import sys
import time
import urllib.request
import pytest

ROOT=Path(__file__).resolve().parents[1]


def rpc(url,method,params):
    request=urllib.request.Request(url,data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),
        headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:return json.load(response)


def call(service,tool_name,**arguments):
    document=rpc(service['url'],'tools/call',{'name':tool_name,'arguments':arguments})
    assert 'error' not in document,document
    assert not document['result'].get('isError'),document
    return document['result']['structuredContent']


def clean_env():
    return {k:v for k,v in os.environ.items() if not k.startswith(('AGENTSTACK_','MCP_AGENT_'))
        and k not in ('TMUX','TMUX_PANE','CODEX_HOME','CLAUDE_CONFIG_DIR')}


@pytest.fixture(scope='module')
def mail(tmp_path_factory):
    root=tmp_path_factory.mktemp('reservation-http');home=root/'home';home.mkdir()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    env=clean_env();env.update(HOME=str(home),PYTHONPATH=str(ROOT/'packages/agentstack_mail/src'),
        AGENTSTACK_LABEL_PREFIX='org.agentstack.test.reservation-http',
        AGENTSTACK_MAIL_ENV_FILE=str(root/'missing.env'),AGENTSTACK_MAIL_DATABASE_URL=f'sqlite+aiosqlite:///{root / "mail.sqlite3"}',
        AGENTSTACK_MAIL_STORAGE_ROOT=str(root/'archive'),AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=str(root/'signals'),
        AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE='passthrough',AGENTSTACK_MAIL_TOOLS_LOG_ENABLED='false',AGENTSTACK_MAIL_LOG_RICH_ENABLED='false')
    service=dict(url=f'http://127.0.0.1:{port}/mcp',db=root/'mail.sqlite3',root=root)
    launcher=root/'serve.py'
    launcher.write_text('import time\nfrom pathlib import Path\nfrom agentstack_mail import app\nfrom agentstack_mail.cli import main\noriginal=app._patterns_overlap\n'
        + 'gate=Path('+repr(str(root/'pause'))+')\n'
        + 'def overlap(a,b):\n if gate.exists() and a=="foreign-only/**":\n  gate.with_name("reached").touch()\n  deadline=time.monotonic()+8\n  while gate.exists() and time.monotonic()<deadline: time.sleep(.01)\n return original(a,b)\napp._patterns_overlap=overlap\nmain()\n')
    with (root/'server.log').open('w') as log:
        process=subprocess.Popen([sys.executable,str(launcher),'--port',str(port)],env=env,cwd=root,stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+25
            while True:
                try:
                    tools=rpc(service['url'],'tools/list',{})['result']['tools']
                    assert any(t['name']=='renew_file_reservations' for t in tools)
                    break
                except (OSError,ValueError,KeyError):
                    assert process.poll() is None,(root/'server.log').read_text()
                    assert time.monotonic()<deadline,(root/'server.log').read_text()
                    time.sleep(.05)
            yield service
        finally:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=12)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


@pytest.fixture
def reserved(mail,tmp_path):
    project=tmp_path/'project';project.mkdir()
    call(mail,'ensure_project',human_key=str(project))
    for name in ('EditorAlpha','EditorBeta'):
        assert call(mail,'register_agent',project_key=str(project),name=name,program='codex',model='fixture')['name']==name
    assert len(call(mail,'file_reservation_paths',project_key=str(project),agent_name='EditorAlpha',paths=['docs/**'],ttl_seconds=900)['granted'])==1
    return mail,project


def guard(reserved,kind,*,url=None,hook=None,target="docs/note.md",outage="warn-open"):
    service,project=reserved;home=project.parent/'guard-home';home.mkdir(exist_ok=True)
    env=clean_env();env.update(HOME=str(home),AGENTSTACK_HOME=str(ROOT),AGENTSTACK_PYTHON=sys.executable,
        AGENTSTACK_LABEL_PREFIX='org.agentstack.test.reservation-http',AGENTSTACK_PROJECT_KEY=str(project),
        AGENTSTACK_PROTECTED_ROOTS=str(project),AGENTSTACK_RUNTIME_DIR=str(home/'runtime'),AGENT_NAME='EditorAlpha',
        AGENTSTACK_MCP_URL=url or service['url'],AGENTSTACK_MAIL_HTTP_BEARER_MODE='disabled',
        AGENTSTACK_MAIL_OUTAGE_POLICY=outage,FILE_RESERVATION_RETRY_DELAY_SECONDS='0')
    payload={'session_id':'fixture-session','cwd':str(project),'hook_event_name':'PreToolUse'}
    if kind=='claude':
        command=['bash',str(hook or ROOT/'hooks/check-file-reservation.sh')]
        payload.update(tool_name='Edit',tool_input={'file_path':str(project/target)})
    else:
        command=[sys.executable,str(ROOT/'hooks/check-codex-file-reservation.py')]
        payload.update(tool_name='apply_patch',tool_input={'command':'*** Begin Patch\n*** Add File: '+target+'\n+text\n*** End Patch'})
    return subprocess.run(command,input=json.dumps(payload),text=True,capture_output=True,env=env,cwd=project,timeout=20)


@pytest.mark.parametrize('kind',['claude','codex'])
@pytest.mark.parametrize('foreign',['none','active','expired','released','shared','other-path','other-project'])
def test_real_mail_guards_require_owned_and_no_active_foreign_conflict(reserved,kind,foreign):
    service,project=reserved
    if foreign!='none':
        # Only this fixture DB; advisory acquire normally reports this conflict.
        with sqlite3.connect(service['db']) as db:
            pid=db.execute('SELECT id FROM projects WHERE human_key=?',(str(project),)).fetchone()[0]
            aid=db.execute('SELECT id FROM agents WHERE project_id=? AND name=?',(pid,'EditorBeta')).fetchone()[0]
            if foreign=='other-project':
                slug='foreign-'+project.parent.name
                db.execute('INSERT INTO projects(slug,human_key,created_at) VALUES (?,?,?)',(slug,slug,'2026-01-01'))
                pid=db.execute('SELECT id FROM projects WHERE slug=?',(slug,)).fetchone()[0]
            db.execute('INSERT INTO file_reservations(project_id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,released_ts) VALUES (?,?,?,?,?,?,?,?)',
                (pid,aid,'elsewhere/**' if foreign=='other-path' else 'docs/**',foreign!='shared','fixture','2026-01-01',
                 '2000-01-01' if foreign=='expired' else '2999-01-01','2026-01-01' if foreign=='released' else None))
    result=guard(reserved,kind)
    assert result.returncode==(2 if foreign=='active' else 0),result.stderr+result.stdout
    if foreign=='active':assert 'EditorBeta' in result.stderr



@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_managed_nested_launches_share_file_identity_without_false_conflicts(mail, tmp_path, kind):
    repo = tmp_path / "foldera"
    nested = repo / "sub"
    other = tmp_path / "folderb" / "sub"
    nested.mkdir(parents=True)
    other.mkdir(parents=True)
    same_file = nested / "deep" / "note.md"
    different_file = other / "deep" / "note.md"
    same_file.parent.mkdir()
    different_file.parent.mkdir()

    def managed_environment(cwd):
        env = clean_env()
        env.update(
            HOME=str(tmp_path), PROJECT_KEY=str(repo),
            AGENTSTACK_PROJECT_KEY=str(repo),
            AGENTSTACK_EXTRA_PROTECTED_ROOTS="",
            AGENTSTACK_PYTHON=sys.executable,
        )
        context = subprocess.run(
            ["/bin/bash", str(ROOT / "hooks/project-context.sh"),
             "workspace-context-json", str(cwd), str(repo)],
            cwd=cwd, env=env, text=True, capture_output=True, timeout=12,
        )
        assert context.returncode == 0, context.stderr
        env.update(json.loads(context.stdout)["environment"])
        return env

    def managed_key(cwd, target):
        env = managed_environment(cwd)
        script = '. "$1"; reservation_resolve_tool_context "$2" || exit $?; printf "%s" "$REL_PATH"'
        result = subprocess.run(
            ["/bin/bash", "-eu", "-c", script, "test",
             str(ROOT / "hooks/reservation-common.sh"),
             json.dumps({"tool_input": {"file_path": str(target)}})],
            cwd=cwd, env=env, text=True, capture_output=True, timeout=12,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    outer_key = managed_key(repo, same_file)
    inner_key = managed_key(nested, same_file)
    different_key = managed_key(other, different_file)
    assert outer_key == inner_key == str(same_file.resolve())
    assert different_key == str(different_file.resolve())
    assert different_key != inner_key

    call(mail, "ensure_project", human_key=str(repo))
    for agent in ("EditorAlpha", "EditorBeta"):
        call(mail, "register_agent", project_key=str(repo),
             name=agent, program="codex", model="fixture")
    first = call(mail, "file_reservation_paths", project_key=str(repo),
                 agent_name="EditorAlpha", paths=[outer_key], ttl_seconds=900)
    assert len(first["granted"]) == 1, first
    conflict = call(mail, "file_reservation_paths", project_key=str(repo),
                    agent_name="EditorBeta", paths=[inner_key], ttl_seconds=900)
    assert not conflict["granted"] and "EditorAlpha" in str(conflict["conflicts"]), conflict
    separate = call(mail, "file_reservation_paths", project_key=str(repo),
                    agent_name="EditorBeta", paths=[different_key], ttl_seconds=900)
    assert len(separate["granted"]) == 1 and not separate["conflicts"], separate

    def check_edit(cwd, agent, target=same_file):
        env = managed_environment(cwd)
        env.update(
            AGENTSTACK_RUNTIME_DIR=str(tmp_path / ("runtime-" + agent)),
            AGENTSTACK_MCP_URL=mail["url"],
            AGENTSTACK_MAIL_HTTP_BEARER_MODE="disabled",
            FILE_RESERVATION_RETRY_DELAY_SECONDS="0", AGENT_NAME=agent,
        )
        payload = {"session_id": "fixture", "cwd": str(cwd)}
        if kind == "claude":
            command = ["/bin/bash", str(ROOT / "hooks/check-file-reservation.sh")]
            payload.update(tool_name="Edit", tool_input={"file_path": str(target)})
        else:
            command = [sys.executable, str(ROOT / "hooks/check-codex-file-reservation.py")]
            payload.update(tool_name="apply_patch", tool_input={"command":
                "*** Begin Patch\n*** Add File: " + str(target) + "\n+text\n*** End Patch"})
        return subprocess.run(
            command, input=json.dumps(payload),
            text=True, capture_output=True, env=env, cwd=cwd, timeout=15,
        )

    assert check_edit(repo, "EditorAlpha").returncode == 0
    denied = check_edit(nested, "EditorBeta")
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert "EditorAlpha" in denied.stderr
    assert check_edit(other, "EditorBeta", different_file).returncode == 0

    # Ordinary sibling folders survive launch validation and use literal Mail
    # identities through acquire, edit checks, renewal and path-based release.
    wrong = call(mail, "renew_file_reservations", project_key=str(repo),
                 agent_name="EditorAlpha", paths=[different_key])
    assert wrong["renewed"] == 0, wrong
    untouched = call(mail, "release_file_reservations", project_key=str(repo),
                     agent_name="EditorAlpha", paths=[different_key])
    assert untouched["released"] == 0, untouched
    renewed = call(mail, "renew_file_reservations", project_key=str(repo),
                   agent_name="EditorAlpha", paths=[inner_key])
    assert renewed["renewed"] == 1 and not renewed["conflict_check"]["conflicts"], renewed
    released = call(mail, "release_file_reservations", project_key=str(repo),
                    agent_name="EditorAlpha", paths=[inner_key])
    assert released["released"] == 1, released
    assert check_edit(repo, "EditorAlpha").returncode == 2
    assert check_edit(other, "EditorBeta", different_file).returncode == 0


def test_real_renew_carries_checked_scope_and_keeps_resources_unpublished(reserved):
    service,project=reserved;paths=['docs/note.md',str(project/'docs/note.md')]
    result=call(service,'renew_file_reservations',project_key=str(project),agent_name='EditorAlpha',paths=paths)
    assert result['renewed']==1
    assert result['conflict_check']=={'version':1,'project_key':str(project),'paths':paths,'conflicts':[]}
    assert rpc(service['url'],'resources/list',{})['result']['resources']==[]
    assert rpc(service['url'],'resources/templates/list',{})['result']['resourceTemplates']==[]


@pytest.mark.parametrize('kind',['claude','codex'])
@pytest.mark.parametrize('policy,expected',[('block',2),('warn-open',0)])
def test_real_guards_use_outage_policy_for_unreachable_mail(reserved,kind,policy,expected):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    result=guard(reserved,kind,url=f'http://127.0.0.1:{port}/mcp',outage=policy)
    assert result.returncode==expected,result.stderr+result.stdout
    if policy=='block':assert 'AGENT MAIL UNREACHABLE' in result.stderr
    else:assert 'systemMessage' in json.loads(result.stdout)


@pytest.mark.parametrize('pattern,target,expected',[
    ('docs/**/*.md','docs/note.md',2),('docs/**/*.md','docs/deep/note.md',2),
    ('docs/','docs/note.md',2),('**/note.md','note.md',2),('/docs/*.md','docs/note.md',2),
    ('docs\\*.md','docs/note.md',2),('cafe\u0301/**','caf\u00e9/note.md',2),
    ('docs/[!x][!x][!x].md','docs/a/b.md',2),('docs/[^x][^x][^x].md','docs/a/b.md',2),
    ('docs/[.-0][.-0][.-0].md','docs/0/0.md',2),
    ('!','docs/note.md',2),('![z-a]','docs/note.md',2),('!docs/[z-a].md','docs/note.md',2),
    ('!docs/**','docs/note.md',0),('#[z-a]','docs/note.md',0),
])
def test_real_mail_pattern_decision_reaches_guard(reserved,pattern,target,expected):
    service,project=reserved
    with sqlite3.connect(service['db']) as db:
        pid=db.execute('SELECT id FROM projects WHERE human_key=?',(str(project),)).fetchone()[0]
        aid=db.execute("SELECT id FROM agents WHERE project_id=? AND name='EditorBeta'",(pid,)).fetchone()[0]
        db.execute('UPDATE file_reservations SET path_pattern=? WHERE project_id=?',(target,pid))
        db.execute('INSERT INTO file_reservations(project_id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts) VALUES (?,?,?,1,?,?,?)',
            (pid,aid,pattern,'fixture','2026-01-01','2999-01-01'))
    result=guard(reserved,'codex',target=target)
    assert result.returncode==expected,result.stderr+result.stdout
    if expected and pattern[0] not in '!#':assert 'EditorBeta' in result.stderr


@pytest.mark.parametrize('kind',['claude','codex'])
@pytest.mark.parametrize('damage',['old-server','missing','null','version','bool-version','wrong-project','wrong-paths','conflicts-type','entry-type','entry-missing','entry-empty'])
def test_actual_renew_response_damage_is_refused(reserved,kind,damage):
    service,_=reserved
    class Relay(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            document=rpc(service['url'],request['method'],request['params'])
            value=document['result']['structuredContent']
            check=value['conflict_check']
            if damage=='old-server':value.clear();value['renewed']=1
            elif damage=='missing':del value['conflict_check']
            elif damage=='null':value['conflict_check']=None
            elif damage=='version':check['version']=2
            elif damage=='bool-version':check['version']=True
            elif damage=='wrong-project':check['project_key']='elsewhere'
            elif damage=='wrong-paths':check['paths']=['other.md']
            elif damage=='conflicts-type':check['conflicts']={}
            elif damage=='entry-type':check['conflicts']=['bad']
            elif damage=='entry-missing':check['conflicts']=[{'agent':'EditorBeta'}]
            elif damage=='entry-empty':check['conflicts']=[{'agent':'','path_pattern':'docs/**'}]
            body=json.dumps(document).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    relay=ThreadingHTTPServer(('127.0.0.1',0),Relay)
    thread=threading.Thread(target=relay.serve_forever,daemon=True);thread.start()
    try:result=guard(reserved,kind,url=f'http://127.0.0.1:{relay.server_port}/mcp')
    finally:relay.shutdown();relay.server_close();thread.join(timeout=3)
    assert result.returncode==2,result.stderr+result.stdout
    assert 'conflict check rejected' in result.stderr


def test_old_hook_accepts_new_mail_response(reserved,tmp_path):
    hooks=tmp_path/'old-hooks';shutil.copytree(ROOT/'hooks',hooks)
    (tmp_path/'bin').symlink_to(ROOT/'bin',target_is_directory=True)
    # Last caller before #269. The frozen hook ignores the additive certificate.
    raw=(ROOT/'tests/fixtures/reservation-hook-before-269.sh').read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='30fdcbb4f0f49481ff5c182fc31564b28e7af0dfb655d9e076cd4f0399a1b5a3'
    hook=hooks/'check-file-reservation.sh';hook.write_bytes(raw)
    result=guard(reserved,'claude',hook=hook)
    assert result.returncode==0,result.stderr+result.stdout


def test_renew_holds_writer_lock_during_scope_check(reserved):
    service,project=reserved;root=service['root'];gate=root/'pause';reached=root/'reached'
    with sqlite3.connect(service['db']) as db:
        pid=db.execute('SELECT id FROM projects WHERE human_key=?',(str(project),)).fetchone()[0]
        aid=db.execute("SELECT id FROM agents WHERE project_id=? AND name='EditorBeta'",(pid,)).fetchone()[0]
        db.execute('INSERT INTO file_reservations(project_id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts) VALUES (?,?,?,1,?,?,?)',
            (pid,aid,'foreign-only/**','fixture','2026-01-01','2999-01-01'))
    gate.touch();reached.unlink(missing_ok=True);results=[];errors=[]
    def renew():
        try:results.append(call(service,'renew_file_reservations',project_key=str(project),agent_name='EditorAlpha',paths=['docs/note.md']))
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=renew);thread.start()
    try:
        deadline=time.monotonic()+4
        while not reached.exists():
            assert thread.is_alive() and time.monotonic()<deadline,errors
            time.sleep(.01)
        with sqlite3.connect(service['db'],timeout=0) as competing:
            with pytest.raises(sqlite3.OperationalError,match='locked'):competing.execute('BEGIN IMMEDIATE')
    finally:gate.unlink(missing_ok=True);thread.join(timeout=6)
    assert not thread.is_alive() and not errors,errors
    assert results[0]['renewed']==1 and results[0]['conflict_check']['conflicts']==[]


def test_real_zero_renew_keeps_scoped_result_and_no_heartbeat(reserved):
    service,project=reserved
    with sqlite3.connect(service['db']) as db:
        pid=db.execute('SELECT id FROM projects WHERE human_key=?',(str(project),)).fetchone()[0]
        db.execute("UPDATE file_reservations SET released_ts='2026-01-01' WHERE project_id=?",(pid,))
        db.execute("UPDATE agents SET last_active_ts='2000-01-01' WHERE project_id=? AND name='EditorAlpha'",(pid,))
    paths=['docs/note.md',str(project/'docs/note.md')]
    result=call(service,'renew_file_reservations',project_key=str(project),agent_name='EditorAlpha',paths=paths)
    assert result=={'renewed':0,'file_reservations':[],
        'conflict_check':{'version':1,'project_key':str(project),'paths':paths,'conflicts':[]}}
    with sqlite3.connect(service['db']) as db:
        assert db.execute("SELECT last_active_ts FROM agents WHERE project_id=? AND name='EditorAlpha'",(pid,)).fetchone()[0]=='2000-01-01'
    assert guard(reserved,'claude').returncode==2
    assert guard(reserved,'codex').returncode==2


def test_stale_direct_helper_caller_cannot_bypass_conflict_check():
    result=subprocess.run([sys.executable,str(ROOT/'hooks/check-reservation-conflicts.py')],
        env=clean_env(),text=True,capture_output=True,timeout=5)
    assert result.returncode==2
    assert 'update the guard and Mail together' in result.stderr


@pytest.mark.parametrize('kind',['claude','codex'])
@pytest.mark.parametrize('stage,expected',[('before-headers',0),('after-headers-timeout',2),('after-headers-truncated',2),('redirect',2),('error-body-timeout',2)])
def test_renew_response_read_failure_is_not_an_outage(reserved,kind,stage,expected):
    service,_=reserved
    release=threading.Event()
    headers_sent=threading.Event()
    unavailable=socket.socket()
    unavailable.bind(('127.0.0.1',0))
    class Relay(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if stage=='redirect':
                self.send_response(302)
                self.send_header('Location',f'http://127.0.0.1:{unavailable.getsockname()[1]}/missing')
                self.send_header('Content-Length','0');self.end_headers();headers_sent.set();return
            document=rpc(service['url'],request['method'],request['params'])
            body=json.dumps(document).encode()
            if stage=='before-headers':release.wait(6)
            self.send_response(503 if stage=='error-body-timeout' else 200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.flush()
            headers_sent.set()
            if stage in ('after-headers-timeout','error-body-timeout'):release.wait(6)
            try:
                self.wfile.write(body[:1] if stage=='after-headers-truncated' else body)
                self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
    relay=ThreadingHTTPServer(('127.0.0.1',0),Relay)
    thread=threading.Thread(target=relay.serve_forever,daemon=True);thread.start()
    try:
        result=guard(reserved,kind,url=f'http://127.0.0.1:{relay.server_port}/mcp',outage='warn-open')
        if stage!='before-headers':assert headers_sent.is_set()
    finally:release.set();relay.shutdown();relay.server_close();thread.join(timeout=3);unavailable.close()
    assert result.returncode==expected,result.stderr+result.stdout
    if expected:
        assert 'systemMessage' not in result.stdout
        assert ('response' in result.stderr.lower() or 'HTTP 302' in result.stderr)
    else:assert 'systemMessage' in json.loads(result.stdout)
