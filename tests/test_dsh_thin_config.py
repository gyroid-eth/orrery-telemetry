"""Thin generation, local skill discovery and static route admission."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('thin_dsh', ROOT / 'integrations/dsh/thin_config.py')
thin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(thin)


class ThinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.product = self.root / 'product'
        self.integration = self.product / 'integrations/dsh'
        self.integration.mkdir(parents=True)
        for plugin in ('orrery_tools.mjs', 'keychain_credentials.mjs'):
            # Static validation must not execute a plugin, even a broken one.
            (self.integration / plugin).write_text('throw new Error("MUST_NOT_IMPORT")')
        self.node = self.root / 'fixed-node'
        self.node.write_text('synthetic node; must not execute')
        self.node.chmod(0o700)
        self.runtime = self.root / 'generation/runtime'
        pins = thin.read(ROOT / 'integrations/dsh/runtime-pins.json')
        thin.save(self.integration / 'runtime-pins.json', pins)
        for pin in pins:
            thin.save(self.runtime / 'node_modules' / pin['name'] / 'package.json', {'version': pin['version']})
        entry = self.runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js'
        entry.parent.mkdir()
        entry.write_text('native CLI fixture')
        thin.save(self.runtime.parent / 'evidence/node-runtime.json', {
            'version': 'v24.3.0', 'execPath': str(self.node),
            'sha256': hashlib.sha256(self.node.read_bytes()).hexdigest()})
        source = self.runtime / 'node_modules/@deepseek-harness-tui/dsh-tui/lib/native.js'
        source.parent.mkdir()
        source.write_text('unmodified native fixture')
        native = self.runtime.parent / 'evidence/upstream-runtime.json'
        thin.save(self.integration / 'tui-datadir.patch.json', thin.read(ROOT / 'integrations/dsh/tui-datadir.patch.json'))
        thin.save(native, {'variant': 'plain-datadir', 'source_patch': 'tui-datadir-v1',
            'recipe_sha256': hashlib.sha256((self.integration / 'tui-datadir.patch.json').read_bytes()).hexdigest(),
            'files': [{'file': 'lib/native.js', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]})
        owner = self.runtime.parent / '.orrery-managed-install.json'
        thin.save(owner, {'harness': 'dsh', 'complete': True, 'variant': 'plain-datadir', 'thin_config_contract': 1,
            'details': {'node': str(self.node), 'runtime': str(self.runtime), 'runtime_receipt': str(native)}})
        backends = self.root / 'backends.json'
        thin.save(backends, {'backends': {'strata': {'base_url': 'https://strata.invalid', 'model': 'a',
            'keychain': {'service': 'fixture-service', 'account': 'fixture-account'},
            'model_definition': {'id': 'a', 'contextWindow': 131072}}}})
        self.template = self.root / 'template.json'
        thin.save(self.template, {'node': str(self.node), 'runtime': str(self.runtime),
            'generation_receipt': str(owner), 'backend_file': str(backends)})
        self.manifest = self.root / 'manifest.json'
        thin.save(self.manifest, {'enabled': True, 'backends': {'strata': {'model': 'a', 'models': ['a', 'b']}},
            'harnesses': {'dsh': {'config_file': str(self.template)}}})
        self.state = self.root / 'state'
        self.state.mkdir()
        prompt, instructions, mcp = [self.root / f for f in ('prompt.txt', 'instructions.md', 'mcp.json')]
        prompt.write_text('')
        instructions.write_text('shared rendered instructions')
        thin.save(mcp, {'mcpServers': {'orrery-mail': {'command': '/canonical/proxy', 'args': ['--stdio'],
            'env': {'AGENTSTACK_PROXY_PROGRAM': 'dsh'}}}})
        self.doc = dict(manifest_file=str(self.manifest), product_root=str(self.product), backend_id='strata',
            model='a', agent_name='ChildA', cwd=str(self.root), state_dir=str(self.state), runtime_dir=str(self.root),
            mcp_config=str(mcp), prompt_file=str(prompt), instructions_file=str(instructions), env={'AGENT_NAME': 'ChildA'},
            mcp_selection={'servers': {}, 'inherit': False, 'approved': {}, 'enabled_tools': {}})

    def rows(self, plan):
        return thin.read(Path(plan['config_dir']) / 'native.patch.json')

    def test_product_delegate_is_linked_into_native_skills(self):
        source = self.product / 'skills/delegate'
        source.mkdir(parents=True)
        content = (ROOT / 'skills/delegate/SKILL.md').read_bytes()
        (source / 'SKILL.md').write_bytes(content)
        plan = thin.generate(self.doc)
        linked = Path(plan['env']['DSH_HOME']) / 'skills/delegate'
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.resolve(), source.resolve())
        self.assertEqual((linked / 'SKILL.md').read_bytes(), content)

    def personal_fixture(self):
        for name, description in (('log', 'product'), ('delegate', 'product'), ('extra', 'product only')):
            (self.product / 'skills' / name).mkdir(parents=True)
            (self.product / 'skills' / name / 'SKILL.md').write_text(
                f'---\nname: {name}\ndescription: {description}\n---\n')
        personal = self.root / 'claude-config'
        (personal / 'skills/delegate').mkdir(parents=True)
        (personal / 'skills/delegate/SKILL.md').write_text('---\nname: delegate\ndescription: personal\n---\n')
        # A directory under another name that declares a product skill's name.
        (personal / 'skills/daily-log').mkdir()
        (personal / 'skills/daily-log/SKILL.md').write_text(
            '---\nname: log # the vault log\ndescription: personal\n---\n')
        return personal

    def test_personal_skills_come_from_agents_home_and_product_names_step_aside(self):
        personal = self.personal_fixture()
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                plan = thin.generate({**self.doc, 'personal_skills_home': None})
                home = Path(plan['env']['DSH_HOME'])
                self.assertNotIn('DSH_AGENTS_HOME', plan['env'])
                self.assertEqual(sorted(path.name for path in (home / 'skills').iterdir()),
                                 ['delegate', 'extra', 'log'])
                self.assertFalse((self.state / 'personal-skills.json').exists())
                # dsh-tui's preset finds $DSH_AGENTS_HOME/skills after DSH_HOME/skills,
                # so the product links under personal names are removed.
                own = home / 'skills/elsewhere'
                own.symlink_to(self.root)
                with patch.object(thin.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, stdout='["delegate", "log"]')):
                    plan = thin.generate({**self.doc, 'personal_skills_home': str(personal)})
                self.assertEqual(plan['env']['DSH_AGENTS_HOME'], str(personal))
                self.assertEqual(sorted(path.name for path in (home / 'skills').iterdir()), ['elsewhere', 'extra'])
                self.assertEqual(thin.read(self.state / 'personal-skills.json'), {
                    'agents_home': str(personal), 'reason': None, 'shadowed_product_skills': ['delegate', 'log']})
                self.assertFalse(any(row.get('id') == 'skill-filesystem' for row in self.rows(plan)))
                # Without the personal skills the product links come back.
                plan = thin.generate({**self.doc, 'personal_skills_home': None,
                                      'personal_skills_reason': 'unreadable_skill: log'})
                self.assertNotIn('DSH_AGENTS_HOME', plan['env'])
                self.assertEqual(sorted(path.name for path in (home / 'skills').iterdir()),
                                 ['delegate', 'elsewhere', 'extra', 'log'])
                self.assertEqual(thin.read(self.state / 'personal-skills.json')['reason'], 'unreadable_skill: log')
                own.unlink()
                plan = thin.generate({**self.doc, 'personal_skills_home': str(self.root / 'absent')})
                self.assertNotIn('DSH_AGENTS_HOME', plan['env'])
                self.assertEqual(thin.read(self.state / 'personal-skills.json')['reason'],
                                 'personal_skills_root_unavailable')

    def test_preset_skill_provider_returns_the_personal_skills(self):
        # The real dsh-tui preset provider, not the host patch rows it ignores.
        runtime = os.environ.get('ORRERY_DSH_TEST_RUNTIME')
        node = os.environ.get('ORRERY_DSH_TEST_NODE') or shutil.which('node')
        if not runtime or not node:
            self.skipTest('Explicit installed dsh runtime and node required')
        provider = Path(runtime) / 'node_modules/@deepseek-ai/dsh-skill-filesystem/lib/index.js'
        personal = self.personal_fixture()

        def candidates(plan):
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root), **plan['env']}
            result = subprocess.run([node, str(ROOT / 'tests/dsh_skill_candidates.mjs'), str(provider), plan['cwd']],
                                    env=env, capture_output=True, text=True, timeout=60, check=True)
            return [tuple(row) for row in json.loads(result.stdout)]

        facts = thin.validate_input(self.doc)
        real = os.path.realpath
        personal_delegate = personal / 'skills/delegate/SKILL.md'
        product_delegate = self.product / 'skills/delegate/SKILL.md'
        with patch.object(thin, 'validate_input', return_value={
            **facts, 'runtime': Path(runtime), 'node': Path(node),
        }):
            plan = thin.generate({**self.doc, 'personal_skills_home': str(personal)})
            self.assertEqual(candidates(plan), [
                ('delegate', 'user-agents', real(personal_delegate)),
                ('extra', 'user-dsh', real(self.product / 'skills/extra/SKILL.md')),
                ('log', 'user-agents', real(personal / 'skills/daily-log/SKILL.md'))])
            for title, metadata in {
                'missing description': 'name: delegate',
                'wrong type': 'name: delegate\ndescription: [a, b]',
                'bad YAML': 'name: delegate\ndescription: [a',
                'duplicate key': 'name: delegate\ndescription: one\ndescription: two',
                'invalid invocation policy': 'name: delegate\ndescription: personal\nuser-invocable: [bad]',
                'different declared name': 'name: other\ndescription: personal',
            }.items():
                with self.subTest(title=title):
                    personal_delegate.write_text(f'---\n{metadata}\n---\n')
                    plan = thin.generate({**self.doc, 'personal_skills_home': str(personal)})
                    self.assertIn(('delegate', 'user-dsh', real(product_delegate)), candidates(plan))
                    self.assertEqual(plan['env']['DSH_AGENTS_HOME'], str(personal))
            personal_delegate.unlink()
            flat = personal / 'skills/custom.md'
            flat.write_text('---\nname: delegate\ndescription: personal flat skill\n---\n')
            plan = thin.generate({**self.doc, 'personal_skills_home': str(personal)})
            self.assertIn(('delegate', 'user-agents', real(flat)), candidates(plan))
            plan = thin.generate({**self.doc, 'personal_skills_home': None})
            self.assertEqual([row[:2] for row in candidates(plan)],
                             [('delegate', 'user-dsh'), ('extra', 'user-dsh'), ('log', 'user-dsh')])

    def test_actual_generator_recovers_if_root_vanishes_before_start_or_resume(self):
        from hooks import harness_child

        personal = self.personal_fixture()
        personal_root = personal / 'skills'
        parked = personal / 'parked-skills'
        run = subprocess.run

        def remove_after_check(argv, **kwargs):
            source = thin.read(argv[argv.index('--input') + 1])
            self.assertEqual(source['personal_skills_home'], str(personal))
            personal_root.rename(parked)
            return run(argv, **kwargs)

        document = {**self.doc, 'program': 'dsh', 'runtime_dir': str(self.root.resolve()),
                    'tools': {'base': 'mail-only', 'tools': {}}}
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(personal)}), \
                patch.object(harness_child.subprocess, 'run', side_effect=remove_after_check):
            for session in (None, 'resume-fixture'):
                with self.subTest(resume=bool(session)):
                    if session is not None:
                        document = thin.read(self.state / 'input.json')
                        document['resume_session_id'] = session
                    harness_child.generate_plan(document, {'python': sys.executable})
                    plan = thin.read(self.state / 'launch.json')
                    self.assertNotIn('DSH_AGENTS_HOME', plan['env'])
                    self.assertEqual(thin.read(self.state / 'personal-skills.json'), {
                        'agents_home': None, 'reason': 'personal_skills_root_unavailable',
                        'shadowed_product_skills': [],
                    })
                    self.assertTrue((Path(plan['env']['DSH_HOME']) / 'skills/delegate').is_symlink())
                    if session is not None:
                        self.assertEqual(plan['argv'][-2:], ['--resume', session])
                    parked.rename(personal_root)

    def test_inventory_failure_or_root_replacement_keeps_product_links(self):
        personal = self.personal_fixture()
        document = {**self.doc, 'personal_skills_home': str(personal)}

        def switch_root(*args, **kwargs):
            (personal / 'skills').rename(personal / 'parked-skills')
            (personal / 'skills').mkdir()
            return subprocess.CompletedProcess([], 0, stdout='["delegate"]')

        for failure, reason in (
            (PermissionError('optional scan denied'), 'personal_skills_scan_failed'),
            (switch_root, 'personal_skills_root_changed'),
        ):
            with self.subTest(reason=reason), patch.object(thin.subprocess, 'run', side_effect=failure):
                plan = thin.generate(document)
                self.assertNotIn('DSH_AGENTS_HOME', plan['env'])
                self.assertTrue((Path(plan['env']['DSH_HOME']) / 'skills/delegate').is_symlink())
                self.assertEqual(thin.read(self.state / 'personal-skills.json')['reason'], reason)

    def test_child_sandbox_uses_the_native_tui_launch_permission(self):
        for enabled, expected in ((False, 'danger-full-access'), (True, 'workspace-write')):
            with self.subTest(enabled=enabled):
                plan = thin.generate({**self.doc, 'child_sandbox': enabled})
                self.assertEqual(plan['env']['DSH_PERMISSION_MODE'], expected)
        self.assertNotIn('DSH_PERMISSION_MODE', thin.generate(self.doc)['env'])

    def test_native_plan_omits_trial_policy_preserves_proxy_and_instructions(self):
        from hooks import harness_child

        source = harness_child.write_instructions(self.state, '/fixture/project')
        document = {**self.doc, 'instructions_file': str(source)}
        with patch('subprocess.run', side_effect=AssertionError('subprocess forbidden')):
            plan = thin.generate(document)
        self.assertEqual(plan['argv'][:2], [str(self.node), str(self.runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js')])
        self.assertEqual(set(plan['env']), {'DSH_HOME', 'DSH_TUI_DATA_DIR', 'DSH_TUI_LANG', 'AGENTSTACK_LLM_KEY_FILES', 'AGENTSTACK_LLM_KEY_DEFINITIONS'})
        rows = self.rows(plan)
        self.assertEqual([row for row in rows if row.get('disabled')], [
            {'id': 'credentials', 'disabled': True}, {'id': 'working-activity', 'disabled': True}])
        credentials = next(r for row in rows for r in row.get('insert', []) if r['id'] == 'orrery-keychain-credentials')
        self.assertEqual(credentials['name'], str(self.integration / 'keychain_credentials.mjs'))
        self.assertIn('credentials', next(row for row in rows if row.get('id') == 'llm-deepseek')['inject'])
        text = json.dumps(rows)
        for absent in ('maxTokens', 'maxRetries', 'session_requests', 'scope_mode', 'managed_plugin', 'egress.mjs'):
            self.assertNotIn(absent, text)
        proxy = next(r for row in rows for r in row.get('insert', []) if r['id'] == 'orrery-mcp-orrery-mail')
        self.assertEqual(proxy['config']['command'], '/canonical/proxy')
        self.assertEqual(proxy['config']['env']['AGENTSTACK_PROXY_PROGRAM'], 'dsh')
        self.assertEqual((Path(plan['env']['DSH_HOME']) / 'AGENTS.md').read_bytes(), source.read_bytes())
        self.assertIn(b'the refused action.', source.read_bytes())
        self.assertNotIn('HOME', plan['env'])
        self.assertEqual(plan, thin.generate(document))

    def test_inherited_codex_browser_becomes_native_mcp_client(self):
        from hooks import child_tools
        definition = {'command': 'npx', 'args': ['-y', 'chrome-devtools-mcp@latest',
                      '--autoConnect', '--no-usage-statistics'],
                      'env': {'npm_config_cache': '/fixture/cache'}, 'cwd': str(self.root)}
        selected = child_tools.native_plan(child_tools.normalize(), cwd=str(self.root),
            claude_json=str(self.root / 'absent.json'),
            codex_config={'mcp_servers': {'chrome-devtools': definition}})
        mcp = thin.read(self.doc['mcp_config'])
        mcp['mcpServers'].update(selected['servers'])
        thin.save(self.doc['mcp_config'], mcp)
        self.doc['mcp_selection'] = selected
        plan = thin.generate(self.doc)
        row = next(row for parent in self.rows(plan) for row in parent.get('insert', [])
                   if row['id'] == 'orrery-mcp-chrome-devtools')
        self.assertEqual(row['name'], '@deepseek-ai/dsh-mcp-client')
        self.assertEqual(row['config'], {'serverName': 'chrome-devtools', 'transport': 'stdio', **definition})

    def test_mac_control_projects_only_the_managed_screen_client(self):
        from hooks import child_tools

        definition = {'command': '/fixture/python', 'args': ['-m', 'agentstack_mail.screen_mcp'],
                      'env': {'AGENTSTACK_SCREEN_BINDING_FILE': '/fixture/binding.json'}}
        spec = child_tools.normalize(tools={'mcp': ['cua-driver']}, platform='darwin')
        selected = child_tools.native_plan(spec, cwd=str(self.root),
            claude_json=str(self.root / 'absent.json'), managed_screen=definition, platform='darwin')
        mcp = thin.read(self.doc['mcp_config'])
        mcp['mcpServers'].update(selected['servers'])
        thin.save(self.doc['mcp_config'], mcp)
        plan = thin.generate({**self.doc, 'mcp_selection': selected})
        clients = {row['config']['serverName']: row['config']
                   for parent in self.rows(plan) for row in parent.get('insert', [])
                   if row['name'] == '@deepseek-ai/dsh-mcp-client'}
        self.assertEqual(set(clients), {*mcp['mcpServers']})
        self.assertEqual(clients['orrery-screen'], {
            'serverName': 'orrery-screen', 'transport': 'stdio', 'cwd': str(self.root), **definition,
        })

    def test_dynamic_url_model_context_and_resume_pin_without_communication(self):
        manifest = thin.read(self.manifest)
        manifest['backends']['strata'] = {'model_source': 'server', 'urls': [
            'https://first.invalid/v1', 'https://second.invalid/v1', 'https://third.invalid/v1']}
        thin.save(self.manifest, manifest)
        document = {**self.doc, 'model': 'new-wire-id', 'url_index': 2,
                    'server_url': 'https://third.invalid/v1', 'server_context': {'n_ctx': 131072}}
        with patch('subprocess.run', side_effect=AssertionError('static subprocess forbidden')):
            plan = thin.generate(document)
        self.assertEqual(plan['compaction'], {'policy': 'selected_route', 'reason': 'selected_model_budget'})
        provider = next(row['config'] for row in self.rows(plan) if row.get('id') == 'llm-deepseek')
        self.assertEqual(provider['baseURL'], 'https://third.invalid')
        self.assertEqual(provider['models'], [{'id': 'new-wire-id', 'contextWindow': 131072, 'maxTokens': 8192}])
        patches = [row for group in self.rows(plan) for row in group.get('insert', [])
                   if row.get('name') == '@deepseek-harness-tui/dsh-tui/preset-patch']
        self.assertEqual([row['config']['preset'] for row in patches], ['standard', 'ptc', 'cordis'])
        policy = patches[0]['config']['config']['compaction-basic']['modelPolicies'][0]
        self.assertEqual(policy, {'provider': 'deepseek-official', 'model': 'new-wire-id',
            'thresholdRatio': 0.8, 'headroomTokens': 13107, 'retainRatio': 0.16, 'maxTokens': 8192})
        self.assertTrue(all(row['config']['config']['compaction-basic']['modelPolicies'] == [policy]
                            for row in patches))
        resumed = thin.generate({**document, 'resume_session_id': 'original-session'})
        self.assertEqual(resumed['argv'][-2:], ['--resume', 'original-session'])
        self.assertEqual(self.rows(resumed), self.rows(plan))
        manifest['backends']['strata']['urls'][2] = 'https://replacement.invalid/v1'
        thin.save(self.manifest, manifest)
        with self.assertRaisesRegex(ValueError, 'server_url_changed'):
            thin.generate(document)

    def test_compaction_budgets_scale_with_context(self):
        manifest = thin.read(self.manifest)
        manifest['backends']['strata'] = {'model_source': 'server', 'urls': ['https://fixture.invalid/v1']}
        thin.save(self.manifest, manifest)
        for context in (8192, 16384, 32768, 64000, 131072, 262144, 1000000):
            with self.subTest(context=context):
                output = min(8192, context // 4)
                policy = thin.compaction_policy('model', context, output)
                threshold = min(context * 8 // 10, context - output - policy['headroomTokens'])
                retained = (context - output) * 16 // 100
                self.assertGreater(threshold, retained)
                self.assertGreater(threshold, 0)
                self.assertLessEqual(policy['maxTokens'], policy['headroomTokens'])
                self.assertLessEqual(policy['maxTokens'], output)
                self.assertLess(output + policy['headroomTokens'], context)
                plan = thin.generate({**self.doc, 'model': 'model', 'url_index': 0,
                    'server_url': 'https://fixture.invalid/v1', 'server_context': {'n_ctx': context}})
                rows = self.rows(plan)
                provider = next(row['config'] for row in rows if row.get('id') == 'llm-deepseek')
                self.assertEqual(provider['models'][0]['maxTokens'], output)
                patch = next(row for group in rows for row in group.get('insert', [])
                             if row.get('id') == 'orrery-compaction-standard')
                self.assertEqual(patch['config']['config']['compaction-basic']['modelPolicies'], [policy])
        self.assertIsNone(thin.compaction_policy('model', 8, 2))
        self.assertIsNone(thin.compaction_policy('model', 8192, 8192))
        with self.assertRaisesRegex(ValueError, 'invalid_compaction_context'):
            thin.generate({**self.doc, 'model': 'model', 'url_index': 0,
                'server_url': 'https://fixture.invalid/v1', 'server_context': {'n_ctx': 8}})

    def test_dynamic_model_input_modalities_and_budget(self):
        manifest = thin.read(self.manifest)
        manifest['backends']['strata'] = {'model_source': 'server', 'urls': ['https://fixture.invalid/v1']}
        thin.save(self.manifest, manifest)
        for identity, context in [('deepseek-v4.1-flash', 131072), ('glm-5.3-flash', 262144)]:
            for modalities, expected in [
                (['text', 'image'], ['text', 'image']), (['text'], ['text']),
                (['text', 'image', 'video'], ['text', 'image']),
                (['text', 'video'], ['text']), (['text', 'image', 'image'], ['text', 'image']),
                (None, ['text']), ([], ['text']), ('image', ['text']),
                (['image'], ['text']), (['image', 'image'], ['text']),
                ([True, 'image'], ['text']), (['text', 'image', None], ['text']),
                (['text', 'image'] + ['video'] * 31, ['text']),
            ]:
                with self.subTest(identity=identity, modalities=modalities):
                    plan = thin.generate({**self.doc, 'model': identity, 'url_index': 0,
                        'server_url': 'https://fixture.invalid/v1', 'server_context': {'n_ctx': context},
                        'server_input_modalities': modalities})
                    provider = next(row['config'] for row in self.rows(plan) if row.get('id') == 'llm-deepseek')
                    definition = provider['models'][0]
                    self.assertEqual(definition['id'], identity)
                    self.assertEqual(definition['contextWindow'], context)
                    self.assertEqual(definition['maxTokens'], 8192)
                    self.assertEqual(definition.get('inputModalities', ['text']), expected)

    def test_fixed_backend_preserves_explicit_input_modalities(self):
        backends = self.root / 'backends.json'
        value = thin.read(backends)
        definition = value['backends']['strata']['model_definition']
        definition.update(inputModalities=['text', 'image'], maxTokens=8192)
        thin.save(backends, value)
        # Discovery metadata must never replace a fixed backend's declaration.
        plan = thin.generate({**self.doc, 'server_input_modalities': ['text']})
        provider = next(row['config'] for row in self.rows(plan) if row.get('id') == 'llm-deepseek')
        self.assertEqual(provider['models'], [definition])

    def test_dynamic_context_source_precedence_is_preserved(self):
        manifest = thin.read(self.manifest)
        manifest['backends']['strata'] = {'model_source': 'server', 'urls': ['https://fixture.invalid/v1'],
                                          'context_override': 32000}
        thin.save(self.manifest, manifest)
        doc = {**self.doc, 'model': 'model', 'url_index': 0, 'server_url': 'https://fixture.invalid/v1'}
        for metadata, expected in (({'n_ctx': 16384}, 16384), ({}, 32000)):
            plan = thin.generate({**doc, 'server_context': metadata})
            provider = next(row['config'] for row in self.rows(plan) if row.get('id') == 'llm-deepseek')
            self.assertEqual(provider['models'][0]['contextWindow'], expected)
        del manifest['backends']['strata']['context_override']
        thin.save(self.manifest, manifest)
        plan = thin.generate({**doc, 'server_context': {}})
        provider = next(row['config'] for row in self.rows(plan) if row.get('id') == 'llm-deepseek')
        self.assertEqual(provider['models'][0]['contextWindow'], 64000)

    def test_compaction_does_not_guess_fixed_model_budget(self):
        plan = thin.generate(self.doc)
        self.assertEqual(plan['compaction'], {'policy': 'native_default', 'reason': 'missing_or_invalid_budget'})
        self.assertFalse(any(row.get('name') == '@deepseek-harness-tui/dsh-tui/preset-patch'
                             for group in self.rows(plan) for row in group.get('insert', [])))

    def test_fixed_budget_only_applies_to_matching_valid_model(self):
        backends = self.root / 'backends.json'
        value = thin.read(backends)
        value['backends']['strata']['model_definition']['maxTokens'] = 8192
        thin.save(backends, value)
        plan = thin.generate(self.doc)
        self.assertEqual(plan['compaction'], {'policy': 'selected_route', 'reason': 'selected_model_budget'})
        other = thin.generate({**self.doc, 'model': 'b'})
        self.assertEqual(other['compaction'], {'policy': 'native_default', 'reason': 'model_definition_mismatch'})
        value['backends']['strata']['model_definition']['maxTokens'] = 131072
        thin.save(backends, value)
        invalid = thin.generate(self.doc)
        self.assertEqual(invalid['compaction'], {'policy': 'native_default', 'reason': 'missing_or_invalid_budget'})

    def test_only_child_tui_preferences_are_seeded_and_resume_keeps_same_dir(self):
        user = self.root / '.dsh-tui'
        user.mkdir()
        for file in ('lang.json', 'onboarding.json', 'model.json'):
            (user / file).write_text('user-owned-' + file)
        before = {p.name: p.read_bytes() for p in user.iterdir()}
        plan = thin.generate(self.doc)
        tui = Path(plan['env']['DSH_TUI_DATA_DIR'])
        self.assertEqual(plan['env']['DSH_TUI_LANG'], 'en')
        self.assertEqual(thin.read(tui / 'lang.json'), {'lang': 'en'})
        self.assertEqual(thin.read(tui / 'onboarding.json'), {'completed': True, 'version': 1})
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in tui.iterdir()))
        resumed = thin.generate({**self.doc, 'resume_session_id': 'own-session',
                                 'native_profile_env': {'DSH_HOME': plan['env']['DSH_HOME']}})
        self.assertEqual(resumed['env']['DSH_TUI_DATA_DIR'], str(tui))
        self.assertEqual({p.name: p.read_bytes() for p in user.iterdir()}, before)

    def test_native_history_expectation_is_passed_only_to_legacy_guard(self):
        self.doc['history_binding'] = {'agent_id': 7, 'launch_id': 'a' * 32}
        plan = thin.generate(self.doc)
        guard = next(r for row in self.rows(plan) for r in row.get('insert', []) if r.get('id') == 'orrery-tools')
        self.assertEqual(guard['config']['history_binding'], self.doc['history_binding'])
        fresh = thin.generate({**self.doc, 'fresh_client_dir': str(self.root)})
        guard = next(r for row in self.rows(fresh) for r in row.get('insert', []) if r.get('id') == 'orrery-tools')
        self.assertNotIn('history_binding', guard['config'])

    def test_model_helper_effort_and_exact_resume_are_projected(self):
        self.doc.update(model='b', helper_model='a', effort='high', resume_session_id='own-session')
        Path(self.doc['prompt_file']).write_text('literal `x` $(x)\nnext')
        plan = thin.generate(self.doc)
        rows = self.rows(plan)
        self.assertEqual(next(r for r in rows if r.get('id') == 'agent-default-model')['config']['model'], 'b')
        self.assertEqual(next(r for r in rows if r.get('id') == 'tool-subagent')['config']['agentOptions']['model'], 'a')
        self.assertEqual(plan['argv'][-4:], ['--resume', 'own-session', '--', 'literal `x` $(x)\nnext'])

    def test_unknown_model_helper_or_empty_resume_refused(self):
        for key, value, reason in [('model', 'unknown', 'model_mismatch'), ('helper_model', 'other/model', 'invalid_helper_model'),
                                   ('resume_session_id', '', 'invalid_resume_session_id')]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, reason):
                thin.generate({**self.doc, key: value})

    def test_resume_profile_pin_preserves_same_native_home(self):
        first = thin.generate(self.doc)
        self.doc.update(native_profile_env={'DSH_HOME': first['env']['DSH_HOME']},
                        resume_session_id='own-session')
        resumed = thin.generate(self.doc)
        self.assertEqual(resumed['env']['DSH_HOME'], first['env']['DSH_HOME'])
        self.assertEqual(resumed['argv'][-2:], ['--resume', 'own-session'])

    def test_resume_profile_pin_mismatch_refused_before_writes(self):
        home = self.root / 'native/dsh/ChildA'
        for pin in ({'DSH_HOME': str(self.root / 'previous-profile')}, {}, None):
            with self.subTest(pin=pin), self.assertRaisesRegex(ValueError, 'native_resume_profile_changed'):
                thin.generate({**self.doc, 'native_profile_env': pin, 'resume_session_id': 'own-session'})
            self.assertFalse(home.exists())
            self.assertEqual(list(self.state.iterdir()), [])

    def test_old_plain_generation_and_missing_key_plugin_are_not_ready(self):
        plugin = self.integration / 'keychain_credentials.mjs'
        plugin.unlink()
        with self.assertRaisesRegex(ValueError, 'credential_adapter_unavailable'):
            thin.validate_input(self.doc)
        plugin.write_text('synthetic file')
        template = thin.read(self.template)
        owner = thin.read(template['generation_receipt'])
        owner['variant'] = 'plain'
        thin.save(template['generation_receipt'], owner)
        with self.assertRaisesRegex(ValueError, 'plain_runtime_required'):
            thin.validate_input(self.doc)

    def test_selection_subset_is_read_only_and_package_node_changes_are_refused(self):
        subset = {k: self.doc[k] for k in ('manifest_file', 'product_root', 'backend_id', 'model')}
        self.assertEqual(thin.validate_input(subset)['node'], self.node)
        self.assertEqual(list(self.state.iterdir()), [])
        self.node.write_text('changed node')
        with self.assertRaisesRegex(ValueError, 'invalid_runtime'):
            thin.validate_input(subset)

    def test_unsupported_mcp_filters_refused_and_http_copied(self):
        for field in ('approved', 'enabled_tools'):
            self.doc['mcp_selection'][field] = {'server': ['tool']}
            with self.assertRaisesRegex(ValueError, 'unsupported_mcp_selection'):
                thin.generate(self.doc)
            self.doc['mcp_selection'][field] = {}
        thin.save(self.doc['mcp_config'], {'mcpServers': {'extra': {'type': 'http', 'url': 'http://127.0.0.1:1234/mcp'}}})
        plan = thin.generate(self.doc)
        row = next(r for block in self.rows(plan) for r in block.get('insert', []) if r['id'] == 'orrery-mcp-extra')
        self.assertEqual(row['config']['transport'], 'streamable-http')

    def test_mail_only_rejects_unknown_home_layer_instead_of_promising_no_inheritance(self):
        plan = thin.generate(self.doc)
        inherited = Path(plan['env']['DSH_HOME']) / 'cordis.patch.yml'
        inherited.write_text('- insert: [{name: arbitrary-plugin}]')
        with self.assertRaisesRegex(ValueError, 'inherited_mcp_conflict'):
            thin.generate(self.doc)

    def test_child_tui_state_and_selected_models_are_independent(self):
        user = self.root / '.dsh-tui/model.json'
        thin.save(user, {'provider': 'user-provider', 'model': 'user-choice'})
        before = user.read_bytes()
        a = thin.generate(self.doc)
        b_state = self.root / 'child-b'
        b_state.mkdir()
        b = thin.generate({**self.doc, 'agent_name': 'ChildB', 'state_dir': str(b_state), 'model': 'b'})
        a_file = Path(a['env']['DSH_TUI_DATA_DIR']) / 'model.json'
        b_file = Path(b['env']['DSH_TUI_DATA_DIR']) / 'model.json'
        self.assertNotEqual(a_file, b_file)
        self.assertEqual(thin.read(a_file)['model'], 'a')
        self.assertEqual(thin.read(b_file)['model'], 'b')
        thin.save(a_file, {'provider': 'deepseek-official', 'model': 'changed-in-a'})
        self.assertEqual(thin.read(b_file)['model'], 'b')
        self.assertEqual(user.read_bytes(), before)

    def test_cli_failure_is_one_fixed_reason_without_input_content(self):
        Path(self.doc['prompt_file']).write_text('PRIVATE_PROMPT')
        self.doc['backend_id'] = 'PRIVATE_PROVIDER'
        source, target = self.root / 'input.json', self.root / 'plan.json'
        thin.save(source, self.doc)
        result = subprocess.run([sys.executable, str(ROOT / 'integrations/dsh/thin_config.py'),
                                 '--input', str(source), '--out', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, 'DSH_THIN_CONFIG_ERROR=unknown_backend\n')
        self.assertFalse(target.exists())


def test_secret_service_ref_generates_one_reader_and_guard_definition(monkeypatch):
    fixture = ThinTests()
    fixture.setUp()
    try:
        import shutil
        (fixture.product / 'hooks').mkdir()
        shutil.copyfile(ROOT / 'hooks/llm_secret_reader.py', fixture.product / 'hooks/llm_secret_reader.py')
        shutil.copyfile(ROOT / 'hooks/native_connections.py', fixture.product / 'hooks/native_connections.py')
        reference = {'store': 'secret-service', 'attributes': {'service': 'fixture', 'account': 'child'}}
        manifest = thin.read(fixture.manifest)
        manifest['backends']['strata']['credential_ref'] = reference
        thin.save(fixture.manifest, manifest)
        backend_file = Path(thin.read(fixture.template)['backend_file'])
        backend = thin.read(backend_file)
        with fixture.assertRaisesRegex(ValueError, '^credential_reference_conflict$'):
            thin.validate_input(fixture.doc)
        del backend['backends']['strata']['keychain']
        thin.save(backend_file, backend)
        real_file, real_access = Path.is_file, os.access
        monkeypatch.setattr(sys, 'platform', 'linux')
        monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/fixture/bus')
        monkeypatch.setattr(Path, 'is_file', lambda p: str(p) == '/usr/bin/secret-tool' or real_file(p))
        monkeypatch.setattr(os, 'access', lambda p, m: str(p) == '/usr/bin/secret-tool' or real_access(p, m))
        monkeypatch.setattr(subprocess, 'run', lambda *a, **k: fixture.fail('generation looked up a key'))
        plan = thin.generate(fixture.doc)
        fixture.assertEqual(plan['env']['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=/fixture/bus')
        definitions = thin.read(plan['env']['AGENTSTACK_LLM_KEY_DEFINITIONS'])['backends']['strata']
        fixture.assertEqual(definitions['credential_ref'], reference)
        command = definitions['auth_command']
        fixture.assertEqual(command[-4:], ['--manifest', str(fixture.manifest.resolve()), '--backend', 'strata'])
        config = next(row for row in fixture.rows(plan)[5]['insert'] if row['id'] == 'orrery-keychain-credentials')['config']
        fixture.assertEqual(config['reader_command'], command)
        fixture.assertNotIn('service', config)
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
