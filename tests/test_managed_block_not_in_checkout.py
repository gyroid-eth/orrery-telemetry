"""The installer must not write a managed block into its own checkout.

Without ``--project-key`` the default project is the repo the installer was run
from, so ``agentstack-claude-setup`` wrote its block into that checkout's
``CLAUDE.md``. Once the repo started tracking ``CLAUDE.md`` and ``AGENTS.md``,
``git pull`` refused to update them:

    error: The following untracked working tree files would be overwritten by
    merge: CLAUDE.md

A tester hit this on the release that added those files, and every person who
had installed the same way would hit it too. Moving the file aside clears it
once, but the next install writes it again.

The block describes how an agent should behave inside the project it is working
on. The copy of the stack someone installed from is not that project.
"""

from __future__ import annotations

import os
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
SKIP_LINE = "skip managed CLAUDE.md / AGENTS.md blocks"


def _dry_run(tmp_path: pathlib.Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "AGENTSTACK_TERMINAL": "none",
    })
    env.pop("AGENTSTACK_PROJECT_KEY", None)
    env.pop("PROJECT_KEY", None)
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(INSTALL), "--dry-run", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    return result.stdout


def test_default_project_key_does_not_write_into_the_checkout(tmp_path):
    output = _dry_run(tmp_path)
    assert SKIP_LINE in output
    assert "Claude CLAUDE.md managed setup dry-run" not in output


def test_a_real_project_key_still_gets_the_block(tmp_path):
    """The positive control.

    Skipping unconditionally would satisfy the test above while removing the
    feature — the managed block is how a project's agents learn the procedure.
    """
    project = tmp_path / "someones-project"
    project.mkdir()
    output = _dry_run(tmp_path, "--project-key", str(project))
    assert SKIP_LINE not in output
    assert "Claude CLAUDE.md managed setup dry-run" in output
