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

import pytest

from agentstack_mail.storage_origin_contract import canonical
from test_fresh_guard import grant, live
from test_fresh_launch import NATIVE, invoke

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ID = 'agentstack-codex-app@orrery-fresh'


def effective_fixture(tmp_path, skills=None):
    plugin = tmp_path.resolve() / 'plugin'
    target = plugin / 'skills/coordinate/SKILL.md'
    target.parent.mkdir(parents=True);target.write_text('synthetic coordinate')
    config = {'skills': skills, 'plugins': {PLUGIN_ID: {'enabled': True,
        'mcp_servers': {'agentstack': {'enabled': False}}}}, 'mcp_servers': {'orrery-mail': {
        'command': '/bin/bash', 'args': [str(ROOT / 'bin/agentstack-runtime-client'), 'proxy'],
        'env_vars': ['AGENTSTACK_RUNTIME_MODE', 'AGENTSTACK_FRESH_CLIENT_CONTEXT', 'AGENTSTACK_PYTHON'],
        'environment_id': 'local', 'enabled': True, 'tool_timeout_sec': None}}}
    return {'plugin_root': str(plugin), 'plugin_id': PLUGIN_ID,
            'hooks': [{'key': 'fixture-hook', 'currentHash': 'sha256:fixture'}]}, target, config


def mocked_trust_reader(reader):
    return {'AppServer': reader,
            'scoped_hooks': lambda server, cwd, *_: server.request('hooks/list', {'cwds': [str(cwd)]})['hooks'],
            'identity': lambda h: {k: v for k, v in h.items() if k not in ('enabled', 'trustStatus')}}


def skill_entries(overrides):
    import tomllib
    return tomllib.loads(overrides[-1])['skills']['config']


def test_effective_project_skill_intent_and_alias_survive_exclusion(tmp_path):
    from agentstack_mail.fresh_launch import codex_config_overrides
    entries = [{'path': str(tmp_path / 'missing-disabled/SKILL.md'), 'enabled': False},
               {'path': str(tmp_path / 'enabled/SKILL.md'), 'enabled': True}]
    plan, target, config = effective_fixture(tmp_path, {'config': entries})
    config['mcp_servers']['agent_mail'] = {'command': '/old/project-only'}
    before = json.dumps(config, sort_keys=True)
    result = codex_config_overrides(ROOT, plan, config)
    assert 'mcp_servers.agent_mail.enabled=false' in result
    assert skill_entries(result) == [*entries, {'path': str(target), 'enabled': False}]
    assert json.dumps(config, sort_keys=True) == before


def test_unrelated_duplicate_directory_symlink_and_quoted_intent_is_preserved(tmp_path):
    from agentstack_mail.fresh_launch import codex_config_overrides
    unrelated = tmp_path / 'unrelated';unrelated.mkdir()
    alias = tmp_path / 'alias';alias.symlink_to(unrelated, target_is_directory=True)
    entries = [{'path': str(unrelated), 'enabled': False}, {'path': str(unrelated), 'enabled': True},
               {'path': str(alias), 'enabled': False},
               {'path': str(tmp_path / 'quote"back\\日本語\n/SKILL.md'), 'enabled': False}]
    plan, target, config = effective_fixture(tmp_path, {'config': entries})
    # Directory exclusion is retained, followed by the supported exact file.
    entries.append({'path': str(target.parent), 'enabled': True})
    assert skill_entries(codex_config_overrides(ROOT, plan, config)) == [
        *entries, {'path': str(target), 'enabled': False}]


@pytest.mark.parametrize('state', [None, {'config': []}, {'config': 'target'}])
def test_effective_empty_and_target_skill_exclusion(tmp_path, state):
    from agentstack_mail.fresh_launch import codex_config_overrides
    plan, target, config = effective_fixture(tmp_path, state)
    if state == {'config': 'target'}:
        config['skills'] = {'config': [{'path': str(target), 'enabled': True}]}
    result = codex_config_overrides(ROOT, plan, config)
    assert skill_entries(result) == [{'path': str(target), 'enabled': False}]


