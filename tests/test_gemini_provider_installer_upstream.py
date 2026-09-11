"""Regression coverage for the upstream-facing optional Gemini installer."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-gemini-provider.sh"
PROVIDER_FILES = (
    "bin/agent-start-gemini",
    "bin/agentstack-gemini-bootstrap",
    "bin/agentstack-gemini-setup",
    "bin/agentstack-gemini-mcp",
    "bin/agentstack-gemini-child-mail",
    "bin/agentstack-gemini-stream",
    "hooks/spawn_gemini_child.sh",
    "hooks/spawn_gemini_preregistered.sh",
    "dashboard/provider_server.py",
    "dashboard/gemini_provider_runtime.py",
    "dashboard/assets/google.svg",
)


def _seed_fake_core(root: pathlib.Path) -> None:
    (root / "dashboard").mkdir(parents=True)
    (root / "dashboard" / "server.py").write_text(
        """
class SpawnLaunchSpec:
    pass

def _is_agent_process_name(name, program):
    return program == 'antigravity' and name == 'agy'

def _agent_process_alive(pane_pid, process_tree, program):
    return None

def _spawn_request(payload):
    return None, None

def _spawn_unavailable_error():
    return None

def spawn_with_launch_spec(payload, spec):
    return {}
""".lstrip(),
        encoding="utf-8",
    )
    # The current core service runner is the extension point that selects an
    # optional provider_server.py when one has been installed.  Model that
    # contract in the fake core so this fixture represents a current install,
    # rather than an older core that the provider installer intentionally
    # rejects during preflight.
    (root / "dashboard" / "service_runner.py").write_text(
        """
from pathlib import Path

HERE = Path(__file__).resolve().parent

def _default_server_path():
    provider_server = HERE / 'provider_server.py'
    return provider_server if provider_server.is_file() else HERE / 'server.py'
""".lstrip(),
        encoding="utf-8",
    )
    (root / "install-state.json").write_text(
        json.dumps({"owned_files": [], "owned_dirs": []}) + "\n",
        encoding="utf-8",
    )

    required_files = (
        "bin/lib/agentstack-register.sh",
        "hooks/project-context.sh",
    )
    required_executables = (
        "bin/agentstack-preregister-child",
        "hooks/cleanup-child-agent.sh",
        "integrations/codex_app/plugin/scripts/run-mcp.sh",
    )
    for relative in required_files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    for relative in required_executables:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)


def _run_installer(install_root: pathlib.Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            str(INSTALLER),
            "--install-dir",
            str(install_root),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _installer_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AGENTSTACK_PYTHON": sys.executable,
            # Keep the fixture independent of a developer's local `agy` and
            # make the fake core shell utilities explicit.
            "PATH": "/usr/bin:/bin",
        }
    )
    return env


def _write_cp_probe(
    bin_dir: pathlib.Path,
    install_root: pathlib.Path,
    manifest: pathlib.Path,
    observation: pathlib.Path,
) -> None:
    required_payload = tuple(relative for relative in PROVIDER_FILES if relative != "dashboard/provider_server.py")
    script = bin_dir / "cp"
    script.write_text(
        f"""#!{sys.executable}
import json
from pathlib import Path
import subprocess
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
if source.name == "provider_server.py" and ".gemini-provider-staging." in str(source):
    if destination.name.startswith("provider_server.py.tmp."):
        print("activation temp uses predictable name", file=sys.stderr)
        raise SystemExit(92)
    root = Path({str(install_root)!r}).resolve()
    missing = [relative for relative in {required_payload!r}
               if not (root / relative).is_file()]
    owned_files = json.loads(Path({str(manifest)!r}).read_text(encoding="utf-8"))["owned_files"]
    provider = str(root / "dashboard/provider_server.py")
    if missing or provider not in owned_files:
        print(f"activation published before ready: missing={{missing}}, owned={{provider in owned_files}}", file=sys.stderr)
        raise SystemExit(91)
    Path({str(observation)!r}).write_text("ready\\n", encoding="utf-8")

