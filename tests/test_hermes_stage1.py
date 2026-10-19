"""Managed controller -> Hermes fixtures, with no live Mail, API or credentials."""
import asyncio
import contextlib
import io
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_hermes_managed as pilot
import test_hermes_task as task_tests

sys.path.insert(0, str(pilot.ADAPTER))
import managed_lifecycle
import mail_lifecycle
import mail_probe
import runner
import task_spec
import task_evidence


class ManagedFixtures(unittest.TestCase):
    def setUp(self):
        self.fixture = pilot.PilotFixture('runTest')
        self.fixture.setUp()
        self.root = self.fixture.root

    def tearDown(self):
        self.fixture.tearDown()

    def task(self):
        return {'prompt': 'Create the requested result file.', 'allowed_paths': ['result.txt'],
                'expected': [{'path': 'result.txt', 'contains': 'requested-result'}]}

    def test_installer_owned_profiles_link_only_and_current_profile_confinement(self):
        config = self.fixture.config
        root = Path(config['product_root']).resolve() / 'harnesses/hermes/fixture-generation'
        root.mkdir(parents=True)
        persistent = root.parent / 'profiles'; persistent.mkdir()
        (root / 'profiles').symlink_to(persistent, target_is_directory=True)
        config['managed_root'] = str(root)
        key = __import__('hashlib').sha256(str(Path(config['hermes_source']).resolve()).encode()).hexdigest()[:16]
        self.fixture.install = root / 'installs' / key
        self.fixture.add_generation('installed')
        receipt = {'harness': 'hermes', 'complete': True, 'details': {
            k: config[k] for k in ('managed_root', 'hermes_source', 'hermes_executable')}}
        runner.save(root / '.orrery-managed-install.json', receipt)
        runner.save(self.fixture.config_path, config)
        state = self.root / 'installed-state'
        runner.initialize(self.fixture.config_path, state, 'strata', self.fixture.product.run, file_task=True)
        mail_lifecycle.register(state, self.fixture.product.run, generate_config=False)
        runner.configure(state, self.fixture.product.run)
        configured, _ = runner.check_configured(state)
        self.assertEqual(Path(configured['profile_home']).resolve(), persistent / 'strata')
        for target in (self.root / 'home/.hermes', self.root / 'hermes-managed', self.root / 'other'):
            (root / 'profiles').unlink()
            (root / 'profiles').symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'Unapproved'):
                runner.check_configured(state)
        (root / 'profiles').unlink(); (root / 'profiles').symlink_to(persistent, target_is_directory=True)
        runner.save(root / '.orrery-managed-install.json', {**receipt, 'complete': False})
        with self.assertRaisesRegex(ValueError, 'Unapproved'):
            runner.check_configured(state)
        runner.save(root / '.orrery-managed-install.json', receipt)
        saved = persistent.with_name('saved-profiles')
        persistent.rename(saved); persistent.symlink_to(saved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Unapproved'):
            runner.check_configured(state)
        persistent.unlink(); saved.rename(persistent)
        (persistent / 'deepseek').symlink_to(self.root / 'other', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            runner.check_profile_paths(root / 'profiles/deepseek')

    def test_unstarted_reservation_free_retired_cleanup_has_no_fabricated_group(self):
        state = self.root / 'never-started'
        runner.initialize(self.fixture.config_path, state, 'strata', self.fixture.product.run, file_task=True)
        mail_lifecycle.register(state, self.fixture.product.run, generate_config=False)
        mail_lifecycle.retire(state, self.fixture.product.run, require_release=False)
        capture = self.root / 'whois.json'
        runner.save(capture, {'name': 'FixtureChild', 'retired_at': datetime.now(timezone.utc).isoformat()})
        mail_lifecycle.confirm_retire(state, capture)
        import completion
        result = completion.evaluate_runtime(state)
        self.assertTrue(result['success'])
        self.assertEqual(result['runtime_conclusion'], 'not-started-retired')
        self.assertIsNone(result['group_absent']); self.assertIsNone(result['reservations_released'])
        retire = runner.read(state / 'retire-receipt.json')
        for change in ({'confirmed': False}, {'attempt_id': 'other'}, {'source': 'unverified'}):
            runner.save(state / 'retire-receipt.json', {**retire, **change})
            self.assertIsNot(completion.evaluate_runtime(state)['success'], True)
        runner.save(state / 'retire-receipt.json', retire)
        for name in ('start-intent.json', 'runtime-process.json', 'credential-read.json',
                     'reservation-intent.json', 'reservation-result.json', 'reservations.json'):
            runner.save(state / name, {})
            self.assertIsNot(completion.evaluate_runtime(state)['success'], True, name)
            (state / name).unlink()
        task = runner.read(state / 'task.json')
        runner.save(state / 'task.json', {**task, 'reservation_receipts': [{'id': 1}]})
        self.assertIsNot(completion.evaluate_runtime(state)['success'], True)
        runner.save(state / 'task.json', task)
        def cli(argv, **kwargs):
            action = argv[3]
            if action == 'complete-runtime':
                value = runner.evaluate(state, runtime=True)
            elif action == 'clean':
                value = runner.clean(state, lambda *a, **k: self.fail('unstarted cleanup must not execute processes'))
            else:
                self.fail('unexpected lifecycle action ' + action)
            return subprocess.CompletedProcess(argv, 0, json.dumps({'result': value}), '')
        outcome = managed_lifecycle.close_unstarted(state, run=cli)
        self.assertTrue(outcome['runtime_success']); self.assertTrue(outcome['cleaned'])
        self.assertFalse((state / 'token-FixtureChild').exists())
        self.assertFalse((state / 'runtime-exit.json').exists())
        self.assertFalse((state / 'diagnostic-scan.json').exists())

    def test_step_failure_uses_only_known_codes_and_cli_never_echoes_values(self):
        state = self.root / 'safe-failure'; state.mkdir()
        stderr = io.StringIO()
        with patch('sys.argv', ['runner.py', 'configure', '--state', str(state)]), \
             patch.object(runner, 'configure', side_effect=ValueError('Profile escaped the managed root')), \
             contextlib.redirect_stderr(stderr):
            self.assertEqual(runner.main(), 1)
        self.assertEqual(json.loads(stderr.getvalue())['error_code'], 'profile_path_mismatch')
        for text in (stderr.getvalue(), 'raw private content must not be echoed'):
            with self.assertRaises(managed_lifecycle.StepFailure) as raised:
                managed_lifecycle.step(state, 'configure', run=lambda *a, **k: subprocess.CompletedProcess([], 1, '', text))
            self.assertEqual(raised.exception.managed_step, 'configure')
            self.assertNotIn('raw private', str(raised.exception))
        with self.assertRaises(managed_lifecycle.StepFailure) as raised:
            managed_lifecycle.step(state, 'configure', run=lambda *a, **k:
                (_ for _ in ()).throw(subprocess.TimeoutExpired(['private-argv'], 1)))
        self.assertEqual(raised.exception.managed_step, 'configure')
        self.assertEqual(raised.exception.managed_reason, 'process_failed')
        self.assertNotIn('private-argv', str(raised.exception))
        with self.assertRaises(managed_lifecycle.StepFailure) as raised:
            managed_lifecycle.step(state, 'configure', run=lambda *a, **k:
                subprocess.CompletedProcess([], 0, 'private-invalid-output', ''))
        self.assertEqual(raised.exception.managed_reason, 'validation_failed')
        self.assertNotIn('private-invalid-output', str(raised.exception))

    def test_general_task_new_file_and_hash_mail_gate(self):
        task = self.task()
        import hashlib
        task['expected'] = [{'path': 'result.txt', 'sha256': hashlib.sha256(b'requested-result').hexdigest()}]
        state = self.root / 'generic-state'
        runner.initialize(self.fixture.config_path, state, 'strata', self.fixture.product.run, file_task=True, task_spec=task)
        target = Path(runner.read(state / 'task.json')['worktree']) / 'result.txt'
        self.assertFalse(target.exists())
        self.assertEqual((state / 'task.md').read_text().count(task['prompt']), 1)
        self.assertNotIn('hermes-first-ok', (state / 'task.md').read_text())
        mail_lifecycle.register(state, self.fixture.product.run, generate_config=False)
        runner.configure(state, self.fixture.product.run)
        helper = task_tests.FileTask('runTest')
        helper.state, helper.config, helper.task = state, runner.read(state / 'run-config.json'), runner.read(state / 'task.json')
        helper.attempt = runner.read(state / 'attempt.json')
        rows = [{'id': 71, 'owner': 'FixtureChild', 'path_pattern': 'result.txt',
                 'expires_ts': (datetime.now(timezone.utc) + timedelta(seconds=1800)).isoformat()}]
        helper.task['reservation_receipts'] = rows
        runner.save(state / 'task.json', helper.task)
        callbacks = helper.plugin()
        before = callbacks['pre_tool_call']
        message = runner.read(state / 'message.json')
        self.assertEqual(before('mcp__orrery_mail__send_message', message)['action'], 'block')
        self.assertIsNone(before('write_file', {'path': str(target)}))
        target.write_text('wrong')
        self.assertEqual(before('mcp__orrery_mail__send_message', message)['action'], 'block')
        target.write_text('requested-result')
        other = target.with_name('linked-result.txt')
        target.rename(other)
        target.symlink_to(other)
        self.assertEqual(before('mcp__orrery_mail__send_message', message)['action'], 'block')
        target.unlink()
        other.rename(target)
        self.assertIsNone(before('mcp__orrery_mail__send_message', message))
        empty = self.task()
        empty['expected'][0]['contains'] = ''
        self.assertEqual(task_spec.validate(empty), empty)

    def test_bad_task_rejected_before_state_or_registration(self):
        for value in ({}, {**self.task(), 'allowed_paths': ['../live']},
                      {**self.task(), 'allowed_paths': ['files/**']},
                      {**self.task(), 'expected': [{'path': 'outside', 'contains': 'x'}]},
                      {**self.task(), 'expected': [{'path': 'result.txt', 'contains': 'x', 'sha256': 'a' * 64}]},
                      {**self.task(), 'expected': [{'path': 'result.txt', 'sha256': 'invalid'}]}):
            with self.assertRaises(ValueError):
                runner.initialize(self.fixture.config_path, self.root / 'bad-state', 'strata',
                                  self.fixture.product.run, file_task=True, task_spec=value)
            self.assertFalse((self.root / 'bad-state').exists())

    def test_reasoning_effort_projection_and_unstarted_handoff_cleanup(self):
        import profile_projection
        spec = {**self.fixture.config['profiles']['strata'], 'reasoning_effort': 'none'}
        config = profile_projection.project(spec, self.root / 'profile', ['title_generation', 'background_review'])
        self.assertEqual(config['profile_config']['agent'], {'reasoning_effort': 'none'})
        state = self.root / 'partial-handoff'
        runner.initialize(self.fixture.config_path, state, 'strata', self.fixture.product.run, file_task=True)
        def bad_handoff(argv, **kwargs):
            result = self.fixture.product.run(argv, **kwargs)
            if str(argv[0]).endswith('agentstack-preregister-child'):
                Path(argv[argv.index('--token-file-out') + 1]).unlink()
            return result
        with self.assertRaises(FileNotFoundError):
            mail_lifecycle.register(state, bad_handoff, generate_config=False)
        self.assertEqual(runner.read(state / 'mail-identity.json')['agent_name'], 'FixtureChild')
        receipt = mail_lifecycle.retire(state, self.fixture.product.run, require_release=False)
        self.assertFalse(receipt['confirmed'])
        self.assertEqual(receipt['launch_state'], 'not-started')

    def test_stop_partial_start_proves_absence_without_inventing_runtime_exit(self):
        state = self.root / 'partial-start'
        runner.initialize(self.fixture.config_path, state, 'strata', self.fixture.product.run, file_task=True)
        mail_lifecycle.register(state, self.fixture.product.run, generate_config=False)
        runner.save(state / 'start-intent.json', {'agent_name': 'FixtureChild', 'socket_path': '/fixture/socket'})
        self.assertEqual(runner.stop(state, self.fixture.product.run), {'launch_state': 'not-started-session-absent'})
        self.assertFalse((state / 'runtime-exit.json').exists())

    def test_ready_uses_runtime_pid_and_not_preflight_or_wrapper_exit(self):
        state = self.root / 'status'
        state.mkdir()
        save = lambda name, value: runner.save(state / name, value)
        save('attempt.json', {'attempt_id': 'aid'})
        save('task.json', {'task_id': 'tid'})
        save('mail-identity.json', {'agent_name': 'FixtureChild'})
        save('mail-probe.json', {'schema': True, 'backend': True, 'ok': True, 'agent_name': 'FixtureChild'})
        save('preflight-complete.json', {'attempt_id': 'aid', 'guard_loaded': True})
        save('runtime-process.json', {'pid': 42})
        row = {'attempt_id': 'aid', 'task_id': 'tid', 'phase': 'plugin-ready', 'process_id': 41}
        (state / 'events.jsonl').write_text(json.dumps(row) + '\n')
        self.assertFalse(managed_lifecycle.status(state)['ready'])
        row['process_id'] = 42
        (state / 'events.jsonl').write_text(json.dumps(row) + '\n')
        self.assertTrue(managed_lifecycle.status(state)['ready'])
        save('run-config.json', {'credential_mode': 'owned-once'})
        self.assertFalse(managed_lifecycle.status(state)['ready'])
        save('credential-read.json', {'attempt_id': 'aid', 'status': 'available', 'process_id': 42})
        self.assertTrue(managed_lifecycle.status(state)['ready'])
        with (state / 'events.jsonl').open('a') as handle:
            handle.write(json.dumps({'attempt_id': 'aid', 'task_id': 'tid', 'phase': 'turn-end'}) + '\n')
        self.assertFalse(managed_lifecycle.status(state)['terminal'])
        save('runtime-exit.json', {'exit_code': 1})
        self.assertTrue(managed_lifecycle.status(state)['terminal'])
        self.assertIsNone(managed_lifecycle.status(state)['task_success'])

    def test_two_profile_controller_cycle_and_partial_reservation_cleanup(self):
        for profile, partial in (('strata', False), ('deepseek', False), ('deepseek', True)):
            state = self.root / ('cycle-' + profile + ('-partial' if partial else ''))
            actions = []
            outer = self

            class Session:
                async def list_tools(self):
                    return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in mail_probe.REQUIRED])

                async def call_tool(self, name, args):
                    config = runner.read(state / 'run-config.json')
                    if name == 'runtime_status':
                        value = {'agent_name': 'FixtureChild', 'program': 'claude-code', 'project_key': config['project_key'],
                                 'lineage': {'parent_agent': 'FixtureParent'}}
                    elif name == 'whois':
                        value = {'name': 'FixtureChild', 'program': 'hermes'}
                    elif name == 'reserve_files':
                        paths = args['paths'][:1] if partial else args['paths']
                        value = {'granted': [{'id': 71 + n, 'owner': 'FixtureChild', 'path_pattern': path,
                                  'expires_ts': (datetime.now(timezone.utc) + timedelta(seconds=1800)).isoformat()}
                                 for n, path in enumerate(paths)]}
                    else:
                        outer.assertEqual(name, 'release_reservations')
                        value = {'released': len(args['file_reservation_ids'])}
                    return pilot.reply(value)

            def cli(argv, *, timeout=60, check=True, background=False):
                action = argv[3]
                actions.append(action)
                get = lambda flag: argv[argv.index(flag) + 1]
                if background:
                    outer.assertEqual(action, 'start')
                    return SimpleNamespace(poll=lambda: 0)
                try:
                    if action == 'init-task':
                        result = runner.initialize(get('--config'), state, get('--profile'), self.fixture.product.run,
                                                    file_task=True, task_spec=runner.read(get('--input')))
                    elif action == 'preregister':
                        result = mail_lifecycle.register(state, self.fixture.product.run, generate_config=False)
                    elif action == 'configure':
                        result = runner.configure(state, self.fixture.product.run)
                    elif action in ('probe', 'reserve', 'release'):
                        report = {}
                        asyncio.run(mail_probe.exercise(Session(), state, action, report))
                        runner.save(state / 'mail-probe.json', report | {'agent_name': 'FixtureChild'})
                        result = runner.apply_reservations(state) if action == 'reserve' else report
                    elif action == 'preflight':
                        result = {'attempt_id': runner.read(state / 'attempt.json')['attempt_id'], 'guard_loaded': True}
                        runner.save(state / 'preflight-complete.json', result)
                    elif action == 'stop':
                        result = runner.stop(state, self.fixture.product.run)
                    elif action == 'scan-diagnostics':
                        from diagnostics import scan
                        result = scan(state)
                    elif action == 'retire':
                        result = mail_lifecycle.retire(state, self.fixture.product.run,
                                  require_release=bool(runner.read(state / 'reservations.json')['receipts']))
                    elif action == 'confirm-retire':
                        result = mail_lifecycle.confirm_retire(state, get('--input'))
                    elif action == 'record-mail':
                        result = mail_lifecycle.record_mail(state, get('--input'))
                    elif action in ('complete', 'complete-runtime'):
                        result = runner.evaluate(state, runtime=action == 'complete-runtime')
                    elif action == 'clean':
                        with patch('pane_entry.group_absent', return_value=True):
                            result = runner.clean(state, self.fixture.product.run)
                    else:
                        raise AssertionError(action)
                    return subprocess.CompletedProcess(argv, 0, json.dumps({'result': result}))
                except (ValueError, OSError):
                    return subprocess.CompletedProcess(argv, 1, '')

            task = self.task()
            if partial:
                task['allowed_paths'].append('second.txt')
            prepare = lambda: managed_lifecycle.prepare(state=state, config_file=self.fixture.config_path,
                backend_id=profile, task=task, worktree=self.root / ('work-' + state.name), parent='FixtureParent', run=cli)
            if partial:
                with self.assertRaisesRegex(RuntimeError, 'reserve'):
                    prepare()
                self.assertEqual(len(runner.read(state / 'reservations.json')['receipts']), 1)
            else:
                self.assertFalse(prepare()['ready'])
                with self.assertRaises(ValueError):
                    prepare()
                managed_lifecycle.start(state, run=cli)
                self.assertFalse(managed_lifecycle.status(state)['ready'])
                attempt, manifest = runner.read(state / 'attempt.json'), runner.read(state / 'task.json')
                root = Path(manifest['worktree']);(root / 'result.txt').write_text('requested-result')
                task_evidence.event(state, 'turn-end', reason_kind='completed')
                task_evidence.event(state, 'mail-sent', ok=True, verified_sender=True, deliveries=1,
                                    message_id=82, to=['FixtureParent'], **{'from': 'FixtureChild'})
                task_evidence.audit(state, 'provider-request-start', seq=1)
                if profile == 'deepseek':
                    runner.save(state / 'parent-inbox.json', [{'id': 82, 'from': 'FixtureChild', 'kind': 'to'}])
                runner.save(state / 'diagnostic-scan.json', {'attempt_id': attempt['attempt_id'],
                            'source': 'owned-process-prefix-scan', 'matches': 0, 'errors': 0})
                runner.save(state / 'runtime-process.json', {'pid': 42, 'pgid': 42, 'agent_name': 'FixtureChild'})
                runner.save(state / 'runtime-exit.json', {'group_absent': True, 'group_check': 'observed'})
                runner.save(state / 'owned-pane.json', {'agent_name': 'FixtureChild', 'session': 'FixtureChild', 'socket_path': '/fixture/socket'})
                runner.save(state / 'start-intent.json', {'agent_name': 'FixtureChild'})
            calls = []
            def parent_call(tool, args):
                calls.append((tool, args))
                return {'name': 'FixtureChild', 'retired_at': datetime.now(timezone.utc).isoformat()}
            result = managed_lifecycle.finalize(state, parent_call=parent_call, run=cli)
            self.assertEqual(calls, [('whois', {'name': 'FixtureChild'})])
            self.assertTrue(runner.read(state / 'release-receipt.json')['confirmed'])
            self.assertTrue(runner.read(state / 'retire-receipt.json')['confirmed'])
            self.assertEqual(actions.count('preregister'), 1)
            if not partial:
                self.assertTrue(result['task_success'])
                self.assertTrue(result['runtime_success'])
                managed_lifecycle.clean(state, run=cli)
                self.assertTrue((state / 'events.jsonl').exists())
                self.assertTrue((root / 'result.txt').exists())
                self.assertFalse((state / 'mcp.json').exists())
            else:
                self.assertIsNone(result['runtime_success'])
                with self.assertRaises(RuntimeError):
                    managed_lifecycle.clean(state, run=cli)

    def test_check_rejects_unknown_skip_reason(self):
        spec = importlib.util.spec_from_file_location('hermes_check_fixture', pilot.ADAPTER / 'check.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        class Skipped(unittest.TestCase):
            def runTest(self):
                self.skipTest('approved-platform')
        result = unittest.TestResult();Skipped().run(result)
        self.assertFalse(module.report(result, [])['success'])
        self.assertTrue(module.report(result, ['approved-platform'])['success'])

    def test_owned_key_is_read_once_stays_out_of_children_and_is_reused_for_scan(self):
        python, source = os.environ.get('ORRERY_HERMES_TEST_PYTHON'), os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not python or not source:
            self.skipTest('Explicit installed dependency interpreter/source required')
        state = self.root / 'owned-key-state'
        runner.initialize(self.fixture.config_path, state, 'strata', self.fixture.product.run, file_task=True)
        mail_lifecycle.register(state, self.fixture.product.run, generate_config=False)
        runner.configure(state, self.fixture.product.run)
        config = runner.read(state / 'run-config.json')
        config.update(credential_mode='owned-once', hermes_source=source)
        runner.save(state / 'run-config.json', config)
        profile = Path(config['profile_home']) / 'config.yaml'
        data = runner.read(profile)
        command = '/usr/bin/security find-generic-password -s fixture-service -a fixture-account -w'
        data['providers'][config['provider']]['key_cmd'] = command
        runner.save(profile, data)
        script = self.root / 'owned_key_fixture.py'
        script.write_text('''
import json,os,subprocess,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[3])
state=Path(sys.argv[2]);secret=b'offline-owned-key-canary'
from owned_credential import install,wrap_exit
from mail_lifecycle import read
calls=[]
def keychain(argv,**kwargs):
    calls.append(argv)
    assert argv[0]=='/usr/bin/security' and all(secret.decode() not in str(v) for v in kwargs['env'].values())
    return SimpleNamespace(returncode=0,stdout=secret+b'\\n')
scan=install(state,keychain,platform='darwin')
from agent.command_token_source import build_command_token_provider
command='/usr/bin/security find-generic-password -s fixture-service -a fixture-account -w'
for _ in range(4):
    assert build_command_token_provider(command,'fixture')().encode()==secret
try:
    build_command_token_provider(command+' different','fixture')
except ValueError:
    pass
else:
    raise AssertionError('another credential route accepted')
child=subprocess.run([sys.executable,'-I','-c','import json,os;print(json.dumps(dict(os.environ)))'],capture_output=True)
assert secret not in child.stdout and secret.decode() not in os.environ.values()
exits=[]
module=SimpleNamespace(_exit_after_oneshot=lambda rc:exits.append(rc))
wrap_exit(module,scan);module._exit_after_oneshot(0)
from diagnostics import scan as verify_saved
proof=verify_saved(state,lambda *a,**kw:(_ for _ in ()).throw(AssertionError('second key read')))
assert proof['matches']==0 and proof['errors']==0 and exits==[0],proof
assert len(calls)==1
try:
    install(state,keychain,platform='darwin')
except FileExistsError:
    pass
else:
    raise AssertionError('credential read retried')
assert len(calls)==1
for path in state.rglob('*'):
    if path.is_file() and not path.name.startswith('token-'):
        assert secret not in path.read_bytes()
print('owned-key-offline-fixture-ok')
''')
        result = subprocess.run([python, '-I', '-B', str(script), str(pilot.ADAPTER), str(state), source],
            env=runner.managed_env(config, runner.read(state / 'mail-identity.json'), state),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_owned_key_read_is_not_retried_or_reported_available(self):
        python, source = os.environ.get('ORRERY_HERMES_TEST_PYTHON'), os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not python or not source:
            self.skipTest('Explicit installed dependency interpreter/source required')
        script = self.root / 'failed_key_fixture.py'
        state = self.root / 'failed-key';state.mkdir()
        profile = self.root / 'failed-profile';profile.mkdir()
        runner.save(state / 'run-config.json', {**self.fixture.config, 'profile_home': str(profile),
                    'credential_mode': 'owned-once', 'provider': 'fixture'})
        runner.save(state / 'attempt.json', {'attempt_id': 'fixture-failure'})
        runner.save(profile / 'config.yaml', {'providers': {'fixture': {'key_cmd':
            '/usr/bin/security find-generic-password -s fixture-service -w'}}})
        script.write_text('''
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[3])
from owned_credential import install
from mail_lifecycle import read
calls=[]
def fail(*args,**kwargs):
    calls.append(True)
    return SimpleNamespace(returncode=1,stdout=b'offline-failure-must-not-be-saved')
for n in range(2):
    try:install(Path(sys.argv[2]),fail,platform='darwin')
    except (ValueError,FileExistsError):pass
    else:raise AssertionError('failed key read succeeded')
assert len(calls)==1 and read(Path(sys.argv[2])/'credential-read.json')['status']=='failed'
assert b'offline-failure-must-not-be-saved' not in (Path(sys.argv[2])/'credential-read.json').read_bytes()
''')
        result = subprocess.run([python, '-I', '-B', str(script), str(pilot.ADAPTER), str(state), source],
                                 env=runner.base_env(self.fixture.config), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
