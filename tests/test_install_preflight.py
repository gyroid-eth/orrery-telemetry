"""Installer preflight failures stay early, actionable, and aggregated."""

from __future__ import annotations

import http.server
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import tomllib


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_command(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    command = directory / name
    command.write_text(body, encoding="utf-8")
    command.chmod(0o755)
    return command


def _minimal_path(tmp_path: pathlib.Path, *, os_name: str) -> pathlib.Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_command(
        fake_bin,
        "dirname",
        "#!/bin/sh\nexec /usr/bin/dirname \"$@\"\n",
    )
    _write_command(fake_bin, "uname", f"#!/bin/sh\nprintf '%s\\n' {os_name}\n")
    return fake_bin


def _base_env(tmp_path: pathlib.Path, fake_bin: pathlib.Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": str(fake_bin),
            "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.preflight",
            "AGENTSTACK_PROJECT_KEY": str(project),
            "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
            "AGENTSTACK_PORT": str(_free_port()),
            "AGENTSTACK_TERMINAL": "none",
        }
    )
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(INSTALLER), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def test_preflight_aggregates_commands_os_and_install_target(tmp_path):
    fake_bin = _minimal_path(tmp_path, os_name="FreeBSD")
    env = _base_env(tmp_path, fake_bin)
    install_target = pathlib.Path(env["AGENTSTACK_HOME"])
    install_target.write_text("not a directory\n", encoding="utf-8")

    result = _run(env, "--dry-run")

    assert result.returncode != 0
    assert "preflight failed with 4 problem(s)" in result.stderr
    assert "git is required" in result.stderr
    assert "tmux is required" in result.stderr
    assert "operating system 'FreeBSD' is unsupported" in result.stderr
    assert "exists but is not a directory" in result.stderr
    assert "Tier1 will show" not in result.stdout


def test_preflight_reports_incompatible_python_with_install_hint(tmp_path):
    fake_bin = _minimal_path(tmp_path, os_name="Linux")
    _write_command(fake_bin, "git", "#!/bin/sh\nexit 0\n")
    _write_command(fake_bin, "tmux", "#!/bin/sh\nexit 0\n")
    old_python = _write_command(
        fake_bin,
        "python-old",
        """#!/bin/sh
case "$*" in
  *'sys.version_info[:3]'*) printf '%s\n' 3.9.18; exit 0 ;;
  *) exit 1 ;;
esac
""",
    )
    env = _base_env(tmp_path, fake_bin)
    env["AGENTSTACK_PYTHON"] = str(old_python)
    env["AGENTSTACK_PREFLIGHT_SKIP_PORT"] = "1"

    result = _run(env, "--dry-run")

    assert result.returncode != 0
    assert "preflight failed with 1 problem(s)" in result.stderr
    assert "AGENTSTACK_PYTHON must be Python 3.10 or newer" in result.stderr
    assert "found 3.9.18" in result.stderr
    assert "Install a current Python" in result.stderr


def test_preflight_os_escape_hatch_is_individual(tmp_path):
    fake_bin = _minimal_path(tmp_path, os_name="FreeBSD")
    env = _base_env(tmp_path, fake_bin)
    env["AGENTSTACK_PREFLIGHT_SKIP_OS"] = "1"

    result = _run(env, "--dry-run")

    assert result.returncode != 0
    assert "OS check skipped by AGENTSTACK_PREFLIGHT_SKIP_OS=1" in result.stdout
    assert "unsupported" not in result.stderr
    assert "git is required" in result.stderr
    assert "tmux is required" in result.stderr


