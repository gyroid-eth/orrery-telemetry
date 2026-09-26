from __future__ import annotations

import json
import os
import plistlib
import pytest
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-codex-app-integration.sh"
EXPORTER = ROOT / "scripts" / "export-component.sh"
MARKETPLACE_BUILDER = ROOT / "scripts" / "build-codex-app-marketplace.py"
PLUGIN_ID = "agentstack-codex-app@agentstack-local"
PROXY_TOOLS = (
    "bootstrap",
    "fetch_inbox",
    "send_message",
    "acknowledge_message",
    "reserve_files",
    "renew_reservations",
    "release_reservations",
    "runtime_status",
)


# Unix socket paths are capped near 104 bytes, so these tests need a *short*
# temp directory. macOS puts the real one at /private/tmp; Linux has no such
# path at all, and pointing at a directory that does not exist made every one
# of these fail there with FileNotFoundError. None means "use the platform
# default", which on Linux is /tmp and is already short.
SHORT_TMP_DIR = "/private/tmp" if os.path.isdir("/private/tmp") else None

def _environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["CODEX_HOME"] = str(home / ".codex")
    environment.pop("AGENTSTACK_CODEX_APP_INSTALL_DIR", None)
    environment.pop("AGENTSTACK_CODEX_APP_RUNTIME_DIR", None)
    return environment


# Some of these tests only need a path to hand the installer; others drive the
# real `codex plugin` registry and mean nothing without it. Keep the two apart:
# stub the first group so it runs everywhere, and skip the second with a reason
# that names what is missing. A test that quietly asserts nothing is worse than
# one that says it did not run.
HAVE_CODEX_CLI = shutil.which("codex") is not None
needs_codex_cli = pytest.mark.skipif(
    not HAVE_CODEX_CLI,
    reason="requires the Codex CLI on PATH (it drives `codex plugin`)",
)


def _stub_codex(home: Path) -> str:
    """A stand-in `codex` for machines that do not have the CLI installed.

    These tests only need a path to hand the installer, which records it and
    (in these dry runs) asks it for a version at most. Requiring the real CLI
    made five of them fail on every CI run with `assert None is not None` —
    a machine-shaped requirement stated as an assertion about nothing.
    """
    stub = home / "stub-bin" / "codex"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/bin/sh\necho 'codex-cli 0.0.0-stub'\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)


def _install_args(
    home: Path,
    *,
    runtime_dir: Path | None = None,
    agent_mail_url: str = "http://127.0.0.1:18765/mcp",
    service: bool = False,
) -> list[str]:
    codex_binary = shutil.which("codex") or _stub_codex(home)
    args = [
        str(INSTALLER),
        "--project-key",
        str(home / "project"),
        "--agent-mail-url",
        agent_mail_url,
        "--agent-mail-env",
        str(home / ".agentstack" / "mail" / ".env"),
        "--signals-dir",
        str(home / ".agentstack" / "mail" / "signals"),
        "--codex-bin",
        codex_binary,
    ]
    if not service:
        args.insert(1, "--no-service")
    if runtime_dir is not None:
        args.extend(["--runtime-dir", str(runtime_dir)])
    return args


def _prepare_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / "project").mkdir()
    mail_env = home / ".agentstack" / "mail" / ".env"
    mail_env.parent.mkdir(parents=True)
    mail_env.write_text(
        "HTTP_BEARER_" + "TOKEN=example-secret-value\n",
        encoding="utf-8",
    )
    return home


