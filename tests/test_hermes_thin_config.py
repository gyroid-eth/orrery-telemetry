"""Thin generation: private synthetic trees, no upstream import or credential call."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('hermes_thin_config', ROOT / 'integrations/hermes/thin_config.py')
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)


class ThinConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.runtime = self.base / 'runtime'
        self.source = self.runtime / 'source'
        (self.source / 'hermes_cli').mkdir(parents=True)
        (self.source / 'hermes_cli/auth.py').write_text("_REGISTRY_ROWS: tuple = [('deepseek',), ('anthropic',)]\n")
        (self.source / 'hermes_cli/config_defaults.py').write_text(
            "DEFAULT_CONFIG = {'auxiliary': {'vision': _aux(120), 'curator': {}, 'transient_retries': 2}}\n")
        store = self.runtime / 'installs' / hashlib.sha256(str(self.source).encode()).hexdigest()[:16]
        venv = store / 'environments/fixture'
        (venv / 'bin').mkdir(parents=True)
        for name in ('hermes', 'python'):
            (venv / 'bin' / name).write_text('synthetic executable')
            (venv / 'bin' / name).chmod(0o700)
        (venv / 'pyvenv.cfg').write_text('synthetic venv')
        self.save(store / 'facts.json', {'packages': {'venv': {'environment': str(venv)}}})
        self.template = self.base / 'template.json'
        self.profile_spec = {'provider': 'fixture-api', 'model': 'fixture-main',
            'base_url': 'https://provider.invalid/v1', 'api_mode': 'chat_completions',
            'key_cmd': '/fixture/key-helper --selector fixture-key'}
        self.template_data = {'managed_root': str(self.runtime), 'hermes_source': str(self.source),
            'hermes_executable': str(venv / 'bin/hermes'), 'hermes_python': str(venv / 'bin/python'),
            'profiles': {'deepseek': self.profile_spec, 'strata': self.profile_spec}}
        self.save(self.template, self.template_data)
        self.manifest = self.base / 'manifest.json'
        self.manifest_data = {'harnesses': {'hermes': {'config_file': str(self.template)}},
                              'backends': {'deepseek': {'model': 'fixture-main', 'models': ['fixture-main', 'fixture-helper']}}}
        self.save(self.manifest, self.manifest_data)
        self.save(self.base / 'mcp.json', {'mcpServers': {'orrery_mail': {'command': '/fixture/proxy', 'args': ['--bound-token-file', '/fixture/token-reference']}}})
        (self.base / 'instructions').write_text('Shared canonical instructions')
        (self.base / 'prompt').write_text('literal `date` $(touch forbidden)\nsecond line')
        self.document = {'program': 'hermes', 'agent_name': 'FixtureAgent', 'project_key': '/fixture/project', 'backend_id': 'deepseek',
            'model': 'fixture-main', 'manifest_file': str(self.manifest), 'product_root': str(ROOT),
            'state_dir': str(self.base / 'child'), 'cwd': str(self.base), 'mcp_config': str(self.base / 'mcp.json'),
            'instructions_file': str(self.base / 'instructions'), 'prompt_file': str(self.base / 'prompt')}
        self.enterContext(patch.object(subprocess, 'Popen', side_effect=AssertionError('No subprocess during generation')))
        self.enterContext(patch.object(socket, 'socket', side_effect=AssertionError('No network during generation')))

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def generate(self, **overrides):
        return config.generate({**self.document, **overrides})

    @property
    def profile(self):
        return config.child_home(self.document)

    def test_static_admission_does_not_create_profile_or_state(self):
        subset = {key: self.document[key] for key in ('manifest_file', 'product_root', 'backend_id', 'model')}
        self.assertEqual(config.validate_input(subset)['provider'], 'fixture-api')
        self.assertFalse((self.runtime / 'profiles').exists())
        self.assertFalse((self.base / 'child').exists())

    def test_native_plan_no_caps_no_key_env_and_literal_query_file(self):
        plan = self.generate()
        self.assertEqual(plan['argv'][1:3], ['chat', '--provider'])
        self.assertRegex(plan['argv'][3], '^orrery-deepseek-[a-f0-9]{24}$')
        self.assertEqual(plan['argv'][4:6], ['--model', 'fixture-main'])
        self.assertEqual(plan['argv'][-2:], ['--query-file', str(self.base / 'prompt')])
        for option in ('--oneshot', '--toolsets', '--reasoning', '--quiet', '--yolo'):
            self.assertNotIn(option, plan['argv'])
        overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
        self.assertEqual(overlay['display']['busy_input_mode'], 'queue')
        self.assertEqual(overlay['auxiliary']['vision']['model'], 'fixture-main')
        self.assertNotIn('security', overlay)
        self.assertNotIn('max_tokens', json.dumps(overlay))
        self.assertNotIn('provider_request_limit', json.dumps(overlay))
        self.assertEqual(config.read(plan['env']['AGENTSTACK_LLM_KEY_DEFINITIONS'])['backends']['hermes']['auth_command'][0], '/fixture/key-helper')
        self.assertFalse(any('API_KEY' in name or name == 'HERMES_MANAGED' for name in plan['env']))
        self.assertFalse((self.base / 'forbidden').exists())

    def test_dynamic_selected_url_and_model_ignore_old_per_model_settings(self):
        self.template_data['profiles']['strata'] = {'provider': 'fixture-api', 'key_cmd': self.profile_spec['key_cmd']}
        self.save(self.template, self.template_data)
        self.manifest_data['backends']['strata'] = {'model_source': 'server',
            'urls': ['https://first.invalid/v1', 'https://second.invalid/v1', 'https://third.invalid/v1']}
        self.save(self.manifest, self.manifest_data)
        document = {**self.document, 'backend_id': 'strata', 'model': 'new-wire-id',
            'url_index': 2, 'server_url': 'https://third.invalid/v1', 'server_context': {'n_ctx': 131072}}
        plan = config.generate(document)
        overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
        route = overlay['providers'][plan['argv'][3]]
        self.assertEqual(route['base_url'], 'https://third.invalid/v1')
        self.assertEqual(route['context_length'], 131072)
        self.assertEqual(overlay['model']['default'], 'new-wire-id')
        self.assertTrue(all(row['model'] == 'new-wire-id' for row in overlay['auxiliary'].values()))
        self.manifest_data['backends']['strata']['context_override'] = 96000
        self.save(self.manifest, self.manifest_data)
        self.assertEqual(config.validate_input({**document, 'server_context': {}})['route']['context_length'], 96000)
        with self.assertRaisesRegex(ValueError, 'invalid_runtime'):
            config.generate({**document, 'server_url': 'https://first.invalid/v1'})

    def test_two_children_keep_separate_memory_profiles_and_overlays(self):
        first = self.generate()
        profile = Path(first['profile_home'])
        self.save(profile / 'config.yaml', {'agent': {'system_prompt': 'User instructions'}, 'memory': {'memory_enabled': True}})
        marker = profile / 'memories/MEMORY.md'
        marker.parent.mkdir()
        marker.write_text('kept memory')
        second = self.generate(agent_name='OtherAgent', state_dir=str(self.base / 'other'))
        self.assertNotEqual(first['profile_home'], second['profile_home'])
        self.assertNotEqual(first['config_dir'], second['config_dir'])
        self.assertNotEqual(first['argv'][3], second['argv'][3])
        self.assertEqual(marker.read_text(), 'kept memory')
        self.assertNotIn('User instructions', config.read(Path(second['config_dir']) / 'config.yaml')['agent']['system_prompt'])
        self.assertFalse((Path(second['profile_home']) / 'memories/MEMORY.md').exists())
        again = self.generate(state_dir=str(self.base / 'again'))
        self.assertEqual(again['profile_home'], first['profile_home'])
        self.assertIn('User instructions', config.read(Path(again['config_dir']) / 'config.yaml')['agent']['system_prompt'])
        self.assertEqual(config.read(profile / 'config.yaml')['mcp_servers'], {
            'orrery_mail': {'enabled': True, 'command': '/fixture/proxy',
                            'args': ['--bound-token-file', '/fixture/token-reference']}})

    def test_preexisting_private_provider_slug_is_not_merged(self):
        first = self.generate()
        path = Path(first['profile_home']) / 'config.yaml'
        self.save(path, {'providers': {first['argv'][3]: {'api': 'https://stale.invalid/v1'}}})
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, '^native_provider_conflict$'):
            self.generate()
        self.assertEqual(path.read_bytes(), original)

    def test_helper_and_main_only_same_backend_ids_and_effort_explicit(self):
        plan = self.generate(model='fixture-helper', helper_model='fixture-main', effort='high')
        overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
        self.assertEqual(overlay['delegation']['provider'], plan['argv'][3])
        self.assertEqual(overlay['delegation']['model'], 'fixture-main')
        self.assertEqual(plan['argv'][-4:-2], ['--reasoning', 'high'])
        for changes, reason in [({'helper_model': 'foreign/model'}, 'invalid_helper_model'),
                                ({'model': 'foreign/model'}, 'model_mismatch'),
                                ({'effort': 'invented'}, 'invalid_effort'),
                                ({'backend_id': 'unknown'}, 'unknown_backend')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '^' + reason + '$'):
                config.validate_input({**self.document, **changes})

    def test_deepseek_anthropic_selection_uses_native_chat_thinking_replay(self):
        self.profile_spec.update(base_url='https://api.deepseek.com/anthropic', api_mode='anthropic_messages')
        self.save(self.template, self.template_data)
        for effort in (None, 'none', 'low', 'high'):
            with self.subTest(effort=effort):
                plan = self.generate(helper_model='fixture-helper', effort=effort)
                overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
                provider = overlay['providers'][plan['argv'][3]]
                self.assertEqual(provider['api_mode'], 'chat_completions')
                self.assertEqual(provider['api'], 'https://api.deepseek.com/v1')
                self.assertEqual(provider['base_url'], 'https://api.deepseek.com/v1')
                self.assertEqual(overlay['model']['api_mode'], 'chat_completions')
                self.assertEqual(overlay['delegation']['provider'], plan['argv'][3])
                self.assertEqual(overlay['delegation']['model'], 'fixture-helper')
                if effort is None:
                    self.assertNotIn('--reasoning', plan['argv'])
                else:
                    index = plan['argv'].index('--reasoning')
                    self.assertEqual(plan['argv'][index + 1], effort)

    def test_strata_and_non_deepseek_hosts_keep_selected_transport(self):
        for backend, url in [('strata', 'https://api.deepseek.com/anthropic'),
                             ('deepseek', 'https://custom-provider.invalid/anthropic')]:
            with self.subTest(backend=backend):
                self.profile_spec.update(base_url=url, api_mode='anthropic_messages')
                self.save(self.template, self.template_data)
                self.manifest_data['backends'][backend] = self.manifest_data['backends']['deepseek']
                self.save(self.manifest, self.manifest_data)
                plan = self.generate(backend_id=backend)
                overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
                provider = overlay['providers'][plan['argv'][3]]
                self.assertEqual(provider['api_mode'], 'anthropic_messages')
                self.assertEqual(provider['base_url'], url)
                self.assertNotIn('--reasoning', plan['argv'])

    def test_resume_only_exact_id_and_empty_prompt_stays_interactive(self):
        (self.base / 'prompt').write_text('')
        first = self.generate()
        (self.profile / 'state.db').write_bytes(b'synthetic history presence fixture')
        plan = self.generate(resume_session_id='session-exact-123',
            native_profile_env={'HERMES_HOME': first['profile_home']})
        self.assertEqual(plan['argv'][-2:], ['--resume', 'session-exact-123'])
        self.assertNotIn('--query-file', plan['argv'])
        for value in ('', 'latest', '-c'):
            with self.assertRaisesRegex(ValueError, '^invalid_resume_session_id$'):
                self.generate(resume_session_id=value)

    def test_resume_reads_new_and_existing_yaml_profile_without_changing_answer(self):
        profile = self.profile
        path = profile / 'config.yaml'
        for raw in (None, '# keep comment\nmemory: {memory_enabled: true}\n'):
            with self.subTest(existing_yaml=raw is not None):
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    profile.mkdir(parents=True, exist_ok=True)
                    path.write_text(raw)
                first = self.generate()
                (profile / 'state.db').write_bytes(b'synthetic history presence fixture')
                before = path.read_bytes()
                self.assertEqual(config.read(path)['telemetry']['shared_metrics'],
                                 {'enabled': False, 'send': False, 'offer_version': 2})
                resumed = self.generate(resume_session_id='exact-owned-fixture-id',
                    native_profile_env={'HERMES_HOME': first['profile_home']})
                index = resumed['argv'].index('--resume')
                self.assertEqual(resumed['argv'][index + 1], 'exact-owned-fixture-id')
                self.assertEqual(resumed['profile_home'], first['profile_home'])
                self.assertEqual(path.read_bytes(), before)
                if raw:
                    self.assertIn(raw.encode(), before)

    def test_invalid_telemetry_config_keeps_specific_cli_reason(self):
        self.save(self.profile / 'config.yaml', {'telemetry': 'invalid fixture'})
        source, output = self.base / 'invalid-input.json', self.base / 'invalid-plan.json'
        self.save(source, self.document)
        stderr = io.StringIO()
        with patch('sys.argv', ['thin_config.py', '--input', str(source), '--out', str(output)]), \
                patch('sys.stderr', stderr), self.assertRaises(SystemExit) as stopped:
            config.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(stderr.getvalue(), 'HERMES_THIN_CONFIG_ERROR=invalid_telemetry_config\n')
        self.assertFalse(output.exists())

    def test_resume_profile_pin_is_canonical_and_checked_before_writes(self):
        with self.assertRaisesRegex(ValueError, '^native_resume_profile_changed$'):
            self.generate(native_profile_env={'HERMES_HOME': str(self.base / 'other-profile')})
        self.assertFalse((self.runtime / 'profiles').exists())
        self.assertFalse((self.base / 'child').exists())
        persistent = self.base / 'persistent'
        persistent.mkdir()
        (self.runtime / 'profiles').symlink_to(persistent, target_is_directory=True)
        profile = self.profile
        plan = self.generate(native_profile_env={'HERMES_HOME': str(profile)})
        self.assertEqual(Path(plan['env']['HERMES_HOME']).resolve(), profile)

    def test_cli_error_has_common_launcher_marker_and_no_output_plan(self):
        source, output = self.base / 'input.json', self.base / 'plan.json'
        self.save(source, {**self.document, 'native_profile_env': {'HERMES_HOME': str(self.base / 'other')}})
        stderr = io.StringIO()
        with patch('sys.argv', ['thin_config.py', '--input', str(source), '--out', str(output)]), \
                patch('sys.stderr', stderr), self.assertRaises(SystemExit) as exit:
            config.main()
        self.assertEqual(exit.exception.code, 1)
        self.assertEqual(stderr.getvalue(), 'HERMES_THIN_CONFIG_ERROR=native_resume_profile_changed\n')
        self.assertFalse(output.exists())
        self.assertFalse((self.runtime / 'profiles').exists())

    def test_builtin_provider_rejected_before_any_files_created(self):
        for provider in ('deepseek', ' DeepSeek ', 'auto', 'main'):
            self.profile_spec['provider'] = provider
            self.save(self.template, self.template_data)
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, '^builtin_provider_collision$'):
                self.generate()
        self.assertFalse((self.runtime / 'profiles').exists())

    def test_mcp_inheritance_is_disabled_explicitly_or_refused(self):
        profile = self.profile
        self.save(profile / 'config.yaml', {'mcp_servers': {'unselected': {'command': '/never/run'}}})
        plan = self.generate(mcp_selection={'inherit': False})
        overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
        self.assertEqual(overlay['mcp_servers']['unselected'], {'enabled': False})
        self.save(profile / 'config.yaml', {'plugins': {'enabled': ['portable-example']}})
        with self.assertRaisesRegex(ValueError, '^inherited_mcp_conflict$'):
            self.generate(mcp_selection={'inherit': False})

    def test_selected_mcp_servers_project_raw_without_rewriting_other_profile_fields(self):
        profile = self.profile
        old = {'telemetry': {'shared_metrics': {'enabled': False, 'send': False, 'offer_version': 2}},
               'mcp_servers': {'orrery-mail': {'command': '/stale/retired-child-proxy'},
                               'chosen': {'command': '/stale/selected-server'},
                               'unrelated': {'command': '/keep/native-server'}}}
        self.save(profile / 'config.yaml', old)
        self.save(self.base / 'mcp.json', {'mcpServers': {
            'orrery_mail': {'command': '/fixture/proxy'}, 'chosen': {'command': '/selected/native-server'}}})
        before = (profile / 'config.yaml').read_bytes()
        plan = self.generate()
        overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
        self.assertEqual(overlay['mcp_servers']['orrery-mail'], {'enabled': False})
        self.assertEqual(overlay['mcp_servers']['orrery_mail']['command'], '/fixture/proxy')
        self.assertEqual(overlay['mcp_servers']['unrelated'], {'enabled': False})
        self.assertEqual(overlay['mcp_servers']['chosen'], {'enabled': True, 'command': '/selected/native-server'})
        revised = (profile / 'config.yaml').read_bytes()
        self.assertEqual(revised.split(b'"mcp_servers"')[0], before.split(b'"mcp_servers"')[0])
        raw = config.read(profile / 'config.yaml')['mcp_servers']
        self.assertEqual(raw['chosen'], {'enabled': True, 'command': '/selected/native-server'})
        self.assertEqual(raw['unrelated'], {'command': '/keep/native-server', 'enabled': False})
        self.assertEqual(raw['orrery-mail'], {'command': '/stale/retired-child-proxy', 'enabled': False})
        self.assertEqual(raw['orrery_mail'], {'enabled': True, 'command': '/fixture/proxy'})

    def test_packaging_locale_reference_is_from_selected_source(self):
        plan = self.generate()
        self.assertEqual(plan['env']['HERMES_BUNDLED_LOCALES'], str(self.source / 'locales'))

    def test_same_named_mcp_transport_conflict_has_named_diagnostic_without_profile_writes(self):
        profile = self.profile
        old = {'mcp_servers': {'orrery_mail': {'url': 'https://stale.invalid/mcp'}}}
        self.save(profile / 'config.yaml', old)
        before = (profile / 'config.yaml').read_bytes()
        with self.assertRaisesRegex(ValueError, '^inherited_mcp_conflict$'):
            self.generate()
        log = self.base / 'child/config-generation.log'
        self.assertEqual(json.loads(log.read_text()), {'event': 'inherited_mcp_conflict', 'mcp_server': 'orrery_mail'})
        source, output = self.base / 'conflict-input.json', self.base / 'conflict-plan.json'
        self.save(source, self.document)
        stderr = io.StringIO()
        with patch('sys.argv', ['thin_config.py', '--input', str(source), '--out', str(output)]), \
                patch('sys.stderr', stderr), self.assertRaises(SystemExit) as stopped:
            config.main()
        self.assertEqual(stopped.exception.code, 1)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(lines, ['HERMES_THIN_CONFIG_ERROR=inherited_mcp_conflict'])
        self.assertFalse(output.exists())
        self.assertEqual((profile / 'config.yaml').read_bytes(), before)

    def test_same_named_mcp_command_and_args_are_replaced_by_selection(self):
        profile = self.profile
        self.save(profile / 'config.yaml', {'mcp_servers': {'orrery_mail': {
            'command': '/old/proxy', 'args': ['old-reference']}}})
        plan = self.generate()
        overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
        self.assertEqual(overlay['mcp_servers']['orrery_mail'], {'enabled': True, **config.read(self.base / 'mcp.json')['mcpServers']['orrery_mail']})

    def test_raw_mcp_reselection_updates_refs_and_honors_explicit_disabled(self):
        first = self.generate()
        profile = Path(first['profile_home'])
        path = profile / 'config.yaml'
        self.save(self.base / 'mcp.json', {'mcpServers': {}})
        disabled = self.generate()
        self.assertEqual(config.read(path)['mcp_servers']['orrery_mail']['enabled'], False)
        self.assertEqual(config.read(Path(disabled['config_dir']) / 'config.yaml')['mcp_servers']['orrery_mail'], {'enabled': False})
        selected = {'command': '/updated/proxy', 'args': ['new-reference']}
        self.save(self.base / 'mcp.json', {'mcpServers': {'orrery_mail': selected}})
        # Presence is the pre-existing static resume precondition, not native history evidence.
        (profile / 'state.db').write_bytes(b'synthetic history presence fixture')
        resumed = self.generate(resume_session_id='fixture-native-id', native_profile_env={'HERMES_HOME': str(profile)})
        self.assertEqual(resumed['env']['HERMES_HOME'], str(profile))
        self.assertEqual(resumed['argv'][-2:], ['--query-file', str(self.base / 'prompt')])
        self.assertEqual(resumed['argv'][resumed['argv'].index('--resume') + 1], 'fixture-native-id')
        self.assertEqual(config.read(path)['mcp_servers']['orrery_mail'], {'enabled': True, **selected})
        self.save(self.base / 'mcp.json', {'mcpServers': {'orrery_mail': {**selected, 'enabled': False}}})
        self.generate()
        self.assertIs(config.read(path)['mcp_servers']['orrery_mail']['enabled'], False)

    def test_invalid_raw_mcp_map_is_rejected_without_profile_writes(self):
        for value in (None, [], 'invalid', {'fixture': None}):
            with self.subTest(value=value):
                path = self.profile / 'config.yaml'
                self.save(path, {'mcp_servers': value})
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, '^invalid_mcp_config$'):
                    self.generate()
                self.assertEqual(path.read_bytes(), before)

    def test_product_delegate_is_linked_into_native_skills(self):
        plan = self.generate()
        linked = Path(plan['env']['HERMES_HOME']) / 'skills/delegate'
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.resolve(), (ROOT / 'skills/delegate').resolve())
        self.assertEqual((linked / 'SKILL.md').read_bytes(),
                         (ROOT / 'skills/delegate/SKILL.md').read_bytes())

    def test_unowned_skill_is_not_replaced(self):
        path = self.profile / 'skills/delegate'
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text('user skill')
        with self.assertRaisesRegex(ValueError, '^owned_link_conflict$'):
            self.generate()
        self.assertEqual((path / 'SKILL.md').read_text(), 'user skill')

    def test_native_personality_kept_with_shared_instructions(self):
        (self.source / 'hermes_cli/personality.py').write_text("BUILTIN_PERSONALITIES: dict = {'concise': 'Be concise'}\n")
        self.save(self.profile / 'config.yaml', {'display': {'personality': 'concise'}})
        plan = self.generate()
        self.assertEqual(plan['env']['HERMES_EPHEMERAL_SYSTEM_PROMPT'], 'Be concise\n\nShared canonical instructions')



NATIVE_SCRIPT = r"""import json, os, sys, shutil, socket, subprocess
real_popen, real_socket = subprocess.Popen, socket.socket
from pathlib import Path
sys.path.insert(0, os.environ['FIXTURE_PRODUCT'])
sys.path.insert(0, str(Path(os.environ['FIXTURE_PRODUCT']) / 'tests'))
from test_hermes_thin_config import ThinConfig
from integrations.hermes.thin_config import generate
fixture = ThinConfig()
fixture.setUp()
try:
    source = Path(os.environ['ORRERY_HERMES_TEST_SOURCE'])
    for name in ('auth.py', 'config_defaults.py'):
        shutil.copyfile(source / 'hermes_cli' / name, fixture.source / 'hermes_cli' / name)
    plan = generate(fixture.document)
    broken = os.environ.get('FIXTURE_BROKEN_GUARD') == '1'
    if broken:
        entry = Path(plan['guard']['plugin'])
        entry.unlink()
        entry.write_text("raise RuntimeError('fixture guard import failed')\n")
    os.environ.update(plan['env'])
    os.environ.update(AGENTSTACK_HOME=str(fixture.base / 'fake-product'), AGENT_NAME='FixtureAgent')
    hooks = Path(os.environ['AGENTSTACK_HOME']) / 'hooks'
    hooks.mkdir(parents=True)
    for name in ('check-agent-registered.sh', 'check-file-reservation.sh'):
        (hooks / name).write_text('echo thin-fixture-registration-denied >&2\nexit 2\n')
    release_log = fixture.base / 'released.json'
    (hooks / 'release-file-reservation.sh').write_text('cat >> ' + str(release_log) + '\n')
    (hooks / 'deny-llm-key-read.py').write_text('raise SystemExit(0)\n')
    subprocess.Popen, socket.socket = real_popen, real_socket
    def audit(name, args):
        if name in ('socket.connect', 'socket.getaddrinfo'):
            raise RuntimeError('network forbidden')
        if name == 'subprocess.Popen':
            argv = args[1]
            executable = Path(argv[0]).name
            if executable == 'git' and (argv[1:] == ['branch', '--show-current'] or argv[1:] == ['tag', '--merged', 'HEAD', '--list', 'v[0-9]*']):
                return
            if executable == 'git' and any(item in argv for item in ('rev-parse', 'describe', 'log', 'status', 'show', 'rev-list')):
                return  # legacy unstamped source only, read-only
            if executable == 'bash':
                return  # native LocalEnvironment's private file-operation scripts
            if len(argv) > 1 and Path(argv[1]).parent == hooks:
                return
            raise RuntimeError('unexpected subprocess forbidden: ' + executable)
    sys.addaudithook(audit)
    sys.path.insert(0, str(source))
    from hermes_cli.config import load_config
    loaded = load_config()
    assert loaded['display']['busy_input_mode'] == 'queue'
    assert loaded['agent']['system_prompt'] == 'Shared canonical instructions'
    assert loaded['mcp_servers']['orrery_mail']['command'] == '/fixture/proxy'
    from hermes_cli import plugins
    plugins.discover_plugins()
    if not broken:
        result = plugins.get_pre_tool_call_block_message('write_file', {'path': str(fixture.base / 'no-write'), 'content': 'denied'}, task_id='fixture', session_id='fixture')
        assert result == 'ORRERY hook denied: check-agent-registered.sh\nthin-fixture-registration-denied', 'native plugin did not reach common hook: ' + str(result)
        assert not (fixture.base / 'no-write').exists()
        for name in ('check-agent-registered.sh', 'check-file-reservation.sh'):
            (hooks / name).write_text('exit 0\n')
        (hooks / 'deny-llm-key-read.py').unlink()
        (hooks / 'deny-llm-key-read.py').symlink_to(Path(os.environ['FIXTURE_PRODUCT']) / 'hooks/deny-llm-key-read.py')
        denied = plugins.get_pre_tool_call_block_message('terminal', {'command': '/fixture/key-helper --selector fixture-key'}, task_id='fixture', session_id='fixture')
        import ast
        guard_tree = ast.parse((Path(os.environ['FIXTURE_PRODUCT']) / 'hooks/deny-llm-key-read.py').read_text())
        public_message = next(ast.literal_eval(node.value) for node in guard_tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'MESSAGE' for target in node.targets))
        expected_denial = 'ORRERY hook denied: deny-llm-key-read.py\n' + public_message
        assert denied == expected_denial
        protected = Path(plan['config_dir']) / 'config.yaml'
        protected_config = json.loads(protected.read_text())
        protected_config['fixture_secret'] = 'fixture-provider-key-canary'
        protected.write_text(json.dumps(protected_config))
        denied_read = plugins.get_pre_tool_call_block_message('read_file', {'path': str(protected)}, task_id='fixture', session_id='fixture')
        assert denied_read == expected_denial
        for value in ('/fixture/key-helper --selector fixture-key', str(protected), 'fixture-provider-key-canary'):
            assert value not in denied and value not in denied_read
        from agent.delegation_context import delegated_child_context
        plugins.invoke_hook('on_session_start', session_id='owned-native-session')
        native_session = Path(plan['env']['AGENTSTACK_NATIVE_SESSION_FILE'])
        assert json.loads(native_session.read_text())['native_session_id'] == 'owned-native-session'
        with delegated_child_context():
            plugins.invoke_hook('on_session_start', session_id='delegated-session')
        assert json.loads(native_session.read_text())['native_session_id'] == 'owned-native-session'
    target = fixture.base / 'allowed.txt'
    args = {'path': str(target), 'content': 'native-write-ok'}
    if not broken:
        (hooks / 'check-file-reservation.sh').write_text("printf '%s\\n' '{\"systemMessage\":\"fixture public reservation warning\"}'\n")
    assert plugins.get_pre_tool_call_block_message('write_file', args, task_id='fixture', session_id='fixture') is None
    from tools.file_tools import _handle_write_file
    output = _handle_write_file(args, task_id='fixture', session_id='fixture')
    assert target.read_text() == 'native-write-ok', output
    if not broken:
        from model_tools import _emit_post_tool_call_hook
        _emit_post_tool_call_hook(function_name='write_file', function_args=args, result=output,
                                  task_id='fixture', session_id='fixture')
        released = json.loads(release_log.read_text())
        assert released['tool_input']['file_path'] == str(target)
        assert released['tool_result'] == {'success': True}
        assert 'native-write-ok' not in str(released['tool_result'])
        release_log.unlink()
        _emit_post_tool_call_hook(function_name='write_file', function_args=args,
                                  result='{"error":"fixture failure"}', task_id='fixture')
        assert not release_log.exists()
    print(json.dumps({'config':'loaded','native_pre_tool':'blocked','native_write':'allowed','key_helper_calls':0,'network':0,'guard_mode':'broken' if broken else 'loaded'}))
