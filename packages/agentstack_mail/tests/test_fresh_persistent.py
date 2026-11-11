"""Persistent Channels use real fresh HTTP/hooks with synthetic native CLIs."""
import asyncio
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys

import pytest

from agentstack_mail.fresh_persistent import KIND, SECRET_ENV, profile
from agentstack_mail.fresh_runtime import _write_private
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_guard import grant, live
from test_fresh_launch import NATIVE, prepare, ROOT
from test_fresh_proxy import environment


def channels(home, tmp_path):
    api = runpy.run_path(str(ROOT / 'tests/test_persistent_agents.py'))
    plugin = tmp_path / 'fixture-plugin'
    (plugin / '.claude-plugin').mkdir(parents=True, mode=0o700)
    (plugin / '.claude-plugin/plugin.json').write_text(json.dumps(
        {'name': 'dummy-channel', 'version': '0.0.1', 'channels': [{'server': 'dummy'}]}))
    api['_register_claude_fixture_plugin'](home, 'dummy-channel@fixture', plugin)


def configure(client, tmp_path, extra=''):
    home, binary = prepare(client, 'claude', tmp_path)
    channels(home, tmp_path)
    value = dict(kind=KIND, provider='claude', context=str(client.path), working_directory=str(tmp_path),
                 channels=['plugin:dummy-channel@fixture'], model='sonnet',
                 environment={'TELEGRAM_STATE_DIR': str(tmp_path / 'channel-state')})
    path = tmp_path / 'bot.json';_write_private(path, value)
    # Run the same proxy/guard fixture; persistent adds a session check first.
    native = NATIVE.replace("guard_cmd = ['/bin/bash','-c',settings['hooks']['PreToolUse'][0]['hooks'][0]['command']]",
        "guard_cmd = ['/bin/bash','-c',settings['hooks']['PreToolUse'][-1]['hooks'][0]['command']]")
    native = native.replace("pathlib.Path(os.environ['TEST_TRACE']).write_text(json.dumps(result))",
        "result['terminal_key_present']=bool(subprocess.run(['/bin/bash','-c','printenv TELEGRAM_BOT_TOKEN'],capture_output=True).stdout)\n"
        "pathlib.Path(os.environ['TEST_TRACE']).write_text(json.dumps(result))")
    native = native.replace("if os.environ.get('TEST_BLOCK'):", extra + "\nif os.environ.get('TEST_BLOCK'):")
    binary.write_text('#!' + sys.executable + '\n' + native);binary.chmod(0o700)
    return path, binary


def command(path):
    return [sys.executable, str(ROOT / 'bin/agentstack-persistent'), 'run', '--profile', str(path)]


def env(client, binary, tmp_path, **extra):
    value = environment(client, AGENTSTACK_RUNTIME_MODE='fresh', AGENTSTACK_CLAUDE_BIN=str(binary),
        TEST_PROVIDER='claude', TEST_TRACE=str(tmp_path / 'native-trace.json'),
        PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / 'packages/agentstack_mail/src')]), **extra)
    return {key: val for key, val in value.items() if not SECRET_ENV.search(key) and key not in {'TMUX', 'TMUX_PANE'}}


def current(client):
    base = client.path.parent / 'launch'
    return base / 'runs' / json.loads((base / 'current.json').read_text())['launch_id']


@pytest.mark.parametrize('change', [dict(environment={'TELEGRAM_BOT_TOKEN': 'fake'}),
                                  dict(command=['/bin/sh']), dict(provider='codex'),
                                  dict(channels=['--dangerously-skip-permissions'])])
def test_profile_rejects_secret_values_and_arbitrary_commands(tmp_path, change):
    value = dict(kind=KIND, provider='claude', context='/fixture/context.json',
                 working_directory='/fixture', channels=['plugin:dummy@fixture'])
    path = tmp_path / 'bot.json';_write_private(path, {**value, **change})
    with pytest.raises(B1Error):profile(path)


