from __future__ import annotations

import os
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "hooks" / "project-context.sh"
INSTALLER = ROOT / "scripts" / "install.sh"


def _clean_env(home: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "AGENTSTACK_HOME",
        "AGENTSTACK_MAIL_DB",
        "AGENTSTACK_MAIL_ENV",
        "AGENTSTACK_MAIL_HOME",
        "AGENTSTACK_MAIL_STATE_ROOT",
        "AGENTSTACK_PROJECT_KEY",
        "AGENTSTACK_PROTECTED_ROOTS",
        "PROJECT_KEY",
    ):
        env.pop(name, None)
    env["HOME"] = str(home)
    return env


def _resolve(home: pathlib.Path, cwd: pathlib.Path) -> str:
    result = subprocess.run(
        ["/bin/bash", str(HELPER), "resolve-project-key", str(cwd)],
        cwd=cwd,
        env=_clean_env(home),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_installed_project_key_wins_over_an_unrelated_cwd_without_sourcing(
    tmp_path: pathlib.Path,
) -> None:
    home = tmp_path / "home"
    installed = tmp_path / "Project With Spaces"
    unrelated = tmp_path / "unrelated" / "nested"
    sentinel = tmp_path / "env-was-sourced"
    (home / ".agentstack").mkdir(parents=True)
    installed.mkdir()
    unrelated.mkdir(parents=True)
    (home / ".agentstack" / "env.sh").write_text(
        f"export AGENTSTACK_PROJECT_KEY='{installed}'\n"
        f"touch '{sentinel}'\n",
        encoding="utf-8",
    )

    assert _resolve(home, unrelated) == str(installed)
    assert not sentinel.exists(), "reading one setting executed env.sh"


def test_live_project_values_win_over_installed_project_key(
    tmp_path: pathlib.Path,
) -> None:
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True)
    (home / ".agentstack" / "env.sh").write_text(
        "export AGENTSTACK_PROJECT_KEY=/installed\n", encoding="utf-8"
    )
    env = _clean_env(home)
    env["PROJECT_KEY"] = "/legacy-live"
    result = subprocess.run(
        ["/bin/bash", str(HELPER), "resolve-project-key", "/cwd"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "/legacy-live"

    env["AGENTSTACK_PROJECT_KEY"] = "/agentstack-live"
    result = subprocess.run(
        ["/bin/bash", str(HELPER), "resolve-project-key", "/cwd"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "/agentstack-live"


def test_missing_installed_env_falls_back_to_cwd(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project" / "subdirectory"
    home.mkdir()
    cwd.mkdir(parents=True)
    assert _resolve(home, cwd) == str(cwd)


def test_all_five_consumers_call_the_shared_resolver() -> None:
    shell_consumers = (
        "hooks/reservation-common.sh",
        "hooks/check-agent-registered.sh",
        "hooks/session-start-reminder.sh",
        "hooks/cleanup-child-agent.sh",
    )
    for relative in shell_consumers:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert ". \"$PROJECT_CONTEXT_LIB\"" in text, relative
        assert "agentstack_resolve_project_key" in text, relative
    await_reply = (ROOT / "bin" / "agentstack-await-reply").read_text(
        encoding="utf-8"
    )
    assert '"project-context.sh"' in await_reply
    assert '"resolve-project-key"' in await_reply


def _fake_installer_env(home: pathlib.Path, fake_bin: pathlib.Path) -> dict[str, str]:
    env = _clean_env(home)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_TERMINAL": "none",
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_PORT": "18963",
        }
    )
    return env


def _fake_installer_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "uname": "#!/bin/sh\necho Linux\n",
        "tmux": "#!/bin/sh\nexit 0\n",
        "uv": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\nexit 0\n",
    }.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return fake_bin


def test_reinstall_defaults_to_the_installed_project_key(
    tmp_path: pathlib.Path,
) -> None:
    home = tmp_path / "home"
    installed = tmp_path / "installed-project"
    (home / ".agentstack").mkdir(parents=True)
    installed.mkdir()
    (home / ".agentstack" / "env.sh").write_text(
        f"export AGENTSTACK_PROJECT_KEY={installed}\n", encoding="utf-8"
    )
    result = subprocess.run(
        ["/bin/bash", str(INSTALLER), "--dashboard-only", "--dry-run"],
        cwd=ROOT,
        env=_fake_installer_env(home, _fake_installer_bin(tmp_path)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"project key: {installed}" in result.stdout


def test_explicit_installer_project_key_wins_over_installed_env(
    tmp_path: pathlib.Path,
) -> None:
    home = tmp_path / "home"
    explicit = tmp_path / "explicit-project"
    (home / ".agentstack").mkdir(parents=True)
    explicit.mkdir()
    (home / ".agentstack" / "env.sh").write_text(
        "export AGENTSTACK_PROJECT_KEY=/installed-project\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            "/bin/bash",
            str(INSTALLER),
            "--dashboard-only",
            "--dry-run",
            "--project-key",
            str(explicit),
        ],
        cwd=ROOT,
        env=_fake_installer_env(home, _fake_installer_bin(tmp_path)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"project key: {explicit}" in result.stdout


def test_first_install_without_a_project_key_stops_before_preflight(
    tmp_path: pathlib.Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["/bin/bash", str(INSTALLER), "--dashboard-only", "--dry-run"],
        cwd=ROOT,
        env=_fake_installer_env(home, _fake_installer_bin(tmp_path)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "project key is required on first install" in result.stderr
    assert "preflight:" not in result.stdout
