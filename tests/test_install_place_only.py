"""Real placement CLI: no service emulation; forbidden functions fail before effect."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'scripts/install.sh'
FORBIDDEN = ('run_preflight', 'preflight_platform', 'preflight_agent_mail_port',
             'resolve_codex_install_setting', 'detect_service_kind', 'check_port',
             'resolve_native_mail_connection', 'retire_legacy_mail_services',
             'check_agent_mail_provisioning_dependencies', 'ensure_native_agentstack_mail',
             'enable_mail_autostart', 'enable_mail_watcher', 'stop_tmux_mail_watcher',
             'safe_merge_settings', 'safe_merge_claude_mcp', 'safe_managed_doc_setups',
             'install_claude_skill_links', 'install_codex_skill_links',
             'write_enrollment_connection_profile', 'start_service', 'verify_dashboard_service')


def snapshot(directory):
    return {str(p.relative_to(directory)): ('link', os.readlink(p)) if p.is_symlink()
            else ('file', hashlib.sha256(p.read_bytes()).hexdigest()) if p.is_file()
            else ('directory', None) for p in directory.rglob('*')}


def environment(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    for name in ('.claude/settings.json', '.claude.json', '.codex/AGENTS.md', '.gemini/config.json'):
        p = home / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('user-owned-data\n')
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'CLAUDE.md').write_text('user project instructions\n')
    stage = tmp_path / 'stage'
    stage.mkdir()
    sentry = tmp_path / 'sentry.sh'
    log = tmp_path / 'forbidden.log'
    command_patterns = '|'.join(pattern for command in
        ('launchctl', 'systemctl', 'tmux', 'uv', 'curl', 'wget', 'fswatch', 'codex', 'claude', 'agy', 'nohup')
        for pattern in (command, command + r'\ *', '*/' + command + r'\ *'))
    race = ('if [[ "${FUNCNAME[0]-}" == place_only && -n "${CLAIM_RACE_DIR:-}" ]]; then '
            + 'mkdir -m 700 "$CLAIM_RACE_DIR"; printf other-install > "$CLAIM_RACE_DIR/marker"; unset CLAIM_RACE_DIR; fi; ')
    trap = (race + 'case "${FUNCNAME[0]-}" in ' + '|'.join(FORBIDDEN)
            + ') printf "%s\\n" "${FUNCNAME[0]}" >> "$SENTRY_LOG"; exit 97;; esac; '
            + 'case "$BASH_COMMAND" in ' + command_patterns
            + ') printf "%s\\n" forbidden-command >> "$SENTRY_LOG"; exit 97;; esac')
    sentry.write_text("set -T\ntrap '" + trap + "' DEBUG\n")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('AGENTSTACK_') and key not in ('HOME', 'CODEX_HOME', 'BASH_ENV', 'TMUX', 'TMUX_PANE')}
    env.update(HOME=str(home), AGENTSTACK_PYTHON=sys.executable,
               AGENTSTACK_LABEL_PREFIX='org.agentstack.test.place-only',
               AGENTSTACK_PROJECT_KEY=str(project), AGENTSTACK_TERMINAL='none',
               BASH_ENV=str(sentry), SENTRY_LOG=str(log), PYTHONDONTWRITEBYTECODE='1')
    return env, home, project, stage / 'payload with space', log


def run(env, *options):
    return subprocess.run(['/bin/bash', str(INSTALLER), '--place-only', *map(str, options)],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=45)


def test_sentry_rejects_actual_function_entry(tmp_path):
    env, _, _, _, log = environment(tmp_path)
    result = subprocess.run(['/bin/bash', '-c', 'start_service() { :; }; start_service'],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 97
    assert log.read_text() == 'start_service\n'


def test_sentry_rejects_launchctl_before_execution(tmp_path):
    env, _, _, _, log = environment(tmp_path)
    result = subprocess.run(['/bin/bash', '-c', '/bin/launchctl print gui/0'],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 97
    assert log.read_text() == 'forbidden-command\n'


@pytest.mark.parametrize('tier', [[], ['--dashboard-only'], ['--scoped']])
def test_real_place_only_publishes_payload_manifest_without_home_or_service_effects(tmp_path, tier):
    env, home, project, destination, log = environment(tmp_path)
    before_home, before_project = snapshot(home), snapshot(project)
    result = run(env, '--install-dir', destination, *tier)
    assert result.returncode == 0, result.stderr
    assert 'Placement complete:' in result.stdout
    assert not log.exists()
    assert snapshot(home) == before_home
    assert snapshot(project) == before_project
    manifest = json.loads((destination / 'install-state.json').read_text())
    assert manifest['installation_mode'] == 'place-only'
    assert manifest['services'] == []
    assert manifest['skill_links'] == []
    assert manifest['settings_merge'] is None
    assert manifest['claude_mcp_merge'] is None
    assert manifest['safe_merge_performed'] is False
    assert manifest['agent_mail']['deployment_identified'] is False
    assert manifest['agent_mail']['enrollment_available'] is False
    assert manifest['agent_mail']['autostart_managed_by_this_install'] is False
    assert manifest['env']['AGENTSTACK_MAIL_HOME'] == str(destination.resolve() / 'mail')
    payload_roots = {'hooks', 'skills', 'dashboard', 'bin', 'codex', 'claude', 'integrations'}
    actual = {str(p) for p in destination.rglob('*')
              if (p.is_file() or p.is_symlink()) and p.relative_to(destination).parts[0] in payload_roots}
    recorded = {p for p in manifest['owned_files'] if Path(p).relative_to(destination).parts[0] in payload_roots}
    assert recorded == actual
    assert all(Path(p).is_relative_to(destination) for p in manifest['owned_files'])
    assert (destination / 'dashboard/server.py').read_bytes() == (ROOT / 'dashboard/server.py').read_bytes()
    assert (destination / 'bin/lib/runtime_client.py').read_bytes() == (ROOT / 'bin/lib/runtime_client.py').read_bytes()
    if not tier or tier == ['--scoped']:
        expected = (ROOT / 'hooks/settings.template.json').read_text().replace('__AGENTSTACK_HOOKS_DIR__', str(destination / 'hooks'))
        assert (destination / 'hooks/settings.template.json').read_text() == expected
    else:
        assert not (destination / 'hooks/settings.template.json').exists()
    # No venv/database/service units are created by payload placement.
    assert not (destination / 'mail').exists()
    assert not (destination / 'mail-service').exists()


def test_explicit_fresh_stage_can_also_be_agentstack_home(tmp_path):
    env, home, _, destination, log = environment(tmp_path)
    env['AGENTSTACK_HOME'] = str(destination)
    before = snapshot(home)
    result = run(env, '--install-dir', destination, '--dashboard-only')
    assert result.returncode == 0, result.stderr
    assert snapshot(home) == before
    assert not log.exists()


def test_place_only_requires_explicit_target_before_any_effect(tmp_path):
    env, home, project, destination, log = environment(tmp_path)
    before = snapshot(home)
    result = run(env)
    assert result.returncode == 2
    assert 'PLACE_ONLY_REQUIRES_INSTALL_DIR' in result.stderr
    assert snapshot(home) == before
    assert not destination.exists()
    assert not log.exists()


@pytest.mark.parametrize('target', ['home', 'default', 'ancestor', 'relative', 'existing', 'symlink'])
def test_place_only_rejects_non_stage_targets_before_any_effect(tmp_path, target):
    env, home, project, destination, log = environment(tmp_path)
    if target == 'home':
        destination = home
    elif target == 'default':
        destination = home / '.agentstack'
    elif target == 'ancestor':
        destination = home.parent
    elif target == 'relative':
        destination = Path('relative-stage')
    elif target == 'existing':
        destination.mkdir()
        (destination / 'marker').write_text('keep')
    else:
        destination.symlink_to(home / '.agentstack')
    before = snapshot(tmp_path)
    result = run(env, '--install-dir', destination)
    assert result.returncode == 2
    assert 'PLACE_ONLY_STAGE_ADMISSION_REJECTED' in result.stderr
    assert snapshot(tmp_path) == before
    assert not log.exists()


def test_place_only_refuses_live_state_override_and_update_options(tmp_path):
    env, home, project, destination, log = environment(tmp_path)
    env['AGENTSTACK_MAIL_STATE_ROOT'] = str(home / '.agentstack/mail')
    result = run(env, '--install-dir', destination)
    assert result.returncode == 2
    assert not destination.exists()
    del env['AGENTSTACK_MAIL_STATE_ROOT']
    for flag in ('--update-mail', '--retire-legacy-mail', '--print-mail-plan', '--print-mail-update-advice'):
        result = run(env, '--install-dir', destination, flag)
        assert result.returncode == 2
        assert 'PLACE_ONLY_CONFLICTING_OPTIONS' in result.stderr
        assert not destination.exists()
    assert not log.exists()


def test_competing_creation_after_admission_is_not_overwritten(tmp_path):
    env, home, _, destination, log = environment(tmp_path)
    env['CLAIM_RACE_DIR'] = str(destination)
    before = snapshot(home)
    result = run(env, '--install-dir', destination)
    assert result.returncode != 0
    assert destination.is_dir()  # Sentry installed the competitor after admission, not early refusal.
    assert list(destination.iterdir()) == [destination / 'marker']
    assert (destination / 'marker').read_text() == 'other-install'
    assert snapshot(home) == before
    assert not log.exists()


def test_place_only_dry_run_writes_nothing_and_does_not_probe(tmp_path):
    env, home, project, destination, log = environment(tmp_path)
    before = snapshot(tmp_path)
    result = run(env, '--install-dir', destination, '--dry-run')
    assert result.returncode == 0, result.stderr
    assert 'Placement dry-run complete: no files were written.' in result.stdout
    assert snapshot(tmp_path) == before
    assert not log.exists()


@pytest.mark.parametrize('setting', ['dashboard_log', 'project_key', 'codex_models'])
@pytest.mark.parametrize('syntax', ['quote', 'backslash', 'newline', 'expression'])
def test_resolved_writer_values_reject_source_syntax_before_placement(tmp_path, setting, syntax):
    env, home, project, destination, log = environment(tmp_path)
    positive = destination.with_name('positive payload with space')
    name = {'dashboard_log': 'AGENTSTACK_DASHBOARD_LOG',
            'project_key': 'AGENTSTACK_PROJECT_KEY',
            'codex_models': 'AGENTSTACK_CODEX_MODELS'}[setting]
    good = str(positive / 'logs' / 'dashboard with space.log') if setting == 'dashboard_log' else str(project) if setting == 'project_key' else 'fixture-model'
    env[name] = good
    result = run(env, '--install-dir', positive, '--dashboard-only')
    assert result.returncode == 0, result.stderr
    manifest = json.loads((positive / 'install-state.json').read_text())
    assert manifest['env'][name] == good
    canary = tmp_path / 'outside-stage-canary'
    prefix = str(destination / 'logs' / 'dashboard.log') if setting == 'dashboard_log' else str(project / 'literal') if setting == 'project_key' else 'fixture-model'
    bad = {'quote': prefix + '"tail', 'backslash': prefix + chr(92) + 'tail',
           'newline': prefix + chr(10) + 'tail',
           'expression': prefix + '\", "fixture_injection": __import__("pathlib").Path(' + repr(str(canary)) + ').write_text("owned"), "fixture_tail": "'}[syntax]
    env[name] = bad
    before = snapshot(tmp_path)
    result = run(env, '--install-dir', destination, '--dashboard-only')
    assert result.returncode == 2
    assert 'PLACE_ONLY_VALUE_INVALID' in result.stderr
    assert not destination.exists()
    assert not canary.exists()
    assert snapshot(tmp_path) == before
    assert not log.exists()


def writer_source_sinks(source):
    import re
    result = set()
    variable = re.compile(r'\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)(?::[-+][^{}$`\n]*)?\})')
    marker = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2[ \t]*$")
    for start, end in (('write_env_file() {', '\nstop_new_agent_mail()'),
                       ('write_manifest() {', '\n# Closed placement path:')):
        lines = source[source.index(start):source.index(end)].splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            index += 1
            if line.lstrip().startswith('#') or '<<' not in line:
                continue
            declaration = marker.search(line)
            assert declaration is not None and line.count('<<') == 1, 'UNSUPPORTED_HEREDOC_DECLARATION'
            strip_tabs, quote, delimiter = declaration.groups()
            body = []
            while index < len(lines):
                line = lines[index]
                index += 1
                if (line.lstrip('\t') if strip_tabs else line) == delimiter:
                    break
                body.append(line)
            else:
                raise AssertionError('UNTERMINATED_HEREDOC')
            if quote:
                continue  # A quoted delimiter disables shell expansion.
            code = '\n'.join(body)
            position = 0
            while position < len(code):
                if code[position] == chr(96):
                    raise AssertionError('UNSUPPORTED_HEREDOC_EXPANSION')
                if code[position] == '$':
                    match = variable.match(code, position)
                    assert match is not None, 'UNSUPPORTED_HEREDOC_EXPANSION'
                    result.add(match.group(1) or match.group(2))
                    position = match.end()
                else:
                    position += 1
    return result


def assert_writer_coverage(source):
    admission = source.split('local -a literal_names=(', 1)[1].split(')', 1)[0].split()
    assert len(admission) == len(set(admission))
    assert set(admission) == writer_source_sinks(source)


def test_place_only_validates_every_source_spliced_writer_value():
    assert_writer_coverage(INSTALLER.read_text())


@pytest.mark.parametrize('mutation', ['FUTURE_SETTING', 'future_value', '${future_value}',
                                      '${_future}', 'second-heredoc', '$(command)', '${array[0]}'])
def test_writer_coverage_catches_case_and_additional_heredoc_mutations(mutation):
    source = INSTALLER.read_text()
    if mutation == 'second-heredoc':
        anchor = '\nPY\n}\n\nstop_new_agent_mail()'
        changed = source.replace(anchor, '\nPY\n  cat <<SECOND\n${future_value}\nSECOND\n}\n\nstop_new_agent_mail()', 1)
    else:
        expansion = mutation if mutation.startswith('$') else '$' + mutation
        changed = source.replace('values = {', 'values = {\n    "future": "' + expansion + '",', 1)
    assert changed != source
    with pytest.raises(AssertionError):
        assert_writer_coverage(changed)


@pytest.mark.parametrize('operator', ['-', '+'])
def test_writer_coverage_rejects_command_substitution_in_existing_name_default(operator):
    source = INSTALLER.read_text()
    expansion = '${MURMUR_SETTING:' + operator + chr(96) + 'printf marker' + chr(96) + '}'
    changed = source.replace('values = {', 'values = {\n    "future": "' + expansion + '",', 1)
    assert changed != source
    with pytest.raises(AssertionError, match='UNSUPPORTED_HEREDOC_EXPANSION'):
        assert_writer_coverage(changed)


@pytest.mark.parametrize('choice', ['default', 'command', 'resolved-quote', 'resolved-backslash'])
def test_place_only_checks_final_selected_python_before_creating_destination(tmp_path, choice):
    env, home, project, destination, log = environment(tmp_path)
    # Keep the persisted service PATH ordinary; only command lookup's PATH has
    # the unusual directory, so rejection must inspect final PYTHON_BIN.
    env['AGENTSTACK_PATH'] = '/usr/bin:/bin'
    directory = tmp_path / ('python-bin' if choice in ('default', 'command') else
                            'python"bin' if choice == 'resolved-quote' else 'python' + chr(92) + 'bin')
    directory.mkdir()
    command = 'python3' if choice == 'default' else 'fixture-python'
    selected = directory / command
    selected.symlink_to(sys.executable)
    env['PATH'] = str(directory) + ':/usr/bin:/bin'
    if choice == 'default':
        del env['AGENTSTACK_PYTHON']
    else:
        env['AGENTSTACK_PYTHON'] = command
    before_home, before_project = snapshot(home), snapshot(project)
    result = run(env, '--install-dir', destination, '--dashboard-only')
    if choice in ('default', 'command'):
        assert result.returncode == 0, result.stderr
        text = (destination / 'env.sh').read_text()
        assert str(selected) in text
        assert 'PLACE_ONLY_VALUE_INVALID' not in result.stderr
    else:
        assert result.returncode == 2
        assert 'PLACE_ONLY_VALUE_INVALID' in result.stderr
        assert not destination.exists()
    assert snapshot(home) == before_home
    assert snapshot(project) == before_project
    assert not log.exists()
