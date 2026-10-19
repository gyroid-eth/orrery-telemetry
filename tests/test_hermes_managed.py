"""Mail pilot adapter tests; no real registration, Keychain, provider or Mail."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ADAPTER = Path(__file__).resolve().parents[1] / 'integrations/hermes'
sys.path.insert(0, str(ADAPTER))
import mail_lifecycle
import mail_plugin
import mail_probe
import profile_projection
import runner


def reply(value):
    return SimpleNamespace(is_error=False, structured_content=value, content=[])


class FakeProduct:
    def __init__(self, root):
        self.root, self.calls = root, []
        self.product = root / 'product'
        for relative in ('bin/agentstack-preregister-child', 'hooks/child_tools.py',
                         'integrations/codex_app/plugin/scripts/run-mcp.sh'):
            path = self.product / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# offline fixture\n')
            path.chmod(0o700)

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assert_no_inherited_secret(kwargs.get('env', {}))
        if any(str(arg).endswith('profile_projection.py') for arg in argv):
            spec = runner.read(argv[argv.index('--spec') + 1])
            profile = argv[argv.index('--profile') + 1]
            projected = profile_projection.project(spec, profile, ['title_generation', 'background_review', 'curator'])
            return subprocess.CompletedProcess(argv, 0, json.dumps(projected), '')
        if argv[-1] == 'mode':
            return subprocess.CompletedProcess(argv, 0, 'legacy\n', '')
        if str(argv[0]).endswith('agentstack-preregister-child'):
            path = Path(argv[argv.index('--token-file-out') + 1])
            path.write_text('offline-owner-token')
            path.chmod(0o600)
            return subprocess.CompletedProcess(argv, 0, 'FixtureChild\n', '')
        if 'claude-config' in argv:
            get = lambda flag: argv[argv.index(flag) + 1]
            proxy = {'command': get('--runner'), 'args': [], 'env': {
                'AGENTSTACK_PROXY_PROGRAM': 'claude-code', 'AGENTSTACK_PROXY_AGENT_NAME': get('--child'),
                'AGENTSTACK_PROXY_TOKEN_FILE': get('--token-file'), 'AGENTSTACK_PROJECT_KEY': get('--project-key'),
                'AGENTSTACK_PROXY_PARENT_AGENT': get('--parent-agent'), 'AGENTSTACK_MCP_URL': get('--mcp-url')}}
            runner.save(get('--out'), {'mcpServers': {'orrery-mail': proxy}})
            return subprocess.CompletedProcess(argv, 0, '', '')
        if 'has-session' in argv:
            return subprocess.CompletedProcess(argv, 1, '', '')
        if 'new-session' in argv:
            return subprocess.CompletedProcess(argv, 0, '%41\n', '')
        return subprocess.CompletedProcess(argv, 0, '', '')

    @staticmethod
    def assert_no_inherited_secret(env):
        assert 'DEEPSEEK_API_KEY' not in env and 'MCP_AGENT_MAIL_TOKEN' not in env


class PilotFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hermes-mail-pilot-')
        self.root = Path(self.temp.name)
        self.product = FakeProduct(self.root)
        self.state = self.root / 'state with spaces'
        profile = {'provider': 'fixture', 'model': 'fixture-model', 'base_url': 'https://provider.invalid',
                   'api_mode': 'anthropic_messages', 'key_cmd': 'fixture-keychain-reference'}
        self.config = {'product_root': str(self.product.product), 'home': str(self.root / 'home'),
            'project_key': str(self.root / 'project'), 'worktree': str(self.root / 'work'),
            'managed_root': str(self.root / 'hermes-root'), 'hermes_executable': str(self.root / 'hermes'),
            'hermes_python': sys.executable, 'hermes_source': str(self.root / 'source'),
            'proxy_python': sys.executable, 'tmux': str(self.root / 'tmux'), 'tmux_socket': str(self.root / 'socket'),
            'parent': 'FixtureParent', 'connection': {'runtime_dir': str(self.root / 'runtime'),
                'mcp_url': 'http://127.0.0.1:1/mcp', 'mail_env': str(self.root / 'mail.env'), 'bearer_mode': 'off'},
            'profiles': {'strata': profile, 'deepseek': profile}}
        self.config_path = self.root / 'input.json'
        runner.save(self.config_path, self.config)
        runner.initialize(self.config_path, self.state, 'strata', self.product.run)

    def tearDown(self):
        self.temp.cleanup()

    def bind(self):
        name = mail_lifecycle.register(self.state, self.product.run, generate_config=False)
        self.assertEqual(name, 'FixtureChild')
        self.assertFalse((self.state / 'mcp.json').exists())
        runner.configure(self.state, self.product.run)

    def test_product_registration_generation_and_tmux_projection(self):
        self.bind()
        proxy = runner.read(self.state / 'mcp.json')['mcpServers']['orrery-mail']
        self.assertEqual(proxy['env']['AGENTSTACK_PROXY_PROGRAM'], 'claude-code')
        profile = runner.read(Path(runner.read(self.state / 'run-config.json')['profile_home']) / 'config.yaml')
        self.assertTrue(profile['memory']['memory_enabled'])
        self.assertTrue(profile['auxiliary']['curator']['provider'] == 'fixture')
        self.assertEqual(profile['mcp_servers']['orrery_mail']['tools']['include'], ['send_message'])
        self.assertEqual(profile['skills']['external_dirs'], [])
        self.assertNotIn('api_key', json.dumps(profile))
        prereg = [argv for argv, _ in self.product.calls if str(argv[0]).endswith('agentstack-preregister-child')][0]
        self.assertEqual(prereg[prereg.index('--program') + 1], 'hermes')
        with self.assertRaisesRegex(ValueError, 'Identity already exists'):
            mail_lifecycle.register(self.state, self.product.run, generate_config=False)
        runner.save(self.state / 'mail-probe.json', {'schema': True, 'backend': True, 'ok': True,
                                                  'agent_name': 'FixtureChild', 'fixture': ''})
        proof = runner.start(self.state, self.product.run)
        self.assertEqual(proof['pane_id'], '%41')
        argv = next(argv for argv, _ in self.product.calls if 'new-session' in argv)
        command = shlex.split(argv[-1])
        self.assertIn(str(self.state.resolve()), command)
        self.assertNotIn((self.state / 'task.md').read_text(), argv[-1])
        self.assertEqual(command[:2], ['/usr/bin/env', '-i'])
        self.assertEqual(runner.read(self.state / 'start-intent.json')['agent_name'], 'FixtureChild')
        with self.assertRaises(FileExistsError):
            runner.start(self.state, self.product.run)

    def test_no_success_from_exit_zero_or_wrong_receipt(self):
        self.bind()
        runner.save(self.state / 'runtime-exit.json', {'exit_code': 0, 'group_absent': True, 'group_check': 'observed'})
        self.assertFalse(runner.status(self.state)['mail_probe_accepted'])
        attempt = runner.read(self.state / 'attempt.json')
        receipt = {'attempt_id': attempt['attempt_id'], 'ok': True, 'verified_sender': True,
                   'from': 'DifferentChild', 'to': ['FixtureParent'], 'message_id': 4}
        runner.save(self.state / 'mail-send-receipt.json', receipt)
        self.assertFalse(runner.status(self.state)['mail_probe_accepted'])
        receipt['from'] = 'FixtureChild'
        runner.save(self.state / 'mail-send-receipt.json', receipt)
        self.assertTrue(runner.status(self.state)['mail_probe_accepted'])
        self.assertIsNone(runner.status(self.state)['full_managed_task_accepted'])
        receipt = mail_lifecycle.retire(self.state, self.product.run, require_release=False)
        self.assertFalse(receipt['confirmed'])
        self.assertIsNone(receipt['retired_at'])

    def test_profile_link_or_shared_directory_cannot_point_outside(self):
        self.bind()
        config = runner.read(self.state / 'run-config.json')
        profile = Path(config['profile_home'])
        outside = self.root / 'other-profile'
        outside.mkdir()
        link = profile / 'memories' / 'other'
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'outside the current profile'):
            runner.check_configured(self.state)
        link.unlink()
        plan = runner.read(profile / 'config.yaml')
        plan['skills']['external_dirs'] = [str(outside)]
        with self.assertRaisesRegex(ValueError, 'current profile'):
            runner.check_profile_paths(profile, plan)

    def test_owned_supervisor_with_fake_executable_and_clean_environment(self):
        self.bind()
        config = runner.read(self.state / 'run-config.json')
        fake = Path(config['hermes_executable'])
        fake.write_text('#!' + sys.executable + '\n' +
            'import json,os,sys\n'
            'assert "DEEPSEEK_API_KEY" not in os.environ\n'
            'assert "CHILD_REGISTRATION_TOKEN" not in os.environ\n'
            'assert os.environ["HERMES_DISABLE_LAZY_INSTALLS"] == "1"\n'
            'assert os.environ["UV_OFFLINE"] == "1"\n'
            'print(json.dumps({"args":sys.argv[1:],"home":os.environ["HERMES_HOME"]}))\n')
        fake.chmod(0o700)
        runner.save(self.state / 'mail-probe.json', {'schema': True, 'backend': True, 'ok': True,
                                                  'agent_name': 'FixtureChild', 'fixture': ''})
        runner.start(self.state, self.product.run)
        env = dict(os.environ, DEEPSEEK_API_KEY='offline-canary', CHILD_REGISTRATION_TOKEN='offline-canary')
        result = subprocess.run([sys.executable, '-I', '-B', str(ADAPTER / 'pane_entry.py'),
                                 '--state', str(self.state)], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads((self.state / 'stdout.log').read_text())
        self.assertEqual(output['args'][:2], ['-p', 'strata'])
        self.assertEqual(output['home'], config['managed_root'])
        self.assertIn((self.state / 'task.md').read_text(), output['args'])
        self.assertTrue(runner.stop(self.state, self.product.run)['group_absent'])
        self.assertFalse(runner.status(self.state)['mail_probe_accepted'])

    def test_schema_alone_cannot_start_or_validate_remote_identity(self):
        self.bind()
        runner.save(self.state / 'mail-probe.json', {'schema': True, 'backend': False, 'ok': False,
                                                  'agent_name': 'FixtureChild'})
        with self.assertRaisesRegex(ValueError, 'Authenticated'):
            runner.start(self.state, self.product.run)
        proxy = runner.read(self.state / 'mcp.json')['mcpServers']['orrery-mail']
        identity = runner.read(self.state / 'mail-identity.json')
        local = {'agent_name': 'FixtureChild', 'project_key': identity['project_key'], 'program': 'claude-code',
                 'lineage': {'parent_agent': 'FixtureParent'}}
        with self.assertRaisesRegex(ValueError, 'remote identity'):
            mail_probe.check_binding(local, {'name': 'FixtureChild', 'program': 'claude-code'}, identity, proxy)
        mail_probe.check_binding(local, {'name': 'FixtureChild', 'program': 'hermes'}, identity, proxy)

    def test_plugin_fixed_send_current_profile_and_actual_receipt(self):
        self.bind()
        config = runner.read(self.state / 'run-config.json')
        callbacks = {}
        class Context:
            def register_hook(self, name, callback):
                callbacks[name] = callback
        previous = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = config['profile_home']
        try:
            mail_plugin.install(Context(), self.state)
            before, after = callbacks['pre_tool_call'], callbacks['post_tool_call']
            self.assertEqual(before('session_search', {'profile': 'other'})['action'], 'block')
            expected = runner.read(self.state / 'message.json')
            self.assertEqual(before(mail_plugin.MAIL_TOOL, {**expected, 'to': ['DifferentParent']})['action'], 'block')
            self.assertIsNone(before(mail_plugin.MAIL_TOOL, expected))
            self.assertEqual(before(mail_plugin.MAIL_TOOL, expected)['action'], 'block')
            value = {'verified_sender': True, 'deliveries': [{'payload': {
                'id': 21, 'from': 'FixtureChild', 'to': ['FixtureParent']}}]}
            after(mail_plugin.MAIL_TOOL, json.dumps({'result': json.dumps(value)}))
            self.assertTrue(runner.status(self.state)['mail_probe_accepted'])
            self.assertIsNone(mail_probe.mail_receipt('I sent the message'))
        finally:
            if previous is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = previous

    def test_partial_registration_is_not_retried_and_live_root_is_rejected(self):
        calls = []
        def failed(argv, **kwargs):
            if argv[-1] == 'mode':
                return subprocess.CompletedProcess(argv, 0, 'legacy\n', '')
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, '', '')
        with self.assertRaisesRegex(ValueError, 'Preregistration failed'):
            mail_lifecycle.register(self.state, failed, generate_config=False)
        with self.assertRaises(FileExistsError):
            mail_lifecycle.register(self.state, failed, generate_config=False)
        self.assertEqual(len(calls), 1)
        self.config['managed_root'] = str(Path(self.config['home']) / '.hermes' / 'child')
        runner.save(self.config_path, self.config)
        with self.assertRaisesRegex(ValueError, 'live Hermes'):
            runner.initialize(self.config_path, self.root / 'other-state', 'strata', self.product.run)

    def test_stdio_contract_with_offline_sdk_server(self):
        python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not python:
            self.skipTest('Explicit installed Hermes dependency interpreter required')
        self.bind()
        server = self.root / 'fake_mcp.py'
        server.write_text(r'''
import os,sys
def audit(event, args):
    if event in ('socket.connect','socket.getaddrinfo'):
        raise RuntimeError('offline fixture forbids network')
sys.addaudithook(audit)
from mcp.server import MCPServer
server=MCPServer('offline-product-shape')
@server.tool()
def runtime_status() -> dict:
    return {'agent_name':'FixtureChild','project_key':os.environ['AGENTSTACK_PROJECT_KEY'],
            'program':'claude-code','lineage':{'parent_agent':'FixtureParent'}}
@server.tool()
def whois(name:str) -> dict:
    if os.environ['AGENTSTACK_MCP_URL']=='http://127.0.0.1:1/mcp':
        raise RuntimeError('offline fixture backend unavailable')
    return {'name':name,'program':'hermes','retired_at':None}
@server.tool()
def send_message(to:list[str],subject:str,body_md:str) -> dict:
    assert 'DEEPSEEK_API_KEY' not in os.environ
    return {'verified_sender':True,'deliveries':[{'payload':{'id':41,'from':'FixtureChild','to':to}}]}
@server.tool()
def reserve_files(paths:list[str],exclusive:bool,ttl_seconds:int,reason:str) -> dict:
    raise RuntimeError('not exercised by the Mail-only pilot')
@server.tool()
def release_reservations(file_reservation_ids:list[int]) -> dict:
    raise RuntimeError('not exercised by the Mail-only pilot')
server.run(transport='stdio')
''')
        proxy_path = self.product.product / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
        proxy_path.write_text('#!/bin/sh\nexec ' + shlex.join([python, '-I', '-B', str(server)]) + '\n')
        proxy_path.chmod(0o700)
        mcp = runner.read(self.state / 'mcp.json')
        mcp['mcpServers']['orrery-mail']['env']['AGENTSTACK_MCP_URL'] = 'https://mail.invalid/mcp'
        runner.save(self.state / 'mcp.json', mcp)
        env = {'HOME': str(self.root / 'home'), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        def invoke(*extra):
            return subprocess.run([python, '-I', '-B', str(ADAPTER / 'mail_probe.py'), '--state',
                                   str(self.state), *extra], env=env, capture_output=True, text=True, timeout=30)
        result = invoke('send')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proof = runner.read(self.state / 'mail-probe.json')
        self.assertTrue(proof['schema'] and proof['backend'] and proof['ok'])
        self.assertEqual(runner.read(self.state / 'mail-send-receipt.json')['message_id'], 41)
        result = invoke('--fixture', 'backend-unreachable')
        self.assertEqual(result.returncode, 2, result.stderr)
        proof = runner.read(self.state / 'mail-probe-backend-unreachable.json')
        self.assertTrue(proof['schema'])
        self.assertFalse(proof['backend'] or proof['ok'])
        result = invoke('--fixture', 'process-unreachable')
        self.assertEqual(result.returncode, 2, result.stderr)
        proof = runner.read(self.state / 'mail-probe-process-unreachable.json')
        self.assertFalse(proof['schema'] or proof['backend'] or proof['ok'])
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if source:
            # Exercise real Hermes discovery -> real tool dispatcher -> fixture
            # stdio server -> real post-tool receipt hook, without an LLM call.
            (self.state / 'report-intent.json').unlink()
            (self.state / 'mail-send-receipt.json').unlink()
            script = self.root / 'hermes_tool_pipeline.py'
            script.write_text(r'''
import json,logging,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,sys.argv[2])
def audit(event,args):
    if event in ('socket.connect','socket.getaddrinfo'):
        raise RuntimeError('offline fixture forbids network')
sys.addaudithook(audit)
from hermes_cli import plugins
plugins.discover_plugins()
loaded=plugins.get_plugin_manager()._plugins['orrery-mail-receipt']
assert loaded.enabled and loaded.error is None
from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
try:
    ensure_mcp_discovery_before_agent_build(logger=logging.getLogger('offline-mail-fixture'),single_query=True)
    import model_tools
    state=Path(sys.argv[3])
    message=json.loads((state/'message.json').read_text())
    result=model_tools.handle_function_call('mcp__orrery_mail__send_message',message,task_id='offline-mail')
    receipt=json.loads((state/'mail-send-receipt.json').read_text())
    assert receipt['ok'] is True and receipt['message_id']==41,result
    print(json.dumps({'real_hermes_tool_pipeline':True,'fixture_message_id':41}))
finally:
    shutdown_mcp_servers()
''')
            profile = runner.read(self.state / 'run-config.json')['profile_home']
            env.update(HERMES_HOME=profile, ORRERY_HERMES_STATE_ROOT=str(self.state),
                       ORRERY_HERMES_ADAPTER_DIR=str(ADAPTER), HERMES_DISABLE_LAZY_INSTALLS='1', UV_OFFLINE='1')
            result = subprocess.run([python, '-I', '-B', str(script), source, str(ADAPTER), str(self.state)],
                env=env, capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])['real_hermes_tool_pipeline'])


if __name__ == '__main__':
    unittest.main()
