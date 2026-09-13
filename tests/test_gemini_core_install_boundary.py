"""The core installer must neither ship, activate, nor overwrite the optional
Gemini provider.

The provider's dashboard runtime and its launcher hooks are one contract that
only ``install-gemini-provider.sh`` installs.  These tests drive the real
``install_payload`` function from ``scripts/install.sh`` against a temporary
install directory, including over a payload laid down by the real provider
installer.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import dashboard.service_runner as service_runner


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"
PROVIDER_INSTALLER = ROOT / "scripts" / "install-gemini-provider.sh"
PROVIDER_PAYLOAD = (
    "dashboard/provider_server.py",
    "dashboard/gemini_provider_runtime.py",
    "hooks/spawn_gemini_child.sh",
    "hooks/spawn_gemini_preregistered.sh",
)
# Static provider files that are not part of the runtime<->adapter contract.
PROVIDER_STATIC_ASSETS = ("dashboard/assets/google.svg",)


def _installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.DOTALL | re.MULTILINE)
    assert match, f"{name} missing from scripts/install.sh"
    return match.group(0)


def _payload_declaration(text: str) -> str:
    match = re.search(r"^OPTIONAL_PROVIDER_PAYLOAD=\(\n.*?^\)\n", text, re.DOTALL | re.MULTILINE)
    assert match, "OPTIONAL_PROVIDER_PAYLOAD declaration missing"
    return match.group(0)


def _provider_installer_files() -> list[str]:
    text = PROVIDER_INSTALLER.read_text(encoding="utf-8")
    match = re.search(r"^FILES=\(\n(.*?)^\)\n", text, re.DOTALL | re.MULTILINE)
    assert match, "provider installer FILES declaration missing"
    return re.findall(r'^  "([^"]+)"$', match.group(1), re.MULTILINE)


def _run_install_payload(install_dir: pathlib.Path, *, tier: str = "tier1",
                         dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    """Run scripts/install.sh's own install_payload into ``install_dir``."""
    text = _installer_text()
    functions = "\n".join([_payload_declaration(text)] + [
        _function(text, name) for name in (
            "copy_tree", "is_optional_provider_payload", "copy_core_tree",
            "install_child_mcp_proxy", "install_payload")
    ])
    script = "\n".join([
        "set -euo pipefail",
        'plan() { printf "%s\\n" "$*"; }',
        'warn() { printf "warning: %s\\n" "$*" >&2; }',
        'REPO_ROOT="$1"; INSTALL_DIR="$2"; TIER="$3"; DRY_RUN="$4"; PYTHON_BIN="$5"',
        'SCRIPT_DIR="$REPO_ROOT/scripts"',
        'MERGE_SETTINGS_SCRIPT="$SCRIPT_DIR/lib/merge_settings.py"',
        'MERGE_CLAUDE_MCP_SCRIPT="$SCRIPT_DIR/lib/merge_claude_mcp.py"',
        'HOOKS_DIR="$INSTALL_DIR/hooks"; SKILLS_DIR="$INSTALL_DIR/skills"',
        'DASHBOARD_DIR="$INSTALL_DIR/dashboard"; BIN_DIR="$INSTALL_DIR/bin"',
        functions,
        # The installer creates these directories before install_payload.
        'if [[ "$DRY_RUN" != true ]]; then mkdir -p "$HOOKS_DIR" "$SKILLS_DIR" "$DASHBOARD_DIR" "$BIN_DIR"; fi',
        "install_payload",
    ])
    return subprocess.run(
        ["/bin/bash", "-c", script, "install-payload", str(ROOT), str(install_dir),
         tier, "true" if dry_run else "false", sys.executable],
        text=True, capture_output=True, check=False,
    )


def test_core_exclusions_cover_every_provider_contract_file_it_would_copy():
    provider_files = _provider_installer_files()
    declared = re.findall(r'^  "([^"]+)"$', _payload_declaration(_installer_text()), re.MULTILINE)
    assert declared == list(PROVIDER_PAYLOAD)
    assert set(declared) <= set(provider_files)

    install_payload = _function(_installer_text(), "install_payload")
    for relative in provider_files:
        top = relative.split("/", 1)[0]
        if relative in PROVIDER_STATIC_ASSETS:
            continue
        if top in ("hooks", "dashboard"):
            # Tree-copied by the core: must be excluded by name.
            assert relative in declared, relative
        else:
            # Copied by the core only when named explicitly.
            assert relative not in install_payload, relative


