"""Synthetic definitions only; never invoke a real credential store."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / 'hooks/deny-llm-key-read.sh'
PLUGIN = ROOT / 'integrations/codex_app/plugin/scripts/run-key-read-guard.sh'


def env(tmp_path):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    return {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'AGENTSTACK_HOME': str(tmp_path / 'core'),
            'AGENTSTACK_PYTHON': sys.executable, 'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test-key-read'}


def definitions(tmp_path, e):
    path = tmp_path / 'backend.json'
    path.write_text(json.dumps({'backends': {'fixture': {'keychain': {'service': 'fixture-model-key', 'account': 'fixture-user'}}}}))
    e['AGENTSTACK_LLM_KEY_DEFINITIONS'] = str(path)
    return path


def hook(command, e, *, tool='Bash', entry=SH):
    key = 'cmd' if tool == 'exec_command' else 'command'
    return subprocess.run(['/bin/bash', str(entry)], input=json.dumps({'tool_name': tool, 'tool_input': {key: command}}),
                          text=True, capture_output=True, env=e, timeout=10)


@pytest.mark.parametrize('command', [
    'security find-generic-password -w -s fixture-model-key -a fixture-user',
    '/usr/bin/security find-generic-password -s fixture-model-key -g',
    'security find-generic-password -wsfixture-model-key -afixture-user',
    'KEY="$(security find-generic-password -s fixture-model-key -w)"',
    'KEY=`security find-generic-password -s fixture-model-key -w`',
    "bash -lc 'security find-generic-password -w -s fixture-model-key'",
    'echo before; security find-generic-password -w -s fixture-model-key | cat',
    'security find-generic-password -w',
    'security find-generic-password -w -s "$SERVICE"',
])
@pytest.mark.parametrize('tool', ['Bash', 'exec_command', 'shell_command'])
def test_same_key_value_reads_are_denied(tmp_path, command, tool):
    e = env(tmp_path)
    definitions(tmp_path, e)
    done = hook(command, e, tool=tool)
    assert done.returncode == 2, done.stderr
    assert 'LOCAL_MODEL_KEY_READ_DENIED' in done.stderr
    assert done.stdout == ''
    assert 'fixture-model-key' not in done.stderr


@pytest.mark.parametrize('command', [
    'security find-generic-password -s fixture-model-key -a fixture-user',
    'security find-generic-password -w -s another-key',
    'security find-generic-password -w -s fixture-model-key -a another-user',
    "grep 'fixture-model-key' backend.json",
    "echo 'security find-generic-password -w -s fixture-model-key'",
    "grep '$(security find-generic-password -w -s fixture-model-key)' examples.txt",
    'git status --short',
])
def test_attributes_other_keys_and_config_search_pass(tmp_path, command):
    e = env(tmp_path)
    definitions(tmp_path, e)
    assert hook(command, e).returncode == 0


def test_missing_definition_is_noop_and_default_installed_path_is_used(tmp_path):
    e = env(tmp_path)
    command = 'security find-generic-password -w -s fixture-model-key'
    assert hook(command, e).returncode == 0
    path = Path(e['AGENTSTACK_HOME']) / 'integrations/dsh/backend.json'
    path.parent.mkdir(parents=True)
    other = definitions(tmp_path, e)
    path.write_bytes(other.read_bytes())
    del e['AGENTSTACK_LLM_KEY_DEFINITIONS']
    assert hook(command, e).returncode == 2


def test_r3_auth_argv_and_wsl_selector_share_the_guard(tmp_path):
    e = env(tmp_path)
    root = tmp_path / 'profiles'
    root.mkdir()
    (root / 'local.toml').write_text('[claude]\nauth_command = ["/usr/bin/secret-tool", "lookup", "service", "fixture-local", "account", "fixture-user"]\n')
    e['AGENTSTACK_LLM_KEY_DEFINITIONS'] = str(root)
    assert hook('secret-tool lookup account fixture-user service fixture-local', e).returncode == 2
    assert hook('secret-tool lookup service unrelated', e).returncode == 0
    assert hook('secret-tool search service fixture-local', e).returncode == 0
    assert hook('grep auth_command local.toml', e).returncode == 0


def test_failure_only_closes_credential_extraction_shapes(tmp_path):
    e = env(tmp_path)
    path = definitions(tmp_path, e)
    path.write_text('{invalid json')
    assert hook('security find-generic-password -w -s fixture-model-key', e).returncode == 2
    assert hook('git status', e).returncode == 0
    assert hook('security find-generic-password -s fixture-model-key', e).returncode == 0
    assert hook("grep 'security find-generic-password -w' guide.md", e).returncode == 0
    e['AGENTSTACK_PYTHON'] = '/does-not-exist/fixture-python'
    assert hook('git status', e).returncode == 0
    assert hook('security find-generic-password -w -s fixture-model-key', e).returncode == 2


def test_non_shell_tools_never_block(tmp_path):
    e = env(tmp_path)
    definitions(tmp_path, e)
    assert hook('security find-generic-password -w -s fixture-model-key', e, tool='Read').returncode == 0


def test_codex_plugin_executes_same_installed_guard(tmp_path):
    e = env(tmp_path)
    core_hooks = Path(e['AGENTSTACK_HOME']) / 'hooks'
    core_hooks.mkdir(parents=True)
    for name in ('deny-llm-key-read.sh', 'deny-llm-key-read.py'):
        (core_hooks / name).write_bytes((ROOT / 'hooks' / name).read_bytes())
    definitions(tmp_path, e)
    done = hook(['/bin/bash', '-lc', 'security find-generic-password -w -s fixture-model-key'], e,
                tool='shell_command', entry=PLUGIN)
    assert done.returncode == 2
    (core_hooks / 'deny-llm-key-read.sh').unlink()
    assert hook('git status', e, tool='exec_command', entry=PLUGIN).returncode == 0
    assert hook('secret-tool lookup service fixture-local', e, tool='exec_command', entry=PLUGIN).returncode == 2


def test_launch_auth_helper_is_not_a_shell_tool_and_is_not_executed_by_inspection(tmp_path):
    e = env(tmp_path)
    marker = tmp_path / 'called'
    helper = tmp_path / 'fixture-auth'
    helper.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\nprintf fake-credential\n')
    helper.chmod(0o700)
    root = tmp_path / 'profiles'
    root.mkdir()
    (root / 'local.toml').write_text('[codex]\nauth_command = [' + json.dumps(str(helper)) + ']\n')
    e['AGENTSTACK_LLM_KEY_DEFINITIONS'] = str(root)
    assert hook(str(helper), e).returncode == 2
    assert not marker.exists()
    # A launcher/key_cmd subprocess does not call a model PreToolUse hook.
    result = subprocess.run([str(helper)], env=e, capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stdout == b'fake-credential'
    assert marker.read_text() == 'invoked'


def test_denial_precedes_the_synthetic_credential_process(tmp_path):
    e = env(tmp_path)
    definitions(tmp_path, e)
    directory = tmp_path / 'bin'
    directory.mkdir()
    marker = tmp_path / 'credential-process-ran'
    fake_security = directory / 'security'
    fake_security.write_text(f'#!/bin/sh\nprintf called > "{marker}"\n')
    fake_security.chmod(0o700)
    e['PATH'] = str(directory) + ':/usr/bin:/bin'
    command = 'security find-generic-password -w -s fixture-model-key'
    decision = hook(command, e)
    if decision.returncode == 0:
        subprocess.run(['/bin/bash', '-c', command], env=e, check=True, timeout=5)
    assert decision.returncode == 2
    assert not marker.exists()


def test_installer_merges_the_key_guard_without_replacing_user_hooks(tmp_path):
    e = env(tmp_path)
    settings = tmp_path / 'settings.json'
    custom = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo user-hook'}]}
    settings.write_text(json.dumps({'hooks': {'PreToolUse': [custom]}}))
    done = subprocess.run([sys.executable, str(ROOT / 'scripts/lib/merge_settings.py'),
        '--settings', str(settings), '--template', str(ROOT / 'hooks/settings.template.json'),
        '--hooks-dir', str(tmp_path / 'core/hooks'), '--bin-dir', str(tmp_path / 'core/bin'),
        '--skills-dir', str(tmp_path / 'core/skills'), '--backup-dir', str(tmp_path / 'backup')],
        env=e, capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    groups = json.loads(settings.read_text())['hooks']['PreToolUse']
    assert custom in groups
    assert any(h['command'] == '/bin/bash ' + str(tmp_path / 'core/hooks/deny-llm-key-read.sh')
               for g in groups for h in g['hooks'])


@pytest.mark.parametrize('prefix', ['sudo -E ', 'timeout 5 ', 'env FIXTURE=1 timeout 5 '])
def test_ordinary_command_wrappers_do_not_hide_the_lookup(tmp_path, prefix):
    e = env(tmp_path)
    definitions(tmp_path, e)
    assert hook(prefix + 'security find-generic-password -w -s fixture-model-key', e).returncode == 2


@pytest.mark.parametrize('structure', [
    'if true; then {read}; fi',
    '{{ {read}; }}',
    'true && {read}',
    '( {read} )',
    'KEY="$( {read} )"',
])
@pytest.mark.parametrize('store', ['keychain', 'secret-tool'])
@pytest.mark.parametrize('tool', ['Bash', 'exec_command'])
def test_extraction_is_found_anywhere_in_shell_control_syntax(tmp_path, structure, store, tool):
    e = env(tmp_path)
    if store == 'keychain':
        definitions(tmp_path, e)
        read = 'security find-generic-password -w -s fixture-model-key -a fixture-user'
    else:
        path = tmp_path / 'profile.toml'
        path.write_text('[codex]\nauth_command = ["/usr/bin/secret-tool", "lookup", "service", "fixture-model-key", "account", "fixture-user"]\n')
        e['AGENTSTACK_LLM_KEY_DEFINITIONS'] = str(path)
        read = 'secret-tool lookup service fixture-model-key account fixture-user'
    assert hook(structure.format(read=read), e, tool=tool).returncode == 2


@pytest.mark.parametrize('broken', [False, True])
@pytest.mark.parametrize('command', [
    'if true; then security find-generic-password -s fixture-model-key; fi',
    'if true; then grep -e security -e find-generic-password -e -w backend.json; fi',
    "{ grep '$(security find-generic-password -w -s fixture-model-key)' guide.md; }",
])
def test_full_command_scan_still_allows_attributes_and_grep(tmp_path, broken, command):
    e = env(tmp_path)
    path = definitions(tmp_path, e)
    if broken:
        path.write_text('{invalid')
    assert hook(command, e).returncode == 0


def test_control_prefix_also_protects_defined_auth_helper_and_broken_definitions(tmp_path):
    e = env(tmp_path)
    path = tmp_path / 'profile.toml'
    path.write_text('[claude]\nauth_command = ["/fixture/auth-helper"]\n')
    e['AGENTSTACK_LLM_KEY_DEFINITIONS'] = str(path)
    assert hook('if true; then /fixture/auth-helper; fi', e).returncode == 2
    path.write_text('{broken')
    assert hook('if true; then security find-generic-password -w -s fixture-model-key; fi', e).returncode == 2
    assert hook('if true; then git status; fi', e).returncode == 0
