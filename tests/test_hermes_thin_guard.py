"""Native hook adaptation with real common key guard and synthetic tool bodies."""
import json
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import shlex
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('hermes_thin_guard', ROOT / 'integrations/hermes/thin_guard.py')
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class ThinGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.core = self.base / 'core'
        (self.core / 'hooks').mkdir(parents=True)
        for name in ('check-agent-registered.sh', 'check-file-reservation.sh'):
            (self.core / 'hooks' / name).write_text('#!/bin/sh\nexit 0\n')
        (self.core / 'hooks/deny-llm-key-read.py').symlink_to(ROOT / 'hooks/deny-llm-key-read.py')
        definitions = self.base / 'definitions.json'
        self.helper = self.base / 'key-helper'
        self.helper.write_text('#!/bin/sh\ntouch ' + str(self.base / 'helper-was-called') + '\n')
        self.helper.chmod(0o700)
        definitions.write_text(json.dumps({'backends': {'fixture': {'auth_command': [str(self.helper), '--fixture']}}}))
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.base), 'PATH': os.defpath,
            'AGENTSTACK_HOME': str(self.core), 'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test-hermes-thin-guard',
            'AGENTSTACK_LLM_KEY_DEFINITIONS': str(definitions),
            'AGENTSTACK_NATIVE_SESSION_FILE': str(self.base / 'native-session.json')}, clear=True))
        self.native_cwd = guard._cwd
        self.enterContext(patch.object(guard, '_cwd', return_value=str(self.base)))

    def test_native_credential_tool_denied_before_real_helper_execution(self):
        command = str(self.helper) + ' --fixture'
        result = guard.before('terminal', {'command': command}, session_id='own-session')
        if result is None:
            subprocess.run([str(self.helper), '--fixture'], check=True)
        self.assertEqual(result['action'], 'block')
        self.assertFalse((self.base / 'helper-was-called').exists())
        self.assertNotIn(command, result['message'])

    def test_repo_guard_blocks_native_and_shell_provider_file_reads(self):
        provider = self.base / 'provider.json'
        provider.write_text('fixture-provider-canary')
        with patch.dict(os.environ, {'AGENTSTACK_LLM_KEY_FILES': json.dumps([str(provider)])}):
            for tool, args in [('read_file', {'path': str(provider)}),
                               ('terminal', {'command': 'cat ' + shlex.quote(str(provider))})]:
                with self.subTest(tool=tool):
                    result = guard.before(tool, args)
                    self.assertEqual(result['action'], 'block')
                    self.assertTrue(result['message'].startswith('ORRERY hook denied: deny-llm-key-read.py\nLOCAL_MODEL_KEY_READ_DENIED:'))
                    self.assertNotIn('fixture-provider-canary', result['message'])

    def test_registration_veto_precedes_reservation_and_tool(self):
        (self.core / 'hooks/check-agent-registered.sh').write_text('echo unresolved >&2\nexit 2\n')
        (self.core / 'hooks/check-file-reservation.sh').write_text('touch ' + str(self.base / 'reservation-called') + '\n')
        result = guard.before('write_file', {'path': 'file.txt', 'content': 'never'})
        self.assertEqual(result, {'action': 'block', 'message': 'ORRERY hook denied: check-agent-registered.sh\nunresolved'})
        self.assertFalse((self.base / 'reservation-called').exists())
        self.assertFalse((self.base / 'file.txt').exists())

    def test_common_reservation_veto_blocks_all_patch_targets_before_writes(self):
        events = []
        def run(argv, **kwargs):
            payload = json.loads(kwargs['input'])
            events.append((Path(argv[-1]).name, payload))
            denied = Path(argv[-1]).name == 'check-file-reservation.sh' and payload['tool_input']['file_path'].endswith('/destination')
            return SimpleNamespace(returncode=2 if denied else 0, stdout='', stderr='reservation conflict' if denied else '')
        with patch.object(guard.subprocess, 'run', side_effect=run):
            result = guard.before('patch', {'mode': 'patch', 'patch': '*** Move File: source -> destination\n'}, session_id='own-session')
        self.assertEqual(result['action'], 'block')
        checked = [payload['tool_input']['file_path'] for name, payload in events if name == 'check-file-reservation.sh']
        self.assertEqual(checked, [str(self.base / 'source'), str(self.base / 'destination')])
        self.assertTrue(all(payload['session_id'] == 'own-session' for _, payload in events))

    def test_blocking_stderr_reason_reaches_caller_without_raw_stdout(self):
        (self.core / 'hooks/check-agent-registered.sh').write_text('echo private-stdout\necho registration-required >&2\nexit 2\n')
        self.assertEqual(guard.before('terminal', {'command': 'echo harmless'}),
                         {'action': 'block', 'message': 'ORRERY hook denied: check-agent-registered.sh\nregistration-required'})

    def test_structured_denial_precedes_stderr_and_is_bounded(self):
        cases = [({'reason': 'reserve-before-edit'}, 'reserve-before-edit'),
                 ({'hookSpecificOutput': {'permissionDecisionReason': 'identity-required'}}, 'identity-required'),
                 ({'reason': 7}, 'stderr-reason'),
                 ({'reason': '\x1b[31m' + 'x' * 2500 + '\x1b[0m'}, 'x' * 2000)]
        for public, reason in cases:
            with self.subTest(public=public), patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=2, stdout=json.dumps(public), stderr='stderr-reason')):
                result = guard.before('write_file', {'path': 'file', 'content': 'never'})
                self.assertEqual(result, {'action': 'block', 'message': 'ORRERY hook denied: deny-llm-key-read.py\n' + reason})

    def test_internal_errors_and_nonblocking_exit_codes_do_not_become_public_reasons(self):
        for code, stderr in [(1, 'private internal failure'),
                             (2, 'Traceback (most recent call last):\nsecret diagnostic'),
                             (2, '  File "private/path.py", line 5\nprivate diagnostic'),
                             (2, 'RuntimeError: private diagnostic'),
                             (2, '/private/hook.sh: line 3: private diagnostic')]:
            with self.subTest(code=code, stderr=stderr), patch.object(guard.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=code, stdout='', stderr=stderr)):
                self.assertEqual(guard.before('terminal', {'command': 'true'}),
                    {'action': 'block', 'message': 'ORRERY hook denied: deny-llm-key-read.py'})

    def test_coloured_internal_diagnostics_never_become_public_reasons(self):
        for diagnostic in ('Traceback (most recent call last):\nprivate diagnostic',
                           'RuntimeError: private diagnostic'):
            coloured = '\x1b[31m' + diagnostic + '\x1b[0m'
            for structured in (False, True):
                with self.subTest(diagnostic=diagnostic, structured=structured), \
                        patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(
                            returncode=2, stdout=json.dumps({'reason': coloured}) if structured else '',
                            stderr='' if structured else coloured)):
                    self.assertEqual(guard.before('terminal', {'command': 'true'}),
                        {'action': 'block', 'message': 'ORRERY hook denied: deny-llm-key-read.py'})

    def test_denial_codepoint_boundary_controls_and_invalid_utf8(self):
        boundary = '拒😀' * 1000
        cases = [(2, '', 'short denial', 'short denial'), (2, '', boundary, boundary),
                 (2, '', boundary + 'x', boundary), (2, '', '', ''),
                 (2, 'not JSON', '', ''),
                 (2, '', '\x1b[31m公開拒否\x1b[0m\x07', '公開拒否'),
                 (1, '', 'not an exit-two refusal', '')]
        for code, stdout, stderr, expected in cases:
            with self.subTest(code=code, length=len(stderr)), patch.object(guard.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)):
                self.assertEqual(guard.call_hook('check-file-reservation.sh', {'cwd': str(self.base)}).reason, expected)
        (self.core / 'hooks/check-agent-registered.sh').write_text("printf '\\377A' >&2\nexit 2\n")
        result = guard.before('terminal', {'command': 'true'})
        self.assertEqual(result['message'], 'ORRERY hook denied: check-agent-registered.sh\n\ufffdA')

    def test_only_allowed_public_structured_warning_is_displayed(self):
        cases = [(0, '{"systemMessage":"reservation is unconfirmed"}', 'reservation is unconfirmed\n'),
                 (0, '', ''), (0, 'private stdout', ''),
                 (0, '{"other":"private field"}', ''), (0, '{"systemMessage":3}', ''),
                 (2, '{"systemMessage":"not an allowed warning"}', '')]
        for code, stdout, expected in cases:
            with self.subTest(stdout=stdout, code=code):
                visible = io.StringIO()
                with patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(
                        returncode=code, stdout=stdout, stderr='private stderr')), patch('sys.stderr', visible):
                    self.assertEqual(bool(guard.call_hook('check-file-reservation.sh', {'cwd': str(self.base)})), code == 0)
                self.assertEqual(visible.getvalue(), expected)

    def test_credential_guard_precedes_registration(self):
        (self.core / 'hooks/check-agent-registered.sh').write_text('touch ' + str(self.base / 'registration-called') + '\n')
        result = guard.before('terminal', {'command': str(self.helper) + ' --fixture'})
        self.assertEqual(result['action'], 'block')
        self.assertFalse((self.base / 'registration-called').exists())

    def test_successful_edits_release_without_passing_output(self):
        calls = []
        with patch.object(guard, 'call_hook', side_effect=lambda name, payload: calls.append((name, payload))):
            for status in ('error', 'blocked', 'cancelled', None):
                guard.after('write_file', 'private-output', {'path': 'file'}, status=status)
            guard.after('read_file', 'private-output', {'path': 'file'}, status='ok')
            self.assertEqual(calls, [])
            guard.after('write_file', 'private-output', {'path': 'file', 'content': 'body'}, status='ok', session_id='own')
        self.assertEqual(len(calls), 1)
        name, payload = calls[0]
        self.assertEqual(name, 'release-file-reservation.sh')
        self.assertEqual(payload['tool_result'], {'success': True})
        self.assertNotIn('private-output', json.dumps(payload))
        self.assertEqual(payload['cwd'], str(self.base))

    def test_real_resolver_uses_native_cwd_for_relative_checks_and_release(self):
        a, b = self.base / 'A', self.base / 'B'
        a.mkdir(); b.mkdir()
        for directory in (a, b):
            (directory / 'work.txt').write_text('unchanged')
        grant, seen = self.base / 'grant', self.base / 'seen'
        resolver = ('#!/bin/bash\n. "$FIXTURE_COMMON"\ninput=$(cat)\n'
                    'reservation_resolve_tool_context "$input" || exit 2\n'
                    'printf "%s|%s|%s\\n" "$FILE_PATH" "$MATCHED_ROOT" "$PWD" >> "$FIXTURE_SEEN"\n')
        (self.core / 'hooks/check-file-reservation.sh').write_text(
            resolver + '[ "$FILE_PATH" = "$(cat "$FIXTURE_GRANT")" ] || exit 2\n')
        (self.core / 'hooks/release-file-reservation.sh').write_text(resolver)
        env = {'AGENTSTACK_PROJECT_KEY': str(self.base), 'AGENTSTACK_PROTECTED_ROOTS': str(self.base),
               'AGENTSTACK_HOOKS_DIR': str(self.core / 'hooks'), 'AGENTSTACK_PYTHON': sys.executable,
               'FIXTURE_COMMON': str(ROOT / 'hooks/reservation-common.sh'),
               'FIXTURE_GRANT': str(grant), 'FIXTURE_SEEN': str(seen)}
        original = Path.cwd()
        try:
            os.chdir(a)
            with patch.dict(os.environ, env), patch.object(guard, '_cwd', return_value=str(b)):
                args = {'path': 'work.txt', 'content': 'native write'}
                grant.write_text(str(a / 'work.txt'))
                self.assertEqual(guard.before('write_file', args)['action'], 'block')
                self.assertEqual((b / 'work.txt').read_text(), 'unchanged')
                grant.write_text(str(b / 'work.txt'))
                self.assertIsNone(guard.before('write_file', args))
                (b / 'work.txt').write_text(args['content'])
                guard.after('write_file', 'private-result', args, status='ok')
        finally:
            os.chdir(original)
        self.assertEqual((a / 'work.txt').read_text(), 'unchanged')
        self.assertEqual((b / 'work.txt').read_text(), 'native write')
        self.assertEqual(seen.read_text().splitlines(), ['|'.join(map(str, (b / 'work.txt', self.base, b)))] * 3)

    def test_real_common_reservation_hook_missing_expired_conflict_and_allowed(self):
        from test_check_file_reservation import _Server, _mcp_result
        wrapper = self.core / 'hooks/check-file-reservation.sh'
        wrapper.write_text('exec /bin/bash ' + shlex.quote(str(ROOT / 'hooks/check-file-reservation.sh')) + '\n')
        runtime = self.base / 'runtime'
        runtime.mkdir()
        (runtime / 'agent_token_FixtureAgent').write_text('synthetic-owner-token')
        env = {'AGENT_NAME': 'FixtureAgent', 'AGENTSTACK_PROJECT_KEY': str(self.base),
               'AGENTSTACK_PROTECTED_ROOTS': str(self.base), 'AGENTSTACK_RUNTIME_DIR': str(runtime),
               'AGENTSTACK_HOOKS_DIR': str(self.core / 'hooks'), 'AGENTSTACK_PYTHON': sys.executable,
               'AGENTSTACK_MAIL_HTTP_BEARER_MODE': 'disabled', 'FILE_RESERVATION_RETRY_DELAY_SECONDS': '0'}
        for state in ('missing', 'expired', 'conflict', 'allowed'):
            response = json.loads(_mcp_result(1 if state in ('conflict', 'allowed') else 0))
            if state == 'conflict':
                response['result']['structuredContent']['conflict_check']['conflicts'] = [
                    {'agent': 'OtherAgent', 'path_pattern': 'native.txt'}]
            with self.subTest(state=state), _Server(lambda _: (200, json.dumps(response).encode())) as server:
                with patch.dict(os.environ, {**env, 'AGENTSTACK_MCP_URL': server.url}):
                    result = guard.before('write_file', {'path': 'native.txt', 'content': state}, session_id='own-session')
                target = self.base / 'native.txt'
                if result is None:
                    target.write_text(state)  # native handler is reached only after pre-tool approval
                self.assertEqual(result is None, state == 'allowed', result)
                if state != 'allowed':
                    self.assertIn('FILE RESERVATION REQUIRED:', result['message'])
                    self.assertIn('before editing', result['message'])
                self.assertEqual(target.exists(), state == 'allowed')
                self.assertTrue(server.requests)
        self.assertEqual((self.base / 'native.txt').read_text(), 'allowed')

    def test_registered_hook_timeout_is_not_reported_as_allow(self):
        with patch.object(guard.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 60)):
            with self.assertRaises(subprocess.TimeoutExpired):
                guard.before('write_file', {'path': 'file.txt', 'content': 'never'})

    def test_native_base_cwd_and_relative_read_mapping(self):
        payload = next(guard.payloads('read_file', {'path': 'provider.json'}, cwd=str(self.base / 'changed'), session_id='s'))
        self.assertEqual(payload['tool_input']['file_path'], str(self.base / 'changed/provider.json'))
        self.assertEqual(payload['tool_name'], 'Read')

    def test_terminal_workdir_is_hook_process_directory(self):
        paths = ModuleType('tools.file_tools_paths')
        paths._resolve_base_dir = lambda task: self.base / 'changed'
        (self.base / 'changed/nested').mkdir(parents=True)
        with patch.dict(sys.modules, {'tools.file_tools_paths': paths}):
            native = self.native_cwd('terminal', {'workdir': 'nested'}, 'own-task')
        calls = []
        with patch.object(guard, '_cwd', return_value=native), \
                patch.object(guard.subprocess, 'run', side_effect=lambda argv, **kwargs:
                             calls.append(kwargs['cwd']) or SimpleNamespace(returncode=0)):
            guard.before('terminal', {'command': 'true', 'workdir': 'nested'})
        self.assertEqual(calls, [str(self.base / 'changed/nested')] * 2)

    def test_session_event_is_own_id_not_delegation_or_mru(self):
        context = ModuleType('agent.delegation_context')
        context.is_delegated_child_context = lambda: False
        with patch.dict(sys.modules, {'agent.delegation_context': context}):
            guard.session_started('own-exact-session')
            path = self.base / 'native-session.json'
            self.assertEqual(json.loads(path.read_text())['native_session_id'], 'own-exact-session')
            context.is_delegated_child_context = lambda: True
            guard.session_started('delegated-child')
            self.assertEqual(json.loads(path.read_text())['native_session_id'], 'own-exact-session')
            path.unlink()
            path.mkdir()
            context.is_delegated_child_context = lambda: False
            guard.session_started('not-persistable')  # optional metadata cannot kill startup

    def test_register_only_adds_native_hooks_no_preflight(self):
        hooks = {}
        guard.register(SimpleNamespace(register_hook=lambda name, fn: hooks.setdefault(name, fn)))
        self.assertEqual(set(hooks), {'pre_tool_call', 'post_tool_call', 'on_session_start'})


if __name__ == '__main__':
    unittest.main()