@pytest.mark.parametrize('mutation', [
    {'env': {'MCP_AGENT_MAIL_TOKEN': 'secret-sentinel'}}, {'enabled': False},
    {'enabled': 1}, {'environment_id': 'remote'}, {'environment_id': None},
    {'environment_id': 1}, {'cwd': '/foreign'}, {'url': 'https://foreign.invalid'},
    {'tool_timeout_sec': 60}, {'unknown': None}, {'env_vars': ['MCP_AGENT_MAIL_TOKEN']},
])
def test_effective_unsafe_mcp_fields_are_refused_without_values(tmp_path, mutation):
    from agentstack_mail.fresh_launch import codex_config_overrides
    from agentstack_mail.storage_origin_contract import B1Error
    plan, _, config = effective_fixture(tmp_path)
    config['mcp_servers']['orrery-mail'].update(mutation)
    with pytest.raises(B1Error) as error:codex_config_overrides(ROOT, plan, config)
    assert str(error.value) == 'FRESH_CODEX_MCP_CONFIG_UNSUPPORTED'


@pytest.mark.parametrize('skills', ['missing', {}, {'config': None}, [], {'config': [False]},
    {'config': [{'path': 'relative/SKILL.md', 'enabled': False}]},
    {'config': [{'path': '/absolute/SKILL.md', 'enabled': 'false'}]}])
def test_effective_missing_or_malformed_skills_do_not_mean_empty(tmp_path, skills):
    from agentstack_mail.fresh_launch import codex_config_overrides
    from agentstack_mail.storage_origin_contract import B1Error
    plan, _, config = effective_fixture(tmp_path, skills)
    if skills == 'missing':config.pop('skills')
    with pytest.raises(B1Error, match='FRESH_CODEX_MCP_CONFIG_UNSUPPORTED'):
        codex_config_overrides(ROOT, plan, config)


def test_effective_target_duplicate_and_symlink_are_refused(tmp_path):
    from agentstack_mail.fresh_launch import codex_config_overrides
    from agentstack_mail.storage_origin_contract import B1Error
    plan, target, config = effective_fixture(tmp_path)
    alias = tmp_path / 'target-alias';alias.symlink_to(target)
    for entries in ([{'path': str(alias), 'enabled': True}],
                    [{'path': str(target), 'enabled': True}, {'path': str(target), 'enabled': False}]):
        config['skills'] = {'config': entries}
        with pytest.raises(B1Error, match='FRESH_CODEX_MCP_CONFIG_UNSUPPORTED'):
            codex_config_overrides(ROOT, plan, config)


@pytest.mark.parametrize('plugin', [None, False, {}, {'enabled': False}, {'enabled': 1},
    {'enabled': True}, {'enabled': True, 'mcp_servers': {'agentstack': {'enabled': True}}}])
def test_effective_disabled_or_unknown_plugin_is_not_silently_enabled(tmp_path, plugin):
    from agentstack_mail.fresh_launch import codex_config_overrides
    from agentstack_mail.storage_origin_contract import B1Error
    plan, _, config = effective_fixture(tmp_path)
    config['plugins'][PLUGIN_ID] = plugin
    before = json.dumps(config, sort_keys=True)
    with pytest.raises(B1Error, match='^CODEX_HOOK_CONFIGURE_REQUIRED$'):
        codex_config_overrides(ROOT, plan, config)
    assert json.dumps(config, sort_keys=True) == before