class _OccupiedPort(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_preflight_marks_occupied_port_as_existing_install_update(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OccupiedPort)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fake_bin = _minimal_path(tmp_path, os_name="Linux")
        _write_command(fake_bin, "git", "#!/bin/sh\nexit 0\n")
        _write_command(fake_bin, "tmux", "#!/bin/sh\nexit 0\n")
        env = _base_env(tmp_path, fake_bin)
        env["PATH"] = f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"
        install_dir = pathlib.Path(env["AGENTSTACK_HOME"])
        install_dir.mkdir()
        (install_dir / "install-state.json").write_text("{}\n", encoding="utf-8")
        env["AGENTSTACK_MCP_URL"] = (
            f"http://127.0.0.1:{server.server_port}/mcp"
        )

        result = _run(env, "--dashboard-only", "--dry-run")
    finally:
        server.shutdown()

    assert result.returncode != 0
    assert (
        "existing AgentStack install detected; occupied agent-mail port "
        f"{server.server_port} will be verified for reuse"
    ) in result.stdout
    assert "AGENTSTACK_MAIL_DB" in result.stderr


def test_shell_python_floor_matches_package_metadata():
    metadata = tomllib.loads(
        (ROOT / "packages" / "agentstack_mail" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    requires_python = metadata["project"]["requires-python"]
    match = re.fullmatch(r">=(\d+)\.(\d+)", requires_python)
    assert match, requires_python

    installer = INSTALLER.read_text(encoding="utf-8")
    major = re.search(r"^PYTHON_MIN_MAJOR=(\d+)$", installer, re.MULTILINE)
    minor = re.search(r"^PYTHON_MIN_MINOR=(\d+)$", installer, re.MULTILINE)
    assert major and minor
    assert (major.group(1), minor.group(1)) == match.groups()


def _python_with_broken_loopback(
    tmp_path: pathlib.Path, errno_value: int, message: str
) -> pathlib.Path:
    """A real interpreter whose outbound connects fail like a gated loopback.

    Faking the interpreter itself would skip the probe under test, so only
    ``socket.create_connection`` is replaced. macOS 26 returns EADDRNOTAVAIL for
    every loopback connect made from an SSH session -- including one aimed at a
    socket the same process just opened -- which is what made a held port look
    free.
    """
    shim = tmp_path / "socketshim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import socket\n"
        "def _refuse(*_args, **_kwargs):\n"
        f"    raise OSError({errno_value}, {message!r})\n"
        "socket.create_connection = _refuse\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "python-broken-loopback"
    launcher.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{shim}${{PYTHONPATH:+:$PYTHONPATH}}" '
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _env_with_working_deps(tmp_path: pathlib.Path) -> dict[str, str]:
    fake_bin = _minimal_path(tmp_path, os_name="Linux")
    _write_command(fake_bin, "git", "#!/bin/sh\nexit 0\n")
    _write_command(fake_bin, "tmux", "#!/bin/sh\nexit 0\n")
    return _base_env(tmp_path, fake_bin)


def test_preflight_reports_free_port_as_available(tmp_path):
    env = _env_with_working_deps(tmp_path)

    result = _run(env, "--dry-run")

    port = env["AGENTSTACK_MCP_URL"].rsplit(":", 1)[1].split("/", 1)[0]
    assert f"preflight: agent-mail port {port} is available" in result.stdout


def test_preflight_refuses_to_call_an_unprobeable_port_available(tmp_path):
    env = _env_with_working_deps(tmp_path)
    env["AGENTSTACK_PYTHON"] = str(
        _python_with_broken_loopback(
            tmp_path, 49, "Can't assign requested address"
        )
    )

    result = _run(env, "--dry-run")

    port = env["AGENTSTACK_MCP_URL"].rsplit(":", 1)[1].split("/", 1)[0]
    assert result.returncode != 0
    assert f"agent-mail port {port} is available" not in result.stdout
    assert "could not determine whether agent-mail port" in result.stderr
    assert "Can't assign requested address" in result.stderr
    assert "run the installer from a local terminal" in result.stderr


def test_preflight_port_skip_switch_accepts_an_unprobeable_port(tmp_path):
    env = _env_with_working_deps(tmp_path)
    env["AGENTSTACK_PYTHON"] = str(
        _python_with_broken_loopback(
            tmp_path, 49, "Can't assign requested address"
        )
    )
    env["AGENTSTACK_PREFLIGHT_SKIP_PORT"] = "1"

    result = _run(env, "--dry-run")

    assert "could not determine whether agent-mail port" not in result.stderr
    assert "preflight: passed" in result.stdout
