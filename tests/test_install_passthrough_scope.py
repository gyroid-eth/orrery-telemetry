#!/usr/bin/env python3
"""What the installer may patch, and what it may only ask about.

The naming patch is three lines and inert until the mode selects it, which is
what makes it reasonable to offer at all. It is still somebody's source tree.
The line this draws: a checkout this installer created is configured without
asking; anything else is left exactly as it was found unless a human says
otherwise, and a non-interactive run has no human to say it.

Runnable two ways:
    python3 tests/test_install_passthrough_scope.py
    pytest tests/test_install_passthrough_scope.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX, stop_dashboard  # noqa: E402
INSTALL = ROOT / "scripts" / "install.sh"
# The installer only treats a checkout as its own when the remote matches
# exactly, ".git" suffix included.
UPSTREAM = "https://github.com/Dicklesworthstone/mcp_agent_mail.git"

FIXTURE = ROOT / "tests" / "fixtures" / "agent_mail_stock"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _fake_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    for name, body in {
        "systemctl": "#!/bin/sh\nexit 1\n",
        "tmux": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    return fake_bin


def _stock_checkout(root: pathlib.Path, *, remote: str) -> pathlib.Path:
    """A checkout shaped like upstream at the pinned ref.

    Copied from the shared fixture rather than written inline: an anchor that
    only matches a convenient approximation of the source passes here and
    misses in the field.
    """
    shutil.copytree(FIXTURE, root)
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, **quiet)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", remote], check=True, **quiet
    )
    return root


def _run_installer(home: pathlib.Path, tmp_path: pathlib.Path, mail_dir: pathlib.Path,
                   extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{_fake_bin(tmp_path)}:/usr/bin:/bin:/usr/sbin:/sbin",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(home / ".agentstack"),
        "AGENTSTACK_MAIL_DIR": str(mail_dir),
        "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
        "AGENTSTACK_MAIL_DB": str(mail_dir / "storage.sqlite3"),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
        # Never register under the label a real install uses.
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_ASSUME_YES": "1",
    })
    env.update(extra_env)
    (mail_dir / "storage.sqlite3").touch()
    try:
        return subprocess.run(
            ["bash", str(INSTALL), "--dashboard-only"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
    finally:
        stop_dashboard(home)


def _patched(mail_dir: pathlib.Path) -> bool:
    app = (mail_dir / "src" / "mcp_agent_mail" / "app.py").read_text(encoding="utf-8")
    return 'mode == "passthrough"' in app


def _manifest(home: pathlib.Path) -> dict:
    return json.loads(
        (home / ".agentstack" / "install-state.json").read_text(encoding="utf-8")
    )


def test_honored_names_are_recorded_without_a_warning(tmp_path):
    """Null case first: #140 support is healthy even with patching disabled."""
    home = tmp_path / "home"
    home.mkdir()
    mail_dir = _stock_checkout(tmp_path / "mail", remote=UPSTREAM)
    result = _run_installer(
        home, tmp_path, mail_dir, {"AGENTSTACK_AGENT_MAIL_PASSTHROUGH": "0"}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "agent-mail requested-name handling: honored" in result.stdout
    assert "warning: agent-mail requested-name handling" not in result.stderr
    recorded = _manifest(home)["agent_mail"]["requested_name_honoring"]
    assert recorded["status"] == "honored"
    assert recorded["evidence"] == "validate_explicit_agent_id"
    installed_classifier = (
        home / ".agentstack" / "bin" / "lib" / "agent_mail_passthrough.py"
    )
    assert installed_classifier.is_file()


def test_legacy_names_warn_before_they_are_replaced(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    mail_dir = _stock_checkout(tmp_path / "mail", remote=UPSTREAM)
    app = mail_dir / "src" / "mcp_agent_mail" / "app.py"
    app.write_text(
        app.read_text(encoding="utf-8").replace(
            "validate_explicit_agent_id", "legacy_explicit_name_check"
        ),
        encoding="utf-8",
    )
    result = _run_installer(
        home, tmp_path, mail_dir, {"AGENTSTACK_AGENT_MAIL_PASSTHROUGH": "0"}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warning: agent-mail requested-name handling: replaced" in result.stderr
    assert "requested names will be replaced by generated names" in result.stderr
    recorded = _manifest(home)["agent_mail"]["requested_name_honoring"]
    assert recorded["status"] == "replaced"
    assert recorded["evidence"] == "legacy-naming"


def test_unreadable_source_is_recorded_as_unknown(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    mail_dir = _stock_checkout(
        tmp_path / "mail", remote="https://example.invalid/package.git"
    )
    (mail_dir / "src" / "mcp_agent_mail" / "app.py").unlink()
    result = _run_installer(home, tmp_path, mail_dir, {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warning: agent-mail requested-name handling: unknown" in result.stderr
    recorded = _manifest(home)["agent_mail"]["requested_name_honoring"]
    assert recorded["status"] == "unknown"
    assert recorded["evidence"] == "source-unreadable"


def test_a_checkout_pointing_elsewhere_is_left_alone(tmp_path):
    """Not our clone: the installer already declines to manage it."""
    home = tmp_path / "home"
    home.mkdir()
    mail_dir = _stock_checkout(tmp_path / "mail", remote="https://example.invalid/fork.git")
    result = _run_installer(home, tmp_path, mail_dir, {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _patched(mail_dir), "the installer patched a checkout it does not own"
    assert "left as it is" in result.stdout


def test_the_patch_can_be_declined_for_our_own_clone(tmp_path):
    """The escape hatch has to actually stop it, or it is decoration."""
    home = tmp_path / "home"
    home.mkdir()
    mail_dir = _stock_checkout(
        tmp_path / "mail", remote=UPSTREAM
    )
    result = _run_installer(
        home, tmp_path, mail_dir, {"AGENTSTACK_AGENT_MAIL_PASSTHROUGH": "0"}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _patched(mail_dir)
    assert "AGENTSTACK_AGENT_MAIL_PASSTHROUGH=0" in result.stdout
    env_text = (mail_dir / ".env").read_text(encoding="utf-8")
    assert "AGENT_NAME_ENFORCEMENT_MODE=passthrough" not in env_text


def test_our_own_clone_is_configured_without_asking(tmp_path):
    """The null case for the two above: with nothing declined, it happens."""
    home = tmp_path / "home"
    home.mkdir()
    mail_dir = _stock_checkout(
        tmp_path / "mail", remote=UPSTREAM
    )
    result = _run_installer(home, tmp_path, mail_dir, {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _patched(mail_dir), "our own clone was left unable to accept the names we ask for"
    assert "patched agent-mail to accept explicit names" in result.stdout
    env_text = (mail_dir / ".env").read_text(encoding="utf-8")
    assert "AGENT_NAME_ENFORCEMENT_MODE=passthrough" in env_text


def test_a_dry_run_writes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    mail_dir = _stock_checkout(
        tmp_path / "mail", remote=UPSTREAM
    )
    env = {"AGENTSTACK_DRY_RUN": "1"}
    result = subprocess.run(
        ["bash", str(INSTALL), "--dashboard-only", "--dry-run"],
        cwd=ROOT,
        env={**os.environ, **{
            "HOME": str(home),
            "PATH": f"{_fake_bin(tmp_path)}:/usr/bin:/bin:/usr/sbin:/sbin",
            "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_MAIL_DIR": str(mail_dir),
            "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
            "AGENTSTACK_MAIL_DB": str(mail_dir / "storage.sqlite3"),
            "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
            "AGENTSTACK_PORT": str(_free_port()),
            "AGENTSTACK_PROJECT_KEY": str(tmp_path / "project"),
            "AGENTSTACK_TERMINAL": "none",
        # Never register under the label a real install uses.
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        }, **env},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _patched(mail_dir), "--dry-run patched the checkout"


def test_teardown_works_where_launchd_does_not_exist(tmp_path):
    """CI is Linux. Calling launchctl unconditionally failed five tests there.

    Reproduces the condition rather than trusting the guard: with no launchctl
    on the machine, teardown must still complete.
    """
    import service_teardown

    original = service_teardown.shutil.which
    service_teardown.shutil.which = lambda name: None if name == "launchctl" else original(name)
    try:
        stop_dashboard(tmp_path / "home", appear_timeout=0.1)
    finally:
        service_teardown.shutil.which = original


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as directory:
                try:
                    fn(pathlib.Path(directory))
                    print(f"PASS {name}")
                except AssertionError as exc:
                    failures += 1
                    print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
