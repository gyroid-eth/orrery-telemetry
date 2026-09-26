"""Environment isolation shared by conftest.py and its tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Variables that point tools at the real user's config directories. Installer
# rehearsals must write under the test's temporary HOME, never through these.
INHERITED_CONFIG_DIR_VARS = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")



def _real_home() -> Path:
    """The account's home directory, even when a test has changed HOME."""
    try:
        import pwd
    except ImportError:  # Windows has no pwd; USERPROFILE is not rewritten by tests
        return Path(os.environ.get("USERPROFILE") or Path.home())
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


_REAL_HOME = _real_home()

# Files the installers write managed blocks into.
REAL_USER_INSTRUCTION_FILES = (
    _REAL_HOME / ".codex" / "AGENTS.md",
    _REAL_HOME / ".claude" / "CLAUDE.md",
)


def is_inherited_stack_variable(name: str) -> bool:
    return name.startswith("AGENTSTACK_") or name in INHERITED_CONFIG_DIR_VARS


def digest_files(paths) -> dict[Path, str | None]:
    """SHA-256 of each file's bytes, or None when it does not exist."""
    digests: dict[Path, str | None] = {}
    for path in paths:
        try:
            digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            digests[path] = None
    return digests
