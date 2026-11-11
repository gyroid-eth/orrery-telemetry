"""The ordinary managed plan launches an independent persistent Hermes TUI."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from agentstack_mail.fresh_runtime import _write_private
from test_fresh_guard import grant, live
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tests'))
from test_fresh_harness import fixture, ROOT
from test_fresh_persistent import current
from test_fresh_proxy import environment

pytestmark = pytest.mark.usefixtures('isolated_managed_harness_home')


@pytest.mark.parametrize('ending', ['normal', 'requested-stop', 'session-change', 'unexpected-exit'])
def test_persistent_hermes_uses_native_plan_hooks_mail_and_end_record(tmp_path, ending):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            root, manifest, model, cleanup = fixture('hermes', tmp_path, people[0]['agent_id'])
            template = json.loads(Path(json.loads(manifest.read_text())['harnesses']['hermes']['config_file']).read_text())
            binary = Path(template['hermes_executable'])
            # Extend the existing real hook/proxy fixture at its final observation.
            native = binary.read_text() + "\nassert '--tui' in sys.argv\n"
            native += "assert not subprocess.run(['/bin/bash','-c','printenv TELEGRAM_BOT_TOKEN'],capture_output=True).stdout\n"
            native += "import signal,time\nchild=None\n"
            if ending == 'requested-stop':
                native += "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'])\n"
                native += "def stop(signum,_):\n child.wait(timeout=5)\n sys.exit(128+signum)\nsignal.signal(signal.SIGTERM,stop)\n"
            native += "pathlib.Path(os.environ['TEST_TREE']).write_text(json.dumps([os.getpid(),os.getppid(),child.pid if child else None]))\n"
            native += "pathlib.Path(os.environ['TEST_STARTED']).touch()\n"
            if ending == 'unexpected-exit':native += 'sys.exit(23)\n'
            if ending == 'session-change':native += "try:guard.session_started(str(uuid.uuid4()))\nexcept RuntimeError:pass\n"
            if ending != 'normal':native += 'import time\ntime.sleep(30)\n'
            binary.write_text(native)
            # Observe only this fixture's known parent/child PIDs; sandbox ps is unavailable.
            observer = root / 'bin/lib/process_observation.py'
            observer.write_text(observer.read_text() + "\n" + r"""
def process_snapshot(**_):
    import json
    from pathlib import Path
    path=Path(os.environ['TEST_TREE'])
    if not path.exists():return {}
    pid,parent,child=json.loads(path.read_text())
    rows={}
    for p,pp in ((pid,parent),(child,pid)):
        if p is None:continue
        try:os.kill(p,0)
        except ProcessLookupError:continue
        rows[p]=(pp,os.getuid(),'fixture-birth')
    return rows
""")
            value = dict(kind='orrery-fresh-persistent-v1', provider='hermes', context=str(client.path),
                working_directory=str(tmp_path), model=model, managed=dict(manifest_file=str(manifest), backend_id='strata'),
                telegram=dict(token_ref=dict(store='keychain', service='fixture.telegram', account='fixture'), allow_from=[123]))
            profile = tmp_path / 'bot.json';_write_private(profile, value)
            await grant(runtime, people, [tmp_path / 'note.py'])
            env = environment(client, AGENTSTACK_RUNTIME_MODE='fresh', AGENTSTACK_PYTHON=sys.executable,
                TEST_STARTED=str(tmp_path / 'started'), TEST_TREE=str(tmp_path / 'tree.json'), PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / 'packages/agentstack_mail/src')]))
            for name in ('TMUX','TMUX_PANE'):env.pop(name, None)
            env['TELEGRAM_BOT_TOKEN'] = 'synthetic-secret'
            argv = [sys.executable, str(root / 'bin/agentstack-persistent'), 'run', '--profile', str(profile)]
            log = (tmp_path / 'launch.log').open('w')
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                for _ in range(800):
                    if (tmp_path / 'started').exists():break
                    assert process.poll() is None, (tmp_path / 'launch.log').read_text()
                    await asyncio.sleep(.05)
                else:pytest.fail('native did not become ready')
                if ending == 'requested-stop':process.send_signal(signal.SIGTERM)
                await asyncio.to_thread(process.wait, timeout=40)
                first = current(client)
                if ending == 'requested-stop':
                    child = json.loads((tmp_path / 'tree.json').read_text())[2]
                    with pytest.raises(ProcessLookupError):os.kill(child, 0)
                assert (first / 'ended.json').exists() == (ending != 'unexpected-exit')
                if ending == 'session-change':
                    assert (first / 'restart-required.json').exists()
                    saved = json.loads((first / 'session.json').read_text())
                    native_session = json.loads((first / 'managed/native-session.json').read_text())
                    assert saved['native_session_id'] == native_session['native_session_id']
                proof = json.loads((tmp_path / 'native-result.json').read_text())
                assert proof['status']['mail_agent_id'] == people[0]['agent_id']
                assert proof['reserved'] and proof['key_read_denied']
                document = json.loads((first / 'managed/input.json').read_text())
                assert document['telegram'] == value['telegram']
                assert not (client.path.parent / 'family').exists()
                overlay = json.loads((first / 'managed/overlay/config.yaml').read_text())
                assert 'orrery-telegram' in overlay['plugins']['enabled']
                assert (client.path.parent / 'providers/hermes/plugins/orrery-telegram/__init__.py').is_file()
                definitions = json.loads((first / 'managed/credential-references.json').read_text())
                assert definitions['backends']['telegram']['keychain']['service'] == 'fixture.telegram'
                # Normal stop allows a new run with the same owner; unexpected exit does not.
                binary.write_text(binary.read_text().split("assert '--tui'")[0])
                again = await asyncio.to_thread(subprocess.run, argv, env=env, capture_output=True, text=True, timeout=60)
                if ending == 'unexpected-exit':
                    assert 'FRESH_LAUNCH_OUTCOME_UNKNOWN' in again.stderr and current(client) == first
                else:
                    assert again.returncode == 0, again.stderr
                    assert current(client) != first
            finally:
                try:os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:pass
                try:process.wait(timeout=20)
                except subprocess.TimeoutExpired:os.killpg(process.pid, signal.SIGKILL);process.wait()
                log.close();cleanup()
    asyncio.run(run())
