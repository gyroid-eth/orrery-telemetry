"""The suite must not inherit variables that point at the real user's config."""

from __future__ import annotations

import os

from _env_isolation import INHERITED_CONFIG_DIR_VARS, is_inherited_stack_variable
from service_teardown import TEST_LABEL_PREFIX


def test_config_dir_variables_count_as_inherited_stack_state() -> None:
    for name in INHERITED_CONFIG_DIR_VARS:
        assert is_inherited_stack_variable(name)
    assert is_inherited_stack_variable("AGENTSTACK_HOME")
    assert not is_inherited_stack_variable("PATH")
    assert not is_inherited_stack_variable("HOME")


def test_tests_run_without_the_callers_config_dirs() -> None:
    # Run under a Codex child, CODEX_HOME points at a directory whose AGENTS.md
    # is a symlink to the real one (#73). The session fixture must remove it.
    leaked = [name for name in INHERITED_CONFIG_DIR_VARS if name in os.environ]
    assert not leaked, f"inherited into the test run: {leaked}"


def test_installer_rehearsal_env_writes_codex_block_under_the_test_home(tmp_path) -> None:
    # Installer tests build their environment as os.environ.copy() plus a
    # temporary HOME. That must be enough to keep agentstack-codex-setup from
    # writing anywhere but the temporary home (#73).
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    stack = home / ".agentstack"
    project = tmp_path / "project"
    for path in (home, stack, project):
        path.mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "AGENTSTACK_HOME": str(stack),
            "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
            "AGENTSTACK_TEMPLATE_HOME": str(root),
            "AGENTSTACK_PROJECT_KEY": str(project),
        }
    )
    subprocess.run(
        ["bash", str(root / "bin" / "agentstack-codex-setup")],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    written = home / ".codex" / "AGENTS.md"
    assert written.is_file(), "the managed block was written outside the test home"
    assert str(project) in written.read_text(encoding="utf-8")
