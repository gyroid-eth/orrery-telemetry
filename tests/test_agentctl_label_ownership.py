"""``agentctl.sh stop`` must not unload a launchd job this HOME never registered.

launchd labels live in the per-user domain and take no notice of ``HOME``. So a
run pointed at a scratch HOME reaches exactly the same
``gui/<uid>/org.agentstack.agentdashboard`` as the real install, and stopping
"its own" service stops the machine's.

That is not hypothetical. A green test run
(``test_installer_reuses_existing_agent_mail_listener_database``) called this
teardown and removed a working dashboard, launchd job and all. The suite passed;
the machine lost the service. Anyone with the product installed who then runs
the tests, a second checkout, or a trial install in another HOME hits the same
thing.

The plist under ``$HOME/Library/LaunchAgents`` is the ownership record: if this
HOME did not write one, this HOME did not register the job.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTCTL = ROOT / "dashboard" / "agentctl.sh"

FAKE_LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$LAUNCHCTL_CALLS"
exit 0
"""


def _run_stop(tmp_path: pathlib.Path) -> list[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    calls = tmp_path / "launchctl-calls.txt"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o755)

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "LAUNCHCTL_CALLS": str(calls),
        "AGENTSTACK_ENV_FILE": str(tmp_path / "absent-env.sh"),
        "AGENTSTACK_RUNTIME_DIR": str(home / ".agentstack" / "runtime"),
    })
    subprocess.run(
        ["bash", str(AGENTCTL), "stop"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    if not calls.exists():
        return []
    return [line for line in calls.read_text(encoding="utf-8").splitlines() if line]


def test_stop_leaves_a_label_this_home_never_registered(tmp_path):
    assert not [call for call in _run_stop(tmp_path) if call.startswith("bootout")]


def test_stop_still_unloads_the_job_this_home_did_register(tmp_path):
    """The positive control.

    Without this, "never call bootout at all" would satisfy the test above and
    the stop command would quietly stop stopping anything.
    """
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "org.agentstack.agentdashboard.plist").write_text("", encoding="utf-8")

    calls = _run_stop(tmp_path)
    assert [call for call in calls if call.startswith("bootout")], calls


def test_restart_refuses_a_loaded_job_this_home_never_registered(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "launchctl-calls.txt"
    loaded = tmp_path / "foreign-job"
    loaded.write_text("another HOME's job")
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("""#!/bin/sh
echo "$1" >> "$LAUNCHCTL_CALLS"
case "$1" in
  print) test -f "$LOADED_JOB"; exit $? ;;
  bootout) rm -f "$LOADED_JOB" ;;
  bootstrap) touch "$LOADED_JOB" ;;
esac
exit 0
""")
    launchctl.chmod(0o755)
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\necho Darwin\n")
    uname.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    label = "org.agentstack.test.restart-ownership"
    plist = home / "Library/LaunchAgents" / (label + ".agentdashboard.plist")
    env = {
        "HOME": str(home),
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "AGENTSTACK_ENV_FILE": str(tmp_path / "absent-env.sh"),
        "AGENTSTACK_RUNTIME_DIR": str(home / "runtime"),
        "AGENTSTACK_LABEL_PREFIX": label,
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_PORT": "19878",
        "LAUNCHCTL_CALLS": str(calls),
        "LOADED_JOB": str(loaded),
    }
    result = subprocess.run(
        ["/bin/bash", str(AGENTCTL), "restart"], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "refusing to restart launchd job without a plist owned by this HOME" in result.stderr
    assert calls.read_text().splitlines() == ["print"]
    assert loaded.read_text() == "another HOME's job"
    assert not plist.exists()


def test_fresh_agentctl_uses_saved_preparation_and_renders_same_context_without_touching_foreign_job(tmp_path):
    import asyncio,json,shlex,shutil,plistlib,re
    from uuid import uuid4
    from agentstack_mail.fresh_runtime import FreshRuntime
    from agentstack_mail.fresh_client import FreshBoundClient,_http
    from agentstack_mail.fresh_server import build_fresh_server
    async def prepare():
        runtime=await FreshRuntime.initialize(tmp_path/'prepared',mode='fresh')
        try:
            person=await runtime.register_local(request_id=str(uuid4()),name='Dashboard',program='fixture',model='fixture')
            server=build_fresh_server(core=runtime.core)
            async def wire(endpoint,tool,args):return await _http(server,tool,args)
            client=await FreshBoundClient.prepare(runtime.root/'config.json',person['binding_path'],
                'http://127.0.0.1:19876/mcp',client_id=str(uuid4()),dispatch=wire)
            return str(client.path),runtime.config['instance_id'],str(runtime.root/'config.json')
        finally:await runtime.close()
    context,instance,config=asyncio.run(prepare())
    home=tmp_path/'home';install=home/'installed';(install/'dashboard').mkdir(parents=True)
    for rel in ('dashboard/agentctl.sh','dashboard/agentdashboard.plist.template','bin/lib/global-client-loader.sh',
                'hooks/project-context.sh','hooks/installed-env.py'):
        dst=install/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/rel,dst)
    fake=tmp_path/'fake-bin';fake.mkdir();calls=tmp_path/'calls'
    for name,body in {'uname':'echo Darwin','curl':'echo 200','launchctl':
        'echo "$*" >> "$CALLS"\ncase "$1" in print) exit 0;; esac\nexit 0'}.items():
        p=fake/name;p.write_text('#!/bin/sh\n'+body+'\n');p.chmod(0o700)
    values=dict(AGENTSTACK_RUNTIME_MODE='fresh',AGENTSTACK_HOME=str(install),AGENTSTACK_PYTHON=sys.executable,
        AGENTSTACK_FRESH_CLIENT_CONTEXT=context,AGENTSTACK_FRESH_SERVER_CONFIG=config,
        AGENTSTACK_FRESH_DASHBOARD_SOCKET=str(tmp_path/'tmux.sock'),ORRERY_FRESH_MAIL_INSTANCE_ID=instance,
        AGENTSTACK_LABEL_PREFIX='org.orrery.test.normal',AGENTSTACK_PORT='19877')
    path=install/'env.sh';path.write_text('\n'.join('export '+k+'='+shlex.quote(v) for k,v in values.items())+'\n');path.chmod(0o600)
    env={k:v for k,v in os.environ.items() if not k.startswith(('AGENTSTACK_','ORRERY_FRESH_'))}
    env.update(HOME=str(home),PATH=str(fake)+':'+os.environ['PATH'],CALLS=str(calls))
    command=['/bin/bash',str(install/'dashboard/agentctl.sh')]
    done=subprocess.run(command+['restart'],env=env,capture_output=True,text=True,timeout=15)
    assert done.returncode!=0 and 'without a plist owned by this HOME' in done.stderr
    assert calls.read_text().splitlines()==[f'print gui/{os.getuid()}/org.orrery.test.normal.agentdashboard']
    # Exercise the actual bootstrap and render function without invoking any
    # process manager. The normal start/restart path calls this same function.
    code=(install/'dashboard/agentctl.sh').read_text();prefix=code[:code.index('\ncase "${1:-status}"')]
    script=install/'dashboard/render-test.sh';script.write_text(prefix+'\nrender_plist\nexport_background_env\nenv\n')
    done=subprocess.run(['/bin/bash',str(script)],env=env,capture_output=True,text=True,timeout=15)
    assert done.returncode==0,done.stderr
    plist=home/'Library/LaunchAgents/org.orrery.test.normal.agentdashboard.plist'
    rendered=plistlib.loads(plist.read_bytes())['EnvironmentVariables']
    for key in ('AGENTSTACK_HOME','AGENTSTACK_RUNTIME_MODE','AGENTSTACK_FRESH_CLIENT_CONTEXT',
                'AGENTSTACK_FRESH_DASHBOARD_SOCKET','AGENTSTACK_FRESH_SERVER_CONFIG','ORRERY_FRESH_MAIL_INSTANCE_ID'):
        assert rendered[key]==values[key] and key+'='+values[key] in done.stdout
    assert 'registration_token=' not in done.stdout
    values['ORRERY_FRESH_MAIL_INSTANCE_ID']='b'*32
    path.write_text('\n'.join('export '+k+'='+shlex.quote(v) for k,v in values.items())+'\n')
    calls.unlink()
    refused=subprocess.run(command+['stop'],env=env,capture_output=True,text=True,timeout=15)
    assert refused.returncode!=0 and 'FRESH_STARTUP_SETTINGS_UNAVAILABLE' in refused.stderr
    assert not calls.exists()
