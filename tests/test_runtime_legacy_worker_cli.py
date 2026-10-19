"""Actual isolated Python CLI, not actual provider/ps/key acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_runtime_legacy_auth_binding import bound, configure_fixture, context_fixture, TOKEN


def invoke(bound, args, *, input=''):
    target = bound.intent['legacy_context']
    env = {'HOME': str(bound.ws.tmp / 'home'), 'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
        'AGENTSTACK_RUNTIME_MODE': 'legacy', 'AGENTSTACK_HOME': target['install_root'],
        'AGENTSTACK_RUNTIME_DIR': target['runtime_dir'], 'AGENTSTACK_PROJECT_KEY': target['project_key'],
        'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test.preview-worker-cli'}
    return subprocess.run([sys.executable, '-I', '-B',
        str(Path(target['install_root']) / 'bin/lib/runtime_legacy_preview_cli.py'), *args],
        env=env, cwd=str(bound.ws.tmp), input=input, capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize('args', [
    ['entry', 'runner', 'FixtureChild'],
    ['entry', 'provider', 'FixtureChild'],
    ['entry', 'proxy', 'FixtureChild', '--launch-id=' + 'a' * 32],
    ['entry', 'runner', 'FixtureChild', '--owner=/other'],
    ['entry', 'provider', 'FixtureChild', '--observer=DO_NOT_ECHO'],
    ['entry', 'proxy', 'FixtureChild', '--bearer=DO_NOT_ECHO'],
    ['entry', 'arbitrary-exec', 'FixtureChild'],
    ['entry', 'session-start', 'FixtureChild', '--launch-id=' + 'a' * 32],
    ['entry', 'end-session', 'FixtureChild', '--receipt-nonce=' + 'b' * 43],
])
def test_actual_isolated_bootstrap_and_closed_argv_before_any_actor(bound, args):
    positive = invoke(bound, ['status', 'FixtureChild'])
    assert positive.returncode == 0 and json.loads(positive.stdout)['name'] == 'FixtureChild'
    before = bound.store.owner.read_bytes()
    negative = invoke(bound, args)
    assert negative.returncode == 2 and negative.stdout == ''
    assert negative.stderr.strip() == 'LOCAL_MODEL_PREVIEW_ARGUMENT_INVALID'
    assert TOKEN not in negative.stderr and 'DO_NOT_ECHO' not in negative.stderr
    assert bound.store.owner.read_bytes() == before


@pytest.mark.parametrize('payload', ['', '[]', '{"session_id":"one","session_id":"two"}',
    '{"session_id":NaN}', ' ' * 1048577])
def test_actual_isolated_hook_rejects_invalid_json_before_process_observation(bound, payload):
    positive = invoke(bound, ['status', 'FixtureChild'])
    assert positive.returncode == 0
    before = bound.store.owner.read_bytes()
    result = invoke(bound, ['entry', 'session-start', 'FixtureChild'], input=payload)
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.strip() == 'LOCAL_MODEL_SESSION_FAILED' and TOKEN not in result.stderr
    assert bound.store.owner.read_bytes() == before


@pytest.mark.parametrize('operation', ['pre-edit', 'post-edit'])
def test_actual_isolated_edit_hook_rejects_missing_session_without_http(bound, operation):
    positive = invoke(bound, ['status', 'FixtureChild'])
    assert positive.returncode == 0
    before = bound.store.owner.read_bytes()
    result = invoke(bound, ['entry', operation, 'FixtureChild'], input='{}')
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.strip() == 'LOCAL_MODEL_RESERVATION_FAILED' and TOKEN not in result.stderr
    assert bound.store.owner.read_bytes() == before


def test_actual_isolated_runner_rejects_stale_digest_without_starting_a_process(bound):
    from runtime_intent_store import read_owner, write_owner
    frame = read_owner(bound.store.owner); frame.pop('local_execution'); write_owner(bound.store.owner, frame)
    positive = invoke(bound, ['status', 'FixtureChild'])
    assert positive.returncode == 0 and json.loads(positive.stdout)['phase'] == 'registered'
    before = bound.store.owner.read_bytes()
    result = invoke(bound, ['entry', 'runner', 'FixtureChild', '--intent-digest=' + 'c' * 64,
        '--registration-generation=' + frame['local_registration']['generation']])
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.strip() == 'LOCAL_MODEL_EXECUTION_FAILED'
    assert bound.store.owner.read_bytes() == before