def _plugin_list(home: Path) -> dict:
    result = subprocess.run(
        ["codex", "plugin", "list", "--json"],
        env=_environment(home),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _without_cli_recorder(plugin_root: Path) -> None:
    """Turn the current fixture into the pre-recorder bundle without changing metadata."""

    runner = plugin_root / "scripts" / "run-hook.sh"
    text = runner.read_text(encoding="utf-8")
    start = text.index("# CLI history binding is deliberately separate")
    end = text.index("printf '%s' \"$payload\" | exec", start)
    runner.write_text(text[:start] + text[end:], encoding="utf-8")
    (plugin_root / "scripts" / "record-codex-session-index.py").unlink()


def _seed_old_local_plugin(home: Path, install_dir: Path) -> Path:
    """Install a same-version bundle whose hook runner predates the recorder."""

    shutil.copytree(ROOT / "integrations" / "codex_app", install_dir)
    _without_cli_recorder(install_dir / "plugin")
    marketplace = install_dir / "marketplace"
    subprocess.run(
        [
            sys.executable,
            str(MARKETPLACE_BUILDER),
            str(install_dir),
            str(marketplace),
            "--marketplace-name",
            "agentstack-local",
        ],
        check=True,
    )
    environment = _environment(home)
    subprocess.run(
        ["codex", "plugin", "marketplace", "add", str(marketplace), "--json"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    installed = subprocess.run(
        ["codex", "plugin", "add", PLUGIN_ID, "--json"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(json.loads(installed.stdout)["installedPath"])


def _deploy_current_plugin_payload(install_dir: Path) -> None:
    for name in ("plugin", "src"):
        shutil.copytree(
            ROOT / "integrations" / "codex_app" / name,
            install_dir / name,
            dirs_exist_ok=True,
        )


def _refresh_args(home: Path, install_dir: Path, codex_binary: str) -> list[str]:
    return [
        str(INSTALLER),
        "--refresh-plugin-only",
        "--install-dir",
        str(install_dir),
        "--marketplace-name",
        "agentstack-local",
        "--python-bin",
        sys.executable,
        "--codex-bin",
        codex_binary,
    ]


def _fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    command = tmp_path / "fake-bin" / "codex"
    command.parent.mkdir(parents=True)
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "log = pathlib.Path(os.environ['AGENTSTACK_TEST_CODEX_LOG'])\n"
        "with log.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['plugin', 'list', '--json']:\n"
        "    print(os.environ['AGENTSTACK_TEST_PLUGIN_LIST'])\n"
        "    raise SystemExit(0)\n"
        "if len(sys.argv) >= 4 and sys.argv[1:4] == ['plugin', 'marketplace', 'add']:\n"
        "    print('{}')\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:] == ['plugin', 'add', 'agentstack-codex-app@agentstack-local', '--json']:\n"
        "    print(os.environ['AGENTSTACK_TEST_PLUGIN_ADD'])\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    command.chmod(0o755)
    return command, tmp_path / "codex.log"


def _read_generated_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("export "):
            continue
        assignment = shlex.split(line)[1]
        key, separator, value = assignment.partition("=")
        assert separator == "="
        values[key] = value
    return values


def test_installer_guides_hook_review_after_plugin_install(tmp_path):
    home = _prepare_home(tmp_path)
    codex_binary, codex_log = _fake_codex(tmp_path)
    environment = _environment(home)
    environment.update(
        {
            "AGENTSTACK_TEST_CODEX_LOG": str(codex_log),
            "AGENTSTACK_TEST_PLUGIN_LIST": json.dumps({"installed": []}),
            "AGENTSTACK_TEST_PLUGIN_ADD": "{}",
        }
    )
    args = _install_args(home)
    args[args.index("--codex-bin") + 1] = str(codex_binary)

    result = subprocess.run(
        args,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "open /hooks" in result.stdout
    assert "review/approve the AgentStack lifecycle hooks" in result.stdout
    assert "start a new Codex process" in result.stdout


@needs_codex_cli
def test_installer_dry_run_does_not_write_clean_home(tmp_path):
    home = _prepare_home(tmp_path)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    result = subprocess.run(
        [*_install_args(home), "--dry-run"],
        env=_environment(home),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Dry-run complete: no files were written." in result.stdout
    assert not install_dir.exists()
    assert _plugin_list(home)["installed"] == []


@needs_codex_cli
def test_refresh_plugin_only_replaces_same_version_cache_without_bridge_side_effects(
    tmp_path: Path,
) -> None:
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    cached = _seed_old_local_plugin(home, install_dir)
    marketplace_plugin = install_dir / "marketplace" / "plugins" / "agentstack-codex-app"
    current_plugin = ROOT / "integrations" / "codex_app" / "plugin"

    old_manifest = json.loads(
        (cached / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    current_manifest = json.loads(
        (current_plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert old_manifest["version"] == current_manifest["version"] == "0.1.0"
    assert (cached / "hooks" / "hooks.json").read_bytes() == (
        current_plugin / "hooks" / "hooks.json"
    ).read_bytes()
    assert (cached / "scripts" / "run-hook.sh").read_bytes() != (
        current_plugin / "scripts" / "run-hook.sh"
    ).read_bytes()
    assert not (cached / "scripts" / "record-codex-session-index.py").exists()

    sentinels = {
        install_dir / "env.sh": b"sentinel env\n",
        install_dir / "install-state.json": b"sentinel manifest\n",
        install_dir / "launchd" / "sentinel.plist": b"sentinel plist\n",
    }
    for path, value in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    _deploy_current_plugin_payload(install_dir)
    assert (install_dir / "plugin" / "scripts" / "run-hook.sh").read_bytes() == (
        current_plugin / "scripts" / "run-hook.sh"
    ).read_bytes()
    assert (marketplace_plugin / "scripts" / "run-hook.sh").read_bytes() != (
        current_plugin / "scripts" / "run-hook.sh"
    ).read_bytes()

    config = home / ".codex" / "config.toml"
    before_dry_run = {
        "config": config.read_bytes(),
        "marketplace": (marketplace_plugin / "scripts" / "run-hook.sh").read_bytes(),
        "cache": (cached / "scripts" / "run-hook.sh").read_bytes(),
        **{str(path): path.read_bytes() for path in sentinels},
    }
    dry_run = subprocess.run(
        [*_refresh_args(home, install_dir, shutil.which("codex") or "codex"), "--dry-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert "plugin snapshot, config, cache, and Bridge state were not changed" in dry_run.stdout
    assert config.read_bytes() == before_dry_run["config"]
    assert (marketplace_plugin / "scripts" / "run-hook.sh").read_bytes() == before_dry_run[
        "marketplace"
    ]
    assert (cached / "scripts" / "run-hook.sh").read_bytes() == before_dry_run["cache"]
    for path in sentinels:
        assert path.read_bytes() == before_dry_run[str(path)]

    refreshed = subprocess.run(
        _refresh_args(home, install_dir, shutil.which("codex") or "codex"),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refreshed.returncode == 0, refreshed.stderr
    assert f"Plugin refresh complete: {PLUGIN_ID} -> {cached}" in refreshed.stdout
    assert "open /hooks" in refreshed.stdout
    assert "review/approve the AgentStack lifecycle hooks" in refreshed.stdout
    assert "start a new Codex process" in refreshed.stdout
    assert (cached / "scripts" / "run-hook.sh").read_bytes() == (
        current_plugin / "scripts" / "run-hook.sh"
    ).read_bytes()
    cached_recorder = cached / "scripts" / "record-codex-session-index.py"
    assert cached_recorder.read_bytes() == (
        current_plugin / "scripts" / "record-codex-session-index.py"
    ).read_bytes()
    assert cached_recorder.stat().st_mode & 0o111
    for path, value in sentinels.items():
        assert path.read_bytes() == value


@pytest.mark.parametrize(
    ("installed", "enabled", "reason"),
    [(False, False, "not installed"), (True, False, "disabled")],
)
def test_refresh_plugin_only_skips_absent_or_disabled_without_writes(
    tmp_path: Path, installed: bool, enabled: bool, reason: str
) -> None:
    home = _prepare_home(tmp_path)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    install_dir.mkdir(parents=True)
    sentinel = install_dir / "install-state.json"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    codex_binary, codex_log = _fake_codex(tmp_path)
    registry = {"installed": []}
    if installed:
        registry["installed"].append(
            {
                "pluginId": PLUGIN_ID,
                "installed": True,
                "enabled": enabled,
            }
        )
    environment = _environment(home)
    environment.update(
        {
            "AGENTSTACK_TEST_CODEX_LOG": str(codex_log),
            "AGENTSTACK_TEST_PLUGIN_LIST": json.dumps(registry),
            "AGENTSTACK_TEST_PLUGIN_ADD": "{}",
        }
    )

    result = subprocess.run(
        _refresh_args(home, install_dir, str(codex_binary)),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"Plugin refresh skipped: {PLUGIN_ID} is {reason}." in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert codex_log.read_text(encoding="utf-8").splitlines() == ["plugin list --json"]
    assert not (install_dir / "marketplace").exists()


def test_refresh_plugin_only_rejects_a_different_local_marketplace_root(
    tmp_path: Path,
) -> None:
    home = _prepare_home(tmp_path)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    expected_root = install_dir / "marketplace"
    wrong_root = tmp_path / "other-marketplace"
    expected_root.mkdir(parents=True)
    wrong_root.mkdir()
    codex_binary, codex_log = _fake_codex(tmp_path)
    registry = {
        "installed": [
            {
                "pluginId": PLUGIN_ID,
                "installed": True,
                "enabled": True,
                "marketplaceSource": {"sourceType": "local", "source": str(wrong_root)},
                "source": {
                    "source": "local",
                    "path": str(wrong_root / "plugins" / "agentstack-codex-app"),
                },
            }
        ]
    }
    environment = _environment(home)
    environment.update(
        {
            "AGENTSTACK_TEST_CODEX_LOG": str(codex_log),
            "AGENTSTACK_TEST_PLUGIN_LIST": json.dumps(registry),
            "AGENTSTACK_TEST_PLUGIN_ADD": "{}",
        }
    )

    result = subprocess.run(
        _refresh_args(home, install_dir, str(codex_binary)),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "configured marketplace root" in result.stderr
    assert codex_log.read_text(encoding="utf-8").splitlines() == ["plugin list --json"]


def test_refresh_plugin_only_rejects_a_cache_payload_mismatch(tmp_path: Path) -> None:
    home = _prepare_home(tmp_path)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    shutil.copytree(ROOT / "integrations" / "codex_app", install_dir)
    marketplace_root = install_dir / "marketplace"
    subprocess.run(
        [
            sys.executable,
            str(MARKETPLACE_BUILDER),
            str(install_dir),
            str(marketplace_root),
            "--marketplace-name",
            "agentstack-local",
        ],
        check=True,
    )
    marketplace_plugin = marketplace_root / "plugins" / "agentstack-codex-app"
    cached = tmp_path / "stale-cache"
    shutil.copytree(marketplace_plugin, cached)
    stale_runner = cached / "scripts" / "run-hook.sh"
    stale_runner.write_text("#!/bin/sh\n# stale fixture\n", encoding="utf-8")
    stale_runner.chmod(0o755)
    version = json.loads(
        (marketplace_plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]
    codex_binary, codex_log = _fake_codex(tmp_path)
    registry = {
        "installed": [
            {
                "pluginId": PLUGIN_ID,
                "installed": True,
                "enabled": True,
                "marketplaceSource": {
                    "sourceType": "local",
                    "source": str(marketplace_root),
                },
                "source": {"source": "local", "path": str(marketplace_plugin)},
            }
        ]
    }
    add_result = {
        "pluginId": PLUGIN_ID,
        "name": "agentstack-codex-app",
        "marketplaceName": "agentstack-local",
        "version": version,
        "installedPath": str(cached),
    }
    environment = _environment(home)
    environment.update(
        {
            "AGENTSTACK_TEST_CODEX_LOG": str(codex_log),
            "AGENTSTACK_TEST_PLUGIN_LIST": json.dumps(registry),
            "AGENTSTACK_TEST_PLUGIN_ADD": json.dumps(add_result),
        }
    )

    result = subprocess.run(
        _refresh_args(home, install_dir, str(codex_binary)),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "selected scripts/run-hook.sh does not match" in result.stderr
    assert codex_log.read_text(encoding="utf-8").splitlines() == [
        "plugin list --json",
        f"plugin add {PLUGIN_ID} --json",
    ]


def test_installer_skip_git_check_is_explicit_and_persisted(tmp_path):
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    subprocess.run(
        [*_install_args(home), "--no-plugin", "--skip-git-check"],
        env=environment,
        check=True,
    )

    generated_env = _read_generated_env(install_dir / "env.sh")
    assert generated_env["AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK"] == "1"

    subprocess.run(
        [
            str(install_dir / "bin" / "uninstall-codex-app-integration"),
            "--purge-data",
        ],
        env=environment,
        check=True,
    )


def test_launchd_bootstrap_failure_uses_supervised_background(tmp_path):
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    runtime_dir = Path(tempfile.mkdtemp(prefix="cas-codex-app-", dir=SHORT_TMP_DIR))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    for name, body in {
        "uname": "#!/bin/sh\necho Darwin\n",
        "launchctl": """#!/bin/sh
echo "$*" >> "$AGENTSTACK_TEST_LAUNCHCTL_LOG"
case "$1" in
  bootstrap)
    echo "Bootstrap failed: 125: Domain does not support specified action" >&2
    exit 125
    ;;
  print)
    exit 1
    ;;
esac
exit 0
""",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    environment.update({
        "PATH": f"{fake_bin}:{environment['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_CODEX_APP_RESTART_DELAY": "0",
        "AGENTSTACK_TEST_LAUNCHCTL_LOG": str(launchctl_log),
    })

    result = subprocess.run(
        [
            *_install_args(
                home,
                runtime_dir=runtime_dir,
                service=True,
            ),
            "--no-plugin",
        ],
        env=environment,
        text=True,
        capture_output=True,
    )

    try:
        assert result.returncode == 0, result.stderr
        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        pidfile = Path(manifest["service"]["pidfile"])
        supervisor_pid = int(pidfile.read_text(encoding="utf-8").strip())
        os.kill(supervisor_pid, 0)
        assert manifest["service"]["kind"] == "nohup"
        assert manifest["launchd"]["enabled"] is False
        live_plist = (
            home
            / "Library"
            / "LaunchAgents"
            / f'{manifest["launchd"]["label"]}.plist'
        )
        assert not live_plist.exists()
        assert "falling back to supervised background" in result.stderr
        assert "Service mode: supervised background" in result.stdout

        socket_path = runtime_dir / "bridge.sock"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not socket_path.exists():
            time.sleep(0.05)
        assert socket_path.is_socket()

        child_pidfile = runtime_dir / "bridge-child.pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pidfile.exists():
            time.sleep(0.05)
        first_child_pid = int(child_pidfile.read_text(encoding="utf-8").strip())
        os.kill(first_child_pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        restarted_child_pid = first_child_pid
        while time.monotonic() < deadline:
            try:
                restarted_child_pid = int(
                    child_pidfile.read_text(encoding="utf-8").strip()
                )
            except (FileNotFoundError, ValueError):
                pass
            if restarted_child_pid != first_child_pid and socket_path.is_socket():
                break
            time.sleep(0.05)
        assert restarted_child_pid != first_child_pid
        os.kill(supervisor_pid, 0)

        doctor = subprocess.run(
            [str(install_dir / "bin" / "doctor-codex-app-integration")],
            env=environment,
            text=True,
            capture_output=True,
        )
        assert doctor.returncode == 0, doctor.stderr
        assert "Bridge service mode supervised-background" in doctor.stdout

        subprocess.run(
            [
                str(install_dir / "bin" / "uninstall-codex-app-integration"),
                "--purge-data",
            ],
            env=environment,
            check=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(supervisor_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("background Bridge supervisor survived uninstall")
    finally:
        manifest_path = install_dir / "install-state.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pidfile_value = manifest.get("service", {}).get("pidfile")
            if pidfile_value:
                try:
                    os.kill(int(Path(pidfile_value).read_text().strip()), signal.SIGTERM)
                except (FileNotFoundError, ProcessLookupError, ValueError):
                    pass


@needs_codex_cli
def test_clean_home_install_uninstall_reinstall(tmp_path):
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    runtime_dir = Path(tempfile.mkdtemp(prefix="cas-codex-app-", dir=SHORT_TMP_DIR))

    subprocess.run(
        _install_args(home, runtime_dir=runtime_dir),
        env=environment,
        check=True,
    )

    env_file = install_dir / "env.sh"
    assert env_file.is_file()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "example-secret-value" not in env_file.read_text(encoding="utf-8")
    generated_env = _read_generated_env(env_file)
    codex_binary = generated_env["AGENTSTACK_CODEX_BINARY"]
    assert Path(codex_binary).is_absolute()
    resolved = shutil.which("codex")
    if resolved is not None:
        assert Path(codex_binary).samefile(resolved)
    else:
        assert Path(codex_binary).name == "codex" and Path(codex_binary).exists()
    assert generated_env["AGENTSTACK_CODEX_APP_PLUGIN_ID"] == (
        "agentstack-codex-app@agentstack-local"
    )
    assert generated_env["AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK"] == "0"
    assert generated_env["AGENTSTACK_CODEX_APP_STALE_AFTER_SECONDS"] == "3600"
    assert generated_env["AGENTSTACK_CODEX_APP_RETRY_MAX_ATTEMPTS"] == "12"
    assert generated_env["AGENTSTACK_CODEX_APP_RETRY_MAX_AGE_SECONDS"] == "3600"
    assert generated_env["AGENTSTACK_CODEX_APP_RETRY_MAX_BACKOFF_SECONDS"] == "300"
    manifest = json.loads(
        (install_dir / "install-state.json").read_text(encoding="utf-8")
    )
    assert manifest["plugin"]["id"] == "agentstack-codex-app@agentstack-local"
    with (install_dir / "launchd" / "org.agentstack.codex-app-bridge.plist").open(
        "rb"
    ) as handle:
        plist = plistlib.load(handle)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        str(install_dir / "bin" / "run-bridge"),
    ]
    assert plist["StandardOutPath"] == str(runtime_dir / "bridge.stdout.log")
    assert plist["StandardErrorPath"] == str(runtime_dir / "bridge.stderr.log")

    installed = _plugin_list(home)["installed"]
    assert [item["pluginId"] for item in installed] == [
        "agentstack-codex-app@agentstack-local"
    ]
    proxy_script = install_dir / "plugin" / "scripts" / "run-mcp.sh"
    approval_args = [
        "-c",
        (
            "plugins.agentstack-codex-app@agentstack-local."
            "mcp_servers.agentstack.enabled=false"
        ),
        "-c",
        'mcp_servers.agentstack.command="/bin/bash"',
        "-c",
        (
            "mcp_servers.agentstack.args="
            f"[{json.dumps(str(proxy_script))}]"
        ),
    ]
    for tool_name in PROXY_TOOLS:
        approval_args.extend(
            [
                "-c",
                (
                    f"mcp_servers.agentstack.tools.{tool_name}."
                    'approval_mode="approve"'
                ),
            ]
        )
    strict_config = subprocess.run(
        [
            codex_binary,
            "exec",
            "resume",
            "--strict-config",
            *approval_args,
            "--skip-git-repo-check",
            "00000000-0000-0000-0000-000000000000",
            "configuration probe",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert strict_config.returncode != 0
    assert "no rollout found" in strict_config.stderr
    assert "configuration" not in strict_config.stderr.lower()
    cached = (
        home
        / ".codex"
        / "plugins"
        / "cache"
        / "agentstack-local"
        / "agentstack-codex-app"
        / "0.1.0"
    )
    assert (cached / "src" / "agentstack_codex_app" / "mcp_server.py").is_file()
    assert (cached / "schemas" / "migrations" / "001_delivery_state.sql").is_file()
    assert (cached / "scripts" / "record-codex-session-index.py").is_file()
    assert (cached / "scripts" / "run-mcp.sh").is_file()
    mcp = subprocess.run(
        [str(cached / "scripts" / "run-mcp.sh")],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        + "\n",
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(mcp.stdout)["result"]["serverInfo"]["name"] == "agentstack"

    doctor = subprocess.run(
        [
            str(install_dir / "bin" / "doctor-codex-app-integration"),
            "--allow-stopped",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ok: Codex plugin registered" in doctor.stdout
    assert "example-secret-value" not in doctor.stdout + doctor.stderr
    assert not list((install_dir / "src").rglob("__pycache__"))

    stdout_log = runtime_dir / "bridge.stdout.log"
    stderr_log = runtime_dir / "bridge.stderr.log"
    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        bridge = subprocess.Popen(
            [str(install_dir / "bin" / "run-bridge")],
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        socket_path = runtime_dir / "bridge.sock"
        deadline = time.monotonic() + 5
        while (
            bridge.poll() is None
            and not socket_path.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        try:
            assert bridge.poll() is None, stderr_log.read_text(encoding="utf-8")
            assert socket_path.is_socket()
            live_doctor = subprocess.run(
                [str(install_dir / "bin" / "doctor-codex-app-integration")],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            assert "ok: Bridge startup diagnostic" in live_doctor.stdout
            assert "ok: no stale spool drains" in live_doctor.stdout
        finally:
            bridge.terminate()
            bridge.wait(timeout=5)
            socket_path.unlink(missing_ok=True)
    bridge_stderr = stderr_log.read_text(encoding="utf-8")
    assert '"event":"bridge_launcher_start"' in bridge_stderr
    assert '"event":"bridge_start"' in bridge_stderr

    diagnostic_environment = dict(
        environment,
        PYTHONPATH=str(install_dir / "src"),
    )
    identity_probe = subprocess.run(
        [
            generated_env["AGENTSTACK_PYTHON"],
            "-c",
            """
import json
from pathlib import Path
import sys
from agentstack_codex_app.agent_mail_client import Registration
from agentstack_codex_app.daemon import BridgeConfig, BridgeDaemon

class FakeAgentMail:
    def __init__(self):
        self.calls = []

    def register_agent(self, **kwargs):
        self.calls.append(kwargs)
        names = ["BlueLake", "GreenCastle"]
        return Registration(names[len(self.calls) - 1], kwargs["registration_token"])

runtime = Path(sys.argv[1])
project_key = sys.argv[2]
config = BridgeConfig(
    runtime_dir=runtime,
    socket_path=runtime / "bridge.sock",
    spool_path=runtime / "hook-events.jsonl",
    retry_path=runtime / "registration-retry.jsonl",
    snapshot_path=runtime / "snapshot.json",
    project_key=project_key,
    agent_mail_endpoint="http://agent-mail.invalid/api/",
    enforce_surface_eligibility=False,
    cold_wake_enabled=False,
)
mail = FakeAgentMail()
daemon = BridgeDaemon(config, mail)
root = daemon.process_event({
    "schema_version": 1,
    "session_id": "packaging-session",
    "agent_id": None,
    "cwd": project_key,
    "model": "gpt-example",
    "hook_event_name": "SessionStart",
    "turn_id": None,
})
child = daemon.process_event({
    "schema_version": 1,
    "session_id": "packaging-session",
    "agent_id": "child-1",
    "cwd": project_key,
    "model": "gpt-example",
    "hook_event_name": "SubagentStart",
    "turn_id": None,
})
assert all("agent_name" not in call for call in mail.calls)
assert daemon.identities.resolve(root)["agent_name"] == "BlueLake"
assert daemon.identities.resolve(child)["agent_name"] == "GreenCastle"
print(json.dumps({"root": "BlueLake", "child": "GreenCastle"}))
""",
            str(runtime_dir / "packaging-identity-probe"),
            str(home / "project"),
        ],
        env=diagnostic_environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(identity_probe.stdout) == {
        "root": "BlueLake",
        "child": "GreenCastle",
    }
    subprocess.run(
        [
            generated_env["AGENTSTACK_PYTHON"],
            "-c",
            """
from pathlib import Path
import sys
from agentstack_codex_app.delivery import DeliveryManager
from agentstack_codex_app.identity_store import build_binding
from agentstack_codex_app.snapshot import SnapshotStore, runtime_record

runtime = Path(sys.argv[1])
binding = build_binding(
    session_id="session-requeue",
    agent_id=None,
    agent_name="ExampleAgent",
    project_key=sys.argv[2],
)
SnapshotStore(runtime / "snapshot.json").upsert(
    runtime_record(
        binding,
        {"cwd": sys.argv[2], "model": "gpt-example"},
        state="blocked",
    )
)
delivery = DeliveryManager(runtime / "delivery.sqlite3")
delivery.observe(sys.argv[2], "ExampleAgent", [77])
delivery.acquire(
    sys.argv[2],
    "ExampleAgent",
    [77],
    lease_owner="wake-test",
    lease_seconds=30,
)
delivery.mark_failed(
    sys.argv[2],
    "ExampleAgent",
    [77],
    lease_owner="wake-test",
    error_code=(
        "untrusted_workspace exit=0 output="
        "Not inside a trusted directory"
    ),
    max_attempts=5,
    terminal=True,
)
""",
            str(runtime_dir),
            str(home / "project"),
        ],
        env=diagnostic_environment,
        check=True,
    )
    requeue = subprocess.run(
        [
            str(install_dir / "bin" / "doctor-codex-app-integration"),
            "--allow-stopped",
            "--requeue-message",
            "77",
            "--agent-name",
            "ExampleAgent",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "requeued delivery message 77 for ExampleAgent" in requeue.stdout
    assert "untrusted_workspace exit=0" in requeue.stderr
    snapshot = json.loads((runtime_dir / "snapshot.json").read_text(encoding="utf-8"))
    runtime = next(
        item for item in snapshot["runtimes"] if item["agent_name"] == "ExampleAgent"
    )
    assert runtime["state"] == "waiting"
    assert runtime["delivery"]["wake_status"] == "pending"
    delivery_state = subprocess.run(
        [
            generated_env["AGENTSTACK_PYTHON"],
            "-c",
            """
import json
import sys
from agentstack_codex_app.delivery import DeliveryManager
print(json.dumps(DeliveryManager(sys.argv[1]).rows()))
""",
            str(runtime_dir / "delivery.sqlite3"),
        ],
        env=diagnostic_environment,
        capture_output=True,
        text=True,
        check=True,
    )
    row = json.loads(delivery_state.stdout)[0]
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["last_error"] is None

    subprocess.run(
        [str(install_dir / "bin" / "uninstall-codex-app-integration")],
        env=environment,
        check=True,
    )
    assert not install_dir.exists()
    assert runtime_dir.is_dir()
    assert _plugin_list(home)["installed"] == []

    subprocess.run(
        _install_args(home, runtime_dir=runtime_dir),
        env=environment,
        check=True,
    )
    assert install_dir.is_dir()
    assert len(_plugin_list(home)["installed"]) == 1
    subprocess.run(
        [
            str(install_dir / "bin" / "uninstall-codex-app-integration"),
            "--purge-data",
        ],
        env=environment,
        check=True,
    )
    assert not install_dir.exists()
    assert not runtime_dir.exists()
    assert _plugin_list(home)["installed"] == []


def test_doctor_cleanup_retires_orphan_before_local_purge(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            if payload["method"] == "tools/list":
                result = {
                    "tools": [{
                        "name": "whois",
                        "inputSchema": {
                            "properties": {"registration_token": {}}
                        },
                    }]
                }
            else:
                calls.append(payload)
                tool_name = payload["params"]["name"]
                if tool_name == "whois":
                    structured = {
                        "name": "CalmNoether",
                        "program": "codex-app",
                    }
                else:
                    structured = {
                        "status": "retired",
                        "agent_name": "CalmNoether",
                        "project_key": str(home / "project"),
                    }
                result = {
                    "structuredContent": structured,
                }
            response = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": result,
            }
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    runtime_dir = home / ".agentstack" / "runtime" / "codex-app"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/api/"
    try:
        subprocess.run(
            [
                *_install_args(
                    home,
                    runtime_dir=runtime_dir,
                    agent_mail_url=endpoint,
                ),
                "--no-service",
                "--no-plugin",
            ],
            env=environment,
            check=True,
        )
        diagnostic_environment = dict(
            environment,
            PYTHONPATH=str(install_dir / "src"),
        )
        subprocess.run(
            [
                str(Path(_read_generated_env(install_dir / "env.sh")["AGENTSTACK_PYTHON"])),
                "-c",
                """
from agentstack_codex_app.identity_store import IdentityStore, build_binding
from agentstack_codex_app.snapshot import SnapshotStore, runtime_record
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1])
project_key = sys.argv[2]
store = IdentityStore(runtime / "identity")
binding = store.save(
    build_binding(
        session_id="session-orphan",
        agent_id=None,
        agent_name="CalmNoether",
        project_key=project_key,
    )
)
store.store_owner_token(binding["external_id"], "owner-token")
SnapshotStore(runtime / "snapshot.json").upsert(
    runtime_record(binding, {}, state="waiting")
)
""",
                str(runtime_dir),
                str(home / "project"),
            ],
            env=diagnostic_environment,
            check=True,
        )

        doctor = subprocess.run(
            [
                str(install_dir / "bin" / "doctor-codex-app-integration"),
                "--allow-stopped",
                "--cleanup-orphan-bindings",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )

        assert "cleanup complete: 1 cleaned, 0 failed" in doctor.stdout
        assert len(calls) == 2
        assert [call["params"]["name"] for call in calls] == [
            "whois",
            "retire_agent",
        ]
        assert calls[0]["params"]["arguments"]["registration_token"] == "owner-token"
        assert calls[1]["params"]["arguments"]["registration_token"] == "owner-token"
        assert not list((runtime_dir / "identity" / "bindings").glob("*.json"))
        assert not list((runtime_dir / "identity" / "secrets").glob("*.token"))
        snapshot = json.loads(
            (runtime_dir / "snapshot.json").read_text(encoding="utf-8")
        )
        assert snapshot["runtimes"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if install_dir.exists():
            subprocess.run(
                [
                    str(install_dir / "bin" / "uninstall-codex-app-integration"),
                    "--purge-data",
                ],
                env=environment,
                check=True,
            )


def test_export_gate_builds_allowlisted_token_free_artifact(tmp_path):
    home = _prepare_home(tmp_path)
    destination = tmp_path / "exported"
    environment = _environment(home)
    environment["AGENTSTACK_CODEX_APP_INSTALL_DIR"] = str(
        home / "no-live-integration"
    )
    result = subprocess.run(
        [
            str(EXPORTER),
            "codex-app",
            str(destination),
            "--skip-tests",
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    # `check=True` raises CalledProcessError, which prints the argv and hides
    # both streams — so this failing on Linux CI told us only "exit status 1"
    # and nothing about why. A test that captures output owes the reader that
    # output when it fails.
    assert result.returncode == 0, (
        f"export-component.sh exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    assert "Export complete:" in result.stdout
    assert (
        destination
        / "integrations"
        / "codex_app"
        / "src"
        / "agentstack_codex_app"
        / "daemon.py"
    ).is_file()
    exported_env = destination / "integrations" / "codex_app" / "env.sh"
    assert exported_env.stat().st_mode & 0o777 == 0o600
    assert "/workspace/example" in exported_env.read_text(encoding="utf-8")
    exported_recorder = (
        destination
        / "integrations"
        / "codex_app"
        / "plugin"
        / "scripts"
        / "record-codex-session-index.py"
    )
    assert exported_recorder.is_file()
    assert exported_recorder.stat().st_mode & 0o111
    assert not list(destination.rglob("*.sqlite3"))
    assert not list(destination.rglob("__pycache__"))
