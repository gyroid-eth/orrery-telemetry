"""The real core payload supplies explicit fresh preparation, without cutover."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

from test_gemini_core_install_boundary import _run_install_payload

ROOT = Path(__file__).resolve().parents[1]
FRESH_FILES = (
    'templates/fresh/claude/CLAUDE.md', 'templates/fresh/codex/AGENTS.md',
    'scripts/codex_plugin_trust.py', 'scripts/build-codex-app-marketplace.py',
    'integrations/codex_app/plugin/hooks/hooks.json',
    'integrations/codex_app/plugin/scripts/run-hook.sh',
    'integrations/codex_app/plugin/scripts/run-reservation-guard.sh',
    'integrations/codex_app/src/agentstack_codex_app/mcp_server.py',
    'integrations/codex_app/schemas/migrations/001_delivery_state.sql',
)


@pytest.mark.parametrize('tier,dry_run', [('tier0', False), ('tier1', True)])
def test_fresh_payload_keeps_dashboard_only_and_dry_run_boundaries(tmp_path, tier, dry_run):
    install = tmp_path / 'install'
    result = _run_install_payload(install, tier=tier, dry_run=dry_run)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in FRESH_FILES:
        assert not (install / name).exists(), name
    if dry_run:assert not install.exists()


def test_fresh_runtime_modules_match_artifact_inventory():
    source = ROOT / 'packages/agentstack_mail/src/agentstack_mail'
    tree = ast.parse((ROOT / 'packages/agentstack_mail/tests/verify_artifact.py').read_text())
    declaration, = [node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'REQUIRED_RUNTIME_MODULES'
                            for target in node.targets)]
    expected = ast.literal_eval(declaration)
    assert {path.name for path in source.glob('*.py')} == expected
    assert {name for name in expected if name.startswith('fresh_') or name == 'archive_cli.py'} == {
        'archive_cli.py', 'fresh_child.py', 'fresh_cli.py', 'fresh_client.py', 'fresh_delivery.py', 'fresh_guard.py',
        'fresh_harness.py', 'fresh_targets.py',
        'fresh_launch.py', 'fresh_notifications.py', 'fresh_proxy.py', 'fresh_reservations.py',
        'fresh_runtime.py', 'fresh_server.py'}


def test_fresh_manifest_owns_exact_four_files_and_private_uninstall_keeps_neighbors(tmp_path):
    install = tmp_path / 'installed-core'
    payload = _run_install_payload(install)
    assert payload.returncode == 0, payload.stdout + payload.stderr
    for name in ('dashboard/fresh_host.py', 'bin/lib/runtime_tmux.py'):
        assert (install / name).read_bytes() == (ROOT / name).read_bytes()
    neighbors = {'templates/fresh/operator-notes.md': b'keep personal notes',
                 'scripts/operator-helper.py': b'keep personal helper'}
    for name, raw in neighbors.items():(install / name).write_bytes(raw)
    text = (ROOT / 'scripts/install.sh').read_text()
    # Reuse the actual write_manifest, including its Python here-document.
    function = 'write_manifest() {\n' + text.split('write_manifest() {\n', 1)[1].split('\nmain() {', 1)[0]
    variables = {name: '' for name in re.findall(r'\$([A-Za-z_][A-Za-z0-9_]*)', function)}
    variables.update(DRY_RUN='false', TIER='tier1', MANIFEST=str(install / 'install-state.json'),
        INSTALL_DIR=str(install), PYTHON_BIN=sys.executable, REPO_ROOT=str(ROOT), SCRIPT_DIR=str(ROOT / 'scripts'),
        AGENT_MAIL_NAME_CAPABILITY_JSON='{}', ENV_FILE=str(install / 'env.sh'),
        SAFE_MERGE_RESULT_FILE=str(install / 'no-settings-result'), MCP_MERGE_RESULT_FILE=str(install / 'no-mcp-result'),
        CLAUDE_SKILLS_DIR=str(tmp_path / 'unused-claude-skills'), CODEX_SKILLS_DIR=str(tmp_path / 'unused-codex-skills'))
    script = '\n'.join(['set -eu', 'plan() { :; }',
        *[name + '=' + shlex.quote(value) for name, value in variables.items()], function,
        'write_manifest manual "" "" ""'])
    made = subprocess.run(['/bin/bash', '-c', script], cwd=tmp_path, env=os.environ.copy(),
        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)
    assert made.returncode == 0, made.stdout + made.stderr
    state = json.loads((install / 'install-state.json').read_text())
    assert state['services'] == [] and not state['safe_merge_performed']
    owned = set(state['owned_files']);directories = set(state['owned_dirs'])
    for name in FRESH_FILES[:4]:
        assert str(install / name) in owned
        assert str((install / name).parent) in directories
    for name in neighbors:assert str(install / name) not in owned
    removed = subprocess.run(['/bin/bash', str(ROOT / 'scripts/uninstall.sh'), '--install-dir', str(install)],
        cwd=tmp_path, env=os.environ.copy(), stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=20)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    for name in FRESH_FILES[:4]:assert not (install / name).exists()
    for name, raw in neighbors.items():assert (install / name).read_bytes() == raw
    (tmp_path / 'private-uninstall.log').write_text(removed.stdout + removed.stderr)
    (tmp_path / 'manifest-ownership-result.json').write_text(json.dumps(dict(
        exact_four_owned=True, owned_parent_dirs=True, private_uninstall_four_removed=True,
        unrelated_neighbors_preserved=True, services=[], real_install_or_uninstall=False)))


def test_real_payload_to_setup_builder_and_regular_fake_claude_launch(tmp_path):
    # These are shared isolated HTTP/native fixtures, not production sources.
    sys.path.insert(0, str(ROOT / 'packages/agentstack_mail/tests'))
    from test_fresh_guard import grant, live
    from test_fresh_launch import NATIVE, invoke

    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            install = tmp_path / 'installed-core';install.mkdir(mode=0o700)
            saved = {'env.sh': b'# existing operator configuration: keep bytes\n',
                     'runtime-client.json': b'private sentinel; payload must not replace\n'}
            for name, raw in saved.items():(install / name).write_bytes(raw)
            first = await asyncio.to_thread(_run_install_payload, install)
            assert first.returncode == 0, first.stdout + first.stderr
            for name in ('dashboard/fresh_host.py', 'bin/lib/runtime_tmux.py'):
                assert (install / name).read_bytes() == (ROOT / name).read_bytes()
            for name in FRESH_FILES:
                assert (install / name).read_bytes() == (ROOT / name).read_bytes(), name
            # Update the owned payload, preserving existing operator selections.
            (install / FRESH_FILES[0]).write_bytes(b'old template')
            second = await asyncio.to_thread(_run_install_payload, install)
            assert second.returncode == 0, second.stdout + second.stderr
            for name in FRESH_FILES:
                assert (install / name).read_bytes() == (ROOT / name).read_bytes(), name
            for name, raw in saved.items():assert (install / name).read_bytes() == raw
            assert not (install / 'config.json').exists()
            assert not list(install.rglob('storage.sqlite3'))
            assert not (install / 'integrations/codex_app/install-state.json').exists()
            assert not (install / 'dashboard/provider_server.py').exists()
            home = client.path.parent / 'providers/claude'
            home.parent.mkdir(mode=0o700);home.mkdir(mode=0o700)
            env = dict(os.environ, AGENTSTACK_PYTHON=sys.executable, AGENTSTACK_HOME=str(install),
                AGENTSTACK_TEMPLATE_HOME=str(install / 'templates/fresh'),
                CLAUDE_CONFIG_DIR=str(home), CODEX_HOME=str(home), CLAUDE_HOME='',
                AGENTSTACK_CLAUDE_MD_SCOPE='global', AGENTSTACK_PROJECT_KEY='', AGENTSTACK_LEGACY_PROJECT_KEY='')
            async def command(argv):
                return await asyncio.to_thread(subprocess.run, argv, env=env, cwd=home,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
            setup = ['/bin/bash', str(install / 'bin/agentstack-claude-setup')]
            planned = await command([*setup, '--plan', '--json'])
            assert planned.returncode == 0, planned.stdout + planned.stderr
            applied = await command([*setup, '--apply', '--plan-id', json.loads(planned.stdout)['plan_id'], '--json'])
            assert applied.returncode == 0, applied.stdout + applied.stderr
            assert b'explicit fresh client' in (home / 'CLAUDE.md').read_bytes()
            trust_help = await command([sys.executable, str(install / 'scripts/codex_plugin_trust.py'), '--help'])
            assert trust_help.returncode == 0 and 'plan,apply' in trust_help.stdout
            market = tmp_path / 'marketplace-from-installed-payload'
            built = await command([sys.executable, str(install / 'scripts/build-codex-app-marketplace.py'),
                str(install / 'integrations/codex_app'), str(market), '--marketplace-name', 'orrery-fresh'])
            assert built.returncode == 0, built.stdout + built.stderr
            manifest = market / '.agents/plugins/marketplace.json'
            assert json.loads(manifest.read_text())['name'] == 'orrery-fresh'
            plugin = market / 'plugins/agentstack-codex-app'
            assert (plugin / 'schemas/migrations/001_delivery_state.sql').is_file()
            assert (plugin / 'src/agentstack_codex_app/_global-client-loader.sh').read_bytes() == (install / 'bin/lib/global-client-loader.sh').read_bytes()
            binary = tmp_path / 'fake-claude'
            binary.write_text('#!' + sys.executable + '\n' + NATIVE);binary.chmod(0o700)
            await grant(runtime, people, [tmp_path / 'note.py'])
            launched = await invoke(client, 'claude', binary, tmp_path, wrapper_root=install)
            assert launched.returncode == 0, launched.stdout + launched.stderr
            trace = json.loads((tmp_path / 'native-trace.json').read_text())
            assert trace['session_hook_rc'] == trace['guard_rc'] == trace['proxy_rc'] == 0
            assert trace['status']['mail_instance_id'] == runtime.config['instance_id']
            assert trace['status']['mail_agent_id'] == people[0]['agent_id']
            assert trace['context'] == str(client.path) and trace['home'] == str(home)
            assert not (tmp_path / 'note.py').exists()
            for name, raw in saved.items():assert (install / name).read_bytes() == raw
            (tmp_path / 'installed-fresh-result.json').write_text(json.dumps(dict(
                real_installer_payload_twice=True, owned_files_match_source=True,
                operator_env_and_context_preserved=True, installed_setup_and_builder=True,
                installed_regular_launch_same_owner_proxy_guard=True,
                manual_source_copy_used=False, service_or_cutover=False,
                payload_sha256={name:hashlib.sha256((install / name).read_bytes()).hexdigest() for name in FRESH_FILES})))
    asyncio.run(run())
