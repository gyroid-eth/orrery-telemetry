"""Documented manual Codex preparation, using only synthetic native RPC/trust."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from agentstack_mail.storage_origin_contract import canonical
from test_fresh_guard import grant, live
from test_fresh_launch import NATIVE, invoke

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ID = 'agentstack-codex-app@orrery-fresh'

# A fake native CLI, not a replacement trust implementation: real builder and
# codex_plugin_trust.py CLIs perform discovery, plan validation and trust writes.
# The final native foreground branch reuses the regular launch fixture.
SETUP_NATIVE = r'''
import hashlib, json, os, pathlib, shutil, signal, sys, tomllib
home = pathlib.Path(os.environ['CODEX_HOME'])
args = sys.argv[1:]
registry = home / 'fixture-plugin.json'
def audit(event, **values):
    with (home / 'fixture-rpc.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(event=event, pid=os.getpid(), **values))+'\n')
def config():
    path = home / 'config.toml'
    return tomllib.loads(path.read_text()) if path.exists() else {}
def save_config(states):
    text = '[fixture]\nkeep="untouched"\n[plugins.'+json.dumps('agentstack-codex-app@orrery-fresh')+']\nenabled=true\n'
    for key, value in states.items():
        text += '[hooks.state.'+json.dumps(key)+']\ntrusted_hash='+json.dumps(value['trusted_hash'])+'\nenabled='+str(value['enabled']).lower()+'\n'
    path = home / 'config.toml';path.write_text(text);path.chmod(0o600)
if args[:3] == ['plugin', 'marketplace', 'add']:
    assert args[-1] == '--json' and len(args) == 5
    source = pathlib.Path(args[3]);assert source.is_relative_to(home)
    registry.write_text(json.dumps(dict(marketplace=str(source))))
    audit('marketplace-add');print(json.dumps(dict(marketplaceName='orrery-fresh')));sys.exit(0)
if args[:2] == ['plugin', 'add']:
    assert args == ['plugin', 'add', 'agentstack-codex-app@orrery-fresh', '--json']
    source = pathlib.Path(json.loads(registry.read_text())['marketplace'])
    installed = home / 'native-installed' / 'agentstack-codex-app'
    shutil.copytree(source / 'plugins/agentstack-codex-app', installed)
    registry.write_text(json.dumps(dict(installedPath=str(installed), pluginId=args[2])))
    save_config({'unrelated': {'trusted_hash': 'keep', 'enabled': False}})
    audit('plugin-add');print(registry.read_text());sys.exit(0)
if args == ['app-server']:
    selected = json.loads(registry.read_text());root = pathlib.Path(selected['installedPath'])
    audit('app-server-start')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        for line in sys.stdin:
            row = json.loads(line)
            if 'id' not in row:continue
            method = row['method'];params = row.get('params', {});audit(method)
            if method == 'initialize':result = {'serverInfo': {'name': 'fake-codex', 'version': '1'}}
            elif method == 'hooks/list':
                assert params['cwds'] == [os.getcwd()]
                hooks = []
                states = config().get('hooks', {}).get('state', {})
                for event, groups in json.loads((root / 'hooks/hooks.json').read_text())['hooks'].items():
                    for group in groups:
                        for handler in group['hooks']:
                            key = 'fixture-owned:'+str(len(hooks))
                            digest = 'sha256:'+hashlib.sha256(handler['command'].encode()).hexdigest()
                            approved = states.get(key) == dict(trusted_hash=digest, enabled=True)
                            hooks.append(dict(key=key, eventName=event,
                                handler={k: handler[k] for k in ('type', 'command', 'async')},
                                isManaged=False, source='plugin', sourcePath=str(root / 'hooks/hooks.json'),
                                pluginId=selected['pluginId'], timeoutSec=handler['timeoutSec'],
                                currentHash=digest, trustStatus='trusted' if approved else 'untrusted', enabled=approved))
                result = {'data': [dict(cwd=os.getcwd(), hooks=hooks, errors=[])]}
            elif method == 'config/read':result = {'layers': [{'name': {'type': 'user'}, 'version': 'fixture-v1'}]}
            else:
                assert method == 'config/batchWrite', 'no login/thread/model or other config operations'
                assert params['expectedVersion'] == 'fixture-v1' and params['reloadUserConfig'] is True
                edit, = params['edits'];assert edit['keyPath'] == 'hooks.state' and edit['mergeStrategy'] == 'upsert'
                states = config()['hooks']['state'];states.update(edit['value']);save_config(states)
                result = {'status': 'ok'}
            print(json.dumps(dict(id=row['id'], result=result)), flush=True)
    finally:audit('app-server-stop')
    sys.exit(0)
'''


def test_documented_codex_prepare_unapproved_approved_and_changed_payload(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            install = tmp_path / 'private-install';install.mkdir(mode=0o700)
            for folder in ('bin', 'hooks', 'claude', 'codex', 'templates', 'scripts', 'integrations/codex_app'):
                shutil.copytree(ROOT / folder, install / folder, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(ROOT / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py',
                         install / 'bin/lib/schema_contract.py')
            home = client.path.parent / 'providers/codex'
            home.parent.mkdir(mode=0o700);home.mkdir(mode=0o700)
            binary = tmp_path / 'fake-codex'
            binary.write_text('#!' + sys.executable + '\n' + SETUP_NATIVE + '\n' + NATIVE)
            binary.chmod(0o700)
            env = dict(os.environ, CODEX_HOME=str(home), AGENTSTACK_PYTHON=sys.executable,
                AGENTSTACK_HOME=str(install), AGENTSTACK_TEMPLATE_HOME=str(install / 'templates/fresh'),
                AGENTSTACK_PROJECT_KEY='', AGENTSTACK_LEGACY_PROJECT_KEY='',
                orrery_python=sys.executable, orrery_install=str(install), orrery_context=str(client.path),
                orrery_fresh_root=client.context['root'], orrery_provider_home=str(home),
                orrery_codex=str(binary), orrery_marketplace=str(home / 'fresh-marketplace'),
                orrery_plugin_id=PLUGIN_ID, orrery_hook_plan=str(home / 'hook-plan.json'))
            async def command(argv, **changes):
                return await asyncio.to_thread(subprocess.run, argv, env=env, cwd=home,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40, **changes)
            setup = ['/bin/bash', str(install / 'bin/agentstack-codex-setup')]
            planned = await command([*setup, '--plan', '--json'])
            assert planned.returncode == 0, planned.stdout + planned.stderr
            applied = await command([*setup, '--apply', '--plan-id', json.loads(planned.stdout)['plan_id'], '--json'])
            assert applied.returncode == 0, applied.stdout + applied.stderr
            blocks = re.findall(r'```sh\n(.*?)\n```', (ROOT / 'docs/fresh-global-update.md').read_text(), re.S)
            builder = next(block for block in blocks if "orrery_codex='/absolute/" in block)
            builder = builder.replace("'/absolute/path/to/confirmed/codex'", "\"$orrery_codex\"")
            built = await command(['/bin/bash', '-c', builder])
            assert built.returncode == 0, built.stderr
            marker = home / 'fresh-marketplace/keep-existing'
            marker.write_text('must not delete')
            again = await command(['/bin/bash', '-c', builder])
            assert again.returncode != 0 and marker.read_text() == 'must not delete'
            native = next(block for block in blocks if block.startswith('CODEX_HOME=') and 'plugin marketplace add' in block)
            registered = await command(['/bin/bash', '-c', native])
            assert registered.returncode == 0, registered.stderr
            selected = json.loads(registered.stdout.splitlines()[-1])
            assert selected['pluginId'] == PLUGIN_ID
            plugin = Path(selected['installedPath'])
            assert plugin.resolve().is_relative_to(home) and not plugin.is_symlink()
            env['orrery_plugin_root'] = str(plugin)
            plan_block = next(block for block in blocks if block.startswith('orrery_plugin_root='))
            plan_block = plan_block.replace("'/absolute/installedPath/returned/by/native-command'", '"$orrery_plugin_root"')
            discovered = await command(['/bin/bash', '-c', plan_block])
            assert discovered.returncode == 0, discovered.stdout + discovered.stderr
            path = home / 'hook-plan.json';plan_bytes = path.read_bytes();plan = json.loads(plan_bytes)
            assert plan['approved'] is False and path.stat().st_mode & 0o777 == 0o600
            assert plan['cwd'] == client.context['root'] and plan['shared_codex_home'] == str(home)
            assert plan['plugin_root'] == str(plugin) and plan['codex_binary'] == str(binary)
            repeated = await command(['/bin/bash', '-c', plan_block])
            assert repeated.returncode != 0 and path.read_bytes() == plan_bytes
            apply_block = next(block for block in blocks if 'codex_plugin_trust.py" apply' in block)
            before = (home / 'config.toml').read_bytes()
            refused = await command(['/bin/bash', '-c', apply_block])
            assert refused.returncode == 1 and json.loads(refused.stdout.splitlines()[0])['hook_trust'] == 'needs_review'
            assert (home / 'config.toml').read_bytes() == before
            unapproved = await invoke(client, 'codex', binary, tmp_path, wrapper_root=install)
            assert unapproved.returncode == 2 and 'CODEX_HOOK_CONFIGURE_REQUIRED' in unapproved.stderr
            assert not (tmp_path / 'native-trace.json').exists()
            # Test fixture ONLY: simulate independent human approval. The docs
            # require the agent to hand over, and provide no self-approval code.
            plan['approved'] = True;path.write_text(canonical(plan))
            approved = await command(['/bin/bash', '-c', apply_block])
            assert approved.returncode == 0, approved.stdout + approved.stderr
            receipt = json.loads((home / 'hook-trust-result.json').read_text())
            assert receipt['hook_trust'] == 'observed_current' and receipt['history'] == 'unobserved'
            import tomllib
            native_config = tomllib.loads((home / 'config.toml').read_text())
            assert native_config['fixture']['keep'] == 'untouched'
            assert native_config['hooks']['state']['unrelated'] == dict(trusted_hash='keep', enabled=False)
            await grant(runtime, people, [tmp_path / 'note.py'])
            launched = await invoke(client, 'codex', binary, tmp_path, wrapper_root=install)
            assert launched.returncode == 0, launched.stdout + launched.stderr
            trace = json.loads((tmp_path / 'native-trace.json').read_text())
            assert trace['session_hook_rc'] == trace['guard_rc'] == trace['proxy_rc'] == 0
            assert trace['status']['mail_instance_id'] == runtime.config['instance_id']
            assert trace['status']['mail_agent_id'] == people[0]['agent_id']
            assert trace['context'] == str(client.path) and trace['home'] == str(home)
            assert not (tmp_path / 'note.py').exists()
            assert f'plugins."{PLUGIN_ID}".mcp_servers.agentstack.enabled=false' in trace['args']
            hook = plugin / 'scripts/run-hook.sh';hook.write_bytes(hook.read_bytes() + b'\n# changed fixture payload\n')
            before = (home / 'config.toml').read_bytes()
            changed = await command(['/bin/bash', '-c', apply_block])
            assert changed.returncode == 1 and 'needs_review' in changed.stdout
            assert (home / 'config.toml').read_bytes() == before
            old_trace = (tmp_path / 'native-trace.json').read_bytes()
            stale = await invoke(client, 'codex', binary, tmp_path, wrapper_root=install)
            assert stale.returncode == 2 and 'CODEX_HOOK_CONFIGURE_REQUIRED' in stale.stderr
            assert (tmp_path / 'native-trace.json').read_bytes() == old_trace
            audit = [json.loads(line) for line in (home / 'fixture-rpc.jsonl').read_text().splitlines()]
            assert sum(row['event'] == 'config/batchWrite' for row in audit) == 1
            started = [row['pid'] for row in audit if row['event'] == 'app-server-start']
            stopped = [row['pid'] for row in audit if row['event'] == 'app-server-stop']
            assert started == stopped and len(started) == 2
            (tmp_path / 'manual-codex-result.json').write_text(json.dumps(dict(
                builder_existing_destination_preserved=True, plan_existing_preserved=True,
                unapproved_config_unchanged=True, synthetic_approval_regular_launch=True,
                same_instance_agent_proxy_guard=True, changed_payload_refused=True,
                native_rpc_started_stopped=started, native_trust_writes=1, real_codex_or_human_trust=False)))
    asyncio.run(run())