result = subprocess.run(["/bin/cp", *sys.argv[1:]], check=False)
raise SystemExit(result.returncode)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _write_failing_cp(
    bin_dir: pathlib.Path,
    source_name: str,
    source_marker: str | None = None,
) -> None:
    script = bin_dir / "cp"
    script.write_text(
        f"""#!{sys.executable}
from pathlib import Path
import subprocess
import sys

if (Path(sys.argv[1]).name == {source_name!r}
        and ({source_marker!r} is None or {source_marker!r} in sys.argv[1])):
    print("simulated payload copy failure", file=sys.stderr)
    raise SystemExit(97)
result = subprocess.run(["/bin/cp", *sys.argv[1:]], check=False)
raise SystemExit(result.returncode)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _write_failing_python(bin_dir: pathlib.Path, manifest_name: str) -> pathlib.Path:
    script = bin_dir / "python-failing-manifest"
    script.write_text(
        f"""#!{sys.executable}
from pathlib import Path
import subprocess
import sys

if len(sys.argv) > 3 and Path(sys.argv[2]).name == {manifest_name!r}:
    print("simulated manifest update failure", file=sys.stderr)
    raise SystemExit(98)
result = subprocess.run([{str(pathlib.Path(sys.executable))!r}, *sys.argv[1:]], check=False)
raise SystemExit(result.returncode)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _write_post_activation_failing_mv(
    bin_dir: pathlib.Path, provider_destination: pathlib.Path,
) -> None:
    script = bin_dir / "mv"
    script.write_text(
        f"""#!{sys.executable}
from pathlib import Path
import subprocess
import sys

destination = Path(sys.argv[-1]).resolve()
subprocess.run(["/bin/mv", *sys.argv[1:]], check=True)
if destination == Path({str(provider_destination)!r}).resolve():
    print("simulated interruption after provider activation", file=sys.stderr)
    raise SystemExit(99)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_installer_dry_run_succeeds_without_agi_and_does_not_mutate_core() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        manifest = install_root / "install-state.json"
        before = manifest.read_text(encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "AGENTSTACK_PYTHON": sys.executable,
                # Deliberately exclude user-local binary locations where agy
                # is normally installed. Core shell utilities remain present.
                "PATH": "/usr/bin:/bin",
            }
        )
        result = subprocess.run(
            [
                "/bin/bash",
                str(INSTALLER),
                "--install-dir",
                str(install_root),
                "--dry-run",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "agy is not installed/on PATH" in result.stdout
        assert "Gemini provider payload dry-run complete" in result.stdout
        assert manifest.read_text(encoding="utf-8") == before
        assert not (install_root / "bin" / "agent-start-gemini").exists()


def test_provider_entrypoint_is_published_after_payload_and_manifest_are_ready() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        bin_dir = pathlib.Path(tmp) / "fake-bin"
        bin_dir.mkdir()
        manifest = install_root / "install-state.json"
        observation = pathlib.Path(tmp) / "activation-observed"
        _write_cp_probe(bin_dir, install_root, manifest, observation)

        env = _installer_env()
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        result = _run_installer(install_root, env)

        assert result.returncode == 0, result.stderr
        assert observation.read_text(encoding="utf-8") == "ready\n"
        assert (install_root / "dashboard" / "provider_server.py").is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        expected_files = {
            str(install_root.resolve() / relative) for relative in PROVIDER_FILES
        }
        assert expected_files.issubset(set(data["owned_files"]))
        assert not list(install_root.glob(".install-state.json.*"))


def test_partial_provider_install_does_not_expose_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        bin_dir = pathlib.Path(tmp) / "fake-bin"
        bin_dir.mkdir()
        manifest = install_root / "install-state.json"
        before = manifest.read_text(encoding="utf-8")
        _write_failing_cp(bin_dir, "gemini_provider_runtime.py")

        env = _installer_env()
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        result = _run_installer(install_root, env)

        assert result.returncode == 97
        assert "simulated payload copy failure" in result.stderr
        assert "activation state: Gemini provider remains disabled" in result.stderr
        assert "recovery: rerun the installer" in result.stderr
        # The complete payload was staged privately, so no active payload is
        # published when staging fails.
        assert not (install_root / "dashboard" / "provider_server.py").exists()
        assert manifest.read_text(encoding="utf-8") == before
        assert not any(install_root.glob(".gemini-provider-staging.*"))


def test_fresh_failure_after_payload_publish_rolls_back_files_and_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        manifest = install_root / "install-state.json"
        before = manifest.read_bytes()
        bin_dir = pathlib.Path(tmp) / "fake-bin"
        bin_dir.mkdir()
        failing_python = _write_failing_python(bin_dir, "install-state.json")

        env = _installer_env()
        env["AGENTSTACK_PYTHON"] = str(failing_python)
        result = _run_installer(install_root, env)

        assert result.returncode == 98
        assert "simulated manifest update failure" in result.stderr
        assert "activation state: Gemini provider remains disabled" in result.stderr
        assert "recovery: rerun the installer" in result.stderr
        assert manifest.read_bytes() == before
        assert not any((install_root / relative).exists() for relative in PROVIDER_FILES)
        assert not any(install_root.glob(".gemini-provider-staging.*"))


def test_failed_upgrade_disables_provider_instead_of_mixing_payload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        manifest = install_root / "install-state.json"

        initial = _run_installer(install_root, _installer_env())
        assert initial.returncode == 0, initial.stderr
        provider = install_root / "dashboard" / "provider_server.py"
        assert provider.is_file()
        old_manifest = manifest.read_bytes()

        bin_dir = pathlib.Path(tmp) / "fake-bin"
        bin_dir.mkdir()
        _write_failing_cp(
            bin_dir,
            "gemini_provider_runtime.py",
            source_marker=".gemini-provider-staging.",
        )
        env = _installer_env()
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        result = _run_installer(install_root, env)

        assert result.returncode == 97
        # The old entrypoint is quarantined before active dependencies are
        # replaced; a failed upgrade must fall back to core, not run a mixed
        # old-provider/new-runtime payload.
        assert not provider.exists()
        assert manifest.read_bytes() == old_manifest
        assert "activation state: Gemini provider is disabled" in result.stderr
        assert "recovery: rerun the installer" in result.stderr
        for relative in PROVIDER_FILES:
            if relative != "dashboard/provider_server.py":
                assert (install_root / relative).is_file()
        assert not any(install_root.glob(".gemini-provider-staging.*"))


def test_failed_upgrade_after_manifest_update_restores_ownership_without_restoring_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        manifest = install_root / "install-state.json"

        initial = _run_installer(install_root, _installer_env())
        assert initial.returncode == 0, initial.stderr
        old_manifest = manifest.read_bytes()
        bin_dir = pathlib.Path(tmp) / "fake-bin"
        bin_dir.mkdir()
        _write_failing_cp(
            bin_dir,
            "provider_server.py",
            source_marker=".gemini-provider-staging.",
        )
        env = _installer_env()
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"

        result = _run_installer(install_root, env)

        assert result.returncode == 97
        assert "activation state: Gemini provider is disabled" in result.stderr
        assert "recovery: rerun the installer" in result.stderr
        assert not (install_root / "dashboard" / "provider_server.py").exists()
        assert manifest.read_bytes() == old_manifest
        data = json.loads(manifest.read_text(encoding="utf-8"))
        expected_files = {
            str(install_root.resolve() / relative) for relative in PROVIDER_FILES
        }
        assert expected_files == expected_files.intersection(set(data["owned_files"]))
        for relative in PROVIDER_FILES:
            if relative != "dashboard/provider_server.py":
                assert (install_root / relative).is_file()
        assert not any(install_root.glob(".gemini-provider-staging.*"))


def test_post_activation_interruption_reports_actual_active_provider_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        initial = _run_installer(install_root, _installer_env())
        assert initial.returncode == 0, initial.stderr

        bin_dir = pathlib.Path(tmp) / "fake-bin"
        bin_dir.mkdir()
        provider_destination = install_root / "dashboard" / "provider_server.py"
        _write_post_activation_failing_mv(bin_dir, provider_destination)
        env = _installer_env()
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"

        result = _run_installer(install_root, env)

        assert result.returncode == 99
        assert "simulated interruption after provider activation" in result.stderr
        assert "activation state: Gemini provider is active" in result.stderr
        assert "recovery: rerun the installer" in result.stderr
        assert provider_destination.is_file()
        assert not any(install_root.glob(".gemini-provider-staging.*"))


def test_missing_launch_contract_fails_before_provider_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        server = install_root / "dashboard" / "server.py"
        server.write_text(
            """def _is_agent_process_name(name, program):
    return program == 'antigravity' and name == 'agy'

def _agent_process_alive(pane_pid, process_tree, program):
    return None
""",
            encoding="utf-8",
        )
        manifest = install_root / "install-state.json"
        before = manifest.read_bytes()

        result = _run_installer(install_root, _installer_env())

        assert result.returncode != 0
        assert "missing runtime launch contract helper(s)" in result.stderr
        assert "SpawnLaunchSpec" in result.stderr
        assert "activation state: Gemini provider remains disabled" in result.stderr
        assert "recovery: rerun the installer" in result.stderr
        assert manifest.read_bytes() == before
        assert not any(install_root.glob(".gemini-provider-staging.*"))
        assert not any(
            (install_root / relative).exists() for relative in PROVIDER_FILES
        )


def test_optional_installer_never_replaces_core_dashboard() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    files_block = text.split("FILES=(", 1)[1].split(")", 1)[0]
    assert "dashboard/server.py" not in files_block
    assert "dashboard/service_runner.py" not in files_block
    assert "dashboard/provider_runtime.py" not in files_block
    assert "dashboard/provider_classification.py" not in files_block


def test_optional_installer_is_executable() -> None:
    assert INSTALLER.stat().st_mode & 0o100