def test_install_payload_routes_provider_bearing_trees_through_the_exclusion():
    body = _function(_installer_text(), "install_payload")
    assert 'copy_core_tree "$REPO_ROOT/hooks" "$HOOKS_DIR" "hooks"' in body
    assert 'copy_core_tree "$REPO_ROOT/dashboard" "$DASHBOARD_DIR" "dashboard"' in body
    assert 'copy_tree "$REPO_ROOT/hooks"' not in body
    assert 'copy_tree "$REPO_ROOT/dashboard"' not in body


def test_fresh_core_install_omits_provider_payload_and_runs_core_server(tmp_path, monkeypatch):
    install_dir = tmp_path / "agentstack"
    result = _run_install_payload(install_dir)
    assert result.returncode == 0, result.stderr

    for relative in PROVIDER_PAYLOAD:
        assert (ROOT / relative).is_file(), relative
        assert not (install_dir / relative).exists(), relative
    for relative in ("dashboard/server.py", "dashboard/service_runner.py",
                     "hooks/spawn_child.sh", "hooks/cleanup-child-agent.sh",
                     "hooks/project-context.sh"):
        assert (install_dir / relative).read_bytes() == (ROOT / relative).read_bytes(), relative
    assert (install_dir / "dashboard" / "providers" / "codex_app.py").is_file()

    monkeypatch.setattr(service_runner, "HERE", install_dir / "dashboard")
    assert service_runner._default_server_path() == install_dir / "dashboard" / "server.py"


def test_core_reinstall_keeps_an_installed_provider_contract_intact(tmp_path):
    install_dir = tmp_path / "agentstack"
    first = _run_install_payload(install_dir)
    assert first.returncode == 0, first.stderr
    (install_dir / "install-state.json").write_text(
        json.dumps({"owned_files": [], "owned_dirs": []}) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env.update({"AGENTSTACK_PYTHON": sys.executable, "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path / "home")})
    provider = subprocess.run(
        ["/bin/bash", str(PROVIDER_INSTALLER), "--install-dir", str(install_dir)],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    assert provider.returncode == 0, provider.stderr
    for relative in PROVIDER_PAYLOAD:
        assert (install_dir / relative).read_bytes() == (ROOT / relative).read_bytes(), relative

    # Stand in for a provider installed from a different release than the
    # core checkout that is now being reinstalled: any byte the core wrote
    # into these files would pair one release's runtime with another's hooks.
    installed = {}
    for relative in PROVIDER_PAYLOAD:
        path = install_dir / relative
        mode = path.stat().st_mode
        path.write_text(f"# provider release N: {relative}\n", encoding="utf-8")
        path.chmod(mode)
        installed[relative] = (path.read_bytes(), mode)
    stale_core_hook = install_dir / "hooks" / "spawn_child.sh"
    stale_core_hook.write_text("# old core\n", encoding="utf-8")

    again = _run_install_payload(install_dir)
    assert again.returncode == 0, again.stderr
    for relative, (content, mode) in installed.items():
        path = install_dir / relative
        assert path.read_bytes() == content, relative
        assert path.stat().st_mode == mode, relative
    assert stale_core_hook.read_bytes() == (ROOT / "hooks" / "spawn_child.sh").read_bytes()
    assert service_runner_path(install_dir).name == "provider_server.py"


def service_runner_path(install_dir: pathlib.Path) -> pathlib.Path:
    original = service_runner.HERE
    try:
        service_runner.HERE = install_dir / "dashboard"
        return service_runner._default_server_path()
    finally:
        service_runner.HERE = original


def test_dashboard_only_reinstall_keeps_provider_dashboard_files(tmp_path):
    install_dir = tmp_path / "agentstack"
    provider_server = install_dir / "dashboard" / "provider_server.py"
    provider_server.parent.mkdir(parents=True)
    provider_server.write_text("# provider-owned\n", encoding="utf-8")

    result = _run_install_payload(install_dir, tier="tier0")
    assert result.returncode == 0, result.stderr
    assert provider_server.read_text(encoding="utf-8") == "# provider-owned\n"
    assert not (install_dir / "dashboard" / "gemini_provider_runtime.py").exists()
    assert not (install_dir / "hooks" / "spawn_gemini_preregistered.sh").exists()


def test_dry_run_plans_exclusions_without_copying(tmp_path):
    install_dir = tmp_path / "agentstack"
    result = _run_install_payload(install_dir, dry_run=True)
    assert result.returncode == 0, result.stderr
    assert re.search(r"copy \S+/hooks -> \S+/hooks \(core files; optional provider payload excluded\)",
                     result.stdout), result.stdout
    assert re.search(r"copy \S+/dashboard -> \S+/dashboard \(core files; optional provider payload excluded\)",
                     result.stdout), result.stdout
    assert not install_dir.exists()
