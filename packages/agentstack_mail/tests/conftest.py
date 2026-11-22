import os
import sys
from pathlib import Path

import pytest

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

# A shell started by the installed stack carries the live Mail's settings
# (AGENTSTACK_MAIL_MANAGEMENT_SOCKET, AGENTSTACK_MAIL_DB, ...). The server under
# test reads them through python-decouple, which prefers the process environment
# to any env file, so a test server opened the live management socket and 37
# tests failed with "management socket is already active" (#80). The repository
# suite's conftest strips these for tests/ only; this directory needs its own.
# Strip before collection: get_settings() caches the first read.
_INHERITED: dict[str, str] = {}


@pytest.fixture
def isolated_managed_harness_home(tmp_path, monkeypatch):
    """Keep native plans and their fixture subprocesses off the user's MCP settings."""
    home = tmp_path / 'synthetic-user-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    monkeypatch.setenv('AGENTSTACK_CLAUDE_JSON', str(home / '.claude.json'))
    monkeypatch.delenv('AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY', raising=False)


def _is_inherited_stack_variable(name: str) -> bool:
    return name.startswith("AGENTSTACK_") or name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR")


def pytest_configure(config):
    for key in [key for key in os.environ if _is_inherited_stack_variable(key)]:
        _INHERITED[key] = os.environ.pop(key)


def pytest_unconfigure(config):
    os.environ.update(_INHERITED)
    _INHERITED.clear()
