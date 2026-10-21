"""Codex patch reservations against isolated homes and a fake Mail endpoint."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / 'hooks/check-codex-file-reservation.py'
RUNNER = ROOT / 'integrations/codex_app/plugin/scripts/run-reservation-guard.sh'


@pytest.fixture
def sandbox(tmp_path):
    home, project, runtime = (tmp_path / name for name in ('home', 'project', 'runtime'))
    for directory in (home, project, runtime):
        directory.mkdir()
    env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8',
           'CODEX_HOME': str(home / '.codex'), 'AGENTSTACK_HOME': str(ROOT),
           'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test.codex-reservation',
           'AGENTSTACK_RUNTIME_DIR': str(runtime), 'AGENTSTACK_PROJECT_KEY': str(project),
           'AGENTSTACK_PROTECTED_ROOTS': str(project), 'AGENT_NAME': 'EditorAlpha',
           'AGENTSTACK_MAIL_HTTP_BEARER_MODE': 'disabled', 'AGENTSTACK_PYTHON': sys.executable,
           'FILE_RESERVATION_RETRY_DELAY_SECONDS': '0'}
    return project, env


class Mail:
    def __init__(self, owned=(), foreign=(), *, rejected=False):
        self.owned, self.foreign, self.rejected = set(owned), list(foreign), rejected
        self.requests = []
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                parent.requests.append(request)
                assert request['method'] == 'tools/call'
                assert request['params']['name'] == 'renew_file_reservations'
                args = request['params']['arguments']
                assert 'registration_token' not in args
                count = int(any(path in parent.owned for path in args['paths']))
                result = {'structuredContent': {'renewed': count, 'conflict_check': {
                    'version': 1, 'project_key': args['project_key'], 'paths': args['paths'],
                    'conflicts': parent.foreign}}}
                if parent.rejected: result['isError'] = True
                body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    def __enter__(self):
        self.thread.start(); return self
    def __exit__(self, *args):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
    @property
    def url(self): return f'http://127.0.0.1:{self.server.server_port}/mcp'


def patch(*paths):
    return '*** Begin Patch\n' + ''.join(f'*** Add File: {p}\n+new\n' for p in paths) + '*** End Patch'


def run(sandbox, command, mail, *, runner=False, changes=None, payload_changes=None):
    project, env = sandbox
    env = dict(env, AGENTSTACK_MCP_URL=mail.url, **(changes or {}))
    payload = {'hook_event_name': 'PreToolUse', 'session_id': 'fixture-session',
               'cwd': str(project), 'tool_name': 'apply_patch', 'tool_use_id': 'call-1',
               'tool_input': {'command': command}}
    payload.update(payload_changes or {})
    return subprocess.run(['/bin/bash', str(RUNNER)] if runner else [sys.executable, str(ADAPTER)],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=env, cwd=project.parent, timeout=30)


def test_unreserved_patch_is_refused(sandbox):
    with Mail() as mail:
        result = run(sandbox, patch('note.md'), mail)
    assert result.returncode == 2 and 'FILE RESERVATION REQUIRED' in result.stderr
    renews = [r for r in mail.requests if r['method'] == 'tools/call']
    assert len(renews) == 2  # retain the shared guard's definitive-zero retry


def test_foreign_exclusive_reason_names_owner(sandbox):
    foreign = [{'agent': 'EditorBeta', 'path_pattern': 'docs/**'}]
    with Mail(foreign=foreign) as mail:
        result = run(sandbox, patch('docs/note.md'), mail)
    assert result.returncode == 2 and 'EditorBeta' in result.stderr


def test_own_reservation_passes_through_plugin_runner(sandbox):
    with Mail(owned=['note.md']) as mail:
        result = run(sandbox, patch('note.md'), mail, runner=True)
    assert result.returncode == 0, result.stderr
    assert mail.requests[0]['params']['arguments']['agent_name'] == 'EditorAlpha'
    assert not (sandbox[0] / 'note.md').exists()  # a hook only decides, never writes


def test_every_patch_file_requires_ownership(sandbox):
    with Mail(owned=['first.md']) as mail:
        result = run(sandbox, patch('first.md', 'second.md'), mail)
    assert result.returncode == 2 and 'second.md' in result.stderr


@pytest.mark.parametrize('owned', [('old.md',), ('new.md',)])
def test_move_requires_both_source_and_destination(sandbox, owned):
    command = '*** Begin Patch\n*** Update File: old.md\n*** Move to: new.md\n@@\n-old\n+new\n*** End Patch'
    with Mail(owned=owned) as mail:
        result = run(sandbox, command, mail)
    assert result.returncode == 2


@pytest.mark.parametrize('header', ['Add File', 'Update File', 'Delete File'])
def test_all_patch_operations_share_the_guard(sandbox, header):
    with Mail() as mail:
        result = run(sandbox, f'*** Begin Patch\n*** {header}: file.md\n*** End Patch', mail)
    assert result.returncode == 2 and 'file.md' in result.stderr


def test_payload_cwd_controls_relative_paths(sandbox):
    with Mail(owned=['note.md']) as mail:
        result = run(sandbox, patch('./note.md'), mail)
    assert result.returncode == 0, result.stderr
    paths = mail.requests[0]['params']['arguments']['paths']
    assert str(sandbox[0] / 'note.md') in paths


def test_outside_protected_root_is_noop(sandbox):
    with Mail() as mail:
        result = run(sandbox, patch(str(sandbox[0].parent / 'outside.md')), mail)
    assert result.returncode == 0 and mail.requests == []


def test_symlink_into_protected_root_is_checked(sandbox):
    project = sandbox[0]
    alias = project.parent / 'alias'; alias.symlink_to(project, target_is_directory=True)
    with Mail() as mail:
        result = run(sandbox, patch(str(alias / 'note.md')), mail)
    assert result.returncode == 2 and 'FILE RESERVATION REQUIRED' in result.stderr


@pytest.mark.parametrize('command', ['', 'not a patch', '*** Begin Patch\n*** Add File: \n*** End Patch'])
def test_invalid_patch_payload_fails_closed(sandbox, command):
    with Mail() as mail:
        result = run(sandbox, command, mail)
    assert result.returncode == 2 and mail.requests == []


def test_shared_mail_rejection_stays_closed(sandbox):
    with Mail(owned=['note.md'], rejected=True) as mail:
        result = run(sandbox, patch('note.md'), mail)
    assert result.returncode == 2 and 'rejected' in result.stderr.lower()


def test_missing_core_guard_is_visible_and_blocks(sandbox):
    with Mail() as mail:
        result = run(sandbox, patch('note.md'), mail, runner=True,
                     changes={'AGENTSTACK_HOME': str(sandbox[0] / 'missing-core'),
                              'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test.codex-reservation'})
    assert result.returncode == 2 and 'unavailable' in result.stderr.lower()


def test_own_lease_cannot_hide_foreign_exclusive(sandbox):
    foreign = [{'agent': 'EditorBeta', 'path_pattern': 'note.md'}]
    with Mail(owned=['note.md'], foreign=foreign) as mail:
        result = run(sandbox, patch('note.md'), mail)
    assert result.returncode == 2 and 'EditorBeta' in result.stderr


@pytest.mark.parametrize('row', [{'agent': 'EditorBeta'}, 'bad-row',
    dict(agent='EditorBeta', path_pattern='note.md', exclusive=True, released_ts=False,
         expires_ts='2999-01-01T00:00:00+00:00')])
def test_invalid_conflict_response_fails_closed(sandbox, row):
    with Mail(owned=['note.md'], foreign=[row]) as mail:
        result = run(sandbox, patch('note.md'), mail)
    assert result.returncode == 2 and 'conflict check rejected' in result.stderr


@pytest.mark.parametrize('policy,expected', [('block', 2), ('warn-open', 0)])
def test_shared_unmanaged_policy_is_preserved(sandbox, policy, expected):
    with Mail() as mail:
        result = run(sandbox, patch('note.md'), mail,
                     changes={'AGENT_NAME': '', 'AGENTSTACK_UNMANAGED_SESSION_POLICY': policy})
    assert result.returncode == expected, result.stderr
    assert mail.requests == []
    if expected == 0:
        assert 'without a file reservation' in json.loads(result.stdout)['systemMessage']


@pytest.mark.parametrize('policy,expected', [('block', 2), ('warn-open', 0)])
def test_identified_reservation_outage_follows_policy(sandbox, policy, expected):
    with Mail() as mail:
        url = mail.url
    class ClosedMail:
        pass
    closed = ClosedMail(); closed.url = url
    result = run(sandbox, patch('note.md'), closed,
                 changes={'AGENTSTACK_MAIL_OUTAGE_POLICY': policy})
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert 'systemMessage' in json.loads(result.stdout)


@pytest.mark.parametrize('state,phrase', [('untrusted', 'needs_review'), ('missing', 'missing'),
                                        ('trusted', 'enabled and its current hook definition is trusted')])
def test_diagnostic_reports_guard_without_approving_it(sandbox, tmp_path, state, phrase):
    project, env = sandbox
    binary, log = tmp_path / 'codex-fixture', tmp_path / 'rpc.jsonl'
    binary.write_text('#!' + sys.executable + '\n' + '''import json,sys
from pathlib import Path
for line in sys.stdin:
    request=json.loads(line)
    with Path(LOG).open('a') as stream: stream.write(json.dumps(request)+'\\n')
    if 'id' not in request: continue
    result={}
    if request['method']=='hooks/list':
        hook={'key':'fixture','pluginId':'agentstack-codex-app@agentstack-local',
              'eventName':'preToolUse','matcher':'^apply_patch$','enabled':True,
              'trustStatus':STATE,'source':'plugin','isManaged':False,
              'sourcePath':SOURCE,'handlerType':'command',
              'command':'"$PLUGIN_ROOT/scripts/run-reservation-guard.sh"','async':False}
        result={'data':[{'cwd':CWD,'hooks':[] if STATE=='missing' else [hook],'errors':[]}]}
    print(json.dumps({'id':request['id'],'result':result}),flush=True)
'''.replace('LOG', repr(str(log))).replace('STATE', repr(state))
       .replace('SOURCE', repr(str(tmp_path / 'plugin/hooks/hooks.json'))).replace('CWD', repr(str(project))))
    binary.chmod(0o755)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/codex-reservation-status.py'),
                             '--codex-binary', str(binary), '--codex-home', env['CODEX_HOME'],
                             '--cwd', str(project), '--plugin-id', 'agentstack-codex-app@agentstack-local'],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and phrase in result.stdout, result.stderr + result.stdout
    if state != 'trusted':
        assert 'warn:' in result.stdout and '/hooks' in result.stdout and 'unguarded' in result.stdout
    calls = [json.loads(line)['method'] for line in log.read_text().splitlines()]
    assert calls == ['initialize', 'initialized', 'hooks/list']


def test_fixture_replaces_inherited_service_and_home_context(sandbox, monkeypatch, tmp_path):
    # Even an ambient context pointing at another reachable service must not be used.
    with Mail() as ambient, Mail(owned=['note.md']) as isolated:
        monkeypatch.setenv('HOME', str(tmp_path / 'unrelated-home'))
        monkeypatch.setenv('AGENTSTACK_RUNTIME_DIR', str(tmp_path / 'unrelated-runtime'))
        monkeypatch.setenv('AGENTSTACK_MCP_URL', ambient.url)
        result = run(sandbox, patch('note.md'), isolated)
    assert result.returncode == 0, result.stderr
    assert ambient.requests == [] and isolated.requests
    assert not (tmp_path / 'unrelated-home').exists()
    assert not (tmp_path / 'unrelated-runtime').exists()


def test_symlinked_protected_root_cannot_skip_guard(sandbox):
    project = sandbox[0]
    alias = project.parent / 'root-alias'; alias.symlink_to(project, target_is_directory=True)
    with Mail() as mail:
        result = run(sandbox, patch('note.md'), mail,
                     changes={'AGENTSTACK_PROTECTED_ROOTS': str(alias)})
    assert result.returncode == 2 and 'FILE RESERVATION REQUIRED' in result.stderr


def test_check_deadline_stops_owned_descendants_that_keep_output_open(tmp_path):
    import importlib.util
    import time
    spec = importlib.util.spec_from_file_location('codex_reservation_adapter', ADAPTER)
    adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    guard = tmp_path / 'slow-guard.sh'
    guard.write_text('sleep 30 &\nwait\n')
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        adapter.run_shared_guard(guard, {}, str(tmp_path), .1)
    assert time.monotonic() - started < 3


def test_interpreter_launch_failure_still_blocks_with_inherited_errexit(sandbox):
    with Mail() as mail:
        result = run(sandbox, patch('note.md'), mail, runner=True,
                     changes={'AGENTSTACK_PYTHON': str(sandbox[0] / 'missing-python'),
                              'SHELLOPTS': 'errexit'})
    assert result.returncode == 2 and mail.requests == []


@pytest.mark.parametrize('command,owned', [
    ('*** Begin Patch\n*** Add File: owned.md\n+one\n  *** Add File: victim.md\n+two\n*** End Patch', ['owned.md']),
    ('*** Begin Patch\n*** Add File:  note.md\n+one\n*** End Patch', ['note.md']),
])
def test_r1_native_header_and_filename_cannot_hide_unowned_target(sandbox, command, owned):
    with Mail(owned=owned) as mail:
        result = run(sandbox, command, mail)
    assert result.returncode == 2, result.stdout + result.stderr


def test_r1_update_context_that_looks_like_header_is_not_a_target(sandbox):
    command = '*** Begin Patch\n*** Update File: owned.md\n@@\n *** Add File: context.md\n-old\n+new\n*** End Patch'
    with Mail(owned=['owned.md']) as mail:
        result = run(sandbox, command, mail)
    assert result.returncode == 0, result.stderr
    renews = [r['params']['arguments']['paths'] for r in mail.requests if r['method'] == 'tools/call']
    assert len(renews) == 1 and renews[0][0] == 'owned.md'


@pytest.mark.parametrize('operation', ['delete', 'move'])
@pytest.mark.parametrize('owned', [False, True])
def test_r1_outward_symlink_source_checks_entry(sandbox, operation, owned):
    project = sandbox[0]
    outside = project.parent / 'outside.md'; outside.write_text('old\n')
    (project / 'link.md').symlink_to(outside)
    if operation == 'delete':
        command = '*** Begin Patch\n*** Delete File: link.md\n*** End Patch'
        leases = ['link.md'] if owned else []
    else:
        command = '*** Begin Patch\n*** Update File: link.md\n*** Move to: moved.md\n@@\n-old\n+new\n*** End Patch'
        leases = ['link.md', 'moved.md'] if owned else ['moved.md']
    with Mail(owned=leases) as mail:
        result = run(sandbox, command, mail)
    assert result.returncode == (0 if owned else 2), result.stderr
    checked = [r['params']['arguments']['paths'][0] for r in mail.requests if r['method'] == 'tools/call']
    assert 'link.md' in checked


@pytest.mark.parametrize('selective', [False, True])
def test_r1_inner_python_failure_cannot_become_noop(sandbox, tmp_path, selective):
    fake = tmp_path / 'bin'; fake.mkdir()
    stub = fake / 'python3'
    stub.write_text('#!' + sys.executable + '\nimport os,sys\n'
                    + ("if '-c' in sys.argv and 'file_path' in sys.argv[-1]: sys.exit(42)\n" if selective else 'sys.exit(42)\n')
                    + 'os.execv(' + repr(sys.executable) + ', [' + repr(sys.executable) + '] + sys.argv[1:])\n')
    stub.chmod(0o755)
    with Mail() as mail:
        result = run(sandbox, patch('note.md'), mail,
                     changes={'AGENTSTACK_PYTHON': sys.executable,
                              'PATH': str(fake) + ':' + sandbox[1]['PATH']})
    assert result.returncode == 2, result.stdout + result.stderr


def test_r1_changelog_uses_pr_number():
    # リリースで断片は CHANGELOG.md へまとめられて消えるので、どちらかにあればよい
    fragment = ROOT / 'changelog.d/000269-codex-reservation-guard.md'
    assert fragment.is_file() or '#269' in (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert not (ROOT / 'changelog.d/000267-codex-reservation-guard.md').exists()


@pytest.mark.parametrize('status', [1, 3, 42])
def test_r1_shared_interpreter_failure_is_not_a_missing_path(sandbox, tmp_path, status):
    stub = tmp_path / 'broken-python'
    stub.write_text('#!/bin/sh\nexit ' + str(status) + '\n')
    stub.chmod(0o755)
    project, environment = sandbox
    result = subprocess.run(['/bin/bash', str(ROOT / 'hooks/check-file-reservation.sh')],
        input=json.dumps({'tool_input': {'file_path': str(project / 'note.md')}}),
        text=True, capture_output=True, cwd=project,
        env=dict(environment, AGENTSTACK_PYTHON=str(stub)), timeout=10)
    assert result.returncode == 2 and 'CONTEXT INVALID' in result.stderr


def test_r1_configured_python_handles_owned_patch_with_broken_path_decoder(sandbox, tmp_path):
    directory = tmp_path / 'bin'; directory.mkdir()
    stub = directory / 'python3'
    stub.write_text('#!' + sys.executable + '\nimport os,sys\n'
                    + "if '-c' in sys.argv and 'file_path' in sys.argv[-1]: sys.exit(42)\n"
                    + 'os.execv(' + repr(sys.executable) + ', [' + repr(sys.executable) + '] + sys.argv[1:])\n')
    stub.chmod(0o755)
    with Mail(owned=['note.md']) as mail:
        result = run(sandbox, patch('note.md'), mail,
                     changes={'PATH': str(directory) + ':' + sandbox[1]['PATH']})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('command,owned', [
    ('*** Begin Patch\n*** Add File: owned.md\n+one\n  *** Add File: victim.md\n+two\n*** End Patch', ['owned.md', 'victim.md']),
    ('*** Begin Patch\n*** Add File:  note.md\n+one\n*** End Patch', [' note.md']),
])
def test_r1_native_path_forms_pass_with_the_actual_reservations(sandbox, command, owned):
    with Mail(owned=owned) as mail:
        result = run(sandbox, command, mail)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('operation', ['Delete', 'MoveSource', 'Edit'])
@pytest.mark.parametrize('symlink', [False, True])
def test_r1_global_guard_does_not_borrow_symlink_target_coverage(sandbox, tmp_path, operation, symlink):
    import runpy
    from types import SimpleNamespace
    api = runpy.run_path(str(ROOT / 'bin/lib/runtime_client.py'))
    project = sandbox[0]
    outside = tmp_path / 'outside.md'; outside.write_text('old')
    path = project / 'link.md'
    if symlink: path.symlink_to(outside)
    else: path.write_text('old')
    calls = []
    def call(name, arguments):
        calls.append((name, arguments))
        return {'covered': True, 'items': [{'reservation_ids': [1]}]}
    client = SimpleNamespace(outputs={'mutation': tmp_path / 'no-pending-mutation'}, call=call,
        all_leases=lambda: [[{'id': 1, 'expires_ts': '2999-01-01T00:00:00+00:00'}]])
    payload = {'tool_name': operation, 'tool_input': {'file_path': str(path)}}
    if symlink and operation != 'Edit':
        with pytest.raises(api['ClientError'], match='RESERVATION_ENTRY_UNSUPPORTED'):
            api['RuntimeClient'].guard_edit(client, payload)
        assert calls == []
    else:
        assert api['RuntimeClient'].guard_edit(client, payload) == {'ok': True, 'renewed': 0}
        assert calls == [('check_file_reservations', {'paths': [str(path)]})]


# ACTIVE filtering and every pattern vector previously tested through fake
# resource rows now run against the real Mail HTTP server in
# test_reservation_conflict_http.py. The hook only validates its certificate.
