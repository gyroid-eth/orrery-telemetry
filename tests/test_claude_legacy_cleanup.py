"""Exit must preserve old owner material until explicit authenticated resume."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dashboard import server
from test_claude_legacy_resume import legacy, same_material, snapshot
from test_claude_resume_mail import NAME, TOKEN, resume

ROOT = Path(__file__).resolve().parents[1]


def cleanup(runtime, registration, *, days='30'):
    env = dict(os.environ)
    for key in ('AGENT_NAME', 'CHILD_REGISTRATION_TOKEN', 'MCP_AGENT_MAIL_TOKEN', 'MCP_URL'):
        env.pop(key, None)
    env.update(AGENTSTACK_RUNTIME_DIR=str(runtime), AGENTSTACK_HOOKS_DIR=str(ROOT / 'hooks'),
               AGENTSTACK_CHILD_RESUME_HELPER=str(ROOT / 'hooks/child_resume.py'),
               AGENTSTACK_PYTHON=sys.executable, PROJECT_KEY=registration['project_key'],
               AGENTSTACK_MANAGED_AGENTS_FILE=str(runtime / 'managed_agents.txt'),
               AGENTSTACK_MAIL_HTTP_BEARER_MODE='disabled', AGENTSTACK_MCP_URL='http://127.0.0.1:1/mcp',
               AGENTSTACK_CHILD_RESUME_RETENTION_DAYS=days)
    return subprocess.run(['/bin/bash', str(ROOT / 'hooks/cleanup-child-agent.sh'), NAME],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('mode', [0o400, 0o600])
def test_old_child_exit_keeps_original_owner_and_then_authenticated_resume(legacy, mode):
    runtime, registration, launches, calls, state, config, _ = legacy
    token = runtime / ('agent_token_' + NAME)
    state.chmod(mode)
    token.chmod(mode)
    before = {path: value for path, value in snapshot(runtime).items() if path != config}
    result = cleanup(runtime, registration)
    assert result.returncode == 0, result.stderr
    same_material(before)
    assert set(json.loads(state.read_text())) == {'agent_name', 'project_key', 'registration_token'}
    assert not config.exists() and not calls and not launches
    assert TOKEN not in result.stdout + result.stderr
    assert server._resume_capability(NAME, 'claude-code', category='finished') == 'ready'
    resumed = server.do_resume(NAME, open_terminal=False)
    assert resumed['ok'], resumed
    assert [method for method, _ in calls] == ['register_agent', 'unretire_agent']
    assert json.loads(state.read_text())['agent_id'] == registration['agent_id']


def test_retention_zero_remains_an_explicit_legacy_opt_out(legacy):
    runtime, registration, launches, calls, state, config, _ = legacy
    token = runtime / ('agent_token_' + NAME)
    other = {path: value for path, value in snapshot(runtime).items() if 'OtherOwner' in path.name}
    result = cleanup(runtime, registration, days='0')
    assert result.returncode == 0, result.stderr
    assert not state.exists() and not token.exists() and not config.exists()
    same_material(other)
    assert not calls and not launches


@pytest.mark.parametrize('damage', ['token_mismatch', 'missing_token', 'state_public', 'token_public',
                                    'state_symlink', 'token_symlink', 'wrong_name', 'empty_project',
                                    'registration_pending', 'migration_pending', 'corrupt_state'])
def test_invalid_legacy_exit_refuses_without_destroying_private_owner_material(legacy, damage):
    runtime, registration, launches, calls, state, config, _ = legacy
    token = runtime / ('agent_token_' + NAME)
    if damage == 'token_mismatch':
        token.write_text('different-owner')
    elif damage == 'missing_token':
        token.unlink()
    elif damage.endswith('_public'):
        (state if damage == 'state_public' else token).chmod(0o644)
    elif damage.endswith('_symlink'):
        path = state if damage == 'state_symlink' else token
        target = path.with_suffix('.original')
        path.rename(target)
        path.symlink_to(target)
    elif damage in ('wrong_name', 'empty_project'):
        value = json.loads(state.read_text())
        value['agent_name' if damage == 'wrong_name' else 'project_key'] = 'OtherOwner' if damage == 'wrong_name' else ''
        state.chmod(0o600)
        state.write_text(json.dumps(value))
    elif damage.endswith('_pending'):
        suffix = 'registration-pending' if damage == 'registration_pending' else 'legacy-migration'
        pending = state.parent / f'.{NAME}.{suffix}.json'
        pending.write_text('{}')
        pending.chmod(0o600)
    else:
        state.chmod(0o600)
        state.write_text('{broken')
    before = {path: value for path, value in snapshot(runtime).items() if path != config}
    result = cleanup(runtime, registration)
    assert result.returncode != 0
    same_material(before)
    assert not launches and not calls
    assert TOKEN not in result.stdout + result.stderr
