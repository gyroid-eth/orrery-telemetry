"""Windows-only test lane.

Everything under tests/windows/ runs only on native Windows (the `portable
startup (Windows)` CI job). On any other platform pytest does not even
collect these modules, so the macOS suite, its counts and its runtime are
unaffected by Windows contributions. See CONTRIBUTING.md "Windows
contributions".
"""

from __future__ import annotations

import sys

collect_ignore_glob = [] if sys.platform == "win32" else ["*.py"]