def test_effective_reader_uses_exact_context_once_and_never_falls_back(tmp_path, monkeypatch):
    import agentstack_mail.fresh_launch as launch
    plan, _, config = effective_fixture(tmp_path)
    seen = [];failure = False
    class Reader:
        def __init__(self, binary, home, cwd, **kwargs):seen.append((binary, home, cwd, kwargs))
        def __enter__(self):return self
        def __exit__(self, *args):seen.append('closed')
        def request(self, method, params):
            seen.append((method, params))
            if failure:raise RuntimeError('must-not-expose-secret')
            return ({'config': config} if method == 'config/read' else
                    {'hooks': [dict(plan['hooks'][0], enabled=True, trustStatus='trusted')]})
    original = launch.runpy.run_path
    def read(path):
        return mocked_trust_reader(Reader) if str(path).endswith('codex_plugin_trust.py') else original(path)
    monkeypatch.setattr(launch.runpy, 'run_path', read)
    env = {'CODEX_HOME': '/fixed', 'AGENTSTACK_RUNTIME_MODE': 'fresh'}
    args = ['/selected/codex', '-C', str(tmp_path), '-c', 'model_reasoning_effort="medium"']
    launch.codex_effective_overrides(ROOT, tmp_path, args[0], plan, tmp_path, args, env)
    assert seen == [(args[0], tmp_path, tmp_path, {'timeout': 20, 'argv': [*args, 'app-server'], 'environment': env}),
                    ('config/read', {'includeLayers': False, 'cwd': str(tmp_path)}),
                    ('hooks/list', {'cwds': [str(tmp_path)]}), 'closed']
    failure = True
    with pytest.raises(Exception, match='^FRESH_CODEX_MCP_CONFIG_UNSUPPORTED$'):
        launch.codex_effective_overrides(ROOT, tmp_path, args[0], plan, tmp_path, args, env)


@pytest.mark.parametrize('damage', ['ok', 'config-failure', 'plugin-disabled', 'plugin-missing',
    'hook-disabled', 'hook-untrusted', 'hook-mismatch', 'hook-definition', 'hook-failure', 'hook-malformed'])
def test_launch_reads_sanitized_environment_before_publishing_or_starting(tmp_path, monkeypatch, damage):
    import agentstack_mail.fresh_launch as launch
    import agentstack_mail.fresh_child as child
    import agentstack_mail.fresh_delivery as delivery
    from types import SimpleNamespace
    plan, target, config = effective_fixture(tmp_path)
    plan['plugin_id'] = PLUGIN_ID
    if damage == 'plugin-disabled':config['plugins'][PLUGIN_ID]['enabled'] = False
    if damage == 'plugin-missing':config.pop('plugins')
    home = tmp_path / 'home';home.mkdir();(home / 'config.toml').write_text('')
    client = SimpleNamespace(path=tmp_path / 'context.json', context_digest='synthetic-digest',
        context={'mail_instance_id': 'synthetic-instance', 'mail_agent_id': 1,
                 'credential_generation': 1, 'root': str(tmp_path)}, _load=lambda: None)
    async def owner(_):return {'agent_name': 'SyntheticOwner', 'mail_agent_id': 1}
    monkeypatch.setattr(launch, 'verified_owner', owner)
    monkeypatch.setattr(launch, 'provider_setup', lambda *args: (home, '/synthetic/codex', plan))
    monkeypatch.setattr(child, 'launch_task', lambda _: (None, ''))
    for key in ('CHILD_REGISTRATION_TOKEN', 'MCP_AGENT_MAIL_TOKEN', 'AGENTSTACK_GLOBAL_SERVER_CONFIG'):
        monkeypatch.setenv(key, 'must-not-reach-native')
    seen = [];started = []
    class Reader:
        def __init__(self, binary, selected_home, cwd, **kwargs):
            assert (binary, selected_home, cwd) == ('/synthetic/codex', home, tmp_path)
            seen.append(kwargs)
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def request(self, method, params):
            if damage == 'config-failure':raise RuntimeError('private-native-error')
            if method == 'config/read':return {'config': config}
            if damage == 'hook-failure':raise RuntimeError('private-hook-error')
            if damage == 'hook-malformed':raise AttributeError('private-malformed-hook')
            hook = dict(plan['hooks'][0], enabled=damage != 'hook-disabled',
                        trustStatus='untrusted' if damage == 'hook-untrusted' else 'trusted')
            if damage == 'hook-mismatch':hook['currentHash'] = 'unapproved'
            if damage == 'hook-definition':hook['handler'] = {'command': 'unreviewed'}
            return {'hooks': [hook]}
    original = launch.runpy.run_path
    monkeypatch.setattr(launch.runpy, 'run_path', lambda p:
        mocked_trust_reader(Reader) if str(p).endswith('codex_plugin_trust.py') else original(p))
    class Process:
        def wait(self):return 7  # No fabricated successful native receipt.
    def start(args, **kwargs):started.append((args, kwargs));return Process()
    monkeypatch.setattr(launch.subprocess, 'Popen', start)
    monkeypatch.setattr(delivery, 'capture', lambda *args: {})
    monkeypatch.setattr(delivery, 'publish', lambda *args: None)
    if damage != 'ok':
        expected = 'FRESH_CODEX_MCP_CONFIG_UNSUPPORTED' if damage == 'config-failure' else 'CODEX_HOOK_CONFIGURE_REQUIRED'
        with pytest.raises(Exception, match='^' + expected + '$'):
            launch.launch(client, ROOT, 'codex', tmp_path)
        assert not started and not (tmp_path / 'launch/current.json').exists()
    else:
        assert launch.launch(client, ROOT, 'codex', tmp_path) == 7
        args, kwargs = started[0]
        assert kwargs['env'] == seen[0]['environment'] and kwargs['cwd'] == tmp_path
        assert kwargs['env']['CODEX_HOME'] == str(home)
        for key in ('CHILD_REGISTRATION_TOKEN', 'MCP_AGENT_MAIL_TOKEN', 'AGENTSTACK_GLOBAL_SERVER_CONFIG'):
            assert key not in kwargs['env']
        assert skill_entries(args) == [{'path': str(target), 'enabled': False}]
        assert seen[0]['argv'][:-1] == args[:-2]


