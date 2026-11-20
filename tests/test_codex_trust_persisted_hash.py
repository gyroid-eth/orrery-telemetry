"""Verify approved Codex hook trust matches its persisted native form."""
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRUST = runpy.run_path(str(ROOT / 'scripts/codex_plugin_trust.py'))


def fixture(tmp_path, composite):
    home = tmp_path / 'codex'
    root = home / 'plugins' / 'cache' / 'agentstack-local' / 'agentstack-codex-app' / '0.1.0'
    (root / 'hooks').mkdir(parents=True)
    hooks = {'hooks': {'PreToolUse': [{'matcher': '^apply_patch$'}]}}
    (root / 'hooks' / 'hooks.json').write_text(json.dumps(hooks))
    key = 'agentstack-codex-app@agentstack-local:hooks/hooks.json:pre_tool_use:0:0'
    base_hash = 'sha256:' + 'a' * 64
    content_hash = 'sha256:' + 'b' * 64
    current_hash = base_hash + ('/' + content_hash if composite else '')
    state = {'trusted_hash': base_hash, 'enabled': True}
    if composite:
        state['trusted_content_hash'] = content_hash
    plan = {
        'schema_version': 1, 'approved': True, 'enable_hooks': True,
        'shared_codex_home': str(home), 'plugin_root': str(root),
        'plugin_id': 'agentstack-codex-app@agentstack-local',
        'declared_hooks': hooks,
        'payload_digest': TRUST['payload_digest'](root),
        'hooks': [{'key': key, 'currentHash': current_hash}],
    }
    return home, plan, state


def write_state(home, plan, state):
    config = home / 'config.toml'
    config.write_text(
        '[plugins."agentstack-codex-app@agentstack-local"]\n'
        'enabled = true\n'
        f'[hooks.state."{plan["hooks"][0]["key"]}"]\n'
        + ''.join(f'{key} = {json.dumps(value)}\n' for key, value in state.items())
    )
    config.chmod(0o600)


@pytest.mark.parametrize('composite', [False, True])
def test_approved_hook_trust_accepts_native_storage(tmp_path, composite):
    home, plan, state = fixture(tmp_path, composite)
    write_state(home, plan, state)
    TRUST['verify_approved_plan'](plan)


@pytest.mark.parametrize('mutation', ['wrong_base', 'wrong_content', 'missing_content', 'disabled', 'extra'])
def test_approved_composite_trust_refuses_mutated_state(tmp_path, mutation):
    home, plan, state = fixture(tmp_path, True)
    if mutation == 'wrong_base':
        state['trusted_hash'] = 'sha256:' + 'c' * 64
    elif mutation == 'wrong_content':
        state['trusted_content_hash'] = 'sha256:' + 'c' * 64
    elif mutation == 'missing_content':
        del state['trusted_content_hash']
    elif mutation == 'disabled':
        state['enabled'] = False
    else:
        state['unexpected'] = True
    write_state(home, plan, state)
    with pytest.raises(TRUST['TrustUnavailable'], match='approved hook trust is not present'):
        TRUST['verify_approved_plan'](plan)


@pytest.mark.parametrize('invalid', ['sha256:' + 'a' * 64 + '/broken', 'sha256:' + 'a' * 64 + '/sha256:' + 'b' * 64 + '/third'])
def test_invalid_composite_hash_is_refused(tmp_path, invalid):
    home, plan, state = fixture(tmp_path, True)
    plan['hooks'][0]['currentHash'] = invalid
    write_state(home, plan, state)
    with pytest.raises(TRUST['TrustUnavailable'], match='invalid reviewed hook hash'):
        TRUST['verify_approved_plan'](plan)
