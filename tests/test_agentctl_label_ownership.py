"""``agentctl.sh stop`` must not unload a launchd job this HOME never registered.

launchd labels live in the per-user domain and take no notice of ``HOME``. So a
run pointed at a scratch HOME reaches exactly the same
``gui/<uid>/org.agentstack.agentdashboard`` as the real install, and stopping
"its own" service stops the machine's.

That is not hypothetical. A green test run
(``test_installer_reuses_existing_agent_mail_listener_database``) called this
teardown and removed a working dashboard, launchd job and all. The suite passed;
the machine lost the service. Anyone with the product installed who then runs
the tests, a second checkout, or a trial install in another HOME hits the same
thing.

The plist under ``$HOME/Library/LaunchAgents`` is the ownership record: if this
HOME did not write one, this HOME did not register the job.
"""

from __future__ import annotations

import os
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTCTL = ROOT / "dashboard" / "agentctl.sh"

FAKE_LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$LAUNCHCTL_CALLS"
exit 0
"""


def _run_stop(tmp_path: pathlib.Path) -> list[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    calls = tmp_path / "launchctl-calls.txt"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o755)

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "LAUNCHCTL_CALLS": str(calls),
        "AGENTSTACK_ENV_FILE": str(tmp_path / "absent-env.sh"),
        "AGENTSTACK_RUNTIME_DIR": str(home / ".agentstack" / "runtime"),
    })
    subprocess.run(
        ["bash", str(AGENTCTL), "stop"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    if not calls.exists():
        return []
    return [line for line in calls.read_text(encoding="utf-8").splitlines() if line]


def test_stop_leaves_a_label_this_home_never_registered(tmp_path):
    assert not [call for call in _run_stop(tmp_path) if call.startswith("bootout")]


def test_stop_still_unloads_the_job_this_home_did_register(tmp_path):
    """The positive control.

    Without this, "never call bootout at all" would satisfy the test above and
    the stop command would quietly stop stopping anything.
    """
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "org.agentstack.agentdashboard.plist").write_text("", encoding="utf-8")

    calls = _run_stop(tmp_path)
    assert [call for call in calls if call.startswith("bootout")], calls