def test_codex_cli_overrides_disable_actual_plugin_mail_but_keep_hooks(tmp_path):
    from agentstack_mail.fresh_launch import command
    import tomllib

    home = tmp_path / 'codex-home';home.mkdir()
    text = ('[plugins.' + json.dumps(PLUGIN_ID) + ']\nenabled=true\n'
            '[mcp_servers.agent_mail]\ncommand="/forbidden-old-mail"\n')
    (home / 'config.toml').write_text(text)
    effective = tomllib.loads(text)
    args = command(ROOT, home, '/fixture/codex', 'codex',
                   {'plugin_id': PLUGIN_ID}, tmp_path, tmp_path, None)
    for i, value in enumerate(args):
        if not i or args[i - 1] != '-c':continue
        key, raw = value.split('=', 1)
        target = effective
        parts = key.split('.')  # Native CLI literal key path, not TOML headers.
        for part in parts[:-1]:target = target.setdefault(part, {})
        target[parts[-1]] = json.loads(raw)
    assert effective['plugins'][PLUGIN_ID]['enabled'] is True
    assert effective['plugins'][PLUGIN_ID]['mcp_servers']['agentstack']['enabled'] is False
    assert effective['mcp_servers']['agent_mail']['enabled'] is False
    assert effective['mcp_servers']['orrery-mail']['command'] == '/bin/bash'


def test_codex_unaddressable_old_mail_key_is_refused_before_native_launch(tmp_path):
    from agentstack_mail.fresh_launch import command
    from agentstack_mail.storage_origin_contract import B1Error

    name = 'agent"mail'  # Legacy alias recognition removes quotes.
    home = tmp_path / 'codex-home';home.mkdir()
    (home / 'config.toml').write_text('[mcp_servers.' + json.dumps(name) + ']\ncommand="/forbidden"\n')
    with pytest.raises(B1Error, match='FRESH_CODEX_MCP_CONFIG_UNSUPPORTED'):
        command(ROOT, home, '/fixture/codex', 'codex', {'plugin_id': PLUGIN_ID},
                tmp_path, tmp_path, None)


