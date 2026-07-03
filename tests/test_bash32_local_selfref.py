#!/usr/bin/env python3
"""Lint: forbid self-referencing `local`/`declare` statements (bash 3.2 hazard).

Background
----------
macOS ships GNU bash 3.2 as `/bin/bash`, and every launcher/hook here uses a
`#!/bin/bash` shebang, so they always run under 3.2. In a single `local` (or
`declare`) statement, bash 4+/5 evaluate the initializers left-to-right and make
an earlier-declared name visible to a later initializer, but bash 3.2 does NOT —
the earlier name is still unset within the same statement. Combined with the
`set -euo pipefail` these scripts use, a line like

    local agent_name="$1" state_file="$CHILD_STATE_DIR/$agent_name.json"

works on the maintainer's homebrew bash 5 but aborts on macOS system bash 3.2
with `agent_name: unbound variable`. This exact bug took out every
pre-registered (delegate) spawn in spawn_child.sh until it was split into two
`local` statements.

This test scans the shell scripts and fails if any `local`/`declare` statement
references, in a later initializer, a name it declares earlier in the SAME
statement. Split such declarations into separate lines.

Runnable two ways (no third-party dependency required):
    python3 tests/test_bash32_local_selfref.py   # plain script, prints PASS/FAIL
    pytest tests/test_bash32_local_selfref.py     # under pytest if available
"""
from __future__ import annotations

import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("hooks", "bin", "scripts")

# `local`/`declare` as a statement keyword (start of a statement: line start or
# after {, (, ;, &&, ||, then/do/else). Captures the declaration tail; callers
# split on ';' first so a trailing `; echo "$x"` is not mistaken for an
# initializer that references an earlier name.
_DECL_RE = re.compile(r"(?:^|[;{(&|]|\bthen\b|\bdo\b|\belse\b)\s*(?:local|declare)\b(?:\s+-[A-Za-z]+)*\s+(?P<decl>[^#]+)")
# A declared name is a bareword immediately followed by '=' (start or after ws).
_NAME_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=")


def _iter_shell_files():
    for d in _SCAN_DIRS:
        for p in sorted((_ROOT / d).rglob("*.sh")):
            yield p
    # bin/ has extensionless executables too; include any that are bash scripts.
    for p in sorted((_ROOT / "bin").iterdir()):
        if p.is_file() and p.suffix == "" and _is_bash(p):
            yield p


def _is_bash(path: pathlib.Path) -> bool:
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except OSError:
        return False
    return bool(first) and first[0].startswith("#!") and "bash" in first[0]


def _find_violations():
    violations = []
    for path in _iter_shell_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            # Split into ';'-separated statements so a trailing command on the
            # same line (e.g. `local a=..; echo "$a"`) is analysed separately —
            # that `$a` runs after the declaration and is legitimately set.
            for seg in line.split(";"):
                m = _DECL_RE.search(seg)
                if not m:
                    continue
                decl = m.group("decl")
                names = _NAME_RE.findall(decl)
                if len(names) < 2:
                    continue
                # Flag if any name declared here (other than the first) has an
                # earlier-declared name used as $name / ${name...} within the same
                # statement — that use can only be in a *later* initializer.
                flagged = False
                for i in range(1, len(names)):
                    earlier = names[:i]
                    if any(re.search(r"\$\{?" + re.escape(e) + r"\b", decl) for e in earlier):
                        rel = path.relative_to(_ROOT)
                        violations.append(f"{rel}:{lineno}: {line.strip()}")
                        flagged = True
                        break
                if flagged:
                    break  # one violation per line is enough
    return violations


def test_no_selfreferencing_local():
    violations = _find_violations()
    assert not violations, (
        "Self-referencing `local`/`declare` breaks on macOS bash 3.2. "
        "Split into separate statements:\n  " + "\n  ".join(violations)
    )


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
