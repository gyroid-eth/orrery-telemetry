#!/usr/bin/env python3
"""Supply the context footer default for interactive, managed Codex launches."""

import os
from pathlib import Path
import sys
import tomllib


CONTEXT_DEFAULT = 'tui.status_line=["model-with-reasoning","context-remaining","current-dir"]'


def default_flag(cwd):
    """Leave an explicitly configured footer, including an empty one, in place."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    paths = [home / "config.toml", Path("/etc/codex/config.toml")]
    paths.extend(path / ".codex/config.toml" for path in (cwd, *cwd.parents))
    for path in paths:
        try:
            with path.open("rb") as handle:
                config = tomllib.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return ""
        # A selected profile can provide its own footer. Let Codex decide
        # which fields apply in that case.
        tui = config.get("tui", {})
        if not isinstance(tui, dict) or "status_line" in tui or config.get("profile"):
            return ""
    return CONTEXT_DEFAULT


if __name__ == "__main__":
    print(default_flag(Path(sys.argv[1]).resolve()))