@pytest.mark.parametrize('definition', [
    'env={MCP_AGENT_MAIL_TOKEN="synthetic-sentinel"}',
    'env={AGENTSTACK_GLOBAL_SERVER_CONFIG="/synthetic/old-context"}',
    'env="invalid"', 'enabled=false', 'enabled="true"',
    'url="http://synthetic.invalid/mcp"', 'cwd="/synthetic/old-cwd"',
    'bearer_token_env_var="SYNTHETIC_TOKEN"', 'enabled_tools=["bootstrap"]',
    'future_option=true',
])
def test_codex_conflicting_canonical_mail_config_is_refused(tmp_path, definition):
    from agentstack_mail.fresh_launch import command
    from agentstack_mail.storage_origin_contract import B1Error

    home = tmp_path / 'codex-home';home.mkdir()
    (home / 'config.toml').write_text('[mcp_servers.orrery-mail]\n' + definition + '\n')
    with pytest.raises(B1Error, match='FRESH_CODEX_MCP_CONFIG_UNSUPPORTED'):
        command(ROOT, home, '/must-not-launch/codex', 'codex',
                {'plugin_id': PLUGIN_ID}, tmp_path, tmp_path, None)


@pytest.mark.parametrize('definition', [
    '', '[mcp_servers.orrery-mail]\n',
    '[mcp_servers.orrery-mail]\nenv={}\nenabled=true\n'
    'command="/must-be-replaced"\nargs=["old"]\nenv_vars=["MCP_AGENT_MAIL_TOKEN"]\n',
])
def test_codex_stdio_environment_selects_fresh_wrapper_without_parent_credentials(tmp_path, definition):
    """Native stdio starts with an allowlist, not the provider's whole env."""
    from agentstack_mail.fresh_launch import command

    home = tmp_path / 'codex-home';home.mkdir()
    (home / 'config.toml').write_text(definition)
    context = tmp_path / 'context.json'
    interpreter = tmp_path / 'fixture-python'
    interpreter.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
print(json.dumps({'argv': sys.argv[1:],
    'context': os.environ.get('AGENTSTACK_FRESH_CLIENT_CONTEXT'),
    'mode': os.environ.get('AGENTSTACK_RUNTIME_MODE'),
    'old_proof': any(k in os.environ for k in ('CHILD_REGISTRATION_TOKEN', 'MCP_AGENT_MAIL_TOKEN', 'AGENTSTACK_GLOBAL_SERVER_CONFIG'))}))
''')
    interpreter.chmod(0o700)
    args = command(ROOT, home, '/fixture/codex', 'codex',
                   {'plugin_id': PLUGIN_ID}, tmp_path, tmp_path, None)
    values = dict(value.split('=', 1) for i, value in enumerate(args)
                  if i and args[i - 1] == '-c')
    prefix = 'mcp_servers.orrery-mail.'
    env_vars = json.loads(values.get(prefix + 'env_vars', '[]'))
    provider_env = dict(os.environ, AGENTSTACK_RUNTIME_MODE='fresh',
                        AGENTSTACK_FRESH_CLIENT_CONTEXT=str(context),
                        AGENTSTACK_PYTHON=str(interpreter),
                        CHILD_REGISTRATION_TOKEN='must-not-forward',
                        MCP_AGENT_MAIL_TOKEN='must-not-forward',
                        AGENTSTACK_GLOBAL_SERVER_CONFIG='/must-not-forward')
    # Model native stdio filtering, with only the interpreter's OS basics.
    stdio_env = {k: provider_env[k] for k in ('HOME', 'PATH', 'SYSTEMROOT')
                 if k in provider_env}
    stdio_env.update({k: provider_env[k] for k in env_vars if k in provider_env})
    result = subprocess.run([json.loads(values[prefix + 'command']),
                             *json.loads(values[prefix + 'args'])],
                            env=stdio_env, cwd=tmp_path, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {'argv': ['-m', 'agentstack_mail.fresh_client', 'proxy',
                                 '--context', str(context)],
                        'context': str(context), 'mode': 'fresh', 'old_proof': False}

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
            assert f'plugins.{PLUGIN_ID}.mcp_servers.agentstack.enabled=false' in trace['args']
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