finally:
    if 'tools.terminal_tool_lifecycle' in sys.modules:
        sys.modules['tools.terminal_tool_lifecycle'].cleanup_all_environments()
    fixture.doCleanups()
"""


class NativeThinConfig(unittest.TestCase):
    def run_native(self, broken=False):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not source or not python:
            self.skipTest('Explicit installed dependency interpreter/source required')
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([python, '-B', '-c', NATIVE_SCRIPT], cwd=ROOT,
                env={'HOME': temporary, 'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
                     'UV_OFFLINE': '1', 'HERMES_DISABLE_LAZY_INSTALLS': '1',
                     'FIXTURE_PRODUCT': str(ROOT), 'ORRERY_HERMES_TEST_SOURCE': source,
                     'FIXTURE_BROKEN_GUARD': '1' if broken else '0'},
                text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['native_write'], 'allowed')
            self.assertEqual(json.loads(result.stdout)['guard_mode'], 'broken' if broken else 'loaded')
            if broken:
                self.assertIn('fixture guard import failed', result.stderr)
            else:
                self.assertIn('fixture public reservation warning', result.stderr)


    def test_pinned_native_loader_dispatcher_and_file_handler_offline(self):
        self.run_native()

    def test_broken_plugin_warns_and_native_continues_without_added_gate(self):
        self.run_native(broken=True)


def test_secret_service_ref_projects_to_native_route_and_guard_without_lookup(monkeypatch):
    fixture = ThinConfig()
    fixture.setUp()
    try:
        reference = {'store': 'secret-service', 'attributes': {'service': 'fixture', 'account': 'child'}}
        fixture.manifest_data['backends']['deepseek']['credential_ref'] = reference
        fixture.save(fixture.manifest, fixture.manifest_data)
        with fixture.assertRaisesRegex(ValueError, '^credential_reference_conflict$'):
            config.validate_input(fixture.document)
        del fixture.profile_spec['key_cmd']
        fixture.save(fixture.template, fixture.template_data)
        # Metadata needs the route, not a key_cmd synthesized by generation.
        fixture.assertEqual(config.profile_route(fixture.profile_spec, 'deepseek')['route']['api_mode'], 'chat_completions')
        real_file, real_access = Path.is_file, os.access
        monkeypatch.setattr(sys, 'platform', 'linux')
        monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/fixture/bus')
        monkeypatch.setattr(Path, 'is_file', lambda p: str(p) == '/usr/bin/secret-tool' or real_file(p))
        monkeypatch.setattr(os, 'access', lambda p, m: str(p) == '/usr/bin/secret-tool' or real_access(p, m))
        plan = fixture.generate()
        fixture.assertEqual(plan['env']['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=/fixture/bus')
        definitions = config.read(plan['env']['AGENTSTACK_LLM_KEY_DEFINITIONS'])['backends']['hermes']
        fixture.assertEqual(definitions['credential_ref'], reference)
        command = definitions['auth_command']
        fixture.assertEqual(command[-4:], ['--manifest', str(fixture.manifest), '--backend', 'deepseek'])
        fixture.assertNotIn('service', plan['env'])
        overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
        native = overlay['custom_providers'][0] if overlay.get('custom_providers') else overlay['providers'][plan['argv'][3]]
        import shlex
        fixture.assertEqual(shlex.split(native['key_cmd']), command)
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
