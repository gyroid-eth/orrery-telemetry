"""The package suite must not see the live Mail's settings (#80)."""

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_no_inherited_agentstack_variable_reaches_a_test():
    leaked = sorted(key for key in os.environ if key.startswith("AGENTSTACK_"))
    assert leaked == []


def test_a_live_management_socket_in_the_parent_env_is_not_used(tmp_path):
    # Run one real server-backed test under an environment that points the
    # management socket at a path that must never be created.
    canary = tmp_path / "live-mgmt.sock"
    env = dict(os.environ)
    env["AGENTSTACK_MAIL_MANAGEMENT_SOCKET"] = str(canary)
    env["AGENTSTACK_MAIL_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path}/live.sqlite3"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(TESTS / "test_tool_argument_boundary.py"), "-k", "unknown_argument_is_rejected"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert not canary.exists()
    assert not (tmp_path / "live.sqlite3").exists()
