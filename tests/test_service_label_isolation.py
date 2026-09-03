"""A test run must not be able to uninstall the product from this machine.

launchd labels are not namespaced by ``HOME``. A test can point ``HOME`` at a
temporary directory and still reach ``gui/<uid>/org.agentstack.agentdashboard``
— the label a real install of this product owns. ``agentctl.sh stop`` in a test
teardown then boots out the developer's own dashboard, and the suite reports
green while the machine loses its service.

That happened: ``test_installer_reuses_existing_agent_mail_listener_database``
removed a working install, and nothing noticed because until the product was
installed on a development machine there was nothing there to remove.

``service_teardown`` already documents the rule — pass a test-owned label. This
makes the rule enforceable instead of remembered, because the failure it
prevents is silent, is invisible on CI (where nothing is installed), and only
appears on the one machine where someone is dogfooding.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
PRODUCTION_PREFIX = "org.agentstack"
HOME_KEY = "AGENTSTACK_HOME"
LABEL_KEY = "AGENTSTACK_LABEL_PREFIX"


def _static_prefix(node: ast.expr) -> str | None:
    """The part of the value known without running it, or None if opaque."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        # e.g. TEST_LABEL_PREFIX imported from service_teardown.
        return None if node.id != "PRODUCTION_PREFIX" else PRODUCTION_PREFIX
    if isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _env_dicts_with_home(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if HOME_KEY in keys:
            yield node


def _label_value(node: ast.Dict) -> ast.expr | None:
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == LABEL_KEY:
            return value
    return None


def test_no_test_env_can_boot_out_the_installed_service():
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for env in _env_dicts_with_home(tree):
            where = f"{path.name}:{env.lineno}"
            label = _label_value(env)
            if label is None:
                offenders.append(
                    f"{where}: sets {HOME_KEY} without {LABEL_KEY}; a teardown "
                    f"here boots out {PRODUCTION_PREFIX}.agentdashboard"
                )
                continue
            prefix = _static_prefix(label)
            if prefix is None:
                continue  # a name such as TEST_LABEL_PREFIX; checked below.
            if prefix == PRODUCTION_PREFIX or not prefix.startswith(
                f"{PRODUCTION_PREFIX}."
            ):
                offenders.append(
                    f"{where}: {LABEL_KEY}={prefix!r} is the label a real "
                    "install owns; use a test-specific suffix"
                )
    assert not offenders, "tests that can uninstall this machine's dashboard:\n" + (
        "\n".join(offenders)
    )


def test_the_shared_test_prefix_is_not_the_production_label():
    """The positive control: the rule above is only worth having if the name
    the tests share is itself distinct from what a real install registers."""
    from service_teardown import TEST_LABEL_PREFIX

    assert TEST_LABEL_PREFIX != PRODUCTION_PREFIX
    assert TEST_LABEL_PREFIX.startswith(f"{PRODUCTION_PREFIX}.")


def test_no_test_drives_mailctl_under_the_production_label():
    """A test that stops the developer's own mail service looks like a crash.

    tests/test_mail_autostart.py ran `agentstack-mailctl stop` without naming a
    label. The controller defaults to the production one, so every full-suite
    run booted out the real service on 8765 -- five times in one day, each
    investigated as an outage of its own before the pattern showed up.
    """
    import pathlib
    import re

    tests_dir = pathlib.Path(__file__).resolve().parent
    # Executing it, not merely mentioning it: several tests assert on the string
    # because it appears in a message, and flagging those teaches people to
    # silence the check.
    executes = re.compile(
        r"\[\s*(?:str\()?\s*(?:MAILCTL|mailctl)\b"
        r'|"/bin/bash",\s*str\(MAILCTL\)'
    )
    names_a_label = re.compile(r"AGENTSTACK_MAIL_LAUNCHD_LABEL|AGENTSTACK_LABEL_PREFIX")
    offenders = []
    for path in sorted(tests_dir.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not executes.search(text):
            continue
        if not names_a_label.search(text):
            offenders.append(path.name)
    assert not offenders, (
        "these tests drive agentstack-mailctl without naming a test label, so they "
        f"can act on the real install's launchd job: {offenders}"
    )


def test_mailctl_refuses_the_default_label_under_pytest(tmp_path):
    """Prevention, not only detection.

    The watcher reports a stopped service after the fact; by then every agent
    on the machine has already lost coordination. This refuses the action.
    """
    import os
    import subprocess

    mailctl = pathlib.Path(__file__).resolve().parents[1] / "bin" / "agentstack-mailctl"
    env = dict(os.environ)
    env["PYTEST_CURRENT_TEST"] = "explicit for this check"
    env["AGENTSTACK_HOME"] = str(tmp_path / "home")
    env.pop("AGENTSTACK_MAIL_LAUNCHD_LABEL", None)
    env.pop("AGENTSTACK_LABEL_PREFIX", None)
    result = subprocess.run(
        ["/bin/bash", str(mailctl), "stop"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "refusing to act on the default launchd label" in result.stderr


def test_a_scoped_prefix_alone_does_not_unlock_the_default_label(tmp_path):
    """A prefix does not change what the command acts on.

    The refusal briefly accepted a test-looking AGENTSTACK_LABEL_PREFIX, which
    leaves the target label untouched -- a test could carry a scoped prefix and
    still stop the production job.
    """
    import os
    import subprocess

    mailctl = pathlib.Path(__file__).resolve().parents[1] / "bin" / "agentstack-mailctl"
    env = dict(os.environ)
    env["PYTEST_CURRENT_TEST"] = "explicit for this check"
    env["AGENTSTACK_HOME"] = str(tmp_path / "home")
    env["AGENTSTACK_LABEL_PREFIX"] = "org.agentstack.test.something"
    env.pop("AGENTSTACK_MAIL_LAUNCHD_LABEL", None)
    result = subprocess.run(
        ["/bin/bash", str(mailctl), "stop"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 3, result.stdout + result.stderr


def test_mailctl_still_works_when_a_test_names_its_own_label(tmp_path):
    """The null case: refusing everything would be no protection at all."""
    import os
    import subprocess

    mailctl = pathlib.Path(__file__).resolve().parents[1] / "bin" / "agentstack-mailctl"
    env = dict(os.environ)
    env["PYTEST_CURRENT_TEST"] = "explicit for this check"
    env["AGENTSTACK_HOME"] = str(tmp_path / "home")
    env["AGENTSTACK_MAIL_LAUNCHD_LABEL"] = "org.agentstack.test.isolation.mail"
    env["AGENTSTACK_MAILCTL_SKIP_ENV"] = "1"
    result = subprocess.run(
        ["/bin/bash", str(mailctl), "status"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 3, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="drives launchctl directly")
def test_an_explicit_mail_label_reaches_the_env_and_the_manifest(tmp_path):
    """An operator who named the label keeps it -- in every place it is read.

    Matching source text only proved the branch exists. What matters is what
    the install writes: env.sh is what a raw client reads, and the manifest is
    the audit and uninstall contract for a setting that decides which launchd
    job may be stopped.

    macOS only, and everything after the first side effect lives in the
    cleanup block: a failure here must not leave a job or a dashboard behind.
    """
    import json
    import socket
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    # Not "<prefix>.mail": that is the autostart wrapper's own label.
    label = "org.agentstack.test.explicit-label.direct-mail"

    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    # Start from a scrubbed environment: an ambient AGENTSTACK_MAIL_* would
    # point this supposedly isolated install at somebody else's state.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENTSTACK_")
    }
    env.update({
        "HOME": str(home),
        "AGENTSTACK_HOME": str(home / ".agentstack"),
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_PORT": str(_free_port()),
            "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
            "AGENTSTACK_MAIL_SERVICE_VENV": str(
                pathlib.Path(sys.executable).parent.parent
            ),
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.explicit-label",
        "AGENTSTACK_MAIL_LAUNCHD_LABEL": label,
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_PROJECT_KEY": str(tmp_path / "project"),
    })

    def _bootout():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
        )

    try:
        # A previous failed run can leave the job loaded, and the controller
        # then refuses to start a service under a label something else holds.
        _bootout()
        result = subprocess.run(
            ["bash", str(root / "scripts" / "install.sh"), "--dashboard-only"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]

        env_sh = (home / ".agentstack" / "env.sh").read_text(encoding="utf-8")
        assert f"AGENTSTACK_MAIL_LAUNCHD_LABEL={label}" in env_sh.replace('"', ""), env_sh

        state_path = home / ".agentstack" / "install-state.json"
        assert state_path.is_file(), "the install wrote no manifest to audit"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert (state.get("env") or {}).get("AGENTSTACK_MAIL_LAUNCHD_LABEL") == label
    finally:
        # Nested, so one cleanup failing does not skip the rest. The last one
        # matters most: without it the job stays loaded and the next run of
        # this test cannot start a service under that label at all.
        from service_teardown import stop_dashboard

        try:
            try:
                stop_dashboard(home, label_prefix="org.agentstack.test.explicit-label")
            finally:
                mailctl = home / ".agentstack" / "bin" / "agentstack-mailctl"
                if mailctl.is_file():
                    subprocess.run(
                        ["/bin/bash", str(mailctl), "stop"],
                        env={**env, "AGENTSTACK_MAIL_LAUNCHD_LABEL": label},
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
        finally:
            _bootout()


def test_the_installer_records_the_label_it_will_act_on():
    """The manifest is the contract; a label that is not in it cannot be audited."""
    install = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
    text = install.read_text(encoding="utf-8")
    assert text.count('"AGENTSTACK_MAIL_LAUNCHD_LABEL": "$MAIL_LAUNCHD_LABEL_SETTING"') >= 2, (
        "the label is written to env.sh but not recorded in the manifest"
    )


def test_a_mail_label_that_collides_with_the_autostart_job_is_refused(tmp_path):
    """Two different jobs cannot share one name.

    One runs the mail service; the other runs `agentstack-mailctl start` on a
    timer. Given the same label, the controller inspects the wrapper, decides
    the job under its label is not the mail service, and refuses to start --
    an install that looks healthy until the first sweep or the next login.
    """
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    prefix = "org.agentstack.test.collision"

    def _free_port() -> int:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENTSTACK_")}
    env.update({
        "AGENTSTACK_PORT": str(_free_port()),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
        "HOME": str(home),
        "AGENTSTACK_HOME": str(home / ".agentstack"),
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_LABEL_PREFIX": prefix,
        "AGENTSTACK_MAIL_LAUNCHD_LABEL": f"{prefix}.mail",  # the autostart job's label
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_PROJECT_KEY": str(tmp_path / "project"),
    })
    result = subprocess.run(
        ["bash", str(root / "scripts" / "install.sh"), "--dashboard-only", "--dry-run"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "collides with the mail autostart job" in result.stderr, result.stderr


def test_a_distinct_mail_label_is_accepted(tmp_path):
    """The null case: refusing every explicit label would be no better."""
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    prefix = "org.agentstack.test.collision"

    def _free_port() -> int:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENTSTACK_")}
    env.update({
        "AGENTSTACK_PORT": str(_free_port()),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
        "HOME": str(home),
        "AGENTSTACK_HOME": str(home / ".agentstack"),
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_LABEL_PREFIX": prefix,
        "AGENTSTACK_MAIL_LAUNCHD_LABEL": f"{prefix}.direct-mail",
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_PROJECT_KEY": str(tmp_path / "project"),
    })
    result = subprocess.run(
        ["bash", str(root / "scripts" / "install.sh"), "--dashboard-only", "--dry-run"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
