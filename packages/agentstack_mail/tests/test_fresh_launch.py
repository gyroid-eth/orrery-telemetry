"""Actual launch/resume entries with synthetic native CLIs, real HTTP/guard."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from agentstack_mail.fresh_runtime import _write_private
from agentstack_mail.storage_origin_contract import canonical
from test_fresh_guard import grant, live
from test_fresh_proxy import environment

ROOT = Path(__file__).resolve().parents[3]

NATIVE = r'''
import json, os, pathlib, subprocess, sys, time, uuid
provider = os.environ['TEST_PROVIDER']
args = sys.argv[1:]
home = pathlib.Path(os.environ['CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR'])
if '--resume' in args:session = args[args.index('--resume')+1]
elif 'resume' in args:session = args[args.index('resume')+1]
else:session = str(uuid.uuid4())
transcript = home / 'sessions' / (session + '.jsonl')
transcript.parent.mkdir(mode=0o700, exist_ok=True)
transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':session}}) + '\n')
event = dict(hook_event_name='SessionStart', session_id=session, cwd=os.getcwd(), transcript_path=str(transcript))
if os.environ.get('TEST_BAD_SESSION'):event['session_id'] = str(uuid.uuid4())
if os.environ.get('TEST_BAD_NONCE'):os.environ['AGENTSTACK_FRESH_LAUNCH_NONCE'] = 'wrong-fixture-nonce'
if provider == 'claude':
    config = json.loads(pathlib.Path(args[args.index('--mcp-config')+1]).read_text())
    settings = json.loads(pathlib.Path(args[args.index('--settings')+1]).read_text())
    session_cmd = ['/bin/bash','-c',settings['hooks']['SessionStart'][0]['hooks'][0]['command']]
    guard_cmd = ['/bin/bash','-c',settings['hooks']['PreToolUse'][0]['hooks'][0]['command']]
    proxy = config['mcpServers']['orrery-mail']; proxy_cmd = [proxy['command'],*proxy['args']]
else:
    plan = json.loads((home/'hook-plan.json').read_text())
    plugin = pathlib.Path(plan['plugin_root'])
    session_cmd = ['/bin/bash', str(plugin/'scripts/run-hook.sh')]
    guard_cmd = ['/bin/bash', str(plugin/'scripts/run-reservation-guard.sh')]
    settings = None
    values = [args[i+1] for i, value in enumerate(args) if value == '-c']
    command = json.loads(next(value.split('=',1)[1] for value in values if value.startswith('mcp_servers.orrery-mail.command=')))
    proxy_args = json.loads(next(value.split('=',1)[1] for value in values if value.startswith('mcp_servers.orrery-mail.args=')))
    proxy_cmd = [command,*proxy_args]
recorded = subprocess.run(session_cmd, input=json.dumps(event), capture_output=True, text=True, timeout=15)
if provider == 'codex':
    target = '*** Begin Patch\n*** Add File: note.py\n+new\n*** End Patch'
    guard_event = dict(cwd=os.getcwd(), tool_name='apply_patch', tool_input={'command': target})
else:guard_event = dict(cwd=os.getcwd(), tool_name='Write', tool_input={'file_path':'note.py'})
guarded = subprocess.run(guard_cmd, input=json.dumps(guard_event), capture_output=True, text=True, timeout=30)
requests = [{'jsonrpc':'2.0','id':1,'method':'initialize'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'runtime_status','arguments':{}}}]
proxied = subprocess.run(proxy_cmd, input=''.join(json.dumps(row)+'\n' for row in requests), capture_output=True, text=True, timeout=30)
replies = [json.loads(line) for line in proxied.stdout.splitlines()]
status = json.loads(replies[-1]['result']['content'][0]['text']) if replies else None
result = dict(provider=provider, args=args, session_id=session, session_hook_rc=recorded.returncode,
    session_stderr=recorded.stderr, guard_rc=guarded.returncode, guard_stderr=guarded.stderr,
    proxy_rc=proxied.returncode, status=status, mode=os.environ.get('AGENTSTACK_RUNTIME_MODE'),
    context=os.environ.get('AGENTSTACK_FRESH_CLIENT_CONTEXT'), home=str(home),
    runtime=os.environ.get('AGENTSTACK_RUNTIME_DIR'),
    old_identity_present=any(key in os.environ for key in ('PROJECT_KEY','AGENTSTACK_PROJECT_KEY','CHILD_REGISTRATION_TOKEN','MCP_AGENT_MAIL_TOKEN')))
pathlib.Path(os.environ['TEST_TRACE']).write_text(json.dumps(result))
if os.environ.get('TEST_BLOCK'):
    pathlib.Path(os.environ['TEST_BLOCK']).write_text('ready')
    time.sleep(8)
sys.exit(0 if recorded.returncode == 0 and guarded.returncode == 0 and proxied.returncode == 0 else 17)
'''


def prepare(client, provider, tmp_path):
    home = client.path.parent / 'providers' / provider
    home.parent.mkdir(mode=0o700, exist_ok=True);home.mkdir(mode=0o700, exist_ok=True)
    instructions = runpy.run_path(str(ROOT / 'bin/lib/instruction_blocks.py'))
    instructions['provider_instructions'](home, provider, ROOT, '', configure=True,
                                         template_home=ROOT / 'templates/fresh')
    binary = tmp_path / ('fake-' + provider)
    binary.write_text('#!' + sys.executable + '\n' + NATIVE);binary.chmod(0o700)
    if provider == 'claude':
        _write_private(home / '.claude.json', {'mcpServers': {}})
    else:
        plugin = home / 'plugin'
        shutil.copytree(ROOT / 'integrations/codex_app/plugin', plugin)
        trust = runpy.run_path(str(ROOT / 'scripts/codex_plugin_trust.py'))
        plugin_id = 'agentstack-codex-app@fresh-fixture'
        key = 'synthetic-scoped-session-start'
        # Synthetic native trust evidence only: no Codex process or real /hooks
        # approval is run. The production read-only verifier checks this shape.
        plan = dict(schema_version=1, approved=True, enable_hooks=True, codex_binary=str(binary),
            shared_codex_home=str(home), cwd=client.context['root'], plugin_id=plugin_id,
            plugin_root=str(plugin), payload_digest=trust['payload_digest'](plugin),
            declared_hooks=json.loads((plugin / 'hooks/hooks.json').read_text()),
            hooks=[{'key': key, 'currentHash': 'sha256:synthetic'}])
        _write_private(home / 'hook-plan.json', plan)
        config = home / 'config.toml'
        config.write_text('[plugins.' + json.dumps(plugin_id) + ']\nenabled=true\n'
            '[hooks.state.' + json.dumps(key) + ']\ntrusted_hash="sha256:synthetic"\nenabled=true\n'
            '[mcp_servers.agent_mail]\ncommand="FORBIDDEN-OLD-MAIL"\n')
        config.chmod(0o600)
    return home, binary


async def invoke(client, provider, binary, tmp_path, *, session=None, extra=None, wrapper_root=ROOT):
    env = environment(client, AGENTSTACK_RUNTIME_MODE='fresh',
        **{'AGENTSTACK_' + provider.upper() + '_BIN': str(binary)},
        TEST_PROVIDER=provider, TEST_TRACE=str(tmp_path / 'native-trace.json'),
        CHILD_REGISTRATION_TOKEN='old-fixture-must-not-inherit', MCP_AGENT_MAIL_TOKEN='old-bearer-must-not-inherit')
    env.update(extra or {})
    if session is None:
        args = ['/bin/bash', str(wrapper_root / ('bin/agent-start-codex' if provider == 'codex' else 'bin/agent-start')), str(tmp_path)]
    else:
        args = [sys.executable, str(wrapper_root / 'bin/agentstack-resume'), provider, str(tmp_path), '--session-id', session]
    return await asyncio.to_thread(subprocess.run, args, cwd=tmp_path, env=env,
                                   capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_actual_setup_private_copy_consent_backup_and_fresh_instruction_launch(tmp_path, provider):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            install = tmp_path / 'private-install';install.mkdir(mode=0o700)
            for folder in ('bin', 'hooks', 'claude', 'codex', 'templates'):
                shutil.copytree(ROOT / folder, install / folder, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(ROOT / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py',
                         install / 'bin/lib/schema_contract.py')
            home = client.path.parent / 'providers' / provider
            home.parent.mkdir(mode=0o700, exist_ok=True);home.mkdir(mode=0o700)
            env = dict(os.environ, AGENTSTACK_PYTHON=sys.executable, AGENTSTACK_HOME=str(install),
                       AGENTSTACK_TEMPLATE_HOME=str(install / 'templates/fresh'),
                       AGENTSTACK_PROJECT_KEY='', AGENTSTACK_LEGACY_PROJECT_KEY='',
                       AGENTSTACK_CLAUDE_MD_SCOPE='global', CLAUDE_HOME='')
            env['CLAUDE_CONFIG_DIR' if provider == 'claude' else 'CODEX_HOME'] = str(home)
            setup = ['/bin/bash', str(install / f'bin/agentstack-{provider}-setup')]
            async def configured(*args):
                return await asyncio.to_thread(subprocess.run, [*setup, *args], cwd=home, env=env,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            def private_files():
                return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in tmp_path.rglob('*') if p.is_file()}
            # The live fixture is idle; the actual setup plan creates no files.
            before = private_files()
            planned = await configured('--plan', '--json')
            assert planned.returncode == 0, planned.stdout + planned.stderr
            plan = json.loads(planned.stdout)
            assert private_files() == before
            applied = await configured('--apply', '--plan-id', plan['plan_id'], '--json')
            assert applied.returncode == 0, applied.stdout + applied.stderr
            assert json.loads(applied.stdout)['stage'] == 'complete'
            name = 'CLAUDE.md' if provider == 'claude' else 'AGENTS.md'
            target = home / name
            assert b'explicit fresh client' in target.read_bytes()
            assert b'__AGENTSTACK_PROJECT_KEY__' not in target.read_bytes()
            assert b'<set AGENTSTACK_PROJECT_KEY>' not in target.read_bytes()
            helper = runpy.run_path(str(install / 'bin/lib/instruction_blocks.py'))
            helper['provider_instructions'](home, provider, install, '', template_home=install / 'templates/fresh')
            # Omitted template_home keeps the legacy renderer's exact behavior.
            legacy = tmp_path / 'legacy-provider';legacy.mkdir(mode=0o700)
            helper['provider_instructions'](legacy, provider, install, '', configure=True)
            assert helper['block_bytes']((legacy / name).read_bytes()) == helper['render'](
                provider, (install / provider / name).read_bytes(), str(install), '')
            before = private_files()
            again = await configured('--apply', '--json')
            assert again.returncode == 0, again.stdout + again.stderr
            assert private_files() == before
            # Simulate a later template update and personal bytes outside the block.
            outside = b'\xef\xbb\xbfPersonal instructions\r\n'
            tail = b'Personal suffix\r\n'
            target.write_bytes(outside + target.read_bytes() + tail)
            saved = target.read_bytes()
            template = install / 'templates/fresh' / provider / name
            template.write_bytes(template.read_bytes() + b'\nUpdated private fixture generation.\n')
            planned = await configured('--plan', '--json');assert planned.returncode == 0
            plan = json.loads(planned.stdout)
            refused = await configured('--apply', '--plan-id', plan['plan_id'], '--json')
            assert refused.returncode == 2 and 'confirmation required' in refused.stdout
            assert target.read_bytes() == saved
            # Test-only explicit operator consent, never a real user HOME/trust.
            approved = await configured('--apply', '--plan-id', plan['plan_id'], '--assume-yes', '--json')
            assert approved.returncode == 0, approved.stdout + approved.stderr
            assert target.read_bytes().startswith(outside) and target.read_bytes().endswith(tail)
            assert any(p.read_bytes() == saved for p in (install / 'backups').rglob('*.before'))
            helper['provider_instructions'](home, provider, install, '', template_home=install / 'templates/fresh')
            if provider == 'codex':
                assert not (home / 'hook-plan.json').exists()  # Instructions do not grant hook trust.
                return
            binary = tmp_path / 'fake-claude'
            binary.write_text('#!' + sys.executable + '\n' + NATIVE);binary.chmod(0o700)
            await grant(runtime, people, [tmp_path / 'note.py'])
            launched = await invoke(client, provider, binary, tmp_path, wrapper_root=install)
            assert launched.returncode == 0, launched.stderr
            trace = json.loads((tmp_path / 'native-trace.json').read_text())
            assert trace['session_hook_rc'] == trace['guard_rc'] == trace['proxy_rc'] == 0
            assert trace['status']['mail_agent_id'] == people[0]['agent_id']
            assert trace['status']['mail_instance_id'] == runtime.config['instance_id']
            assert trace['context'] == str(client.path) and not (tmp_path / 'note.py').exists()
            (tmp_path / 'manual-provider-result.json').write_text(json.dumps({
                'setup_plan_readonly': True, 'existing_content_requires_consent': True,
                'private_backup_matches': True, 'outside_bytes_preserved': True,
                'fake_claude_regular_launch': True, 'real_trust_or_login': False}) + '\n')
    asyncio.run(run())


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_regular_entries_new_and_explicit_resume_use_same_context_proxy_and_real_guard(tmp_path, provider):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            home, binary = prepare(client, provider, tmp_path)
            await grant(runtime, people, [tmp_path / 'note.py'])
            before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in home.rglob('*') if path.is_file()}
            first = await invoke(client, provider, binary, tmp_path)
            assert first.returncode == 0, first.stderr
            record = json.loads((tmp_path / 'native-trace.json').read_text())
            assert record['session_hook_rc'] == record['guard_rc'] == record['proxy_rc'] == 0
            assert record['mode'] == 'fresh' and record['context'] == str(client.path)
            assert record['status']['mail_agent_id'] == people[0]['agent_id']
            assert record['status']['mail_instance_id'] == runtime.config['instance_id']
            assert not record['old_identity_present']
            assert record['runtime'] == str(client.path.parent / 'launch/runtime')
            assert '--strict-mcp-config' in record['args'] if provider == 'claude' else 'check_for_update_on_startup=false' in record['args']
            if provider == 'codex':
                assert 'mcp_servers."agent_mail".enabled=false' in record['args']
                assert 'plugins."agentstack-codex-app@fresh-fixture".mcp_servers.agentstack.enabled=false' in record['args']
            second = await invoke(client, provider, binary, tmp_path, session=record['session_id'])
            assert second.returncode == 0, second.stderr
            resumed = json.loads((tmp_path / 'native-trace.json').read_text())
            assert resumed['session_id'] == record['session_id']
            assert resumed['status'] == record['status'] and resumed['guard_rc'] == 0
            assert not (tmp_path / 'note.py').exists()
            for name, expected in before.items():assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected
            assert people[0]['auth']['registration_token'] not in first.stdout + first.stderr + second.stdout + second.stderr + canonical(resumed)
            runs = list((client.path.parent / 'launch/runs').iterdir())
            assert len(runs) == 2 and all((path / 'session.json').exists() and (path / 'ended.json').exists() for path in runs)
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['missing-receipt', 'wrong-I', 'wrong-A', 'wrong-g', 'wrong-provider', 'wrong-header'])
def test_resume_requires_bound_native_record_and_current_owner(tmp_path, damage):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            home, binary = prepare(client, 'codex', tmp_path)
            await grant(runtime, people, [tmp_path / 'note.py'])
            first = await invoke(client, 'codex', binary, tmp_path);assert first.returncode == 0, first.stderr
            trace = tmp_path / 'native-trace.json';record = json.loads(trace.read_text());trace.unlink()
            receipt_path = client.path.parent / 'launch/sessions' / (record['session_id'] + '.json')
            receipt = json.loads(receipt_path.read_text())
            if damage == 'missing-receipt':receipt_path.unlink()
            elif damage == 'wrong-header':
                Path(receipt['transcript_path']).write_text(canonical({'type':'session_meta', 'payload':{'id':str(uuid4())}})+'\n')
            else:
                field = {'wrong-I':'mail_instance_id', 'wrong-A':'mail_agent_id', 'wrong-g':'credential_generation', 'wrong-provider':'provider'}[damage]
                receipt[field] = 999 if field in {'mail_agent_id', 'credential_generation'} else 'wrong'
                receipt_path.write_text(canonical(receipt))
            result = await invoke(client, 'codex', binary, tmp_path, session=record['session_id'])
            assert result.returncode == 2 and not trace.exists()
            assert 'FRESH_' in result.stderr and 'Traceback' not in result.stderr
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['instructions', 'unapproved', 'payload', 'wiring', 'revoked', 'unknown'])
def test_launch_refuses_unprepared_trust_revocation_and_unknown_prior_run(tmp_path, damage):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            home, binary = prepare(client, 'codex', tmp_path)
            if damage == 'instructions':(home / 'AGENTS.md').unlink()
            elif damage == 'unapproved':
                path = home / 'hook-plan.json';plan = json.loads(path.read_text());plan['approved'] = False;path.write_text(canonical(plan))
            elif damage == 'payload':(home / 'plugin/scripts/run-hook.sh').write_text('changed')
            elif damage == 'wiring':
                # An approved older payload can have scripts present but omit
                # the pre-edit wiring. Trust alone is not current guard coverage.
                hooks = home / 'plugin/hooks/hooks.json'
                declared = json.loads(hooks.read_text());declared['hooks'].pop('PreToolUse')
                hooks.write_text(canonical(declared))
                path = home / 'hook-plan.json';plan = json.loads(path.read_text())
                trust = runpy.run_path(str(ROOT / 'scripts/codex_plugin_trust.py'))
                plan.update(declared_hooks=declared, payload_digest=trust['payload_digest'](home / 'plugin'))
                path.write_text(canonical(plan))
            elif damage == 'revoked':
                await runtime.core.recover(people[0]['agent_id'], expected_generation=people[0]['credential_generation'], new_token='launch-new-synthetic-private-key')
            else:
                base = client.path.parent / 'launch';base.mkdir(mode=0o700)
                (base / 'runs').mkdir(mode=0o700);run_id = str(uuid4());(base / 'runs' / run_id).mkdir(mode=0o700)
                _write_private(base / 'current.json', {'launch_id':run_id})
            result = await invoke(client, 'codex', binary, tmp_path)
            assert result.returncode == 2 and not (tmp_path / 'native-trace.json').exists()
            assert 'Traceback' not in result.stderr
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['TEST_BAD_SESSION', 'TEST_BAD_NONCE'])
def test_foreign_native_session_and_stale_session_hook_are_rejected(tmp_path, bad):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            home, binary = prepare(client, 'codex', tmp_path)
            await grant(runtime, people, [tmp_path / 'note.py'])
            result = await invoke(client, 'codex', binary, tmp_path, extra={bad:'1'})
            assert result.returncode == 17
            assert not list((client.path.parent / 'launch/sessions').iterdir())
            current = json.loads((client.path.parent / 'launch/current.json').read_text())
            intent = json.loads((client.path.parent / 'launch/runs' / current['launch_id'] / 'intent.json').read_text())
            env = environment(client, AGENTSTACK_RUNTIME_MODE='fresh', AGENTSTACK_FRESH_LAUNCH_ID=current['launch_id'],
                              AGENTSTACK_FRESH_LAUNCH_NONCE=intent['nonce'])
            trace = json.loads((tmp_path / 'native-trace.json').read_text())
            event = dict(hook_event_name='SessionStart', session_id=trace['session_id'],
                         transcript_path=str(home / 'sessions' / (trace['session_id'] + '.jsonl')))
            late = await asyncio.to_thread(subprocess.run, ['/bin/bash', str(ROOT / 'bin/agentstack-runtime-client'), 'session-hook'],
                env=env, input=canonical(event), capture_output=True, text=True, timeout=30)
            assert late.returncode == 2 and 'FRESH_LAUNCH_RECEIPT_MISMATCH' in late.stderr
    asyncio.run(run())


def test_regular_entry_missing_or_mixed_fresh_context_never_uses_old_transport(tmp_path):
    async def run():
        async with live(tmp_path) as (_, _, client, _):
            for entry in ('agent-start', 'agent-start-codex', 'agentstack-resume'):
                args = ([sys.executable] if entry == 'agentstack-resume' else ['/bin/bash']) + [str(ROOT / 'bin' / entry)]
                args += ['codex', str(tmp_path), '--session-id', str(uuid4())] if entry == 'agentstack-resume' else [str(tmp_path)]
                for changes in ({'AGENTSTACK_FRESH_CLIENT_CONTEXT':''}, {'AGENTSTACK_CLIENT_CONFIG':'/old-context'}):
                    env = environment(client, AGENTSTACK_RUNTIME_MODE='fresh', **changes)
                    result = await asyncio.to_thread(subprocess.run, args, cwd=tmp_path, env=env,
                        capture_output=True, text=True, timeout=30)
                    assert result.returncode == 2 and 'FRESH_CONTEXT_REQUIRED' in result.stderr
    asyncio.run(run())


def test_one_context_cannot_start_two_native_processes(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            home, binary = prepare(client, 'claude', tmp_path)
            await grant(runtime, people, [tmp_path / 'note.py'])
            ready = tmp_path / 'provider-ready'
            first = asyncio.create_task(invoke(client, 'claude', binary, tmp_path, extra={'TEST_BLOCK':str(ready)}))
            try:
                for _ in range(120):
                    if ready.exists():break
                    await asyncio.sleep(0.1)
                assert ready.exists()
                second = await invoke(client, 'claude', binary, tmp_path)
                assert second.returncode == 2 and 'FRESH_LAUNCH_BUSY' in second.stderr
            finally:
                result = await first
            assert result.returncode == 0, result.stderr
    asyncio.run(run())


def test_fresh_non_session_start_plugin_event_does_not_open_owner_material(tmp_path):
    # A missing path proves the fast non-SessionStart route does not construct
    # FreshBoundClient or start an HTTP call. No real HOME/Bridge is consulted.
    env = dict(os.environ, AGENTSTACK_RUNTIME_MODE='fresh', AGENTSTACK_PYTHON=sys.executable,
               AGENTSTACK_FRESH_CLIENT_CONTEXT=str(tmp_path / 'not-opened.json'))
    result = subprocess.run(['/bin/bash', str(ROOT / 'integrations/codex_app/plugin/scripts/run-hook.sh')],
        env=env, input='{"hook_event_name":"PostToolUse"}', capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and not result.stdout and not result.stderr
