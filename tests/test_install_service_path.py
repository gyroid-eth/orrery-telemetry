"""The service PATH must keep a Node-installed `codex` with its own runtime.

A Node wrapper resolves its platform package through the first `node` on PATH.
Under the service's minimal PATH that is a different Node than the one the
wrapper was installed under, so `codex app-server` dies and the dashboard
reports no Codex usage at all.
"""

from __future__ import annotations

import pathlib
import re
import shlex
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def test_dashboard_only_payload_copies_and_updates_internal_reader(tmp_path):
    text = INSTALLER.read_text()
    body = text.split('install_payload() {', 1)[1].split('\n}\n', 1)[0]
    target = tmp_path / 'install'
    (target / 'bin').mkdir(parents=True)
    variables = {
        'TIER': 'tier0', 'DRY_RUN': 'false', 'REPO_ROOT': str(ROOT),
        'SCRIPT_DIR': str(ROOT / 'scripts'), 'INSTALL_DIR': str(target),
        'HOOKS_DIR': str(target / 'hooks'), 'DASHBOARD_DIR': str(target / 'dashboard'),
        'BIN_DIR': str(target / 'bin'),
        'MERGE_SETTINGS_SCRIPT': str(ROOT / 'scripts/lib/merge_settings.py'),
        'MERGE_CLAUDE_MCP_SCRIPT': str(ROOT / 'scripts/lib/merge_claude_mcp.py'),
        'RUNTIME_SCHEMA_SOURCE': str(ROOT / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'),
        'RUNTIME_FIXTURE_SOURCE': str(ROOT / 'packages/agentstack_mail/fixtures/global-server-s2a.json'),
    }
    script = '\n'.join(['set -e', *[key + '=' + shlex.quote(value) for key, value in variables.items()],
        'RUNTIME_CLIENT_CONTRACT_SOURCES=()', 'RUNTIME_PUBLIC_ENTRY_SOURCES=()',
        'plan() { :; }', 'copy_core_tree() { :; }',
        'install_payload() {' + body + '\n}', 'install_payload'])
    private = target / 'private-config'
    private.write_bytes(b'keep configuration bytes')
    for _ in range(2):
        done = subprocess.run(['/bin/bash', '-c', script], cwd='/', stdin=subprocess.DEVNULL,
            env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
            text=True, capture_output=True, timeout=10)
        assert done.returncode == 0, done.stderr
        assert (target / 'hooks/llm_secret_reader.py').read_bytes() == (ROOT / 'hooks/llm_secret_reader.py').read_bytes()
        assert (target / 'hooks/native_connections.py').read_bytes() == (ROOT / 'hooks/native_connections.py').read_bytes()
        assert private.read_bytes() == b'keep configuration bytes'
        if _ == 0:
            (target / 'hooks/llm_secret_reader.py').write_bytes(b'old installed reader')
            (target / 'hooks/native_connections.py').write_bytes(b'old installed connection rules')


def _codex_path_block() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r'^if \[\[ -n "\$CODEX_BIN_SETTING" \]\]; then\n.*?^fi\n',
                      text, re.DOTALL | re.MULTILINE)
    assert match, "codex PATH block missing from the installer"
    return match.group(0)


def _resolve(codex_bin: str, path_value: str = DEFAULT_PATH) -> str:
    script = "\n".join([
        "set -euo pipefail",
        f'CODEX_BIN_SETTING={codex_bin!r}',
        f'PATH_VALUE={path_value!r}',
        _codex_path_block(),
        'printf "%s" "$PATH_VALUE"',
    ])
    result = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_codex_outside_the_service_path_brings_its_directory_along(tmp_path):
    codex = tmp_path / "nodebrew" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")

    resolved = _resolve(str(codex))

    assert resolved.split(":")[0] == str(codex.parent)
    assert resolved.endswith(DEFAULT_PATH)


def test_a_codex_already_on_the_service_path_does_not_repeat_it(tmp_path):
    resolved = _resolve("/usr/local/bin/codex")

    assert resolved == DEFAULT_PATH


def test_no_codex_leaves_the_service_path_alone():
    assert _resolve("") == DEFAULT_PATH