def test_channels_launches_twice_with_same_owner_and_regular_hooks(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            path, binary = configure(client, tmp_path)
            await grant(runtime, people, [tmp_path / 'note.py'])
            runs = []
            for _ in range(2):
                launch_env = env(client, binary, tmp_path)
                launch_env['TELEGRAM_BOT_TOKEN'] = 'synthetic-not-a-real-token'
                result = await asyncio.to_thread(subprocess.run, command(path), env=launch_env,
                    capture_output=True, text=True, timeout=60)
                assert result.returncode == 0, result.stderr
                run_path = current(client);runs.append(run_path)
                assert json.loads((run_path / 'ended.json').read_text())['exit_code'] == 0
                trace = json.loads((tmp_path / 'native-trace.json').read_text())
                assert trace['session_hook_rc'] == trace['guard_rc'] == trace['proxy_rc'] == 0
                assert '--channels' in trace['args'] and '--strict-mcp-config' not in trace['args']
                assert trace['status']['mail_agent_id'] == people[0]['agent_id']
                assert trace['status']['mail_instance_id'] == runtime.config['instance_id']
                assert trace['old_identity_present'] is False
                assert trace['terminal_key_present'] is False
            assert runs[0] != runs[1]
    asyncio.run(run())


def test_explicit_fresh_context_never_dispatches_a_legacy_profile(tmp_path):
    path = tmp_path / 'legacy.json';_write_private(path, {'kind': 'orrery-persistent-agent-v1'})
    value = {key: val for key, val in os.environ.items() if not key.startswith('AGENTSTACK_')}
    value['AGENTSTACK_RUNTIME_MODE'] = 'fresh'
    result = subprocess.run(command(path), env=value, capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and 'FRESH_PROFILE_REQUIRED' in result.stderr


@pytest.mark.parametrize('how', ['requested-stop', 'failed-stop', 'unexpected-exit', 'session-change', 'pending-session-change'])
def test_actual_provider_stop_connects_ended_record_and_next_launch(tmp_path, how):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            extra = ''
            if how == 'unexpected-exit':extra = 'sys.exit(23)'
            if how == 'failed-stop':extra = 'import signal\nsignal.signal(signal.SIGTERM,lambda *_:sys.exit(23))'
            if how in {'session-change', 'pending-session-change'}:
                extra = ("event['session_id']=str(uuid.uuid4())\n"
                         "recorded=subprocess.run(session_cmd,input=json.dumps(event),capture_output=True,text=True)\n"
                         "pathlib.Path(os.environ['TEST_CHANGED']).write_text(str(recorded.returncode)+'\\n'+recorded.stderr)\n"
                         "time.sleep(15)")
            path, binary = configure(client, tmp_path, extra)
            if how == 'pending-session-change':
                binary.write_text(binary.read_text().replace('transcript.write_text(', 'False and transcript.write_text('))
            await grant(runtime, people, [tmp_path / 'note.py'])
            log = (tmp_path / 'bot.log').open('w')
            process = subprocess.Popen(command(path), env=env(client, binary, tmp_path,
                TEST_BLOCK=str(tmp_path / 'block'), TEST_CHANGED=str(tmp_path / 'changed')), stdout=log, stderr=log,
                start_new_session=True)
            try:
                for _ in range(600):
                    if (tmp_path / ('block' if how in {'requested-stop', 'failed-stop'} else 'native-trace.json')).exists():break
                    assert process.poll() is None, (tmp_path / 'bot.log').read_text()
                    await asyncio.sleep(.05)
                else:pytest.fail('native fixture did not start')
                if how in {'requested-stop', 'failed-stop'}:process.send_signal(signal.SIGTERM)
                await asyncio.to_thread(process.wait, timeout=35)
                first = current(client)
                unknown = how in {'unexpected-exit', 'failed-stop'}
                assert (first / 'ended.json').exists() != unknown
                if how in {'session-change', 'pending-session-change'}:
                    assert process.returncode != 0
                    assert (first / 'restart-required.json').exists()
                    # The launcher may stop the native process before its hook returns.
                    receipt = 'session-start.json' if how == 'pending-session-change' else 'session.json'
                    assert json.loads((first / receipt).read_text())['native_session_id'] == json.loads(
                        (tmp_path / 'native-trace.json').read_text())['session_id']
                # Replace only the fixture binary: a second process is a new run.
                native = NATIVE.replace("guard_cmd = ['/bin/bash','-c',settings['hooks']['PreToolUse'][0]['hooks'][0]['command']]",
                    "guard_cmd = ['/bin/bash','-c',settings['hooks']['PreToolUse'][-1]['hooks'][0]['command']]")
                binary.write_text('#!' + sys.executable + '\n' + native)
                result = await asyncio.to_thread(subprocess.run, command(path), env=env(client, binary, tmp_path),
                    capture_output=True, text=True, timeout=60)
                if unknown:
                    assert result.returncode != 0 and 'FRESH_LAUNCH_OUTCOME_UNKNOWN' in result.stderr
                    assert current(client) == first
                else:
                    assert result.returncode == 0, result.stderr
                    assert current(client) != first and (first / 'ended.json').exists()
            finally:
                try:os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:pass
                try:process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL);process.wait()
                log.close()
    asyncio.run(run())
